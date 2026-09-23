# -*- coding: utf-8 -*-
# Copyright 2026 YNY MLTX
# Licensed under the Apache License, Version 2.0
"""
ComfyUI-Resource-Manager —— 本地图形化 ComfyUI 素材管理工具（后端入口）

功能
----
* 主页：聚合输出 / 输入资产，瀑布式总览，按钮筛选，点开进入详情
* 输出 / 输入管理：文件夹树分类浏览图片/视频，三分式查看 PNG 内嵌参数与正反提示词，
  比例筛选、实时自动刷新、双击放大预览、删除/新建/打开文件夹
* 工作流管理：工作流解析（节点数/模型/LoRA/采样参数/提示词）、缺失节点检测、
  笔记节点读取编辑、最近生成结果关联、复制/导出 JSON
* 模型管理：多目录扫描，类型 + 系列双层分类，备注、移动、删除，系列显示正则自定义

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

import os
import sys
import threading
import time
import webbrowser

# frozen + --noconsole 下 stdout/stderr 为 None，print 会崩；重定向到空设备
if getattr(sys, "frozen", False):
    try:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
    except Exception:
        pass

from manager import create_app
from manager import config, paths

# 固定端口 8000（不做端口避让；被占则强制关闭占用进程后重新监听）
COMFY_HOST = os.environ.get("COMFY_HOST", "http://127.0.0.1:8188").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))


def _release_port(port):
    """8000 被占用则强制结束占用进程并等待端口释放，之后本程序重新监听。"""
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.status == "LISTEN":
                try:
                    p = psutil.Process(c.pid)
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except Exception:
                        p.kill()
                except Exception:
                    pass
        time.sleep(1.0)
    except Exception:
        pass

app = create_app()


def _ensure_config():
    """首次运行自动检测 ComfyUI 目录写入配置；旧配置补全缺失类别（不覆盖用户设置）。"""
    if not os.path.exists(config.CONFIG_PATH):
        detected = paths.detect_comfy_setup()
        if detected.get("model_dirs") or detected.get("output_dirs") or detected.get("input_dirs"):
            config.save_config({
                "model_dirs": detected["model_dirs"],
                "output_dirs": detected["output_dirs"],
                "input_dirs": detected["input_dirs"],
                "workflow_dirs": detected["workflow_dirs"],
            })
            print("已自动检测目录 -> 模型: %s | 输出: %s | 输入: %s | 工作流: %s"
                  % (len(detected["model_dirs"]), len(detected["output_dirs"]),
                     len(detected["input_dirs"]), len(detected["workflow_dirs"])))
        else:
            print("提示: 未自动检测到目录，%s" % (detected.get("hint") or "可在界面手动添加"))
    else:
        cfg = config.load_config()
        changed = False
        if not cfg.get("workflow_dirs"):
            detected = paths.detect_comfy_setup()
            if detected.get("workflow_dirs"):
                cfg["workflow_dirs"] = detected["workflow_dirs"]
                changed = True
                print("已补全工作流目录: %s" % ", ".join(cfg["workflow_dirs"]))
        if not cfg.get("input_dirs"):
            detected = paths.detect_comfy_setup()
            if detected.get("input_dirs"):
                cfg["input_dirs"] = detected["input_dirs"]
                changed = True
                print("已补全输入目录: %s" % ", ".join(cfg["input_dirs"]))
        if changed:
            config.save_config(cfg)


def _open_browser():
    """服务起来后自动打开浏览器（打包 exe 免手动输入地址）。"""
    try:
        webbrowser.open("http://127.0.0.1:%d" % PORT)
    except Exception:
        pass


if __name__ == "__main__":
    _ensure_config()
    # 固定端口：8000 被占则先强制关闭占用进程，再重新监听（不避让端口）
    _release_port(PORT)
    print("ComfyUI 素材管理器已启动: http://127.0.0.1:%d  (ComfyUI: %s)" % (PORT, config.get_comfy_host()))
    threading.Timer(1.2, _open_browser).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
