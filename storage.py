# -*- coding: utf-8 -*-
"""
storage.py —— 模型与输出文件管理

提供：ComfyUI 安装路径/多模型目录自动检测、模型与输出扫描、缩略图、
路径安全校验、删除/移动/打开文件夹所需的纯函数。路由见 app.py。
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".sft", ".gguf", ".onnx", ".engine"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}
PREVIEW_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("model_dirs", [])
    cfg.setdefault("output_dirs", [])
    cfg.setdefault("workflow_dirs", [])
    cfg.setdefault("output_excludes", [])
    cfg.setdefault("series_filter", {"pattern": "", "exclude": []})
    return cfg


def output_excludes():
    return [e.strip() for e in load_config().get("output_excludes", []) if str(e).strip()]


def get_comfy_host():
    """ComfyUI 服务地址：config.comfyui_host 优先，其次环境变量 COMFY_HOST，默认 8188。"""
    cfg_host = str(load_config().get("comfyui_host") or "").strip().rstrip("/")
    env_host = str(os.environ.get("COMFY_HOST") or "").strip().rstrip("/")
    return cfg_host or env_host or "http://127.0.0.1:8188"


def set_comfy_host(host):
    cfg = load_config()
    cfg["comfyui_host"] = str(host or "").strip().rstrip("/")
    save_config(cfg)
    return get_comfy_host()


def is_excluded_dir(name):
    ex = [e.lower() for e in output_excludes()]
    if not ex:
        return False
    n = name.lower()
    if n in ex:
        return True
    # 排除项可能是完整路径（如 E:\\...\\minimax_seg_cache），取其 basename 与目录名比对
    for e in ex:
        base = e.replace("/", "\\").rstrip("\\").split("\\")[-1]
        if base and n == base:
            return True
    return False


def series_filter_cfg():
    sf = load_config().get("series_filter", {}) or {}
    return {"pattern": sf.get("pattern") or "", "exclude": [s for s in sf.get("exclude", []) if s]}


_notes_lock = threading.Lock()


def load_notes():
    np = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.json")
    try:
        with open(np, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_note(path, note):
    with _notes_lock:
        notes = load_notes()
        if note and note.strip():
            notes[path] = note.strip()
        else:
            notes.pop(path, None)
        np = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.json")
        with open(np, "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)
        return notes.get(path)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# --------------------------------------------------------------------------- #
# 自动检测：从 ComfyUI 进程命令行推导目录
# --------------------------------------------------------------------------- #
def detect_comfy_setup():
    found = {
        "root": None, "output_dirs": [], "model_dirs": [], "workflow_dirs": [],
        "extra_yaml": None, "hint": "",
    }
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match 'ComfyUI' } | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=30)
    except Exception as exc:
        found["hint"] = "检测失败: %s" % exc
        return found

    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if not lines:
        found["hint"] = "未找到 ComfyUI 进程（请先启动 ComfyUI 再自动检测）"
        return found
    cmd = lines[0]

    m = re.search(r"([A-Za-z]:\\[^\"']*?)[\\/]\.venv[\\/]Scripts[\\/]python\.exe", cmd)
    if m:
        found["root"] = m.group(1)
    m = re.search(r"--output-directory\s+(?:\"([^\"]+)\"|(\S+))", cmd)
    if m:
        found["output_dirs"].append((m.group(1) or m.group(2)).strip().rstrip("\\/"))
    m = re.search(r"--extra-model-paths-config\s+\"([^\"]+)\"", cmd)
    if m:
        found["extra_yaml"] = m.group(1).strip().strip('"')

    if not found["root"]:
        found["hint"] = "无法从进程命令行定位 ComfyUI 安装目录"
        return found

    base_models = os.path.join(found["root"], "models")
    if os.path.isdir(base_models):
        found["model_dirs"].append(base_models)

    if found["extra_yaml"] and os.path.isfile(found["extra_yaml"]):
        try:
            with open(found["extra_yaml"], "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            for mm in re.finditer(r"^[ \t]*base_path:[ \t]*['\"]([^'\"]+)['\"]", text, re.M):
                p = mm.group(1).strip().rstrip("\\/")
                if os.path.isdir(p):
                    found["model_dirs"].append(p)
        except Exception:
            pass

    default_out = os.path.join(found["root"], "output")
    if os.path.isdir(default_out) and default_out not in found["output_dirs"]:
        found["output_dirs"].append(default_out)

    default_wf = os.path.join(found["root"], "user", "default", "workflows")
    if os.path.isdir(default_wf):
        found["workflow_dirs"].append(default_wf)

    seen = set()
    found["model_dirs"] = [d for d in found["model_dirs"]
                           if d not in seen and not seen.add(d) and os.path.isdir(d)]
    found["output_dirs"] = [d for d in found["output_dirs"]
                            if os.path.isdir(d)]
    return found


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #
def _root_dirs(kind):
    cfg = load_config()
    if kind == "model":
        lst = cfg.get("model_dirs", [])
    elif kind == "output":
        lst = cfg.get("output_dirs", [])
    else:
        lst = cfg.get("workflow_dirs", [])
    return [os.path.abspath(os.path.normpath(d)) for d in lst if isinstance(d, str)]


def safe_resolve(path, require_exists=True):
    """确保 path 位于已配置的根目录内；返回规范化绝对路径，否则抛 ValueError。"""
    if not path:
        raise ValueError("路径为空")
    p = os.path.abspath(os.path.normpath(path))
    for root in (_root_dirs("model") + _root_dirs("output") + _root_dirs("workflow")):
        try:
            if os.path.commonpath([root, p]) == root:
                if require_exists and not os.path.exists(p):
                    raise ValueError("文件不存在: %s" % p)
                return p
        except ValueError:
            continue
    raise ValueError("路径不在允许的目录内: %s" % p)


# --------------------------------------------------------------------------- #
# 模型扫描
# --------------------------------------------------------------------------- #
def find_preview(model_path):
    d = os.path.dirname(model_path)
    stem = os.path.splitext(os.path.basename(model_path))[0]
    candidates = []
    for ext in PREVIEW_EXTS:
        candidates.append(os.path.join(d, stem + ext))
        candidates.append(os.path.join(d, stem + ".preview" + ext))
    candidates.append(os.path.join(d, "preview", stem + ".png"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _fmt_rel(full, base):
    return os.path.relpath(full, base).replace("\\", "/")


def scan_models(q="", type_f="", series_f="", limit=3000):
    """扫描所有模型根目录，按顶层子文件夹分类 + 二级目录系列（如 A01Z_image）。
    返回 {types, series, type_dirs, items}；series 下拉应用 series_filter 规则。"""
    types = set()
    raw_series = set()
    type_dirs = []
    items = []
    sf = series_filter_cfg()
    pattern = sf["pattern"]
    exclude = sf["exclude"]
    notes = load_notes()
    for base in _root_dirs("model"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        try:
            subdirs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        except OSError:
            subdirs = []
        if not subdirs:
            subdirs = [""]
        for typ in subdirs:
            if type_f and typ != type_f:
                continue
            tdir = os.path.join(base, typ) if typ else base
            if typ:
                types.add(typ)
                type_dirs.append({"label": "%s\\%s" % (base, typ), "path": tdir})
            try:
                for dirpath, dirnames, filenames in os.walk(tdir):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for fn in filenames:
                        if os.path.splitext(fn)[1].lower() not in MODEL_EXTS:
                            continue
                        full = os.path.join(dirpath, fn)
                        rel = _fmt_rel(full, base)
                        if q and q.lower() not in rel.lower():
                            continue
                        parts = rel.split("/")
                        # 系列 = 二级目录名（如 A01Z_image）；文件直接放在类型目录下时
                        # 归为该类型目录名（如 SEEDVR2），避免把文件名误当系列
                        if len(parts) > 2:
                            s = parts[1]
                        elif len(parts) == 2:
                            s = parts[0]
                        else:
                            s = ""
                        if series_f and s != series_f:
                            continue
                        if s:
                            raw_series.add(s)
                        items.append({
                            "id": "m%d" % len(items),
                            "name": fn,
                            "rel": rel,
                            "type": typ or "root",
                            "series": s,
                            "root": base,
                            "path": full,
                            "size": os.path.getsize(full),
                            "mtime": int(os.path.getmtime(full)),
                            "preview_path": find_preview(full) or None,
                            "note": notes.get(full, ""),
                        })
                        if len(items) >= limit:
                            return {"types": sorted(types), "series": _filter_series(raw_series, pattern, exclude),
                                    "type_dirs": type_dirs, "items": _apply_series_exclude(items, exclude),
                                    "truncated": True}
            except OSError:
                continue
    return {"types": sorted(types), "series": _filter_series(raw_series, pattern, exclude),
            "type_dirs": type_dirs, "items": _apply_series_exclude(items, exclude),
            "truncated": False}


def _filter_series(raw, pattern, exclude):
    """下拉候选：排除列表必过滤；pattern 非空时正则匹配。"""
    import re
    out = []
    for s in raw:
        if s in exclude:
            continue
        if pattern and not re.search(pattern, s):
            continue
        out.append(s)
    return sorted(out)


def _apply_series_exclude(items, exclude):
    """列表徽章：排除列表命中的系列置空（不显示徽章）。"""
    if not exclude:
        return items
    for it in items:
        if it.get("series") in exclude:
            it["series"] = ""
    return items


# --------------------------------------------------------------------------- #
# 输出扫描
# --------------------------------------------------------------------------- #
def _root_of(p, kind="output"):
    for r in _root_dirs(kind):
        try:
            if os.path.commonpath([r, p]) == r:
                return r
        except ValueError:
            continue
    return None


RATIO_PRESETS = {
    "1:1": 1.0, "2:3": 2 / 3.0, "3:2": 3 / 2.0, "3:4": 3 / 4.0,
    "4:3": 4 / 3.0, "9:16": 9 / 16.0, "16:9": 16 / 9.0, "21:9": 21 / 9.0,
}


def _img_ratio(path):
    """轻量读取图片宽高比（PIL 只读头部不解码像素）。失败返回 None。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
            if not h:
                return None
            return w / h
    except Exception:
        return None


