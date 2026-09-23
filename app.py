# -*- coding: utf-8 -*-
# Copyright 2026 YNY MLTX
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
ComfyUI Testbench —— 本地出图测试台（后端）

功能
----
* 连接本地 ComfyUI API（默认 http://127.0.0.1:8188），提交生成任务并轮询结果
* 支持通用参数出图（提示词 / 模型 / LoRA / 尺寸 / 种子 / 步数 / CFG / 降噪…）
* 支持采样器批量对比：一次提交多个 sampler+scheduler 组合，结果并列对比
* 支持拖入 PNG 读取其中的 ComfyUI 元数据，自动回填参数

运行
----
    pip install -r requirements.txt
    python app.py
    浏览器打开 http://127.0.0.1:8000

环境变量
--------
    COMFY_HOST   ComfyUI 地址，默认 http://127.0.0.1:8188
    PORT         本服务端口，默认 8000
"""

import json
import mimetypes
import os
import random
import sys
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, jsonify, request, send_file, send_from_directory

import storage

COMFY_HOST = os.environ.get("COMFY_HOST", "http://127.0.0.1:8188").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
def _app_root():
    """静态资源根目录：开发模式=脚本目录；打包 exe=PyInstaller 解压目录(_MEIPASS)。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _app_root()
# 临时上传目录：打包后放 exe 所在目录（_MEIPASS 每次启动重置）
TEMP_DIR = os.path.join(
    os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else BASE_DIR,
    "_tmp")
os.makedirs(TEMP_DIR, exist_ok=True)

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")

_OBJECT_INFO_CACHE = {"ts": 0, "data": None}
_OBJECT_INFO_TTL = 60


# --------------------------------------------------------------------------- #
# ComfyUI 通信
# --------------------------------------------------------------------------- #
def _comfy_host():
    return storage.get_comfy_host()


