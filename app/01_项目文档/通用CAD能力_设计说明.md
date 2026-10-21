# 通用 CAD 能力 · 设计说明

> 依据《通用CAD能力扩展_改造提示词_v2》。本文写落地的设计和取舍；与提示词不一致处注明原因。代码基线是「智绘CAD_改进版」（含检索图元、需求核验、新前端），不是原始仓库。逐项完成度见《通用CAD能力_功能对照表.md》。

## 1. 一句话

在现有“化工流程图”系统旁边加了第二种图：**通用 2D/3D 图（`mode="general"`）**。用户口述或打字 → 口语规范化 → 命令快速通道（命中则不调模型）或 Planner 出 JSON 计划 → Executor（程序）逐步执行：识别图元交给 Recognizer，改图交给 46 个预置函数 → 校验 → 导出 DXF/PNG/SVG/PDF。**模型只出计划，所有几何由确定性代码算；模型不算坐标、不猜编号。**

## 2. 总体结构

```
浏览器 cad.html（SVG 交互视图：缩放平移/捕捉/夹点/图层与特性面板/命令行/语音/计划进度）
   │  /api/drawings/{id}/ops|scene|command|chat|render|revisions|export  /api/tasks  /api/tools  /api/asr
FastAPI（zhihui/api/general.py + app.py：任务队列、run_task 的 general 分支）
   │
GeneralAgent（zhihui/agent/general/engine.py）                         流程图仍走原 Orchestrator/Coordinator
   ├─ speech_normalize（口语规范化）→ command_line（AutoCAD 命令/中文模板，不调模型）
   ├─ router（流程图 or 通用 + 意图类别）→ Planner（LLM 或离线规则）→ guards（数值出处/写死坐标/破坏性确认）
   └─ Executor：拓扑序 → 解析引用 → pydantic 校验 → 调用函数；识别步交给 Recognizer（规则优先，LLM 兜底只写感知函数参数）
                 │  Backend：LocalBackend（进程内，默认）｜McpBackend（经 MCP，子进程里没有任何 *_API_KEY）
tools/general/registry.py：46+6+2 个函数（run_tool：校验→引用→事务→会话回写→返回）
   │
cad/geom/*：确定性几何内核（纯函数，有单测）      cad/general_dxf.py：DXF 读写      cad/render_general.py：渲染/出图
   │
ToolService._mutate（事务，一次成功修改一个版本）→ DrawingStore（版本文件，原件永不覆写）
```

**密钥隔离不变**：所有会调模型的逻辑都在 `zhihui/agent/`；MCP/ToolService/几何内核只有确定性代码。通用函数登记在独立的 `general_mcp`（`ZHIHUI_MCP_GENERAL=1` 选用），不污染原来的 `extended_mcp` 工具目录，所以流程图的工具目录和测试保持不变。

## 3. 数据模型（`zhihui/ir/general.py`，`models.py` 只增不改）

- `Layer / GeoEntity / DraftSettings / SessionState / ViewState` 为新模型。`Drawing` 新增 `mode`（默认 `pfd`，旧图 JSON 无损加载）、`layers`、`entities`、`blocks`、`layouts`、`xrefs`、`constraints`、`dynamic_blocks`、`draft`、`view`、`session`、`plot_setups` 等字段。字段取默认值时不写进 DXF 内嵌 IR（沿用 `_NEW_DRAWING_FIELDS`），**旧流程图导出文件逐字节不变**。
- `GeoEntity(id, kind, layer, color, linetype, …, data, tags, handle, space)`：`id` 是**全图统一的自增编号**（`PL1、C2、L3…`，前缀表示类型），创建后不变，图元被修剪/改参数时沿用；`space` 是 `Model` 或布局名。
- `data` 的几何参数格式固定在 `geom/entities.py` 顶部注释里；曲线统一能分解成「线段 / 圆弧」原语（椭圆、样条取折线近似），求交、修剪、倒角、边界追踪都建立在原语之上。
- 三维实体是 `solid3d`：直拉伸体同时存 z 向切片（`slabs`，供精确的二维布尔）和三角网格；旋转体/锥度体/扫掠体/布尔结果只有网格（`solid_kind` 分别为 `revolve/tapered/sweep/boolean`）。所有网格都是封闭流形、法线朝外（`orient_mesh` 按嵌套深度保证：实体壳朝外、内腔壳朝内）。
- 动态块 = `dynamic_blocks` 里的参数表 + 每组参数值生成的静态块变体（块名 `名字@哈希`）。外部参照 = 缓存下来的块定义 + `xrefs` 记录。约束 = `constraints` 表（几何/标注，标注可带命名参数和表达式）。