def _ratio_match(r, preset):
    """图片比例 r 是否匹配预设（容差 2%）。"""
    t = RATIO_PRESETS.get(preset)
    if t is None:
        return False
    return abs(r - t) / t < 0.02


def _ratio_filter_ok(r, ratios):
    """图片比例 r 是否通过多选比例列表（并集；other=非预设比例）。"""
    matched = any(_ratio_match(r, p) for p in ratios if p != "other")
    if "other" in ratios:
        return matched or not any(_ratio_match(r, p) for p in RATIO_PRESETS)
    return matched


def scan_outputs(q="", sort="new", kind="all", limit=2000, folder=None, ratio=""):
    """扫描输出目录。folder 给定时只列该文件夹内的直接文件（非递归）。"""
    items = []
    if folder:
        base = os.path.abspath(folder)
        root = _root_of(base)
        if root is None:
            raise ValueError("目录不在允许的输出目录内: %s" % base)
        if os.path.isdir(base):
            try:
                names = sorted(os.listdir(base))
            except OSError:
                names = []
            for fn in names:
                full = os.path.join(base, fn)
                if not os.path.isfile(full):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext in IMAGE_EXTS:
                    k = "image"
                elif ext in VIDEO_EXTS:
                    k = "video"
                else:
                    continue
                if kind != "all" and kind != k:
                    continue
                if ratio:
                    if k != "image":
                        continue
                    r = _img_ratio(full)
                    if r is None:
                        continue
                    _ratios = [x.strip() for x in ratio.split(",") if x.strip()]
                    if not _ratio_filter_ok(r, _ratios):
                        continue
                rel = _fmt_rel(full, root)
                if q and q.lower() not in rel.lower():
                    continue
                items.append({
                    "id": "o%d" % len(items),
                    "name": fn,
                    "rel": rel,
                    "root": root,
                    "path": full,
                    "size": os.path.getsize(full),
                    "mtime": int(os.path.getmtime(full)),
                    "kind": k,
                })
                if len(items) >= limit:
                    break
        items.sort(key=lambda x: x["mtime"], reverse=(sort == "new"))
        return items

    for base in _root_dirs("output"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not is_excluded_dir(d)]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in IMAGE_EXTS:
                    k = "image"
                elif ext in VIDEO_EXTS:
                    k = "video"
                else:
                    continue
                if kind != "all" and kind != k:
                    continue
                full = os.path.join(dirpath, fn)
                if ratio:
                    if k != "image":
                        continue
                    r = _img_ratio(full)
                    if r is None:
                        continue
                    _ratios = [x.strip() for x in ratio.split(",") if x.strip()]
                    if not _ratio_filter_ok(r, _ratios):
                        continue
                rel = _fmt_rel(full, base)
                if q and q.lower() not in rel.lower():
                    continue
                items.append({
                    "id": "o%d" % len(items),
                    "name": fn,
                    "rel": rel,
                    "root": base,
                    "path": full,
                    "size": os.path.getsize(full),
                    "mtime": int(os.path.getmtime(full)),
                    "kind": k,
                })
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
    items.sort(key=lambda x: x["mtime"], reverse=(sort == "new"))
    return items


