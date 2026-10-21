# 02_智能体与MCP（负责：A）

MCP Server（11 个工具）、Orchestrator、提示词、模型选型对比结果。

## 代码
| 文件 | 内容 |
|---|---|
| `zhihui/mcp_server/server.py` | MCP Server：11 个工具的 Schema 与注册；stdio / Streamable HTTP（令牌校验） |
| `zhihui/tools/core.py` | 工具实现（事务性、错误码 + hint、影响集声明） |
| `zhihui/tools/store.py` | 图纸版本存储（每次成功调用落一个版本，原件不覆写） |
| `zhihui/agent/orchestrator.py` | 闭环：规划 → 执行 → 程序强制校验 → 修复 ≤2 轮 → 导出；40 次 / 180 s 上限；白名单 |
| `zhihui/agent/prompts.py` | 系统提示词（含反问规则、防提示注入） |
| `zhihui/agent/llm.py` | DeepSeek / 通义 / GLM（OpenAI 兼容接口）一主一备；离线脚本模型 |

## 现状（10.13）
- [x] 11 个工具全部可用，stdio 与 HTTP 两种连接都已实测通过
- [x] 批量 place / connect：一张 7 台设备的图只需 4 轮工具调用
- [x] 闭环与修复轮次、ask_user 反问与续接、白名单
- [ ] **模型选型（已逾期，最优先）**：申请三家 Key，执行 `python -m zhihui.bench.run --llm real --provider deepseek`，qwen、glm 各跑一遍，结果记录到下表
- [ ] 用真实模型调提示词：重点看端口名写错的比例、该反问的时候有没有反问
- [ ] 改图提示词（10.17–10.23）：替换图元、增删设备的完整示例

## 通用 CAD 能力（2026-10-20）

在流程图之外新增“通用 2D/3D 图”模式（`Drawing.mode == "general"`）：三个 Agent（Planner / Recognizer / Executor）+ 46 个预置函数 + 6 个感知函数 + 批量/撤销。设计见 `01_项目文档/通用CAD能力_设计说明.md`，逐项完成度见 `通用CAD能力_功能对照表.md`。

| 文件 | 内容 |
|---|---|
| `zhihui/tools/general/` | 函数注册表（`registry.py`）、各类函数（环境/绘图/编辑/标注/布局/块/约束/三维/专业）、变量引用（`refs.py`）、批量与撤销（`system_tools.py`） |
| `zhihui/agent/general/engine.py` | `GeneralAgent`：规范化 → 命令通道 → Planner → 护栏 → Executor → 校验 → 出图；反问/确认/解锁续接 |
| `zhihui/agent/general/{plan,planner,mock_planner,guards,router,prompts}.py` | 计划协议、LLM Planner 与离线规则 Planner、护栏、路由、提示词 |
| `zhihui/agent/general/{executor,recognizer,recognizer_rules,backend}.py` | Executor、识别 Agent（规则优先 + LLM 兜底）、Local/MCP 两种后端 |
| `zhihui/agent/{speech_normalize,command_line}.py` | 口语规范化（词表 `speech_lexicon.json`）、AutoCAD 命令/中文模板快速通道 |
| `zhihui/mcp_server/server.py` | `general_mcp`（`ZHIHUI_MCP_GENERAL=1` 时暴露通用函数；原 `extended_mcp` 不变） |

**状态**：45 项需求 完成 34 / 部分 6 / 等价实现 5 / 未做 0（另补 `erase_entities`）；14 条演示口令 `--mock` 离线跑通；基准 B/D/A 50 条 scripted 50/50、mock 36 条可跑全过（14 条规则没覆盖，标 SKIP）。真实 DeepSeek 下 B/D/A 50 条（只跑口令写法）：修复前 32/50，修复后三次整轮 46、48、45/50（见《通用CAD能力_变更总结》第 11 节）。

## 模型选型记录

### 2026-10-14 智谱 GLM（同一账号，全部关闭/尽量关闭深度思考）
前 3 条生成用例（G001 7 台、G002 10 台带回流、G005 7 台带分支）：

| 型号 | 通过 | 每条耗时 | 备注 |
|---|---|---|---|
| **glm-4.5-air** | 3/3 | **14–15 s** | 最快，当前默认 |
| glm-4.6 | 3/3 | 33–45 s | |
| glm-5.1 | 3/3 | 35–49 s | |
| glm-5.3 | 1/2 | 52–122 s | 强制深度思考，关不掉；10 台设备的用例超时 |
| glm-5-turbo | — | — | 并行测试触发账户限流（429），待重测 |

glm-4.5-air 全量 18 条：**G 6/6、M 6/6、F 6/6，P50 6.1 s，约 1.6 万 token/条**（加护栏后）。
加护栏之前 F 类是 4/6：F002 不问就挑了一台泵移动，F003 编造了"直径 2.5 m、容积 50 m³"。
现在由 `zhihui/agent/guards.py` 程序强制拦截，拦下后模型会改为反问。

**注意**：并行跑基准会触发 GLM 账户限流，评测请按顺序跑。

### 待测
| 模型 | G | M | F | P50 耗时 | 单条 Token | 结论 |
|---|---|---|---|---|---|---|
| DeepSeek |  |  |  |  |  |  |
| 通义千问 |  |  |  |  |  |  |

### 化工设备图 P0（2026-10-21）

- 新增意图类别 `chem_equipment` 和 5 个函数（`vessel_define` 等）；路由三分（pfd / general / equipment）；设备补丁护栏 `check_vessel_patch`；9 个错误码由引擎直接转反问。
- 真实 DeepSeek（deepseek-flash，temperature 0.1）E 类 12 条：两轮口令写法都是 12/12，口令 + 语音误识别写法一轮 12/12。原始数字见 `01_项目文档/化工设备图_变更总结.md` 第 3 节。
