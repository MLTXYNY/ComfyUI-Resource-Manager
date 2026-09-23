# -*- coding: utf-8 -*-
"""
manager.api —— Flask 蓝图：全部 REST 路由
==========================================
覆盖：配置 / 模型 / 输出 / 输入 / 主页 / 工作流 / 媒体流 / 文件操作。
所有文件操作（删除/移动/新建/打开/读取元数据/媒体流）经 paths.safe_resolve 限到已配置目录内。
"""

import json
import mimetypes
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse

from flask import Blueprint, jsonify, request, send_file

from . import config, paths, scanning, parsing, thumbnails, comfy

bp = Blueprint("api", __name__)

# 静态目录（注册时写入）
STATIC_DIR = None

# 临时上传目录（拖入 PNG 读元数据用）
TEMP_DIR = os.path.join(
    os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
    else config.data_root(),
    "_tmp")
os.makedirs(TEMP_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
# 通用
# --------------------------------------------------------------------------- #
@bp.route("/api/version")
def api_version():
    idx = os.path.join(STATIC_DIR, "index.html")
    return jsonify({"v": str(int(os.path.getmtime(idx)))})


@bp.route("/api/comfyui_host")
def comfyui_host_get():
    return jsonify({"host": config.get_comfy_host()})


@bp.route("/api/comfyui_host", methods=["POST"])
def comfyui_host_set():
    data = request.get_json(silent=True) or {}
    host = str(data.get("host") or "").strip()
    if host and not host.startswith("http://") and not host.startswith("https://"):
        host = "http://" + host
    if not host:
        return jsonify({"error": "地址不能为空"}), 400
    return jsonify({"host": config.set_comfy_host(host)})


@bp.route("/api/health")
def health():
    return jsonify(comfy.health())


@bp.route("/api/object_info")
def object_info():
    return jsonify(comfy.fetch_object_info())


@bp.route("/api/resource")
def resource():
    return jsonify(comfy.collect_resource())


@bp.route("/api/read_metadata", methods=["POST"])
def read_metadata():
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "未收到图片"}), 400
    if not (f.filename or "").lower().endswith(".png"):
        return jsonify({"error": "仅支持 PNG 图片读取参数元数据"}), 400
    name = "up_%d_%d.png" % (int(time.time() * 1000), random.randrange(1000))
    path = os.path.join(TEMP_DIR, name)
    f.save(path)
    try:
        from PIL import Image
        with Image.open(path) as img:
            text = getattr(img, "text", None) or {}
            prompt_text = text.get("prompt")
        if not prompt_text:
            return jsonify({"found": False,
                            "error": "图片中没有找到 ComfyUI 的 prompt 元数据（可能被后处理过）"})
        return jsonify({"found": True, "params": parsing.parse_prompt_graph(prompt_text)})
    except Exception as exc:
        return jsonify({"found": False, "error": str(exc)}), 500
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@bp.route("/api/storage/config", methods=["GET", "POST"])
def storage_config():
    if request.method == "POST":
        cfg = request.get_json(force=True) or {}
        model_dirs = [d for d in (cfg.get("model_dirs") or []) if isinstance(d, str) and d.strip()]
        output_dirs = [d for d in (cfg.get("output_dirs") or []) if isinstance(d, str) and d.strip()]
        input_dirs = [d for d in (cfg.get("input_dirs") or []) if isinstance(d, str) and d.strip()]
        workflow_dirs = [d for d in (cfg.get("workflow_dirs") or []) if isinstance(d, str) and d.strip()]
        output_excludes = [d for d in (cfg.get("output_excludes") or []) if isinstance(d, str) and d.strip()]
        input_excludes = [d for d in (cfg.get("input_excludes") or []) if isinstance(d, str) and d.strip()]
        missing = [d for d in model_dirs + output_dirs + input_dirs + workflow_dirs
                   if not os.path.isdir(d)]
        if missing:
            return jsonify({"error": "以下目录不存在: %s" % ", ".join(missing)}), 400
        sf = cfg.get("series_filter") or {}
        series_filter = {"pattern": sf.get("pattern") or "",
                         "exclude": [s for s in (sf.get("exclude") or [])
                                     if isinstance(s, str) and s.strip()]}
        config.save_config({
            "model_dirs": model_dirs, "output_dirs": output_dirs,
            "input_dirs": input_dirs, "workflow_dirs": workflow_dirs,
            "output_excludes": output_excludes, "input_excludes": input_excludes,
            "series_filter": series_filter,
        })
        return jsonify({"ok": True})
    return jsonify(config.load_config())