## 4. 函数注册表（`zhihui/tools/general/`）

- `registry.py`：`@tool(name, category, Args模型, readonly, needs_drawing, quiet, no_history, quiet_if)`。`Args` 一律 `extra="forbid"`，参数写错得到“哪个字段错了”的结构化报错。
- **成功返回** `{"ok": true, created, modified, deleted, summary, data, warnings, defaults_used}`；**失败** `ToolError(code, message, hint)`，`hint` 是写给模型的一句修复提示；任何未预期异常都兜成 `INTERNAL`（“图纸没有被修改”）。
- **事务与版本**：`run_tool` 在 `ToolService._mutate` 里执行（深拷贝上改，全成功才落盘），**一次成功修改 = 一个版本**；锁定图层上的对象不能改（`LAYER_LOCKED`）。视图/选择/绘图辅助开关/出图等“安静”函数不落版本、不进撤销栈。
- **会话回写**：每次执行后更新 `session`（最近图元栈、选择集、最后一点、撤销栈），“刚才那个”“它”靠它解析。
- **变量引用**（`refs.py`）：`$sN.created[0]`、`$sN.entity_ids[0]`、`$sN.points.primary`、`$sN.boundary.outer`、`$this[i].created[0]`（批内）、`$LAST_CREATED`、`$LAST_POINT`、`step_results[i]`…；坐标表达式 `100,50`、`@30,0`、`@50<45`、`向右100`、`C8.center`、`int(L1,L2)`…（`geom/coords.py`，所有点参数共用）。解析不了报 `REF_UNRESOLVED`。
- `cad_batch_execute`：在同一个事务里顺序执行子步骤，任一步失败即整体丢弃（返回 `failed_step`、`completed_results`）；子步骤全是“安静”函数时整批不落版本。`history_undo_redo`：撤销 = 把更早一版的内容恢复成**新版本**（审计链完整），重做取回被撤销的那一版。
- 工具暴露：每个函数属于一个意图类别（`environment_setup`、`2d_drafting`、`precision_snap`、`view_select`、`2d_editing`、`annotation_dimension`、`layout_plotting`、`block_xref_constraint`、`3d_modeling`、`engineering_specialty`，外加 `perception`、`system`）；Planner 只看到当次意图类别的子集。

## 5. 几何内核（`zhihui/cad/geom/`，纯函数）

| 模块 | 内容 |
|---|---|
| `entities.py` | 数据格式、原语分解、精确包围盒、变换（移动/旋转/缩放/镜像）、块参照包围盒 |
| `curves.py` | 直线/圆弧求交、最近点、切分、偏移；折线近似仅用于椭圆/样条 |
| `coords.py` | 坐标表达式解析（绝对/相对/极坐标/方向距离/特征点/捕捉函数） |
| `snap.py` | 15 种对象捕捉、方位词消歧、对象追踪交点（水平/竖直） |
| `boundary.py` | 最小闭合环追踪、岛检测、缺口诊断（返回缺口位置和大小） |
| `draw.py` | 点/线/射线/构造线/多线/多段线/样条/矩形/多边形/网格/圆/弧/椭圆 |
| `select.py` | 过滤查询、窗选/交叉选/栅栏、关系判断（相交/平行/垂直/相切/同心/内部）、方位区域、排序 |
| `modify.py` | 偏移、镜像、阵列、拉长/修剪/延伸、打断、倒角/圆角、分解、拉伸 |
| `grips.py` | 夹点生成与拖动 |
| `dims.py` | 标注测量与尺寸线自动避让 |
| `layout.py` | 图幅、标准比例、视口比例换算、图框与标题栏 |
| `blocks.py` | 块定义/插入、属性、动态块变体 |
| `constraints.py` | 几何/标注约束的 numpy 阻尼 LM 求解，自由度、过约束诊断，表达式安全求值（AST，无 `eval`） |
| `solid.py` | 拉伸/旋转/扫掠；布尔运算两条路：两个 z 向棱柱走精确的 slab 路径（shapely 二维布尔后重新拉伸，不需要额外依赖），其它实体走可选依赖 manifold3d 的网格布尔；体积、STL |
| `mech.py` | ISO 286 配合偏差、形位公差框格、粗糙度符号、紧固件/轴/法兰/键槽/轴承/齿轮等生成器 |
| `presets.py` | 图层/样式/线型预设 |