def _walk_folders(dirpath, depth, base, out, limit, excluded=None):
    if len(out) >= limit:
        return
    try:
        entries = sorted(os.listdir(dirpath))
    except OSError:
        return
    im = vi = 0
    subs = []
    for e in entries:
        full = os.path.join(dirpath, e)
        if os.path.isdir(full):
            if not e.startswith(".") and not (excluded and is_excluded_dir(e)):
                subs.append(full)
        elif os.path.isfile(full):
            ext = os.path.splitext(e)[1].lower()
            if ext in IMAGE_EXTS:
                im += 1
            elif ext in VIDEO_EXTS:
                vi += 1
    out.append({
        "root": base,
        "path": dirpath,
        "rel": _fmt_rel(dirpath, base),
        "name": os.path.basename(dirpath) or dirpath,
        "depth": depth,
        "images": im,
        "videos": vi,
    })
    for s in subs:
        _walk_folders(s, depth + 1, base, out, limit, excluded=excluded)


def list_output_folders(limit=4000):
    """输出目录文件夹树（含每个文件夹内的图片/视频数量，跳过排除目录）。"""
    nodes = []
    for base in _root_dirs("output"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        nodes.append({
            "root": base, "path": base, "rel": "",
            "name": os.path.basename(base) or base, "depth": 0,
            "images": 0, "videos": 0,
        })
        _walk_folders(base, 1, base, nodes, limit, excluded=True)
    return nodes


# --------------------------------------------------------------------------- #
# 缩略图
# --------------------------------------------------------------------------- #
def _thumb_cache_dir():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp", "thumbs")
    os.makedirs(d, exist_ok=True)
    return d


# 缩略图并发安全：bg/fg 两个 img 共用同一 URL，缓存未命中时可能并发写同一文件，
# 必须加锁 + 原子替换，避免边写边读产生损坏 JPEG（表现为缩略图灰条/半图）。
_thumb_lock = threading.Lock()


def _jpeg_ok(p):
    try:
        with open(p, "rb") as f:
            return f.read(2) == b"\xff\xd8"
    except Exception:
        return False


def make_thumbnail(path, max_side=240, quality=82):
    key = hashlib.sha1(("%s|%d" % (path, os.path.getmtime(path))).encode("utf-8")).hexdigest()[:16]
    out = os.path.join(_thumb_cache_dir(), key + ".jpg")
    if os.path.isfile(out) and _jpeg_ok(out):
        return out
    tmp = out + ".tmp.jpg"
    with _thumb_lock:
        if os.path.isfile(out) and _jpeg_ok(out):
            return out
        ext = os.path.splitext(path)[1].lower()
        if ext in VIDEO_EXTS:
            _make_video_thumb(path, tmp, max_side)
        else:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((max_side, max_side))
                im.save(tmp, "JPEG", quality=quality)
        os.replace(tmp, out)
    return out


# --------------------------------------------------------------------------- #
# 工作流扫描与解析
# --------------------------------------------------------------------------- #
WORKFLOW_EXTS = {".json"}


def _natural_key(name):
    """Windows 资源管理器风格自然排序：数字按数值、字母按字典序（不区分大小写）。"""
    import re
    return [(0, int(t)) if t.isdigit() else (1, t.lower()) for t in re.split(r"(\d+)", name)]


def _sort_workflow_items(items, sort):
    if sort == "name":
        items.sort(key=lambda x: _natural_key(x["name"]))
    elif sort == "old":
        items.sort(key=lambda x: x["mtime"], reverse=False)
    else:
        items.sort(key=lambda x: x["mtime"], reverse=True)


def scan_workflows(q="", sort="new", limit=2000, folder=None):
    items = []
    if folder:
        base = os.path.abspath(folder)
        root = _root_of(base, "workflow")
        if root is None:
            raise ValueError("目录不在允许的工作流目录内: %s" % base)
        if os.path.isdir(base):
            # 递归列出该分支下的全部工作流（点击文件夹 = 浏览其子树）
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() not in WORKFLOW_EXTS:
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = _fmt_rel(full, root)
                    if q and q.lower() not in rel.lower():
                        continue
                    items.append({
                        "id": "w%d" % len(items),
                        "name": fn,
                        "rel": rel,
                        "root": root,
                        "path": full,
                        "size": os.path.getsize(full),
                        "mtime": int(os.path.getmtime(full)),
                    })
                    if len(items) >= limit:
                        _sort_workflow_items(items, sort)
                        return items
        _sort_workflow_items(items, sort)
        return items

    for base in _root_dirs("workflow"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in WORKFLOW_EXTS:
                    continue
                full = os.path.join(dirpath, fn)
                rel = _fmt_rel(full, base)
                if q and q.lower() not in rel.lower():
                    continue
                items.append({
                    "id": "w%d" % len(items),
                    "name": fn,
                    "rel": rel,
                    "root": base,
                    "path": full,
                    "size": os.path.getsize(full),
                    "mtime": int(os.path.getmtime(full)),
                })
                if len(items) >= limit:
                    _sort_workflow_items(items, sort)
                    return items
    _sort_workflow_items(items, sort)
    return items


def _walk_workflow_folders(dirpath, depth, base, out, limit):
    """深度优先构建文件夹树，workflows = 该目录子树内工作流总数（含深层子目录）。"""
    if len(out) >= limit:
        return 0
    try:
        entries = sorted(os.listdir(dirpath))
    except OSError:
        return 0
    cnt = 0
    subs = []
    for e in entries:
        full = os.path.join(dirpath, e)
        if os.path.isdir(full):
            if not e.startswith("."):
                subs.append(full)
        elif os.path.isfile(full):
            if os.path.splitext(e)[1].lower() in WORKFLOW_EXTS:
                cnt += 1
    idx = len(out)
    out.append({
        "root": base,
        "path": dirpath,
        "rel": _fmt_rel(dirpath, base),
        "name": os.path.basename(dirpath) or dirpath,
        "depth": depth,
        "workflows": 0,
    })
    for s in subs:
        cnt += _walk_workflow_folders(s, depth + 1, base, out, limit)
    out[idx]["workflows"] = cnt
    return cnt


def list_workflow_folders(limit=2000):
    """工作流目录文件夹树（节点 workflows 为分支累计数量）。"""
    nodes = []
    for base in _root_dirs("workflow"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        _walk_workflow_folders(base, 0, base, nodes, limit)
    return nodes


# 常见节点在 UI 格式中的 widget 顺序（widgets_values 与之一一对应）
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
    - UI 格式：{nodes:[...], links:[...]}（ComfyUI 编辑器保存格式）
    - API 格式：{id:{class_type, inputs}}（可直接提交 /prompt 的执行图）
    返回格式、节点数、模型、LoRA、采样参数、尺寸、提示词、节点类型列表等。
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


def _parse_ui_nodes(nodes):
    models, loras, classes = [], [], []
    sampler, size = {}, None
    texts = []
    for n in nodes:
        t = n.get("type", "")
        classes.append(t)
        wv = n.get("widgets_values") or []
        title = n.get("title") or ""

        def w(idx, default=None):
            return wv[idx] if idx < len(wv) else default

        if t in ("CheckpointLoaderSimple", "UNETLoader", "CLIPLoader",
                 "DualCLIPLoader", "VAELoader"):
            models.append({
                "type": t, "name": w(0), "title": title,
                "extra": w(1) if len(wv) > 1 else None,
            })
        elif t in ("LoraLoader", "LoraLoaderModelOnly"):
            loras.append({
                "name": w(0),
                "strength_model": w(1, 1.0),
                "strength_clip": w(2, 1.0) if t == "LoraLoader" else None,
                "title": title,
            })
        elif t == "KSampler":
            sampler = {
                "seed": w(0), "steps": w(2), "cfg": w(3),
                "sampler_name": w(4), "scheduler": w(5), "denoise": w(6),
            }
        elif t == "KSamplerAdvanced":
            sampler = {
                "seed": w(0), "steps": w(2), "cfg": w(3),
                "sampler_name": w(4), "scheduler": w(5),
            }
        elif t == "EmptyLatentImage":
            # widgets_values 在宽高为链接输入时只是占位，检测 inputs 的 link，链接则视为不可靠
            wd, ht = w(0), w(1)
            inps = n.get("inputs")
            linked = False
            if isinstance(inps, list):
                for ii in inps:
                    if isinstance(ii, dict) and ii.get("name") in ("width", "height") and ii.get("link") is not None:
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
            notes.append({
                "id": n.get("id"),
                "type": nt,
                "title": n.get("title") or "",
                "text": wv0 if isinstance(wv0, str) else "",
            })
    positive, negative = (texts[0] if len(texts) > 0 else None,
                          texts[1] if len(texts) > 1 else None)
    return {
        "node_count": len(nodes), "classes": classes, "models": models,
        "loras": loras, "sampler": sampler, "size": size,
        "positive": positive, "negative": negative,
        "extra_texts": texts[2:],
        "notes": notes,
    }


def _parse_api_nodes(graph):
    models, loras, classes = [], [], []
    sampler, size = {}, None
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
            models.append({"type": cls, "name": inp.get("clip_name") or inp.get("clip_name1"),
                           "title": "", "extra": inp.get("type")})
        elif cls in ("LoraLoader", "LoraLoaderModelOnly"):
            loras.append({
                "name": inp.get("lora_name"),
                "strength_model": inp.get("strength_model"),
                "strength_clip": inp.get("strength_clip"),
                "title": "",
            })
        elif cls in ("KSampler", "KSamplerAdvanced") and not sampler:
            sampler = {k: inp.get(k) for k in
                       ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise")
                       if k in inp}
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
    return {
        "node_count": len(graph), "classes": classes, "models": models,
        "loras": loras, "sampler": sampler, "size": size,
        "positive": positive, "negative": negative, "extra_texts": [],
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

    # 回退：KSampler 链路解析不到时，收集所有 CLIPTextEncode 文本（按出现顺序取正/反）
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
    return params

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


def _ffmpeg_path():
    """定位 ffmpeg：优先从已配置目录反推 ComfyUI venv 内的 imageio_ffmpeg 二进制，
    其次 PATH。候选按顺序探测，命中第一个存在者即返回。"""
    cand = []
    seen_roots = set()
    for kind in ("model", "output", "workflow"):
        for r in _root_dirs(kind):
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


def _make_video_thumb(path, out_tmp, max_side=240):
    """用 ffmpeg 抽视频一帧生成缩略图，失败抛异常（由调用方转为 404）。"""
    ff = _ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg 不可用")
    r = subprocess.run(
        [ff, "-y", "-ss", "0.2", "-i", path, "-vframes", "1",
         "-vf", "scale=%d:-2" % max_side, "-q:v", "3", "-update", "1", out_tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    if r.returncode != 0 or not os.path.isfile(out_tmp):
        raise RuntimeError("视频抽帧失败")


# --------------------------------------------------------------------------- #
# 工作流 → 最近生成结果匹配（PNG 内嵌 prompt 元数据指纹）
# --------------------------------------------------------------------------- #
def _png_prompt_text(path):
    """手动解析 PNG tEXt chunk 读取 ComfyUI prompt 元数据（流式跳读，避免 PIL 整文件开销）。

    ComfyUI 把 API 格式工作流写进 tEXt 的 prompt 键，位于 IDAT 之前；
    到 IDAT 仍未找到即认为没有元数据（比完整解析快一个数量级）。
    """
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
                    f.read(4)  # crc
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


def _png_size(path):
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


_LOADER_TYPES = {
    "UNETLoader", "CheckpointLoaderSimple", "CLIPLoader", "VAELoader",
    "LoraLoaderModelOnly", "LoraLoader", "UNETLoaderAdvanced", "DualCLIPLoader",
    "CLIPVisionLoader", "DiffusionModelLoader",
}


def _node_sig_prompt(n):
    """API 格式节点 -> 关键签名：loader 类取模型文件名（区分同模板不同模型的工作流）。

    采样器/调度器/步数等易变参数不进指纹——同一工作流改过采样器后，
    历史输出仍应匹配（如 Base+loras 曾用 euler 生成，现在 heun）。
    """
    t = str(n.get("class_type", ""))
    inp = n.get("inputs") or {}
    if t in _LOADER_TYPES:
        for k in ("unet_name", "ckpt_name", "clip_name", "vae_name", "lora_name"):
            v = inp.get(k)
            if v:
                return "L:" + str(v)
    return ""


def _node_sig_ui(node):
    """UI 格式节点 -> 关键签名：loader 类取模型文件名。"""
    t = str(node.get("type", ""))
    wv = node.get("widgets_values") or []
    if t in _LOADER_TYPES and wv and isinstance(wv[0], str):
        return "L:" + wv[0]
    return ""


def _types_from_prompt(text):
    """API 格式 prompt -> 排序后的指纹（节点类型 + 关键参数签名）。

    指纹含 loader 模型文件名与 KSampler 采样器/调度器——同模板但模型/采样器
    不同的工作流（如 Base 与 Turbo 共用同一节点结构）也能区分。
    """
    try:
        g = json.loads(text)
        sigs = []
        for n in g.values():
            if not isinstance(n, dict):
                continue
            ct = str(n.get("class_type", ""))
            sg = _node_sig_prompt(n)
            sigs.append(ct if not sg else ct + "|" + sg)
        return sorted(sigs)
    except Exception:
        return None


def _types_from_ui(ui):
    """UI 格式工作流 -> 排序后的指纹（节点类型 + 关键参数签名）。"""
    try:
        nodes = ui.get("nodes") or []
        sigs = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            ct = str(n.get("type", ""))
            sg = _node_sig_ui(n)
            sigs.append(ct if not sg else ct + "|" + sg)
        return sorted(sigs)
    except Exception:
        return None


def _wf_cache_path():
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
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
    """扫描输出目录，找与该工作流节点类型指纹匹配的最近生成结果（按 mtime 降序）。

    指纹 = 排序后的节点类型集合（忽略参数/seed，同一工作流的历次生成都能匹配；
    结构不同的工作流不会误匹配）。days 限制扫描文件的时间范围以控制开销。
    已解析 PNG 的指纹按 路径+mtime 缓存（_tmp/wf_ref_cache.json），增量更新。
    """
    try:
        with open(wf_path, "r", encoding="utf-8") as f:
            ui = json.load(f)
    except Exception:
        return []
    fg = _types_from_ui(ui)
    if not fg:
        return []
    cutoff = time.time() - days * 86400
    # 锁只保护缓存文件读写（短临界区）；目录扫描与 PNG 元数据解析（磁盘 IO）在锁外执行
    with _wf_cache_lock:
        cache = _load_wf_cache()
    matched = []
    for base in _root_dirs("output"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not is_excluded_dir(d)]
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
                    pt = _png_prompt_text(full)
                    types = _types_from_prompt(pt) if pt else None
                    cache[full] = {"mtime": mt, "types": types}
                if types and types == fg:
                    sz = _png_size(full)
                    matched.append({
                        "name": fn,
                        "path": full,
                        "mtime": mt,
                        "width": sz[0] if sz else None,
                        "height": sz[1] if sz else None,
                    })
    with _wf_cache_lock:
        if len(cache) > 20000:
            cache = {k: v for k, v in cache.items() if v.get("mtime", 0) >= cutoff}
        _save_wf_cache(cache)
    matched.sort(key=lambda x: x["mtime"], reverse=True)
    return matched[:limit]
