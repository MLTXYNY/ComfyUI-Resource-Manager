# -*- coding: utf-8 -*-
"""
manager.parsing —— 工作流解析 / PNG 元数据 / 视频信息 / 指纹匹配
=================================================================
职责：
* parse_workflow_file：兼容 UI 格式 与 API 格式 工作流解析
* parse_prompt_graph：PNG 内嵌 ComfyUI prompt（API 格式）参数解析
* video_info：ffmpeg 探测视频时长 / 分辨率 / 编码
* PNG 手动读取（tEXt prompt 元数据 / IHDR 尺寸），用于工作流指纹匹配
* find_recent_output_for_workflow：按节点类型指纹匹配最近生成结果
"""

import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time

from . import paths, config


# --------------------------------------------------------------------------- #
# 工作流解析
# --------------------------------------------------------------------------- #
_WIDGET_MAP = {
    "CheckpointLoaderSimple": ["ckpt_name"],
    "UNETLoader": ["unet_name", "weight_dtype"],
    "CLIPLoader": ["clip_name", "type"],
    "DualCLIPLoader": ["clip_name1", "clip_name2", "type"],
    "VAELoader": ["vae_name"],
    "LoraLoader": ["lora_name", "strength_model", "strength_clip"],
    "LoraLoaderModelOnly": ["lora_name", "strength_model"],
    "KSampler": ["seed", "control_after_generate", "steps", "cfg",
                 "sampler_name", "scheduler", "denoise"],
    "KSamplerAdvanced": ["noise_seed", "control_after_generate", "steps", "cfg",
                         "sampler_name", "scheduler", "start_at_step", "end_at_step", "add_noise"],
    "EmptyLatentImage": ["width", "height", "batch_size"],
    "CLIPTextEncode": ["text"],
    "CLIPTextEncodeAdvanced": ["text", "clip", "token_normalization", "weight_interpretation"],
    "SaveImage": ["filename_prefix"],
    "SaveAnimatedWEBP": ["filename_prefix", "fps", "lossless", "quality", "method"],
    "SaveVideo": ["filename_prefix", "format", "codec", "pix_fmt", "fps"],
    "VAEDecode": [],
    "VAEEncode": [],
}


