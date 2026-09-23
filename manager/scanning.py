# -*- coding: utf-8 -*-
"""
manager.scanning —— 模型 / 媒体(输出·输入) / 工作流 扫描与文件夹树
===================================================================
职责：
* scan_models：多目录扫描，类型(顶层子目录) + 系列(二级目录)双层分类，备注、预览、系列过滤
* scan_media：输出 / 输入 目录的图片/视频扫描（支持搜索/排序/类型/比例/文件夹限定）
* list_media_folders：输出 / 输入 目录可折叠文件夹树（含图片/视频计数）
* scan_workflows / list_workflow_folders：工作流文件扫描与文件夹树
"""

import os

from . import config, paths
from .config import is_excluded_dir, series_filter_cfg, load_notes
from .paths import (MODEL_EXTS, IMAGE_EXTS, VIDEO_EXTS, WORKFLOW_EXTS,
                    root_dirs, fmt_rel, natural_key, _root_of)


# --------------------------------------------------------------------------- #
# 模型扫描
# --------------------------------------------------------------------------- #
def find_preview(model_path):
    d = os.path.dirname(model_path)
    stem = os.path.splitext(os.path.basename(model_path))[0]
    candidates = []
    for ext in paths.PREVIEW_EXTS:
        candidates.append(os.path.join(d, stem + ext))
        candidates.append(os.path.join(d, stem + ".preview" + ext))
    candidates.append(os.path.join(d, "preview", stem + ".png"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


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


def scan_models(q="", type_f="", series_f="", limit=3000):
    """扫描所有模型根目录，按顶层子文件夹分类 + 二级目录系列。
    返回 {types, series, type_dirs, items, truncated}。"""
    types = set()
    raw_series = set()
    type_dirs = []
    items = []
    sf = series_filter_cfg()
    pattern = sf["pattern"]
    exclude = sf["exclude"]
    notes = load_notes()
    for base in root_dirs("model"):
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
                        rel = fmt_rel(full, base)
                        if q and q.lower() not in rel.lower():
                            continue
                        parts = rel.split("/")
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
                            "name": fn, "rel": rel, "type": typ or "root",
                            "series": s, "root": base, "path": full,
                            "size": os.path.getsize(full),
                            "mtime": int(os.path.getmtime(full)),
                            "preview_path": find_preview(full) or None,
                            "note": notes.get(full, ""),
                        })
                        if len(items) >= limit:
                            return {"types": sorted(types),
                                    "series": _filter_series(raw_series, pattern, exclude),
                                    "type_dirs": type_dirs,
                                    "items": _apply_series_exclude(items, exclude),
                                    "truncated": True}
            except OSError:
                continue
    return {"types": sorted(types),
            "series": _filter_series(raw_series, pattern, exclude),
            "type_dirs": type_dirs,
            "items": _apply_series_exclude(items, exclude),
            "truncated": False}


# --------------------------------------------------------------------------- #
# 媒体（输出 / 输入）扫描
# --------------------------------------------------------------------------- #
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
    t = RATIO_PRESETS.get(preset)
    if t is None:
        return False
    return abs(r - t) / t < 0.02


def _ratio_filter_ok(r, ratios):
    matched = any(_ratio_match(r, p) for p in ratios if p != "other")
    if "other" in ratios:
        return matched or not any(_ratio_match(r, p) for p in RATIO_PRESETS)
    return matched


def _media_item(full, base, kind, idx):
    ext = os.path.splitext(full)[1].lower()
    if ext in IMAGE_EXTS:
        k = "image"
    elif ext in VIDEO_EXTS:
        k = "video"
    else:
        return None
    return {
        "id": "%s%d" % (kind[0], idx),
        "name": os.path.basename(full), "rel": fmt_rel(full, base),
        "root": base, "path": full, "size": os.path.getsize(full),
        "mtime": int(os.path.getmtime(full)), "kind": k,
    }


def scan_media(kind, q="", sort="new", kind_f="all", limit=2000, folder=None, ratio=""):
    """扫描输出(kind="output") 或输入(kind="input") 目录。
    folder 给定时只列该文件夹内的直接文件（非递归）；否则递归扫描全部根目录。"""
    items = []
    if folder:
        base = os.path.abspath(folder)
        root = _root_of(base, kind)
        if root is None:
            raise ValueError("目录不在允许的%s目录内: %s" % (kind, base))
        if os.path.isdir(base):
            try:
                names = sorted(os.listdir(base))
            except OSError:
                names = []
            for fn in names:
                full = os.path.join(base, fn)
                if not os.path.isfile(full):
                    continue
                it = _media_item(full, root, kind, len(items))
                if it is None:
                    continue
                if kind_f != "all" and kind_f != it["kind"]:
                    continue
                if ratio:
                    if it["kind"] != "image":
                        continue
                    r = _img_ratio(full)
                    if r is None:
                        continue
                    _ratios = [x.strip() for x in ratio.split(",") if x.strip()]
                    if not _ratio_filter_ok(r, _ratios):
                        continue
                if q and q.lower() not in it["rel"].lower():
                    continue
                items.append(it)
                if len(items) >= limit:
                    break
        items.sort(key=lambda x: x["mtime"], reverse=(sort == "new"))
        return items

    for base in root_dirs(kind):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and not is_excluded_dir(d, kind)]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                it = _media_item(full, base, kind, len(items))
                if it is None:
                    continue
                if kind_f != "all" and kind_f != it["kind"]:
                    continue
                if ratio:
                    if it["kind"] != "image":
                        continue
                    r = _img_ratio(full)
                    if r is None:
                        continue
                    _ratios = [x.strip() for x in ratio.split(",") if x.strip()]
                    if not _ratio_filter_ok(r, _ratios):
                        continue
                if q and q.lower() not in it["rel"].lower():
                    continue
                items.append(it)
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
    items.sort(key=lambda x: x["mtime"], reverse=(sort == "new"))
    return items


