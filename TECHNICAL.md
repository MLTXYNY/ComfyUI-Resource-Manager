# ComfyUI 素材管理器 · 技术文档

## 架构

- **后端**：Flask（`app.py` + `manager/` 包）。`manager/` 按职责分层，单一入口 `create_app()`。
- **前端**：单文件 `static/index.html`（无构建），原生 JS + fetch，深色主题三分式 / 瀑布式布局。
- **数据**：`config.json`（目录 / 排除 / 系列过滤 / ComfyUI 地址）、`notes.json`（模型备注）、`_tmp/thumbs`（缩略图缓存）、`_tmp/wf_ref_cache.json`（工作流指纹缓存）。

## REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 首页（`Cache-Control: no-store` 防缓存） |
| GET | `/api/version` | 前端版本（index.html mtime） |
| GET/POST | `/api/comfyui_host` | 读写 ComfyUI 地址 |
| GET | `/api/health` | ComfyUI 连接状态 / 版本 |
| GET | `/api/object_info` | ComfyUI 节点可选项（带 60s 缓存） |
| GET | `/api/resource` | 系统资源（CPU / RAM / GPU） |
| POST | `/api/read_metadata` | 拖入 PNG 读取生成参数 |
| GET/POST | `/api/storage/config` | 读写目录配置 |
| GET | `/api/storage/detect` | 从运行中的 ComfyUI 进程自动检测目录 |
| GET | `/api/models` | 模型扫描（q / type / series） |
| GET | `/api/outputs` `/api/inputs` | 媒体扫描（q / sort / kind / ratio / folder） |
| GET | `/api/output_folders` `/api/input_folders` | 文件夹树 |
| GET | `/api/output_meta` `/api/input_meta` | PNG 参数元数据 |
| GET | `/api/output_video_info` `/api/input_video_info` | 视频信息 |
| GET | `/api/home` | 主页瀑布聚合（scope / kind / limit） |
| GET | `/api/workflows` `/api/workflow_folders` | 工作流列表 / 文件夹树 |
| GET | `/api/workflow` | 工作流详情（解析 + 缺失节点 + 节点分组） |
| POST | `/api/workflow/note` | 保存 Note / MarkdownNote 节点文本 |
| GET | `/api/wf_recent` | 按节点指纹匹配最近生成结果 |
| POST | `/api/storage/note` `/mkdir` `/rmdir` `/delete` `/move` `/open` | 文件操作 |
| GET | `/media` | 媒体流（`?thumb=1` 缩略图） |

## 路径安全

所有接收路径的参数（`/api/storage/*`、`/api/*_meta`、`/media`、`/api/workflow*`）统一经 `manager.paths.safe_resolve`：
把路径规范化后与已配置的 model/output/input/workflow 根目录做 `commonpath` 比对，越界抛 `ValueError` → HTTP 403。删除仅限单文件与空文件夹。

## 工作流解析

`manager.parsing.parse_workflow_file` 兼容两种格式：

- **UI 格式**（ComfyUI 编辑器导出）：`{ nodes: [...], links: [...] }`，从 `widgets_values` 提取模型 / LoRA / 采样 / 尺寸 / 文本。
- **API 格式**（`/prompt` 提交）：`{ id: { class_type, inputs } }`，从 `inputs` 提取。

缺失节点判定：将工作流节点类型与 `/object_info` 返回的 `all_classes` 比对（排除装饰类节点），未命中的归入自定义节点组并标红。

## 工作流 → 最近结果指纹匹配

`manager.parsing.find_recent_output_for_workflow`：提取工作流各节点的**类型签名**（模型加载类节点附加模型名），与输出 PNG 内嵌 prompt 解析出的签名做全等比较，返回最近匹配的 PNG（含缓存，最多 2 万条 LRU）。

## 媒体扫描

`manager.scanning.scan_media(kind, ...)` 服务输出/输入两类；`folder` 参数给定时只列该文件夹直接文件（非递归），否则递归扫描全部根目录。比例筛选按 8 档预设 ±2% 容差匹配（`other` 指不落入任何预设档）。

## 前端视图

- `主页`：`/api/home` 瀑布网格，**JS 最短列平衡**（按每张图真实高度放入当前最矮的 `.hcol` 列，列底对齐、消除空隙；列数随容器宽度自适应，窗口缩放自动重排；`_thDim` 缓存缩略图高度避免重复计算），支持来源/类型/比例筛选；**网格单元格直接用原图（`mediaUrl(path,false)`，不走缩略图）保证高清**；点击卡片经 `openHomeModal()` 弹出详情（`#homeModal`），在主页内展示参数/提示词/预览，支持打开文件夹/删除，不再跳转；单击预览图进全屏 lightbox。
- `输出/输入管理`：三分式（文件夹树 / 文件网格 / 详情），**文件网格与主页共用同一套最短列瀑布流**（`buildCellItem` 构建单元格、`layoutMasonry` 按最矮列分配，`cell.__it` 记录选中态、`selectFile/selectInputFile` 仅切换 `.on` 类避免整格重渲染），网格图片同样加载原图高清显示；详情左参数、左提示词、右预览；双击进 lightbox。
- `工作流管理`：三分式，右栏详情含参数 / 节点分组 / 笔记编辑 / 最近结果 / 复制 / 导出。
- `模型管理`：单栏表格，类型 + 系列双下拉，行内备注 / 打开 / 移动 / 删除。

自动刷新：主页 / 输出 / 输入各有一个 4s 轮询，仅签名变化时重渲染，避免闪烁与打断选中。