def comfy_get(path, timeout=8):
    with urllib.request.urlopen(_comfy_host() + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def comfy_post(path, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _comfy_host() + path, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_object_info(force=False):
    """从 ComfyUI /object_info 拉取节点可选项（模型/LoRA/采样器/调度器），带缓存。"""
    now = time.time()
    if not force and _OBJECT_INFO_CACHE["data"] and now - _OBJECT_INFO_CACHE["ts"] < _OBJECT_INFO_TTL:
        return _OBJECT_INFO_CACHE["data"]
    try:
        info = comfy_get("/object_info", timeout=10)
    except Exception as exc:
        return {"error": str(exc)}
    out = {}

    def field(node_cls, key):
        try:
            return info[node_cls]["input"]["required"][key][0]
        except Exception:
            return []

    out["checkpoints"] = field("CheckpointLoaderSimple", "ckpt_name")
    out["vaes"] = field("VAELoader", "vae_name")
    out["loras"] = field("LoraLoader", "lora_name")
    out["samplers"] = field("KSampler", "sampler_name")
    out["schedulers"] = field("KSampler", "scheduler")
    out["unets"] = field("UNETLoader", "unet_name")
    out["clips"] = field("CLIPLoader", "clip_name")
    out["clip_types"] = field("CLIPLoader", "type")
    try:
        out["all_classes"] = sorted(info.keys())
    except Exception:
        out["all_classes"] = []
    _OBJECT_INFO_CACHE.update(ts=now, data=out)
    return out


# --------------------------------------------------------------------------- #
# 元数据读取（PNG 参数 / 工作流文件解析复用 storage.parse_prompt_graph）
# --------------------------------------------------------------------------- #


@app.route("/api/read_metadata", methods=["POST"])
def read_metadata():
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "未收到图片"}), 400
    if not (f.filename or "").lower().endswith(".png"):
        return jsonify({"error": "仅支持 PNG 图片读取参数元数据"}), 400
    name = f"up_{int(time.time() * 1000)}_{random.randrange(1000)}.png"
    path = os.path.join(TEMP_DIR, name)
    f.save(path)
    try:
        from PIL import Image

        with Image.open(path) as img:
            text = getattr(img, "text", None) or {}
            prompt_text = text.get("prompt")
        if not prompt_text:
            return jsonify({"found": False, "error": "图片中没有找到 ComfyUI 的 prompt 元数据（可能被后处理过）"})
        params = storage.parse_prompt_graph(prompt_text)
        return jsonify({"found": True, "params": params})
    except Exception as exc:
        return jsonify({"found": False, "error": str(exc)}), 500
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 工作流构建（API 格式执行图）
# --------------------------------------------------------------------------- #
def build_workflow(p, sampler, scheduler, seed):
    p.setdefault("denoise", 1.0)
    g, seq = {}, [0]

    def node(class_type, inputs):
        seq[0] += 1
        i = str(seq[0])
        g[i] = {"class_type": class_type, "inputs": inputs}
        return i

    # 加载器：优先 UNet+CLIP+VAE（新架构模型），否则 Checkpoint
    unet_mode = bool(p.get("unet_name"))
    if unet_mode:
        model_node = node("UNETLoader", {
            "unet_name": p["unet_name"],
            "weight_dtype": "default",
        })
        clip_node = node("CLIPLoader", {
            "clip_name": p.get("clip_name", ""),
            "type": p.get("clip_type", "qwen_image"),
        })
        vae_node = node("VAELoader", {"vae_name": p.get("vae_name", "")})
        model_src, clip_src = model_node, clip_node
        clip_out = 0  # CLIPLoader 只有 1 个输出
        vae_link = [vae_node, 0]
    else:
        model_node = node("CheckpointLoaderSimple", {"ckpt_name": p["model"]})
        model_src, clip_src = model_node, model_node
        clip_out = 1  # Checkpoint 的 CLIP 是第 2 个输出
        vae_link = [model_node, 2]

    if p.get("lora_name"):
        lora = node("LoraLoader", {
            "lora_name": p["lora_name"],
            "strength_model": float(p.get("lora_strength_model", 1.0)),
            "strength_clip": float(p.get("lora_strength_clip", 1.0)),
            "model": [model_src, 0],
            "clip": [clip_src, clip_out],
        })
        model_src, clip_src = lora, lora
    latent = node("EmptyLatentImage", {
        "width": max(64, int(p["width"]) // 8 * 8),
        "height": max(64, int(p["height"]) // 8 * 8),
        "batch_size": max(1, int(p.get("batch_size", 1))),
    })
    pos = node("CLIPTextEncode", {"text": p.get("positive", ""), "clip": [clip_src, clip_out]})
    neg = node("CLIPTextEncode", {"text": p.get("negative", ""), "clip": [clip_src, clip_out]})
    ks = node("KSampler", {
        "seed": int(seed),
        "steps": max(1, int(p.get("steps", 20))),
        "cfg": float(p.get("cfg", 7.0)),
        "sampler_name": sampler,
        "scheduler": scheduler,
        "denoise": float(p.get("denoise", 1.0)),
        "model": [model_src, 0],
        "positive": [pos, 0],
        "negative": [neg, 0],
        "latent_image": [latent, 0],
    })
    vae = node("VAEDecode", {"samples": [ks, 0], "vae": vae_link})
    node("SaveImage", {"images": [vae, 0], "filename_prefix": "tb_%d" % int(time.time())})
    return g


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    idx = os.path.join(app.static_folder, "index.html")
    html = open(idx, encoding="utf-8").read()
    ver = str(int(os.path.getmtime(idx)))
    html = html.replace("__CUR_VER__", ver)
    resp = app.response_class(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/version")
def api_version():
    idx = os.path.join(app.static_folder, "index.html")
    return jsonify({"v": str(int(os.path.getmtime(idx)))})


@app.route("/api/comfyui_host")
def comfyui_host_get():
    return jsonify({"host": storage.get_comfy_host()})


@app.route("/api/comfyui_host", methods=["POST"])
def comfyui_host_set():
    data = request.get_json(silent=True) or {}
    host = str(data.get("host") or "").strip()
    if host and not host.startswith("http://") and not host.startswith("https://"):
        host = "http://" + host
    if not host:
        return jsonify({"error": "地址不能为空"}), 400
    return jsonify({"host": storage.set_comfy_host(host)})


@app.route("/api/resource")
def api_resource():
    return jsonify(storage.collect_resource())


@app.route("/api/health")
def health():
    try:
        stats = comfy_get("/system_stats", timeout=3)
        return jsonify({
            "comfyui": True,
            "version": (stats.get("system") or {}).get("comfyui_version"),
            "host": _comfy_host(),
        })
    except Exception as exc:
        return jsonify({"comfyui": False, "error": str(exc)})


@app.route("/api/object_info")
def object_info():
    return jsonify(fetch_object_info())


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True) or {}
    params = dict(data.get("params") or {})
    combos = data.get("combos") or []

    if not isinstance(combos, list) or not combos:
        combos = [{
            "sampler": params.get("sampler_name", "euler"),
            "scheduler": params.get("scheduler", "normal"),
        }]

    if not params.get("model") and not params.get("unet_name"):
        info = fetch_object_info()
        ckpts = info.get("checkpoints") or []
        unets = info.get("unets") or []
        if ckpts:
            params["model"] = ckpts[0]
        elif unets:
            params["unet_name"] = unets[0]
            params["clip_name"] = (info.get("clips") or [None])[0]
            params["vae_name"] = (info.get("vaes") or [None])[0]
    if params.get("unet_name"):
        if not params.get("clip_name"):
            info = fetch_object_info()
            params["clip_name"] = (info.get("clips") or [None])[0]
        if not params.get("vae_name"):
            info = fetch_object_info()
            params["vae_name"] = (info.get("vaes") or [None])[0]
    if not params.get("model") and not params.get("unet_name"):
        return jsonify({"error": "没有可用的模型（Checkpoint 或 UNet）：请确认 ComfyUI 已安装模型"}), 400
    if params.get("unet_name") and (not params.get("clip_name") or not params.get("vae_name")):
        return jsonify({"error": "UNet 模式需要同时提供 CLIP 与 VAE"}), 400

    jobs = []
    for idx, c in enumerate(combos):
        sampler = c.get("sampler") or params.get("sampler_name", "euler")
        scheduler = c.get("scheduler") or params.get("scheduler", "normal")
        seed = params.get("seed")
        if params.get("seed_random") or seed in (None, "", "random", -1):
            seed = random.randint(0, 2**31 - 1)
        try:
            graph = build_workflow(params, sampler, scheduler, seed)
            resp = comfy_post("/prompt", {"prompt": graph, "client_id": "comfyui-testbench"})
            pid = resp.get("prompt_id")
            if not pid:
                raise RuntimeError("ComfyUI 未返回 prompt_id: %s" % resp)
            jobs.append({
                "index": idx,
                "label": "%s+%s" % (sampler, scheduler),
                "sampler": sampler,
                "scheduler": scheduler,
                "seed": seed,
                "prompt_id": pid,
            })
        except Exception as exc:
            return jsonify({"error": "提交任务 %s+%s 失败: %s" % (sampler, scheduler, exc)}), 502

    return jsonify({"jobs": jobs})


@app.route("/api/status")
def status():
    pid = request.args.get("prompt_id", "")
    if not pid:
        return jsonify({"error": "缺少 prompt_id"}), 400
    try:
        history = comfy_get("/history/%s" % pid, timeout=6)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return jsonify({"status": "pending"})
        return jsonify({"status": "error", "error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 502

    entry = history.get(pid)
    if not entry:
        return jsonify({"status": "pending"})

    status_str = (entry.get("status") or {}).get("status_str", "unknown")
    images = []
    for out in (entry.get("outputs") or {}).values():
        for img in out.get("images", []) or []:
            q = urllib.parse.urlencode({
                "filename": img.get("filename"),
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            })
            images.append("%s/view?%s" % (_comfy_host(), q))

    if status_str == "success":
        return jsonify({"status": "done", "images": images, "status_str": status_str})
    if status_str == "error":
        return jsonify({"status": "error", "images": images, "status_str": status_str})
    return jsonify({"status": "pending", "images": images, "status_str": status_str})


# --------------------------------------------------------------------------- #
# 模型 / 输出管理
# --------------------------------------------------------------------------- #
@app.route("/api/storage/config", methods=["GET", "POST"])
def storage_config():
    if request.method == "POST":
        cfg = request.get_json(force=True) or {}
        model_dirs = [d for d in (cfg.get("model_dirs") or []) if isinstance(d, str) and d.strip()]
        output_dirs = [d for d in (cfg.get("output_dirs") or []) if isinstance(d, str) and d.strip()]
        workflow_dirs = [d for d in (cfg.get("workflow_dirs") or []) if isinstance(d, str) and d.strip()]
        output_excludes = [d for d in (cfg.get("output_excludes") or []) if isinstance(d, str) and d.strip()]
        missing = [d for d in model_dirs + output_dirs + workflow_dirs if not os.path.isdir(d)]
        if missing:
            return jsonify({"error": "以下目录不存在: %s" % ", ".join(missing)}), 400
        sf = cfg.get("series_filter") or {}
        series_filter = {
            "pattern": sf.get("pattern") or "",
            "exclude": [s for s in (sf.get("exclude") or []) if isinstance(s, str) and s.strip()],
        }
        storage.save_config({
            "model_dirs": model_dirs,
            "output_dirs": output_dirs,
            "workflow_dirs": workflow_dirs,
            "output_excludes": output_excludes,
            "series_filter": series_filter,
        })
        return jsonify({"ok": True})
    return jsonify(storage.load_config())


@app.route("/api/storage/detect")
def storage_detect():
    return jsonify(storage.detect_comfy_setup())


@app.route("/api/models")
def models_list():
    return jsonify(storage.scan_models(
        q=request.args.get("q", ""),
        type_f=request.args.get("type", ""),
        series_f=request.args.get("series", ""),
    ))


@app.route("/api/outputs")
def outputs_list():
    folder = request.args.get("folder", "") or None
    try:
        return jsonify(storage.scan_outputs(
            q=request.args.get("q", ""),
            sort=request.args.get("sort", "new"),
            kind=request.args.get("kind", "all"),
            folder=folder,
            ratio=request.args.get("ratio", ""),
        ))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403


@app.route("/api/output_folders")
def output_folders():
    return jsonify(storage.list_output_folders())


@app.route("/api/output_meta")
def output_meta():
    """读取输出图片内嵌的 ComfyUI 参数元数据（复用于详情面板）。"""
    try:
        p = storage.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    if os.path.splitext(p)[1].lower() not in storage.IMAGE_EXTS:
        return jsonify({"found": False, "error": "仅图片支持读取参数元数据"})
    try:
        from PIL import Image

        with Image.open(p) as img:
            text = getattr(img, "text", None) or {}
            prompt_text = text.get("prompt")
        if not prompt_text:
            return jsonify({"found": False,
                            "error": "该图片没有 ComfyUI prompt 元数据（可能被截图或后处理过）"})
        params = storage.parse_prompt_graph(prompt_text)
        sz = storage._png_size(p)
        if sz:
            params["width"], params["height"] = sz
        return jsonify({"found": True, "params": params})
    except Exception as exc:
        return jsonify({"found": False, "error": str(exc)}), 500


@app.route("/api/workflows")
def workflows_list():
    folder = request.args.get("folder", "") or None
    try:
        return jsonify(storage.scan_workflows(
            q=request.args.get("q", ""),
            sort=request.args.get("sort", "new"),
            folder=folder,
        ))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403


@app.route("/api/workflow_folders")
def workflow_folders():
    return jsonify(storage.list_workflow_folders())


@app.route("/api/workflow")
def workflow_detail():
    """读取单个工作流：节点数、模型/LoRA、采样参数、尺寸、提示词、缺失节点检测。"""
    try:
        p = storage.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    try:
        info = storage.parse_workflow_file(p)
    except json.JSONDecodeError:
        return jsonify({"error": "文件不是有效的 JSON（可能已损坏）"}), 400
    except Exception as exc:
        return jsonify({"error": "解析失败: %s" % exc}), 500

    info["path"] = p
    info["name"] = os.path.basename(p)
    info["mtime"] = int(os.path.getmtime(p))
    info["file_size"] = os.path.getsize(p)

    # 缺失节点检测：对比 ComfyUI 已安装节点（过滤内置说明/装饰类节点）
    missing_set = set()
    if info.get("format") != "unknown":
        oi = fetch_object_info()
        all_classes = oi.get("all_classes") if isinstance(oi, dict) else None
        if all_classes:
            decor = {"Note", "MarkdownNote", "PreviewImage", "ImageScaleBy", "Reroute", "PrimitiveNode"}
            missing = [c for c in info.get("classes") or []
                       if c not in all_classes and c not in decor]
            info["missing"] = missing
            missing_set = set(missing)
    info["builtin_nodes"], info["custom_nodes"] = _node_groups(info.get("classes") or [], missing_set)
    return jsonify(info)


@app.route("/api/output_video_info")
def output_video_info():
    """读取视频文件时长/分辨率/编码（ffmpeg 探测）。"""
    try:
        p = storage.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    info = storage.video_info(p)
    return jsonify({"found": bool(info), "info": info or {}})


@app.route("/api/workflow/note", methods=["POST"])
def workflow_note_save():
    """保存 Note / MarkdownNote 节点文本修改（UI 格式工作流，写回原文件）。"""
    data = request.get_json(force=True) or {}
    try:
        p = storage.safe_resolve(data.get("path"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    nid = data.get("id")
    text = data.get("text") or ""
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list):
        return jsonify({"error": "仅 UI 格式工作流支持修改笔记"}), 400
    hit = False
    for n in doc["nodes"]:
        if n.get("type") in ("Note", "MarkdownNote") and n.get("id") == nid:
            wv = n.get("widgets_values")
            if not isinstance(wv, list):
                wv = []
            if not wv:
                wv = [""]
            wv[0] = text
            n["widgets_values"] = wv
            hit = True
            break
    if not hit:
        return jsonify({"error": "未找到对应笔记节点"}), 404
    with open(p, "w", encoding="utf-8", newline="") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return jsonify({"ok": True, "id": nid})


@app.route("/api/wf_recent")
def wf_recent():
    """按工作流节点类型指纹，匹配输出目录里最近的生成结果（PNG prompt 元数据对比）。"""
    try:
        p = storage.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    try:
        limit = int(request.args.get("limit", "6"))
    except (TypeError, ValueError):
        limit = 6
    limit = max(1, min(limit, 50))
    try:
        items = storage.find_recent_output_for_workflow(p, limit=limit)
    except Exception as exc:
        return jsonify({"error": "匹配失败: %s" % exc}), 500
    return jsonify(items)


# ComfyUI 核心内置节点（不在列表内的节点类型视为插件 / 自定义节点）
BUILTIN_NODES = {
    "CheckpointLoaderSimple", "CheckpointLoader", "CheckpointLoaderWithConfig",
    "UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader", "LoraLoader",
    "LoraLoaderModelOnly", "ControlNetLoader", "GLIGENLoader", "DiffusionModelLoader",
    "CLIPVisionLoader", "StyleModelLoader", "UpscaleModelLoader", "HypernetworkLoader",
    "CLIPTextEncode", "KSampler", "KSamplerAdvanced", "SamplerCustom",
    "SamplerCustomAdvanced", "EmptyLatentImage", "LatentFromBatch", "LatentRebatch",
    "LatentRotate", "LatentFlip", "LatentCrop", "LatentComposite", "LatentBlend",
    "LatentScale", "LatentScaleBy", "LatentUpscale", "LatentUpscaleBy", "LatentDownscale",
    "LatentDenoise", "LatentNoise", "LatentMultiply", "LatentAdd", "LatentSubtract",
    "LatentBatchSeedBehavior", "VAEDecode", "VAEEncode", "VAEEncodeForInpaint",
    "LoadImage", "LoadImageMask", "LoadVideo", "LoadLatent", "LoadMask",
    "SaveImage", "SaveAnimatedWEBP", "SaveAnimatedPNG", "SaveVideo", "PreviewImage",
    "ImageScale", "ImageScaleBy", "ImageScaleToTotalPixels", "ImageCrop",
    "ImageRepeat", "ImagePadForOutpaint", "ImageFlip", "ImageRotate", "ImageBlur",
    "ImageSharpen", "ImageInvert", "ImageColorToMask", "MaskToImage", "ImageToMask",
    "ImageConcat", "ImageBatch", "ImageFromBatch", "ImageUpscaleWithModel",
    "ImageCompositeMasked", "ImageAdjust", "ImageEnhance", "ImageLevels",
    "ConditioningZeroOut", "ConditioningCombine", "ConditioningSetArea",
    "ConditioningSetMask", "ConditioningConcat", "ConditioningAverage",
    "ConditioningSetTimestepRange", "ControlNetApply", "ControlNetApplyAdvanced",
    "CLIPSetLastLayer", "CLIPVisionEncode", "CLIPVisionOutput", "Reroute", "Note",
    "PrimitiveNode", "Seed", "RandomNoise", "BatchUnsampler", "ModelSamplingDiscrete",
    "ModelSamplingSD3", "ModelSamplingAuraFlow", "ModelSamplingContinuousEDM",
    "MarkdownNote",
    "ModelSamplingContinuousV", "ModelSamplingStableCascade", "ImageOnlyCheckpointLoader",
    "CLIPInputSwitch", "LatentInputSwitch", "ControlNetInputSwitch", "ImageInputSwitch",
    "ImageCustomSize", "SolidColor", "VHS_VideoInfo", "VHS_LoadVideo", "VHS_DuplicateImages",
}


def _node_groups(classes, missing_set):
    """把节点类型分为内置 / 自定义（插件）两组，各自去重计数，缺失的插件标红。"""
    counts = {}
    for c in classes or []:
        if not c:
            continue
        counts[c] = counts.get(c, 0) + 1
    builtin = [{"name": n, "count": c} for n, c in counts.items() if n in BUILTIN_NODES]
    custom = [{"name": n, "count": c, "missing": n in missing_set}
              for n, c in counts.items() if n not in BUILTIN_NODES]
    builtin.sort(key=lambda x: (-x["count"], x["name"]))
    custom.sort(key=lambda x: (-x["missing"], -x["count"], x["name"]))
    return builtin, custom


@app.route("/api/storage/note", methods=["POST"])
def storage_note():
    """给模型（或任意允许目录内的文件）添加/删除用户备注。"""
    data = request.get_json(force=True) or {}
    try:
        p = storage.safe_resolve(data.get("path"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    note = data.get("note") or ""
    storage.save_note(p, note)
    return jsonify({"ok": True, "note": storage.load_notes().get(p, "")})


@app.route("/api/storage/mkdir", methods=["POST"])
def storage_mkdir():
    data = request.get_json(force=True) or {}
    try:
        p = storage.safe_resolve(data.get("path"), require_exists=False)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    try:
        os.makedirs(p, exist_ok=True)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": "创建文件夹失败: %s" % exc}), 500


@app.route("/api/storage/rmdir", methods=["POST"])
def storage_rmdir():
    data = request.get_json(force=True) or {}
    try:
        p = storage.safe_resolve(data.get("path"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isdir(p):
        return jsonify({"error": "目标不是文件夹"}), 400
    try:
        os.rmdir(p)  # 仅允许删除空文件夹
        return jsonify({"ok": True})
    except OSError as exc:
        return jsonify({"error": "仅能删除空文件夹（%s）" % (exc.strerror or exc)}), 400
    except Exception as exc:
        return jsonify({"error": "删除失败: %s" % exc}), 500


@app.route("/media")
def media():
    try:
        p = storage.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if request.args.get("thumb") == "1":
        try:
            p = storage.make_thumbnail(p)
        except Exception:
            return "", 404
    mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
    return send_file(p, mimetype=mime, conditional=True)


@app.route("/api/storage/delete", methods=["POST"])
def storage_delete():
    data = request.get_json(force=True) or {}
    try:
        p = storage.safe_resolve(data.get("path"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    try:
        os.remove(p)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": "删除失败: %s" % exc}), 500


@app.route("/api/storage/move", methods=["POST"])
def storage_move():
    data = request.get_json(force=True) or {}
    try:
        src = storage.safe_resolve(data.get("path"))
        target = storage.safe_resolve(data.get("target_dir"), require_exists=False)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isdir(target):
        return jsonify({"error": "目标目录不存在: %s" % target}), 400
    try:
        dst = os.path.join(target, os.path.basename(src))
        if os.path.exists(dst):
            stem, ext = os.path.splitext(os.path.basename(src))
            dst = os.path.join(target, "%s (1)%s" % (stem, ext))
        shutil.move(src, dst)
        return jsonify({"ok": True, "target": dst})
    except Exception as exc:
        return jsonify({"error": "移动失败: %s" % exc}), 500


@app.route("/api/storage/open", methods=["POST"])
def storage_open():
    data = request.get_json(force=True) or {}
    try:
        p = storage.safe_resolve(data.get("path"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    try:
        folder = p if os.path.isdir(p) else os.path.dirname(p)
        if hasattr(os, "startfile"):
            os.startfile(folder)
        else:
            import sys
            if sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": "打开文件夹失败: %s" % exc}), 500


if __name__ == "__main__":
    # 首次运行：自动检测 ComfyUI 目录并写入配置（只扫描，不接管）
    if not os.path.exists(storage.CONFIG_PATH):
        detected = storage.detect_comfy_setup()
        if detected.get("model_dirs") or detected.get("output_dirs"):
            storage.save_config({
                "model_dirs": detected["model_dirs"],
                "output_dirs": detected["output_dirs"],
                "workflow_dirs": detected["workflow_dirs"],
            })
            print("已自动检测目录 -> 模型: %s | 输出: %s | 工作流: %s"
                  % (len(detected["model_dirs"]), len(detected["output_dirs"]),
                     len(detected["workflow_dirs"])))
        else:
            print("提示: 未自动检测到目录，%s" % (detected.get("hint") or "可在界面手动添加"))
    else:
        # 旧配置补全：缺失 workflow_dirs 时从检测结果补齐（不覆盖用户已有的目录设置）
        cfg = storage.load_config()
        if not cfg.get("workflow_dirs"):
            detected = storage.detect_comfy_setup()
            if detected.get("workflow_dirs"):
                cfg["workflow_dirs"] = detected["workflow_dirs"]
                storage.save_config(cfg)
                print("已补全工作流目录: %s" % ", ".join(cfg["workflow_dirs"]))
    print("ComfyUI 素材管理器已启动: http://127.0.0.1:%d  (ComfyUI: %s)" % (PORT, _comfy_host()))
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