def scan_outputs(q="", sort="new", kind="all", limit=2000, folder=None, ratio=""):
    return scan_media("output", q, sort, kind, limit, folder, ratio)


def scan_inputs(q="", sort="new", kind="all", limit=2000, folder=None, ratio=""):
    return scan_media("input", q, sort, kind, limit, folder, ratio)


def _walk_folders(dirpath, depth, base, out, limit, kind):
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
            if not e.startswith(".") and not is_excluded_dir(e, kind):
                subs.append(full)
        elif os.path.isfile(full):
            ext = os.path.splitext(e)[1].lower()
            if ext in IMAGE_EXTS:
                im += 1
            elif ext in VIDEO_EXTS:
                vi += 1
    out.append({
        "root": base, "path": dirpath, "rel": fmt_rel(dirpath, base),
        "name": os.path.basename(dirpath) or dirpath, "depth": depth,
        "images": im, "videos": vi,
    })
    for s in subs:
        _walk_folders(s, depth + 1, base, out, limit, kind)


def list_media_folders(kind, limit=4000):
    nodes = []
    for base in root_dirs(kind):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        nodes.append({
            "root": base, "path": base, "rel": "",
            "name": os.path.basename(base) or base, "depth": 0,
            "images": 0, "videos": 0,
        })
        _walk_folders(base, 1, base, nodes, limit, kind)
    return nodes


def list_output_folders(limit=4000):
    return list_media_folders("output", limit)


def list_input_folders(limit=4000):
    return list_media_folders("input", limit)


# --------------------------------------------------------------------------- #
# 主页聚合：最近媒体（输出 + 输入 合并，供瀑布式总览）
# --------------------------------------------------------------------------- #
def scan_home(limit=300, ratio="", kind="all", scope="all"):
    """聚合输出 + 输入目录的最近媒体，按修改时间降序，带来源标签。
    scope: all / output / input；kind: all / image / video。"""
    merged = []
    for k in ("output", "input"):
        if scope not in ("all", k):
            continue
        items = scan_media(k, q="", sort="new", kind_f="all",
                           limit=limit, folder=None, ratio=ratio)
        for it in items:
            it["asset"] = k  # 来源：输出 / 输入
        merged.extend(items)
    merged.sort(key=lambda x: x["mtime"], reverse=True)
    if kind != "all":
        merged = [it for it in merged if it["kind"] == kind]
    return merged[:limit]


# --------------------------------------------------------------------------- #
# 工作流扫描
# --------------------------------------------------------------------------- #
def _sort_workflow_items(items, sort):
    if sort == "name":
        items.sort(key=lambda x: natural_key(x["name"]))
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
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if os.path.splitext(fn)[1].lower() not in WORKFLOW_EXTS:
                        continue
                    full = os.path.join(dirpath, fn)
                    rel = fmt_rel(full, root)
                    if q and q.lower() not in rel.lower():
                        continue
                    items.append({
                        "id": "w%d" % len(items), "name": fn, "rel": rel,
                        "root": root, "path": full, "size": os.path.getsize(full),
                        "mtime": int(os.path.getmtime(full)),
                    })
                    if len(items) >= limit:
                        _sort_workflow_items(items, sort)
                        return items
        _sort_workflow_items(items, sort)
        return items

    for base in root_dirs("workflow"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() not in WORKFLOW_EXTS:
                    continue
                full = os.path.join(dirpath, fn)
                rel = fmt_rel(full, base)
                if q and q.lower() not in rel.lower():
                    continue
                items.append({
                    "id": "w%d" % len(items), "name": fn, "rel": rel,
                    "root": base, "path": full, "size": os.path.getsize(full),
                    "mtime": int(os.path.getmtime(full)),
                })
                if len(items) >= limit:
                    _sort_workflow_items(items, sort)
                    return items
    _sort_workflow_items(items, sort)
    return items


def _walk_workflow_folders(dirpath, depth, base, out, limit):
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
        "root": base, "path": dirpath, "rel": fmt_rel(dirpath, base),
        "name": os.path.basename(dirpath) or dirpath, "depth": depth, "workflows": 0,
    })
    for s in subs:
        cnt += _walk_workflow_folders(s, depth + 1, base, out, limit)
    out[idx]["workflows"] = cnt
    return cnt


def list_workflow_folders(limit=2000):
    nodes = []
    for base in root_dirs("workflow"):
        base = os.path.abspath(base)
        if not os.path.isdir(base):
            continue
        _walk_workflow_folders(base, 0, base, nodes, limit)
    return nodes
