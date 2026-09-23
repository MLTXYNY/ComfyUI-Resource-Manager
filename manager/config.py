# -*- coding: utf-8 -*-
"""
manager.config —— 配置持久化（config.json / notes.json）
=======================================================
职责：
* 读写 config.json（模型 / 输出 / 输入 / 工作流目录、排除项、系列过滤规则、ComfyUI 地址）
* ComfyUI 服务地址解析（config > 环境变量 > 默认 8188）
* 输出/输入目录排除判断、模型系列过滤规则
* 模型备注持久化（notes.json，path -> 备注文本）
"""

import json
import os
import sys
import threading

# --------------------------------------------------------------------------- #
# 数据根目录：开发=脚本目录；打包 exe=exe 所在目录（用户可写，避免写入临时解压目录）
# --------------------------------------------------------------------------- #
def data_root():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(data_root(), "config.json")

# 各类资产根目录对应的配置键
KIND_KEYS = {
    "model": "model_dirs",
    "output": "output_dirs",
    "input": "input_dirs",
    "workflow": "workflow_dirs",
}


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("model_dirs", [])
    cfg.setdefault("output_dirs", [])
    cfg.setdefault("input_dirs", [])
    cfg.setdefault("workflow_dirs", [])
    cfg.setdefault("output_excludes", [])
    cfg.setdefault("input_excludes", [])
    cfg.setdefault("series_filter", {"pattern": "", "exclude": []})
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# --------------------------------------------------------------------------- #
# ComfyUI 服务地址
# --------------------------------------------------------------------------- #
def get_comfy_host():
    cfg_host = str(load_config().get("comfyui_host") or "").strip().rstrip("/")
    env_host = str(os.environ.get("COMFY_HOST") or "").strip().rstrip("/")
    return cfg_host or env_host or "http://127.0.0.1:8188"


def set_comfy_host(host):
    cfg = load_config()
    cfg["comfyui_host"] = str(host or "").strip().rstrip("/")
    save_config(cfg)
    return get_comfy_host()


# --------------------------------------------------------------------------- #
# 输出 / 输入目录排除
# --------------------------------------------------------------------------- #
def excluded_dirs(kind=None):
    key = "input_excludes" if kind == "input" else "output_excludes"
    return [e.strip() for e in load_config().get(key, []) if str(e).strip()]


def is_excluded_dir(name, kind=None):
    ex = [e.lower() for e in excluded_dirs(kind)]
    if not ex:
        return False
    n = name.lower()
    if n in ex:
        return True
    # 排除项可能是完整路径，取其 basename 与目录名比对
    for e in ex:
        base = e.replace("/", "\\").rstrip("\\").split("\\")[-1]
        if base and n == base:
            return True
    return False


def series_filter_cfg():
    sf = load_config().get("series_filter", {}) or {}
    return {"pattern": sf.get("pattern") or "",
            "exclude": [s for s in sf.get("exclude", []) if s]}


# --------------------------------------------------------------------------- #
# 备注持久化（线程安全）
# --------------------------------------------------------------------------- #
_notes_lock = threading.Lock()


def _notes_path():
    return os.path.join(data_root(), "notes.json")


def load_notes():
    try:
        with open(_notes_path(), "r", encoding="utf-8") as f:
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
        with open(_notes_path(), "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)
        return notes.get(path)


# --------------------------------------------------------------------------- #
# 视频元数据本地旁路存储（线程安全）
# 视频（MP4/WebM 等）不内嵌 ComfyUI 提示词元数据，无法从文件本身读取。
# 故在工作流详情页点视频时，用「当前工作流」提取参数，存到本地旁路文件，
# 项目各端读视频时优先内嵌、否则读旁路；视频删除时同步清理孤儿条目。
# --------------------------------------------------------------------------- #
_vmeta_lock = threading.Lock()


def _vmeta_path():
    return os.path.join(data_root(), "_meta", "video_meta.json")


def load_video_meta():
    try:
        with open(_vmeta_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_video_meta(path, meta):
    with _vmeta_lock:
        m = load_video_meta()
        if meta:
            m[path] = meta
        else:
            m.pop(path, None)
        p = _vmeta_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        return m.get(path)


def remove_video_meta(path):
    return save_video_meta(path, None)
