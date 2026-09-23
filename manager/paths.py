# -*- coding: utf-8 -*-
"""
manager.paths —— 路径根目录、路径安全、ComfyUI 目录自动检测
===========================================================
职责：
* 各类资产（模型/输出/输入/工作流）已配置根目录解析
* safe_resolve：把用户传入路径校验到已配置根目录内（越界抛 ValueError -> 403）
* 从运行中的 ComfyUI 进程命令行推导安装 / 输出 / 工作流 / 额外模型目录
"""

import os
import re
import subprocess

from . import config

# 文件扩展名集合
MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".sft",
              ".gguf", ".onnx", ".engine"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}
PREVIEW_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
WORKFLOW_EXTS = {".json"}


def root_dirs(kind):
    """返回某类资产的已配置根目录绝对路径列表。kind: model/output/input/workflow"""
    key = config.KIND_KEYS.get(kind)
    lst = (config.load_config().get(key) or []) if key else []
    return [os.path.abspath(os.path.normpath(d)) for d in lst if isinstance(d, str) and d.strip()]


def _root_of(path, kind):
    for r in root_dirs(kind):
        try:
            if os.path.commonpath([r, path]) == r:
                return r
        except ValueError:
            continue
    return None


def safe_resolve(path, require_exists=True):
    """确保 path 位于任一已配置根目录内；返回规范化绝对路径，否则抛 ValueError。"""
    if not path:
        raise ValueError("路径为空")
    p = os.path.abspath(os.path.normpath(path))
    all_roots = (root_dirs("model") + root_dirs("output") +
                 root_dirs("input") + root_dirs("workflow"))
    for root in all_roots:
        try:
            if os.path.commonpath([root, p]) == root:
                if require_exists and not os.path.exists(p):
                    raise ValueError("文件不存在: %s" % p)
                return p
        except ValueError:
            continue
    raise ValueError("路径不在允许的目录内: %s" % p)


# --------------------------------------------------------------------------- #
# ComfyUI 目录自动检测（只扫描，不接管）
# --------------------------------------------------------------------------- #
def detect_comfy_setup():
    found = {
        "root": None, "output_dirs": [], "input_dirs": [],
        "model_dirs": [], "workflow_dirs": [],
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
    m = re.search(r"--input-directory\s+(?:\"([^\"]+)\"|(\S+))", cmd)
    if m:
        found["input_dirs"].append((m.group(1) or m.group(2)).strip().rstrip("\\/"))
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

    default_in = os.path.join(found["root"], "input")
    if os.path.isdir(default_in) and default_in not in found["input_dirs"]:
        found["input_dirs"].append(default_in)

    default_wf = os.path.join(found["root"], "user", "default", "workflows")
    if os.path.isdir(default_wf):
        found["workflow_dirs"].append(default_wf)

    for key in ("model_dirs", "output_dirs", "input_dirs", "workflow_dirs"):
        seen = set()
        found[key] = [d for d in found[key]
                      if d not in seen and not seen.add(d) and os.path.isdir(d)]
    return found


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def natural_key(name):
    """Windows 资源管理器风格自然排序：数字按数值、字母按字典序（不区分大小写）。"""
    return [(0, int(t)) if t.isdigit() else (1, t.lower())
            for t in re.split(r"(\d+)", name)]


def fmt_rel(full, base):
    return os.path.relpath(full, base).replace("\\", "/")
