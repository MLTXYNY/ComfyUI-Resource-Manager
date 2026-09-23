# -*- coding: utf-8 -*-
"""
manager.comfy —— ComfyUI 客户端 / 节点信息 / 缺失节点 / 健康 / 资源
====================================================================
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

from . import config

try:
    import psutil
except Exception:
    psutil = None

_OBJECT_INFO_CACHE = {"ts": 0, "data": None}
_OBJECT_INFO_TTL = 60


def comfy_get(path, timeout=8):
    with urllib.request.urlopen(config.get_comfy_host() + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def comfy_post(path, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        config.get_comfy_host() + path, data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_object_info(force=False):
    """从 ComfyUI /object_info 拉取节点可选项（模型/LoRA/采样器/调度器），带缓存。"""
    now = time.time()
    if not force and _OBJECT_INFO_CACHE["data"] and \
            now - _OBJECT_INFO_CACHE["ts"] < _OBJECT_INFO_TTL:
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


def node_groups(classes, missing_set):
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


# --------------------------------------------------------------------------- #
# 健康检查 / 系统资源
# --------------------------------------------------------------------------- #
def health():
    try:
        stats = comfy_get("/system_stats", timeout=3)
        return {
            "comfyui": True,
            "version": (stats.get("system") or {}).get("comfyui_version"),
            "host": config.get_comfy_host(),
        }
    except Exception as exc:
        return {"comfyui": False, "error": str(exc)}


def collect_resource():
    """采集系统资源快照：CPU / RAM 用 psutil，GPU / VRAM / 温度用 nvidia-smi。"""
    res = {"cpu": None, "ram": None, "gpus": []}
    try:
        if psutil is not None:
            res["cpu"] = round(psutil.cpu_percent(interval=0.2), 1)
            vm = psutil.virtual_memory()
            res["ram"] = {"percent": round(vm.percent, 1),
                          "used": vm.used, "total": vm.total}
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                res["gpus"].append({"util": float(parts[0]),
                                    "mem_used": int(float(parts[1])),
                                    "mem_total": int(float(parts[2])),
                                    "temp": float(parts[3])})
    except Exception:
        pass
    return res