**数值口径**：公差配合偏差按 IT 等级与基本偏差公式计算（`cad/codes/iso286.json`），机械标准表在 `cad/codes/mech_std.json`；查不到时反问用户，不编造。

## 6. 三个 Agent（`zhihui/agent/general/`）

| Agent | 实现 | 说明 |
|---|---|---|
| Planner | `planner.py`（LLM）、`mock_planner.py`（离线规则） | 输出计划 JSON（`plan.py`，pydantic 严格校验：步骤编号、依赖、函数名、工具子集）。离线规则 Planner 覆盖演示口令及常见变体，规则覆盖不了的在真实模式下交给模型 |
| Recognizer | `recognizer_rules.py`（短语 → `RecSpec`）、`recognizer.py` | 规则解析（编号、类型词、方位词、大小、序数/倒数、指代、图层/颜色、尺寸条件、关系、特征点）→ 调感知函数 → 判定 `resolved / ambiguous / not_found`，写回选择集（歧义时高亮候选）；规则看不懂才让模型把短语翻译成感知函数参数（模型只写参数、不下结论，识别步骤不能调改图函数） |
| Executor | `executor.py` | 程序为主：拓扑序、引用解析、校验、调用；失败/歧义/`replan_after` 时回到 Planner；参数**格式**错误时可选让模型修一次（`ZHIHUI_EXECUTOR_LLM=1`） |

**执行状态机**（`engine.py`）：反问（歧义选择、缺参、确认批量删除、解锁图层）把执行状态存进一条 `role=general_state` 的消息，用户回答后从断点继续，已完成的步骤不重做。预算：60 次函数调用、180 秒。

**护栏**（`guards.py`）：① 关键数值必须出现在用户原话（规范化后）里，或来自引用、坐标表达式、上一步结果，或是它们的简单换算（半、倍）；② 计划里不许写死需要计算才能得到的坐标；③ 超过 20 个对象的删除/分解、删除/清理图层要确认。**缺少关键数值就反问，不编造**；样式类参数可以用默认值，但必须在“说明”里报告（例如“文字高度使用默认值 3.5”“数值未指定，取当前测量值”）。

**口语与命令**：`speech_normalize.py`（词表 `speech_lexicon.json`）把“一百二十点五、逗号、艾特、举行、图成、倒园角…”规范化，并保留原文；`command_line.py` 支持 `L C REC POL O F TR M RO LA LTS Z U` 等 AutoCAD 命令和常见中文模板，命中即直接执行。

## 7. DXF 读写（`cad/general_dxf.py`）

- **导出**：图层（颜色/线型/线宽/透明度/锁定/冻结）、线型、文字与标注样式、块与属性、模型空间与布局（页面设置、视口及比例、锁定）、外部参照块（真正的 `ezdxf.xref.attach`）、标注（用 ezdxf 的标注渲染生成 DIMENSION）、填充（含渐变）、MESH、头变量（`$LTSCALE $PDMODE $LIMMIN/$LIMMAX $CLAYER` 等；界限同时写模型空间 LAYOUT，否则 ezdxf 保存时会覆盖）。完整 IR 同时嵌入根字典 XRecord，本系统再次打开时无损还原（句柄回写）。
- **导入**：本系统导出的 DXF → 还原内嵌 IR；第三方 DXF → `read_general_doc` 逐图元转换，布局和视口比例也读入；线性/对齐/半径/直径标注转成本系统标注（可继续编辑），其余标注类型分解成线和文字，不支持的对象计数后写入图纸说明。
- 自检：三个 DXF 版本导出后 `ezdxf.audit()` 0 错误 0 修复；**未在 AutoCAD 等商业软件里打开验证**。

## 8. 渲染与出图（`cad/render_general.py`）

2D 用 matplotlib 画，3D 用特征边 + 画家算法着色（隐藏线/着色靠排序，复杂遮挡可能不准）；布局渲染图纸、图框和视口内的模型内容；`plot` 输出 PDF/PNG/SVG，单色/彩色、窗口/范围/界限/显示区域、标准比例，放不下时告警。前端还有 SVG 矢量场景（`cad/scene.py` → `/scene`），缩放平移不失真。

## 9. 与现有流程图逻辑的关系