def parse_workflow_file(path):
    """解析工作流文件。兼容两种格式：
    - UI 格式：{nodes:[...], links:[...]}
    - API 格式：{id:{class_type, inputs}}
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = json.load(f)

    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        return _finalize_workflow(_parse_ui_nodes(data["nodes"]), "ui", data.get("title"))
    if isinstance(data, dict) and any(
            isinstance(v, dict) and "class_type" in v for v in data.values()):
        return _finalize_workflow(_parse_api_nodes(data), "api", "")
    return {"format": "unknown", "error": "无法识别的工作流格式"}


def _finalize_workflow(out, fmt, title):
    out["format"] = fmt
    out["title"] = title or ""
    out["classes"] = sorted(set(out.get("classes") or []))
    return out


_SAMPLER_FIELDS = ("sampler_name", "scheduler", "steps", "cfg", "seed", "denoise", "noise_seed")


def _ui_widget_map(node):
    """UI 格式节点：按 inputs 里 widget 项的顺序对齐 widgets_values，返回 {name: value}。"""
    inps = node.get("inputs") or []
    wv = node.get("widgets_values") or []
    if not isinstance(inps, list) or not isinstance(wv, list):
        return {}
    names = []
    for ii in inps:
        if isinstance(ii, dict) and ii.get("widget") and isinstance(ii.get("widget"), dict):
            names.append(ii["widget"].get("name"))
    out = {}
    for i, nm in enumerate(names):
        if nm and i < len(wv):
            out[nm] = wv[i]
    return out


_SAMPLER_INDEX = {
    # ComfyUI 内置采样器：widgets_values 固定布局（含隐藏的 control_after_generate）
    "KSampler": {"seed": 0, "steps": 2, "cfg": 3, "sampler_name": 4, "scheduler": 5, "denoise": 6},
    "KSamplerAdvanced": {"seed": 1, "steps": 4, "cfg": 3, "sampler_name": 5, "scheduler": 6},
}


def _node_sampler_fields(node, api=False):
    """采样字段提取：API 按 name；UI 已知类型按固定索引，其余按 widget name 通用对齐。"""
    if api:
        inp = node.get("inputs") or {}
        return {k: inp.get(k) for k in _SAMPLER_FIELDS if isinstance(inp.get(k), (str, int, float))}
    wv = node.get("widgets_values") or []
    if not isinstance(wv, list):
        return {}
    t = str(node.get("type", ""))
    idx = _SAMPLER_INDEX.get(t)
    if idx:
        f = {k: wv[i] for k, i in idx.items()
             if i < len(wv) and isinstance(wv[i], (str, int, float))}
    else:
        wm = _ui_widget_map(node)
        f = {k: wm[k] for k in _SAMPLER_FIELDS
             if wm.get(k) is not None and isinstance(wm.get(k), (str, int, float))}
    if "noise_seed" in f:
        f.setdefault("seed", f["noise_seed"])
    return f


def _is_sampler_node(type_name):
    """判定是否为独立采样器节点（KSampler 家族 / SamplerCustom / Ultimate 等）。"""
    tl = str(type_name or "").lower()
    return ("ksampler" in tl or "samplercustom" in tl or "ultimate" in tl)


def _collect_samplers(nodes, api=False):
    """收集工作流所有采样器（支持双/多采样器）。
    - 显式采样节点按 widget name 通用提取（覆盖 KSampler/SamplerCustom/Efficient/Tiled/Ultimate 等）；
    - SamplerCustomAdvanced 自身无字段，从其关联子节点(KSamplerSelect/RandomNoise/BasicScheduler)补；
    - 去重；返回采样器列表。
    """
    key = "class_type" if api else "type"
    samps = []
    seen = set()
    for n in nodes:
        t = n.get(key, "")
        if not _is_sampler_node(t):
            continue
        f = _node_sampler_fields(n, api)
        if f.get("steps") is None and f.get("cfg") is None:
            continue  # 采样器至少含步数或 CFG（KSamplerSelect 等子节点不算独立采样器）
        sig = tuple(sorted((k, str(f[k])) for k in f if f[k] is not None))
        if sig in seen:
            continue
        seen.add(sig)
        samps.append(f)
    # SamplerCustomAdvanced 体系：关联子节点补采样器字段
    for n in nodes:
        if n.get(key, "") != "SamplerCustomAdvanced":
            continue
        fields = {}
        for sub in nodes:
            st = sub.get(key, "")
            f2 = _node_sampler_fields(sub, api)
            if st == "KSamplerSelect" and f2.get("sampler_name"):
                fields.setdefault("sampler_name", f2["sampler_name"])
            elif st == "RandomNoise" and f2.get("seed"):
                fields.setdefault("seed", f2["seed"])
            elif st in ("BasicScheduler", "BasicSchedulerHalfSD") and f2.get("steps"):
                fields.setdefault("scheduler", f2.get("scheduler"))
                fields.setdefault("steps", f2["steps"])
                fields.setdefault("denoise", f2.get("denoise"))
        if not fields:
            continue
        sig = tuple(sorted((k, str(fields[k])) for k in fields if fields[k] is not None))
        if sig in seen:
            continue
        seen.add(sig)
        samps.append(fields)
    return samps


def _parse_ui_nodes(nodes):
    models, loras, classes = [], [], []
    size = None
    texts = []
    for n in nodes:
        t = n.get("type", "")
        classes.append(t)
        wv = n.get("widgets_values") or []
        title = n.get("title") or ""
        w = lambda idx, default=None: wv[idx] if idx < len(wv) else default
        if t in ("CheckpointLoaderSimple", "UNETLoader", "CLIPLoader",
                 "DualCLIPLoader", "VAELoader"):
            models.append({"type": t, "name": w(0), "title": title,
                           "extra": w(1) if len(wv) > 1 else None})
        elif t in ("LoraLoader", "LoraLoaderModelOnly"):
            loras.append({"name": w(0),
                          "strength_model": w(1, 1.0),
                          "strength_clip": w(2, 1.0) if t == "LoraLoader" else None,
                          "title": title})
        elif t == "EmptyLatentImage":
            wd, ht = w(0), w(1)
            inps = n.get("inputs")
            linked = False
            if isinstance(inps, list):
                for ii in inps:
                    if isinstance(ii, dict) and ii.get("name") in ("width", "height") \
                            and ii.get("link") is not None:
                        linked = True
            if not linked and isinstance(wd, (int, float)) and isinstance(ht, (int, float)):
                size = {"width": int(wd), "height": int(ht), "batch_size": w(2, 1)}
        elif t == "CLIPTextEncode":
            text = w(0)
            if text:
                texts.append(text)
    notes = []
    for n in nodes:
        nt = n.get("type", "")
        if nt in ("Note", "MarkdownNote"):
            wv0 = (n.get("widgets_values") or [None])[0]
            notes.append({"id": n.get("id"), "type": nt,
                          "title": n.get("title") or "",
                          "text": wv0 if isinstance(wv0, str) else ""})
    positive, negative = (texts[0] if len(texts) > 0 else None,
                          texts[1] if len(texts) > 1 else None)
    extra_texts = [{"node": "CLIPTextEncode", "text": t} for t in texts[2:]]
    for n in nodes:
        t = n.get("type", "")
        if t == "CLIPTextEncode" or t in _NON_TEXT_UI:
            continue
        label = (n.get("title") or t) if (n.get("title") or "").strip() else t
        wv = n.get("widgets_values") or []
        for v in wv:
            if isinstance(v, str) and _looks_like_prompt(v):
                extra_texts.append({"node": label, "text": v})
    models = models + _ui_generic_models(nodes)
    samplers = _collect_samplers(nodes)
    sampler = samplers[0] if samplers else {}
    multi_sampler = len(samplers) > 1
    return {
        "node_count": len(nodes), "classes": classes, "models": models,
        "loras": loras, "sampler": sampler, "samplers": samplers,
        "multi_sampler": multi_sampler, "size": size,
        "positive": positive, "negative": negative,
        "extra_texts": extra_texts, "notes": notes,
    }


def _parse_api_nodes(graph):
    models, loras, classes = [], [], []
    size = None
    positive = negative = None
    for node in graph.values():
        cls = node.get("class_type", "")
        classes.append(cls)
        inp = node.get("inputs", {}) or {}
        if cls == "CheckpointLoaderSimple":
            models.append({"type": cls, "name": inp.get("ckpt_name"), "title": "", "extra": None})
        elif cls in ("UNETLoader", "VAELoader"):
            k = "unet_name" if cls == "UNETLoader" else "vae_name"
            models.append({"type": cls, "name": inp.get(k), "title": "", "extra": None})
        elif cls in ("CLIPLoader", "DualCLIPLoader"):
            models.append({"type": cls,
                           "name": inp.get("clip_name") or inp.get("clip_name1"),
                           "title": "", "extra": inp.get("type")})
        elif cls in ("LoraLoader", "LoraLoaderModelOnly"):
            loras.append({"name": inp.get("lora_name"),
                          "strength_model": inp.get("strength_model"),
                          "strength_clip": inp.get("strength_clip"), "title": ""})
        elif cls == "EmptyLatentImage" and not size:
            wd, ht = inp.get("width"), inp.get("height")
            if isinstance(wd, (int, float)) and isinstance(ht, (int, float)):
                size = {"width": int(wd), "height": int(ht),
                        "batch_size": inp.get("batch_size")}
        elif cls == "CLIPTextEncode":
            text = inp.get("text", "")
            if text:
                if positive is None:
                    positive = text
                elif negative is None:
                    negative = text
    extra_texts = []
    for node in graph.values():
        cls = node.get("class_type", "")
        if cls == "CLIPTextEncode" or cls in _NON_TEXT_UI:
            continue
        inp = node.get("inputs", {}) or {}
        for k, v in inp.items():
            if isinstance(v, str) and _ui_text_key(k) and len(v.strip()) >= 4:
                extra_texts.append({"node": cls, "text": v})
    models = models + _api_generic_models(graph)
    samplers = _collect_samplers(list(graph.values()), api=True)
    sampler = samplers[0] if samplers else {}
    multi_sampler = len(samplers) > 1
    return {
        "node_count": len(graph), "classes": classes, "models": models,
        "loras": loras, "sampler": sampler, "samplers": samplers,
        "multi_sampler": multi_sampler, "size": size,
        "positive": positive, "negative": negative, "extra_texts": extra_texts,
    }


def parse_prompt_graph(prompt_text):
    """解析 ComfyUI API 格式的 prompt JSON，返回常用参数 dict（供 PNG 元数据读取复用）。"""
    graph = json.loads(prompt_text)
    params = {}

    def resolve(link):
        if isinstance(link, list) and link and str(link[0]) in graph:
            return graph[str(link[0])]
        return None

    for node in graph.values():
        cls = node.get("class_type", "")
        inputs = node.get("inputs", {}) or {}
        if cls == "CheckpointLoaderSimple" and "model" not in params:
            params["model"] = inputs.get("ckpt_name")
        elif cls == "UNETLoader" and "unet_name" not in params:
            params["unet_name"] = inputs.get("unet_name")
        elif cls == "CLIPLoader" and "clip_name" not in params:
            params["clip_name"] = inputs.get("clip_name")
            params["clip_type"] = inputs.get("type")
        elif cls == "VAELoader" and "vae_name" not in params:
            params["vae_name"] = inputs.get("vae_name")
        elif cls == "LoraLoader" and "lora_name" not in params:
            params["lora_name"] = inputs.get("lora_name")
            params["lora_strength_model"] = inputs.get("strength_model")
            params["lora_strength_clip"] = inputs.get("strength_clip")
        elif cls == "EmptyLatentImage" and "width" not in params:
            params["width"] = inputs.get("width")
            params["height"] = inputs.get("height")
            params["batch_size"] = inputs.get("batch_size", 1)
        elif cls in ("KSampler", "KSamplerAdvanced") and "steps" not in params:
            for k in ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"):
                if k in inputs:
                    params[k] = inputs[k]

    for node in graph.values():
        if node.get("class_type") not in ("KSampler", "KSamplerAdvanced"):
            continue
        inputs = node.get("inputs", {}) or {}
        pos, neg = resolve(inputs.get("positive")), resolve(inputs.get("negative"))
        if pos and pos.get("class_type") == "CLIPTextEncode":
            params.setdefault("positive", pos.get("inputs", {}).get("text", ""))
        if neg and neg.get("class_type") == "CLIPTextEncode":
            params.setdefault("negative", neg.get("inputs", {}).get("text", ""))

    # 回退：收集所有 CLIPTextEncode 文本（按出现顺序取正/反）
    if not (params.get("positive") or params.get("negative")):
        texts = []
        for node in graph.values():
            if node.get("class_type") == "CLIPTextEncode":
                t = (node.get("inputs", {}) or {}).get("text", "")
                if t and t not in texts:
                    texts.append(t)
        if texts:
            params.setdefault("positive", texts[0])
        if len(texts) > 1:
            params.setdefault("negative", texts[1])
    samplers = _collect_samplers(list(graph.values()), api=True)
    if samplers:
        s0 = samplers[0]
        for k in ("sampler_name", "scheduler", "steps", "cfg", "seed", "denoise"):
            if s0.get(k) is not None:
                params.setdefault(k, s0[k])
        params["samplers"] = samplers
        if len(samplers) > 1:
            params["multi_sampler"] = True
    return params


# --------------------------------------------------------------------------- #
# 视频信息
# --------------------------------------------------------------------------- #
def _ffmpeg_path():
    """定位 ffmpeg：优先从已配置目录反推 ComfyUI venv 内 imageio_ffmpeg 二进制，其次 PATH。"""
    cand = []
    seen_roots = set()
    for kind in ("model", "output", "input", "workflow"):
        for r in paths.root_dirs(kind):
            parent = os.path.dirname(os.path.abspath(r))
            if parent in seen_roots:
                continue
            seen_roots.add(parent)
            bin_dir = os.path.join(parent, ".venv", "Lib", "site-packages",
                                   "imageio_ffmpeg", "binaries")
            if os.path.isdir(bin_dir):
                for fn in sorted(os.listdir(bin_dir)):
                    if fn.startswith("ffmpeg-") and fn.endswith(".exe"):
                        cand.append(os.path.join(bin_dir, fn))
    cand.append(shutil.which("ffmpeg"))
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return None


def video_info(path):
    """用 ffmpeg -i 读取视频时长/分辨率/编码（stderr 解析），失败返回 None。"""
    ff = _ffmpeg_path()
    if not ff:
        return None
    try:
        r = subprocess.run([ff, "-i", path, "-f", "null", "-"],
                           capture_output=True, timeout=20)
        err = (r.stderr or b"").decode("utf-8", "replace")
    except Exception:
        return None
    out = {}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        hh, mm, ss = m.groups()
        out["duration"] = round(int(hh) * 3600 + int(mm) * 60 + float(ss), 2)
    m = re.search(r"Video:\s*([a-zA-Z0-9]+)", err)
    if m:
        out["codec"] = m.group(1)
    m = re.search(r"(\d{3,5})x(\d{3,5})", err)
    if m:
        out["width"], out["height"] = int(m.group(1)), int(m.group(2))
    return out or None


# --------------------------------------------------------------------------- #
# PNG 手动读取（tEXt prompt / IHDR 尺寸），用于工作流指纹匹配
# --------------------------------------------------------------------------- #
def png_prompt_text(path):
    """手动解析 PNG tEXt chunk 读取 ComfyUI prompt 元数据（流式跳读，避免 PIL 整文件开销）。"""
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                ln = struct.unpack(">I", hdr[:4])[0]
                typ = hdr[4:8]
                if typ == b"tEXt":
                    data = f.read(ln)
                    f.read(4)
                    nul = data.find(b"\x00")
                    if nul > 0:
                        kw = data[:nul].decode("latin-1", "replace")
                        if kw == "prompt":
                            return data[nul + 1:].decode("utf-8", "replace")
                elif typ == b"IDAT":
                    return None
                else:
                    f.seek(ln + 4, 1)
    except Exception:
        return None


def png_size(path):
    """手动读取 PNG IHDR 的宽高（不解码像素）。"""
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            hdr = f.read(8)
            if len(hdr) < 8 or hdr[4:8] != b"IHDR":
                return None
            w, h = struct.unpack(">II", f.read(8))
            return (w, h)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 工作流 -> 最近生成结果匹配（节点类型指纹）
# --------------------------------------------------------------------------- #
_LOADER_TYPES = {
    "UNETLoader", "CheckpointLoaderSimple", "CLIPLoader", "VAELoader",
    "LoraLoaderModelOnly", "LoraLoader", "UNETLoaderAdvanced", "DualCLIPLoader",
    "CLIPVisionLoader", "DiffusionModelLoader",
}

# 不参与实际执行的编辑器/占位节点：ComfyUI 不会把它们写入 PNG 内嵌 prompt，
# 指纹匹配时两侧都必须排除，否则工作流永远匹配不到输出。
_NON_EXEC_TYPES = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}

# 提示词「全收」时跳过的不含提示词文本的节点类型（加载器/采样/尺寸/占位等）
_NON_TEXT_UI = {
    "CheckpointLoaderSimple", "UNETLoader", "CLIPLoader", "DualCLIPLoader", "VAELoader",
    "LoraLoader", "LoraLoaderModelOnly", "KSampler", "KSamplerAdvanced", "EmptyLatentImage",
    "EmptySD3LatentImage", "Note", "MarkdownNote", "Reroute", "PrimitiveNode",
    "PreviewImage", "SaveImage", "SaveImageAdvanced", "ImageScaleBy",
    "SaveVideo", "LoadAudio", "LoadImage", "LoadImageMask",
    "VideoHelperLoadVideo", "LoadVideo", "VHS_VideoUpload",
}


def _ui_text_key(k):
    """判断 API 格式的输入字段名是否像提示词文本。"""
    kl = str(k).lower()
    return any(x in kl for x in ("text", "prompt", "description", "positive", "negative"))


def _looks_like_prompt(s):
    """启发式判断一段节点字符串值是否像提示词（而非参数值/文件路径/采样算法）。

    UI 格式的 widgets_values 拿不到字段名，只能靠值特征区分：
    - 排除：文件/模型路径、常见采样/缩放/调度选项值
    - 保留：长文本(>=40)、含中文、含自然语言分隔符的文本
    """
    s = (s or "").strip()
    if not s:
        return False
    low = s.lower()
    if any(ext in low for ext in
           (".png", ".jpg", ".jpeg", ".webp", ".safetensors", ".ckpt", ".pt",
            ".pth", ".json", ".mp4", ".webm", ".gguf", ".bin",
            ".wav", ".mp3", ".flac", ".aac", ".wma")):
        return False
    # 数学表达式 / 计算节点输出
    if ("round(" in low or "max(" in low or "min(" in low or "if(" in low) and             not re.search(r"[\u4e00-\u9fff]", s):
        return False
    if "%" in s or re.search(r"\d{4}", s):
        return False
    if len(s) >= 40:
        return True
    if re.search(r"[\u4e00-\u9fff]", s):
        return True
    if sum(s.count(c) for c in ",，。；;、\n") >= 2:
        return True
    opt = ("flow", "euler", "lanczos", "fixed", "bicubic", "nearest", "linear",
           "bilinear", "normal", "simple", "ddim", "dpm", "karras", "exponential",
           "sgm_uniform", "random", "keep")
    if low in opt:
        return False
    return False


# 通用模型识别：模型扩展名（与 scanning.MODEL_EXTS 保持一致）
_GENERIC_MODEL_EXTS = {".safetensors", ".pt", ".pth", ".ckpt", ".gguf",
                       ".bin", ".sft", ".onnx", ".sft", ".safetensor"}


def _generic_model_rel(v):
    """从工作流节点值识别"模型文件名 / 相对路径"（自定义 / 插件加载器引用）。
    不依赖 model_dirs 磁盘验证：只要值以模型扩展名结尾且形态像模型名即识别，
    使工作流引用的模型无论目录是否在配置范围内都能显示（读全）。"""
    if not isinstance(v, str):
        return None
    nm = v.strip().replace("/", "\\").lstrip("\\")
    if not nm or len(nm) > 400 or ".." in nm:
        return None
    if any(ch in nm for ch in "\n\r\t"):
        return None
    ext = os.path.splitext(nm)[1].lower()
    if ext not in _GENERIC_MODEL_EXTS:
        return None
    return nm


def _ui_generic_models(nodes):
    """UI 格式：非标准加载器节点中，widget 值能匹配模型文件的，识别为模型。"""
    out, seen = [], set()
    for n in nodes:
        t = n.get("type", "")
        if t in _LOADER_TYPES or t in ("LoraLoader", "LoraLoaderModelOnly",
                                       "Note", "MarkdownNote", "Reroute", "PrimitiveNode"):
            continue
        for v in (n.get("widgets_values") or []):
            rel = _generic_model_rel(v)
            if rel and rel not in seen:
                seen.add(rel)
                out.append({"type": t, "name": rel,
                            "title": n.get("title") or "", "extra": None})
    return out


def _api_generic_models(graph):
    """API 格式：非标准加载器节点中，inputs 值能匹配模型文件的，识别为模型。"""
    out, seen = [], set()
    for node in graph.values():
        cls = node.get("class_type", "")
        if cls in _LOADER_TYPES or cls in ("LoraLoader", "LoraLoaderModelOnly",
                                           "Note", "MarkdownNote", "Reroute", "PrimitiveNode"):
            continue
        for v in (node.get("inputs") or {}).values():
            rel = _generic_model_rel(v)
            if rel and rel not in seen:
                seen.add(rel)
                out.append({"type": cls, "name": rel, "title": "", "extra": None})
    return out


def _node_sig_prompt(n):
    t = str(n.get("class_type", ""))
    inp = n.get("inputs") or {}
    if t in _LOADER_TYPES:
        for k in ("unet_name", "ckpt_name", "clip_name", "vae_name", "lora_name"):
            v = inp.get(k)
            if v:
                return "L:" + str(v)
    return ""


def _node_sig_ui(node):
    t = str(node.get("type", ""))
    wv = node.get("widgets_values") or []
    if t in _LOADER_TYPES and wv and isinstance(wv[0], str):
        return "L:" + wv[0]
    return ""


def _types_from_prompt(text):
    try:
        g = json.loads(text)
        sigs = []
        for n in g.values():
            if not isinstance(n, dict):
                continue
            ct = str(n.get("class_type", ""))
            if ct in _NON_EXEC_TYPES:
                continue
            sg = _node_sig_prompt(n)
            sigs.append(ct if not sg else ct + "|" + sg)
        return sorted(sigs)
    except Exception:
        return None


def _types_from_ui(ui):
    try:
        nodes = ui.get("nodes") or []
        sigs = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            ct = str(n.get("type", ""))
            if ct in _NON_EXEC_TYPES:
                continue
            sg = _node_sig_ui(n)
            sigs.append(ct if not sg else ct + "|" + sg)
        return sorted(sigs)
    except Exception:
        return None


def _wf_cache_path():
    d = os.path.join(config.data_root(), "_tmp")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "wf_ref_cache.json")


_wf_cache_lock = threading.Lock()


def _load_wf_cache():
    try:
        with open(_wf_cache_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_wf_cache(c):
    try:
        with open(_wf_cache_path(), "w", encoding="utf-8") as f:
            json.dump(c, f)
    except Exception:
        pass


def find_recent_output_for_workflow(wf_path, limit=6, days=30):
    """扫描输出目录，找与该工作流节点类型指纹匹配的最近生成结果（按 mtime 降序）。"""
    try:
        with open(wf_path, "r", encoding="utf-8") as f:
            ui = json.load(f)
    except Exception:
        return []
    fg = _types_from_ui(ui)
    if not fg:
        return []
    cutoff = time.time() - days * 86400
    with _wf_cache_lock:
        cache = _load_wf_cache()
    matched = []
    for base in paths.root_dirs("output"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and not config.is_excluded_dir(d)]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() != ".png":
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    mt = int(os.path.getmtime(full))
                except OSError:
                    continue
                if mt < cutoff:
                    continue
                ent = cache.get(full)
                if ent and ent.get("mtime") == mt:
                    types = ent.get("types")
                else:
                    pt = png_prompt_text(full)
                    types = _types_from_prompt(pt) if pt else None
                    cache[full] = {"mtime": mt, "types": types}
                if types and types == fg:
                    sz = png_size(full)
                    matched.append({"name": fn, "path": full, "mtime": mt,
                                    "width": sz[0] if sz else None,
                                    "height": sz[1] if sz else None})
    with _wf_cache_lock:
        if len(cache) > 20000:
            cache = {k: v for k, v in cache.items() if v.get("mtime", 0) >= cutoff}
        _save_wf_cache(cache)
    # 仅视频工作流才追加最近的视频输出（避免图片工作流的最近结果混入视频）
    is_video_wf = False
    try:
        for n in (ui.get("nodes") or []):
            t = str(n.get("type", "")).lower()
            if any(k in t for k in ("video", "视频", "t2v", "to_video", "createvideo",
                                    "savevideo", "vhs_video", "videocrop", "videocombine")):
                is_video_wf = True
                break
    except Exception:
        pass
    vid_limit = limit - len(matched)
    if is_video_wf and vid_limit > 0:
        vids = []
        for base in paths.root_dirs("output"):
            base = os.path.abspath(base)
            if not os.path.isdir(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and not config.is_excluded_dir(d)]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() not in (".mp4", ".webm", ".mov", ".m4v"):
                        continue
                    full = os.path.join(dirpath, fn)
                    try:
                        mt = int(os.path.getmtime(full))
                    except OSError:
                        continue
                    if mt < cutoff:
                        continue
                    vids.append({"name": fn, "path": full, "mtime": mt, "kind": "video"})
        vids.sort(key=lambda x: x["mtime"], reverse=True)
        matched = matched + vids[:vid_limit]
    matched.sort(key=lambda x: x["mtime"], reverse=True)
    return matched[:limit]
