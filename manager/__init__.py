# -*- coding: utf-8 -*-
"""
manager —— ComfyUI 素材管理器 后端包
=====================================
模块：
* config      配置持久化（config.json / notes.json）、ComfyUI 地址、排除与系列过滤
* paths       路径根目录、路径安全（safe_resolve）、ComfyUI 目录自动检测
* scanning    模型 / 媒体(输出·输入) / 工作流 扫描与文件夹树、主页聚合
* parsing     工作流解析、PNG 元数据、视频信息、工作流指纹匹配
* thumbnails  缩略图生成（图片 / 视频抽帧）
* comfy       ComfyUI 客户端、节点信息、缺失节点、健康、系统资源
* api         Flask 蓝图：全部 REST 路由
"""

import os

from flask import Flask

from . import api


def create_app(static_dir=None):
    """Flask 应用工厂。static_dir 缺省时使用包同级 static/。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static_dir = static_dir or os.path.join(base, "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")
    api.register_app_routes(app, static_dir)
    return app
