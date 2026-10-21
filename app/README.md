# 智绘CAD · 化工设计绘图（云端 MCP 驱动）

参赛版 MVP 提交截止：2026-10-29 17:00（AI 智能体赛道）

关键节点：10.17 XDATA 实测 · 10.20 功能冻结 · 10.21 云端切换 · 10.28 缓冲日 · 10.29 提交

## 5 分钟跑起来（Windows）

**产品里没有离线版：所有自然语言口令都必须接入大模型。** 在 `.env` 里填 API Key（或打开页面后点 ⚙ 设置填写），再启动；没接入时页面会明确提示，不会悄悄退回规则。直接输入的 AutoCAD 命令（如 `L 0,0 @100,0`）不需要大模型。`--mock` / `ZHIHUI_LLM=mock` 只给自动化测试和开发用。

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q                     # 全量测试（含通用 CAD 的内核、DXF、识别、执行器、演示口令、基准）
.venv/Scripts/python -m zhihui.api                    # 需要已配置 API Key，打开 http://127.0.0.1:8000
```

- **化工设备图**（2026-10-21 新增，P0：立式/卧式储罐方案图）：顶部选“化工设备图”，或直接说“画一台立式储罐的设备图，内径1600，筒体高2400，标准椭圆封头，壁厚8，材料Q345R，设计压力0.6MPa，设计温度80℃，人孔DN500，进料口DN80，出料口DN80，两个液位计口DN25，四个耳式支座”。系统新建一张 A1 设备图，按参数生成主视图、管口方位图、管口表、设计数据表、明细栏、技术要求和标题栏；接着可以说“把进料口转到90度”“筒体加长500”“上封头再加一个DN50的放空口”“导出 DXF 和 PDF”。缺参数时一次反问。**输出是方案图，标准件数据未核对，未做强度计算，不得直接用于制造**。见 [化工设备图_设计说明](01_项目文档/化工设备图_设计说明.md)、[功能对照表](01_项目文档/化工设备图_功能对照表.md)、[数据表清单](01_项目文档/化工设备图_数据表清单.md)、[变更总结](01_项目文档/化工设备图_变更总结.md)。
- **流程图**：首页 `http://127.0.0.1:8000/`，一句话生成/修改化工流程图（原有功能）。
- **可选依赖**：任意实体的三维布尔（旋转体/锥度体/扫掠体/布尔结果）需要 `pip install manifold3d`（或 `pip install -e ".[solid]"`）；不装也能跑，只是这类布尔会报 `BOOLEAN_UNSUPPORTED` 并提示安装，两个竖直拉伸体的布尔不受影响。
- **两个页面是同一个应用**：顶部“化工流程图 / 通用绘图”页签切换，共用 ⚙ 设置（API Key、模型）；在一边说了另一边的话会自动跳过去（流程图页说“画一个矩形…”→ 通用绘图页；通用绘图页说“绘制精馏工艺方案…”→ 流程图页）。
- **通用 2D/3D 绘图**（新）：`http://127.0.0.1:8000/`，可以点鼠标画、敲命令（`L 0,0 @100,0`）、打字或语音口述（“画一个矩形，左下角在零零，长120宽80”）。《改造提示词》第 10 节的 14 条演示口令在自动化测试里用规则 Planner 验证过（`tests/test_demo_phrases.py`），真实使用时由大模型规划。设计见 [通用CAD能力_设计说明](01_项目文档/通用CAD能力_设计说明.md)，逐项完成度和限制见 [功能对照表](01_项目文档/通用CAD能力_功能对照表.md)。

`--mock` 只给自动化测试和前后端联调用（用脚本模拟智能体，不是产品形态）。正式使用设置 `DEEPSEEK_API_KEY`（或 `DASHSCOPE_API_KEY` / `ZHIPUAI_API_KEY`），见 [接口约定](01_项目文档/接口约定/接口约定.md) 第 7 节。

常用命令：

| 做什么 | 命令 |
|---|---|
| 化工设备图基准（E 类，scripted / 真实模型） | `.venv/Scripts/python -m zhihui.bench.run --category E` / `--llm real --no-asr --category E` |
| 跑基准（检验工具链，不调大模型；含通用 CAD 的 B/D/A 类） | `.venv/Scripts/python -m zhihui.bench.run` |
| 通用 CAD 基准：离线规则 Planner 从口令（含语音误识别写法）生成计划 | `.venv/Scripts/python -m zhihui.bench.run --llm mock --category BDA` |
| 真实模型跑基准 / 模型选型 | `.venv/Scripts/python -m zhihui.bench.run --llm real --provider deepseek` |
| 单独启动 MCP Server（stdio） | `.venv/Scripts/python -m zhihui.mcp_server` |
| MCP Server（内网 HTTP） | `ZHIHUI_MCP_TOKEN=xxx .venv/Scripts/python -m zhihui.mcp_server --http --port 8765` |
| 重新导出接口 Schema（流程图工具 + 通用 CAD 函数） | `.venv/Scripts/python -m zhihui.contracts` |
| 通用 CAD 函数经 MCP 暴露（stdio） | `ZHIHUI_MCP_GENERAL=1 .venv/Scripts/python -m zhihui.mcp_server` |
| XDATA 实测 | 见 [06_测试与基准用例/xdata_probe/实测步骤.md](06_测试与基准用例/xdata_probe/实测步骤.md) |

## 代码在哪

Python 代码统一放在 `zhihui/` 包里（中文目录名不适合作为 Python 包名）；各角色目录放文档、配置、用例和前端静态页。

| 文件夹 | 内容 | 负责 | 对应代码 |
|---|---|---|---|
| 01_项目文档 | 研发方案书、接口约定、会议记录 | 全员 | `zhihui/contracts.py` 导出 Schema |
| 02_智能体与MCP | MCP Server、11 个工具、提示词、模型选型 | A | `zhihui/mcp_server/`、`zhihui/agent/`、`zhihui/tools/` |
| 03_CAD算法与图元库 | 12 种图元、auto_layout、正交连线、渲染、validate | B | `zhihui/cad/` |
| 04_后端与部署 | API、任务队列、Worker、Docker Compose | C | `zhihui/api/`、本目录 Dockerfile |
| 05_前端 | 输入页、进度、预览下载、工具日志与差异展示 | D | `05_前端/web/` |
| 06_测试与基准用例 | 基准用例（G/M/F/C/S/W + 通用 CAD 的 B/D/A）、评测脚本、每晚结果、XDATA 实测 | E | `zhihui/bench/`、`tests/` |
| 07_比赛提交材料 | 说明书、PPT、演示视频（注意去身份化） | E | — |

## 架构一句话

浏览器 → FastAPI（任务队列 + 状态机）→ Orchestrator（大模型一主一备）→ **MCP** → 11 个语义工具 → CAD 引擎（ezdxf、确定性布局、正交布线、校验、差异）→ DXF / PNG / PDF。
大模型只决定「有哪些设备、怎么连」；坐标和几何由程序计算。模型说"完成"后，由程序强制校验；校验不通过就把错误发回给模型修复，最多 2 轮。

通用 CAD 模式（`Drawing.mode == "general"`）的结构：口语规范化 → 命令快速通道（不调模型）→ Planner 出 JSON 计划 → Executor（程序）逐步执行，识别图元交给 Recognizer（规则优先）、改图交给 46 个预置函数（`zhihui/tools/general/`）+ 6 个感知函数 + 批量/撤销；几何全部在确定性内核 `zhihui/cad/geom/` 里。模型不算坐标、不猜编号，缺关键数值就反问。