@bp.route("/api/storage/detect")
def storage_detect():
    return jsonify(paths.detect_comfy_setup())


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
@bp.route("/api/models")
def models_list():
    return jsonify(scanning.scan_models(
        q=request.args.get("q", ""),
        type_f=request.args.get("type", ""),
        series_f=request.args.get("series", "")))


# --------------------------------------------------------------------------- #
# 媒体（输出 / 输入）—— 通用 + 便捷路由
# --------------------------------------------------------------------------- #
def _resolve_media():
    """校验请求中的 path 参数并返回绝对路径，越界抛 403。"""
    try:
        return paths.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return None, jsonify({"error": str(exc)}), 403


@bp.route("/api/outputs")
def outputs_list():
    folder = request.args.get("folder", "") or None
    try:
        return jsonify(scanning.scan_outputs(
            q=request.args.get("q", ""), sort=request.args.get("sort", "new"),
            kind=request.args.get("kind", "all"), folder=folder,
            ratio=request.args.get("ratio", "")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403


@bp.route("/api/inputs")
def inputs_list():
    folder = request.args.get("folder", "") or None
    try:
        return jsonify(scanning.scan_inputs(
            q=request.args.get("q", ""), sort=request.args.get("sort", "new"),
            kind=request.args.get("kind", "all"), folder=folder,
            ratio=request.args.get("ratio", "")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403


@bp.route("/api/output_folders")
def output_folders():
    return jsonify(scanning.list_output_folders())


@bp.route("/api/input_folders")
def input_folders():
    return jsonify(scanning.list_input_folders())


def _media_meta(path):
    if not os.path.isfile(path):
        return {"found": False, "error": "文件不存在"}
    ext = os.path.splitext(path)[1].lower()
    if ext not in paths.IMAGE_EXTS:
        if ext in paths.VIDEO_EXTS:
            meta = config.load_video_meta().get(path)
            if meta:
                return {"found": True, "params": meta, "from": "video_meta"}
            return {"found": False,
                    "error": "该视频没有关联的工作流元数据（可在工作流页点击视频自动关联）"}
        return {"found": False, "error": "仅图片/视频支持读取参数元数据"}
    try:
        from PIL import Image
        with Image.open(path) as img:
            text = getattr(img, "text", None) or {}
            prompt_text = text.get("prompt")
        if not prompt_text:
            return {"found": False,
                    "error": "该图片没有 ComfyUI prompt 元数据（可能被截图或后处理过）"}
        params = parsing.parse_prompt_graph(prompt_text)
        sz = parsing.png_size(path)
        if sz:
            params["width"], params["height"] = sz
        return {"found": True, "params": params}
    except Exception as exc:
        return {"found": False, "error": str(exc)}


@bp.route("/api/video_meta", methods=["POST"])
def video_meta_link():
    """用指定工作流提取参数，写入该视频的本地旁路元数据。"""
    data = request.get_json(force=True) or {}
    try:
        p = paths.safe_resolve(data.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    if os.path.splitext(p)[1].lower() not in paths.VIDEO_EXTS:
        return jsonify({"error": "仅支持视频文件关联元数据"}), 400
    try:
        wf = paths.safe_resolve(data.get("workflow_path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(wf):
        return jsonify({"error": "工作流不存在: %s" % wf}), 404
    try:
        info = parsing.parse_workflow_file(wf)
    except Exception as exc:
        return jsonify({"error": "工作流解析失败: %s" % exc}), 500
    meta = _wf_to_video_meta(info, p, wf)
    config.save_video_meta(p, meta)
    return jsonify({"found": True, "params": meta})


def _wf_to_video_meta(info, vpath, wf):
    s = info.get("sampler") or {}
    samplers = info.get("samplers") or ([s] if s else [])
    meta = {
        "positive": info.get("positive") or "",
        "negative": info.get("negative") or "",
        "extra_texts": info.get("extra_texts") or [],
        "samplers": samplers,
        "multi_sampler": bool(info.get("multi_sampler") or len(samplers) > 1),
        "sampler_name": s.get("sampler_name"),
        "scheduler": s.get("scheduler"),
        "steps": s.get("steps"),
        "cfg": s.get("cfg"),
        "seed": s.get("seed"),
        "denoise": s.get("denoise"),
        "workflow_path": wf,
        "workflow_name": os.path.basename(wf),
    }
    try:
        vi = parsing.video_info(vpath)
        if vi:
            meta["width"] = vi.get("width")
            meta["height"] = vi.get("height")
    except Exception:
        pass
    return meta


@bp.route("/api/output_meta")
def output_meta():
    try:
        p = paths.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    return jsonify(_media_meta(p))


@bp.route("/api/input_meta")
def input_meta():
    try:
        p = paths.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    return jsonify(_media_meta(p))


def _video_info_route():
    try:
        p = paths.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    info = parsing.video_info(p)
    return jsonify({"found": bool(info), "info": info or {}})


@bp.route("/api/output_video_info")
def output_video_info():
    return _video_info_route()


@bp.route("/api/input_video_info")
def input_video_info():
    return _video_info_route()


# --------------------------------------------------------------------------- #
# 主页（瀑布式聚合）
# --------------------------------------------------------------------------- #
@bp.route("/api/home")
def home_list():
    try:
        limit = int(request.args.get("limit", "300"))
    except (TypeError, ValueError):
        limit = 300
    limit = max(1, min(limit, 1000))
    items = scanning.scan_home(
        limit=limit,
        kind=request.args.get("kind", "all"),
        scope=request.args.get("scope", "all"),
        ratio=request.args.get("ratio", ""))
    return jsonify(items)


# --------------------------------------------------------------------------- #
# 工作流
# --------------------------------------------------------------------------- #
@bp.route("/api/workflows")
def workflows_list():
    folder = request.args.get("folder", "") or None
    try:
        return jsonify(scanning.scan_workflows(
            q=request.args.get("q", ""), sort=request.args.get("sort", "new"),
            folder=folder))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403


@bp.route("/api/workflow_folders")
def workflow_folders():
    return jsonify(scanning.list_workflow_folders())


@bp.route("/api/workflow")
def workflow_detail():
    try:
        p = paths.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if not os.path.isfile(p):
        return jsonify({"error": "文件不存在"}), 404
    try:
        info = parsing.parse_workflow_file(p)
    except json.JSONDecodeError:
        return jsonify({"error": "文件不是有效的 JSON（可能已损坏）"}), 400
    except Exception as exc:
        return jsonify({"error": "解析失败: %s" % exc}), 500

    info["path"] = p
    info["name"] = os.path.basename(p)
    info["mtime"] = int(os.path.getmtime(p))
    info["file_size"] = os.path.getsize(p)

    # 联动：把模型管理页的全局备注带到工作流「调用模型 / LoRA」区块
    for m in info.get("models") or []:
        if isinstance(m, dict):
            m["note"] = _note_for_model(m.get("name"))
    for l in info.get("loras") or []:
        if isinstance(l, dict):
            l["note"] = _note_for_model(l.get("name"))

    missing_set = set()
    if info.get("format") != "unknown":
        oi = comfy.fetch_object_info()
        all_classes = oi.get("all_classes") if isinstance(oi, dict) else None
        if all_classes:
            decor = {"Note", "MarkdownNote", "PreviewImage", "ImageScaleBy",
                     "Reroute", "PrimitiveNode"}
            missing = [c for c in info.get("classes") or []
                       if c not in all_classes and c not in decor]
            info["missing"] = missing
            missing_set = set(missing)
    info["builtin_nodes"], info["custom_nodes"] = comfy.node_groups(
        info.get("classes") or [], missing_set)
    return jsonify(info)


def _note_for_model(name):
    """把工作流引用的模型名映射到磁盘完整路径，返回全局备注（notes.json，key 为模型完整路径）。
    工作流加载器里的模型名通常相对"类型子目录"（如 diffusion_models/A01Z_image/...），
    因此依次尝试：根目录直接拼接 -> 根下每个类型子目录拼接。"""
    if not name:
        return ""
    try:
        notes = config.load_notes()
    except Exception:
        return ""
    nm = str(name).replace("/", "\\")
    for base in paths.root_dirs("model"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        try:
            cand = os.path.join(base, nm)
            if os.path.isfile(cand):
                return notes.get(cand, "")
        except OSError:
            pass
        try:
            for sub in os.listdir(base):
                subp = os.path.join(base, sub)
                if not os.path.isdir(subp):
                    continue
                c2 = os.path.join(subp, nm)
                if os.path.isfile(c2):
                    return notes.get(c2, "")
        except OSError:
            continue
    return ""


@bp.route("/api/workflow/note", methods=["POST"])
def workflow_note_save():
    """保存 Note / MarkdownNote 节点文本修改（UI 格式工作流，写回原文件）。"""
    data = request.get_json(force=True) or {}
    try:
        p = paths.safe_resolve(data.get("path"))
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


@bp.route("/api/wf_recent")
def wf_recent():
    try:
        p = paths.safe_resolve(request.args.get("path", ""))
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
        items = parsing.find_recent_output_for_workflow(p, limit=limit)
    except Exception as exc:
        return jsonify({"error": "匹配失败: %s" % exc}), 500
    return jsonify(items)


# --------------------------------------------------------------------------- #
# 文件操作（均限已配置目录内，越界 403）
# --------------------------------------------------------------------------- #
@bp.route("/api/storage/note", methods=["POST"])
def storage_note():
    data = request.get_json(force=True) or {}
    try:
        p = paths.safe_resolve(data.get("path"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    note = data.get("note") or ""
    config.save_note(p, note)
    return jsonify({"ok": True, "note": config.load_notes().get(p, "")})


@bp.route("/api/storage/mkdir", methods=["POST"])
def storage_mkdir():
    data = request.get_json(force=True) or {}
    try:
        p = paths.safe_resolve(data.get("path"), require_exists=False)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    try:
        os.makedirs(p, exist_ok=True)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": "创建文件夹失败: %s" % exc}), 500


@bp.route("/api/storage/rmdir", methods=["POST"])
def storage_rmdir():
    data = request.get_json(force=True) or {}
    try:
        p = paths.safe_resolve(data.get("path"))
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


@bp.route("/api/storage/delete", methods=["POST"])
def storage_delete():
    data = request.get_json(force=True) or {}
    try:
        p = paths.safe_resolve(data.get("path"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    try:
        os.remove(p)
        if os.path.splitext(p)[1].lower() in paths.VIDEO_EXTS:
            config.remove_video_meta(p)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": "删除失败: %s" % exc}), 500


@bp.route("/api/storage/move", methods=["POST"])
def storage_move():
    data = request.get_json(force=True) or {}
    try:
        src = paths.safe_resolve(data.get("path"))
        target = paths.safe_resolve(data.get("target_dir"), require_exists=False)
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


@bp.route("/api/storage/open", methods=["POST"])
def storage_open():
    data = request.get_json(force=True) or {}
    try:
        p = paths.safe_resolve(data.get("path"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    try:
        folder = p if os.path.isdir(p) else os.path.dirname(p)
        if hasattr(os, "startfile"):
            os.startfile(folder)
        else:
            if sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": "打开文件夹失败: %s" % exc}), 500


@bp.route("/api/cache/clear", methods=["POST"])
def cache_clear():
    """清空 _tmp 缓存目录内容（缩略图 / 工作流匹配缓存 / 临时上传），保留目录本身。"""
    t = TEMP_DIR
    if not os.path.isdir(t):
        return jsonify({"ok": True, "removed": 0, "size": 0})
    removed = 0
    freed = 0
    for name in os.listdir(t):
        full = os.path.join(t, name)
        try:
            if os.path.isdir(full):
                for _r, _d, files in os.walk(full):
                    for fn in files:
                        try:
                            freed += os.path.getsize(os.path.join(_r, fn))
                        except OSError:
                            pass
                shutil.rmtree(full)
            else:
                try:
                    freed += os.path.getsize(full)
                except OSError:
                    pass
                os.remove(full)
            removed += 1
        except OSError:
            pass
    return jsonify({"ok": True, "removed": removed, "size": freed})


# --------------------------------------------------------------------------- #
# 媒体流（缩略图 / 原图 / 视频）
# --------------------------------------------------------------------------- #
@bp.route("/media")
def media():
    try:
        p = paths.safe_resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 403
    if request.args.get("thumb") == "1":
        try:
            p = thumbnails.make_thumbnail(p)
        except Exception:
            return "", 404
    mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
    return send_file(p, mimetype=mime, conditional=True)


def register_app_routes(app, static_dir):
    """挂载首页路由（带 no-store 防缓存）与蓝图。"""
    global STATIC_DIR
    STATIC_DIR = static_dir

    @app.route("/")
    def index():
        idx = os.path.join(static_dir, "index.html")
        html = open(idx, encoding="utf-8").read()
        ver = str(int(os.path.getmtime(idx)))
        html = html.replace("__CUR_VER__", ver)
        resp = app.response_class(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    app.register_blueprint(bp)
