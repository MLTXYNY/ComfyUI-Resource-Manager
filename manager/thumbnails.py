# -*- coding: utf-8 -*-
"""
manager.thumbnails —— 缩略图生成（图片 / 视频抽帧），并发安全 + 缓存
=====================================================================
"""

import hashlib
import os
import subprocess
import threading

from . import config
from .paths import VIDEO_EXTS
from .parsing import _ffmpeg_path

_thumb_lock = threading.Lock()


def _thumb_cache_dir():
    d = os.path.join(config.data_root(), "_tmp", "thumbs")
    os.makedirs(d, exist_ok=True)
    return d


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