- `Drawing.mode` 默认 `pfd`；`router.py` 按用户原话和当前图纸判断走哪条路径（“画垃圾焚烧流程并填充”这类仍走流程图）。流程图的 20 个工具、Coordinator、校验、导出路径不动。
- `invoke_engineering_toolset(domain="chem"/"water")` 不在通用图里重写，而是新建/切换到流程图。
- `validate_drawing / export_artifacts / inspect_drawing` 在 `mode="general"` 时走通用分支（`check_profile="general"`）。
- 回归保护：既有 pytest 和 `python -m zhihui.bench.run`（G/M/F/C/S）全部保持通过。

## 10. 前端（`05_前端/web/cad.html|cad.css|cad.js`）

**一个应用，两个页签**：“化工流程图”（`index.html`）和“通用绘图”（`cad.html`）共用 `shell.js` / `shell.css`——同一排页签、同一个设置对话框（API Key、模型、接口地址，保存后两页同时生效）；口令会自动送到对的页签：流程图页收到通用绘图口令（`/api/tasks` 返回 `mode=general`）就跳到通用绘图页并接着显示进度（`cad.html#task=<id>`），通用绘图页发送前先问 `/api/route`，是流程图口令就跳到流程图页并自动发送（`index.html#prompt=…`），不会改动当前图纸。

SVG 交互视图：滚轮缩放、中键/空格平移、捕捉标记（端点/中点/圆心/交点/切点/垂足…含相交点）、正交/极轴约束与提示、窗选（左→右）/交叉选（右→左）、夹点拖动（调用 `grip_edit_entity`）、图层面板与特性面板（调用对应函数）、状态栏开关（SNAP/GRID/ORTHO/POLAR/OSNAP/OTRACK/DYN）、命令行、麦克风按钮（Web Speech API，可选 `/api/asr`）、计划进度、识别反问时高亮候选并可点图作答、模型/布局标签页与视口、三维图片视图和视图方向按钮。`ops` 接口直接调用函数（不经过大模型），同样落版本、可撤销。

## 11. 测试与基准

见《功能对照表》第 4 节。要点：几何内核有独立单测；每个函数至少一个正常用例和一个典型错误用例；DXF 导出读回、第三方 DXF 导入再导出；14 条演示口令在 `--mock` 下离线跑通并断言几何结果；基准 B/D/A 共 50 条，scripted 全过，mock 36 条可跑全过（14 条如实标 SKIP）。

## 12. 取舍与未做

- **三维布尔**：两个 z 向棱柱走精确路径；其它实体（旋转体、锥度体、扫掠体、旋转过的实体、布尔结果）用**可选依赖 manifold3d**（2026-10-20 经你同意引入，`pip install manifold3d` 或 `pip install -e ".[solid]"`）。没装时这些布尔明确报 `BOOLEAN_UNSUPPORTED` 并给出安装提示，棱柱布尔不受影响。已知限制：两个实体只是共面“擦边接触”时，manifold3d 可能输出零厚度的碎片（体积和封闭性正确，包围盒/渲染线条可能偏大）；封闭性不过关的网格报 `BOOLEAN_FAILED`。
- **动态块、约束、ACIS 实体**：AutoCAD 里看到的是静态块变体、求解后的几何、MESH。
- **没做**：对象追踪虚线与光标旁动态输入框、UCS、TOLERANCE 实体、XCLIP 写入 DXF、DWG 读取、圆弧参与的倒角圆角、上传图纸的语义识别、真实大模型下语音误识别写法和 Qwen/GLM 的测量（DeepSeek 口令写法已测，见变更总结第 11 节）、商业 CAD 软件人工抽检、前端自动化测试。
- **新增依赖**：无。语音本地识别（`faster-whisper`）是可选依赖，没有安装，`/api/asr` 未实测。

## 13. 环境变量（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZHIHUI_GENERAL_BACKEND` | `local` | `local` 进程内直接调用；`mcp` 经 MCP 子进程（密钥隔离） |
| `ZHIHUI_MCP_GENERAL` | 空 | `1` 时 MCP Server 暴露通用函数（`general_mcp`） |
| `ZHIHUI_EXECUTOR_LLM` | 空 | `1` 时参数格式错误允许模型修一次 |
| `ZHIHUI_ASR_MODEL` | `small` | 本地语音识别模型（装了 faster-whisper 才有用） |
| `ZHIHUI_ODA` | — | ODA File Converter 路径（导出 DWG 用） |
