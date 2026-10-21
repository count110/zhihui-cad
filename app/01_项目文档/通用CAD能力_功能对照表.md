# 通用 CAD 能力 · 功能对照表

> 对应《通用CAD能力扩展_改造提示词_v2》第 4.2 节的 45 项需求。状态含义：**完成** = 函数、几何内核、测试齐全，行为与需求一致；**部分** = 主体可用，缺的部分写在“说明与限制”里；**等价实现** = ezdxf/本系统做不到 AutoCAD 原生效果，用另一种方式达到同样的使用结果（见第 3 节等价实现表）；**未做** = 没有实现。
> 状态以代码和测试为准，写文档时逐项用脚本验证过（见第 5 节“怎么复核”）。**没有任何一项被标成“未做”，但“部分”的 7 项里的缺口不能当作已实现来宣传。**

## 1. 45 项预置函数（外加补充的第 46 个）

演示口令编号见《改造提示词》第 10 节（1 新建机械图…14 撤销/改图层/全部显示）。“基准”列是 `06_测试与基准用例/cases/` 里的用例（每条带一个语音误识别写法）。

| # | 需求 | 函数 | 实现位置（几何内核 / 工具） | 测试 | 基准 | 演示 | 状态 | 说明与限制 |
|---|---|---|---|---|---|---|---|---|
| 1 | 认识工作空间、工具栏和命令行 | `manage_workspace_ui` | `tools/general/env_tools.py`；前端 `cad.html` | test_general_tools（workspace） | B001 | — | 等价实现 | 三种工作空间 + 面板开关 + 说明文字；没有 Ribbon 和 AutoCAD 原生工作空间。界面状态不落版本 |
| 2 | 新建、打开、保存图形文件 | `manage_document_file` | `cad/general_dxf.py`、`tools/store.py` | test_general_tools、test_dxf_general（全部） | B002 | 1、11 | 部分 | new/open/save/save_as/close；DXF R2010/R2013/R2018、JSON、STL。**DWG 导出需要 ODA File Converter，本机没装，返回 `E_NO_ODA`；不能打开 DWG**。模板是自有 JSON（`blank_a3` / `gb_a3_mech` / `gb_a3_arch` / `iso_a3`），不是 .dwt |
| 3 | 设置绘图窗口颜色和图形界限 | `configure_canvas_and_limits` | `env_tools.py` | test_general_tools、test_dxf_general（$LIMMAX） | B003 | 1 | 完成 | 背景色只影响预览和前端，不写 DXF；界限写 `$LIMMIN/$LIMMAX/$LIMCHECK`（连同模型空间 LAYOUT 的 limmin/limmax，ezdxf 保存时以后者为准） |
| 4 | 调整线型比例，让虚线正常显示 | `set_linetype_scale` | `env_tools.py` | test_general_tools | B004 | 7 | 完成 | 没给数值时 `auto=true` 按图形界限推荐，并在“说明”里告知 |
| 5 | 创建和管理图层 | `manage_layers` | `geom/presets.py`、`env_tools.py` | test_general_tools | B005 | 1 | 完成 | GB_MECH / GB_ARCH / ISO 预设；图层 0 不可删改名；有图元或当前层不能删 |
| 6 | 设置图层颜色、线型和线宽 | `set_layer_properties` | `env_tools.py` | test_general_tools | B006 | — | 完成 | 线型未加载自动加载；非法颜色/线型报错 |
| 7 | 控制图层的可见性与当前状态 | `set_layer_state` | `env_tools.py`、`ctx.py`（锁定检查） | test_general_tools（锁定、当前层不能冻结） | B007 | 1 | 完成 | 锁定图层上的对象不能改，报 `LAYER_LOCKED`，Agent 会问“要不要先解锁” |
| 8 | 使用捕捉、栅格、正交和极轴 | `configure_drafting_modes` | `env_tools.py`、`geom/snap.py`、前端 | test_general_tools（quiet）、test_general_api | B008 | 2 | 完成 | 正交与极轴互斥；开关不落版本；前端按设置约束光标 |
| 9 | 开启对象捕捉与对象追踪 | `configure_osnap_and_otracking` | `env_tools.py`、`geom/snap.py` | test_general_tools | B009 | 2 | 部分 | 捕捉模式开关与前端捕捉标记完成。**对象追踪只保存开关，前端不画追踪虚线**；追踪交点由 `resolve_osnap_point(otrack_intersection)` 计算，只做水平/竖直两个方向 |
| 10 | 动态输入和坐标显示 | `configure_dynamic_input_and_coords` | `env_tools.py`、前端状态栏 | test_general_tools | B010 | — | 部分 | 开关和坐标显示模式保存，状态栏显示坐标；**光标旁的动态输入框没做** |
| 11 | 绘制点、直线、射线和构造线 | `draw_linear_primitives` | `geom/draw.py` | test_general_tools、test_geom_kernel | D001 | 2 | 完成 | xline 六种模式（两点/水平/竖直/角度/偏移/平分）；连续画线每段独立；闭合 = 回到起点 |
| 12 | 绘制多线、多段线和样条曲线 | `draw_complex_curves` | `geom/draw.py` | test_general_tools | D002 | — | 完成 | |
| 13 | 绘制矩形、正多边形和网格 | `draw_polygons_and_meshes` | `geom/draw.py` | test_general_tools、test_geom_kernel | D003 | 3 | 完成 | 矩形可圆角/倒角/旋转；圆角过大报错 |
| 14 | 绘制圆、圆弧、椭圆和椭圆弧 | `draw_circular_primitives` | `geom/draw.py`、`geom/curves.py` | test_general_tools、test_geom_kernel（TTR 无解） | D004 | 3、5 | 完成 | 多种构造方式；三点共线、半径小于半弦等报错 |
| 15 | 使用图案填充表达剖切区域 | `create_hatch` | `geom/boundary.py` | test_general_tools、test_dxf_general（渐变） | D005 | — | 完成 | 实体边界/拾取内部点/自定义边界；岛检测；关联填充随边界移动；边界不闭合返回缺口位置；渐变色写入 DXF |
| 16 | 通过键盘输入绝对坐标和相对坐标 | `parse_coordinate_input` | `geom/coords.py` | test_coords（39 条） | D006 | 2、3 | 部分 | 绝对/相对直角、极坐标、方向加距离、特征点引用、`int/per/tan/nea/ext/m2p`。**UCS 未支持**：传 `UCS` 时按 WCS 处理并给警告 |
| 17 | 利用对象捕捉精确定位特殊点 | `resolve_osnap_point` | `geom/snap.py` | test_general_tools（15 种模式逐个验证） | D007 | 5 | 完成 | 多个候选时按方位词/参考点消歧，仍不唯一返回候选（`AMBIGUOUS`）；`insertion` 只对块参照/文字有效 |
| 18 | 实时缩放、平移和范围显示图形 | `control_viewport_view` | `env_tools.py`/`view_tools.py`、前端 | test_general_tools、test_general_api | D008 | 14c | 完成 | 视图操作不落版本；`zoom_previous`、命名视图 |
| 19 | 选择单个对象或批量选择对象 | `select_entities` | `geom/select.py` | test_general_tools、test_recognizer | D009 | 6 | 完成 | 11 种选择方式；窗选/交叉选语义与 AutoCAD 一致 |
| 20 | 复制、镜像、偏移和阵列图形 | `replicate_entities` | `geom/modify.py` | test_general_tools、test_geom_kernel | D010 | 4、5 | 完成 | 偏移过大报错；阵列数量包含原对象 |
| 21 | 移动、旋转、比例缩放和拉伸 | `transform_entities` | `geom/modify.py`、`geom/entities.py` | test_general_tools | D011 | 5 | 完成 | `stretch` 需要交叉窗口，只动框内顶点 |
| 22 | 拉长线条、修剪和延伸图形 | `adjust_line_lengths` | `geom/modify.py` | test_general_tools、test_geom_kernel | D012 | — | 完成 | 修剪**沿用原图元编号**（改原对象）；拉长只支持直线/圆弧/开口多段线，延伸只支持直线和圆弧，其余报 `UNSUPPORTED` |
| 23 | 打断图线、倒角和倒圆角 | `modify_corners_and_breaks` | `geom/modify.py` | test_general_tools、test_geom_kernel | D013 | 4 | 部分 | 打断（一点/两点）、直线-直线倒角/圆角、多段线整体倒角/圆角完成；**圆弧参与的倒角圆角不支持**（用 TTR 画圆或先分解），报 `UNSUPPORTED` |
| 24 | 分解复合对象以便单独修改 | `explode_compound_entities` | `geom/modify.py` | test_general_tools | D014 | — | 完成 | 超过 20 个对象要先确认 |
| 25 | 使用夹点快速拉伸、移动和旋转 | `grip_edit_entity` | `geom/grips.py`、前端夹点 | test_general_tools、test_general_api | D015 | — | 完成 | 角色别名（insert/insertion、mid/midpoint…）；圆的象限点 = 改半径 |
| 26 | 通过特性面板修改对象属性 | `modify_entity_properties` | `tools/general/edit_tools.py`、前端右侧面板 | test_general_tools | D016 | 14b | 完成 | 长度等只读特性报 `READ_ONLY_PROP` |
| 27 | 用特性匹配复制对象格式 | `match_entity_properties` | `edit_tools.py` | test_general_tools | D017 | — | 完成 | |
| 28 | 创建单行文字和多行文字 | `create_text_annotation` | `tools/general/annot_tools.py` | test_general_tools | A001 | — | 完成 | `%%c %%d %%p` 自动换成 ⌀ ° ±；样式必须已存在，否则 `STYLE_NOT_FOUND` |
| 29 | 设置文字样式、字体和比例 | `manage_text_style` | `annot_tools.py` | test_general_tools | A002 | — | 完成 | 字体文件名写进样式表；预览用系统中文字体渲染 |
| 30 | 标注线性尺寸、对齐尺寸和角度 | `create_linear_and_angular_dim` | `geom/dims.py` | test_general_tools、test_dxf_general | A003 | 8 | 完成 | 尺寸线位置自动避让；矩形/多段线取整体长宽 |
| 31 | 标注半径、直径及连续尺寸 | `create_radial_and_chain_dim` | `geom/dims.py` | test_general_tools | A004 | 8 | 完成 | 含弧长、基线、连续标注 |
| 32 | 设置统一的标注样式 | `manage_dim_style` | `annot_tools.py` | test_general_tools | A005 | — | 完成 | 可对已有标注批量应用 |
| 33 | 区分模型空间和图纸空间 | `switch_model_and_layout_space` | `geom/layout.py`、`layout_tools.py` | test_general_tools、test_dxf_general | A006 | 11 | 完成 | 新建/切换/改名/复制/删除；ISO A0–A4 图框和标题栏，**没给的标题栏必填项留占位并报告，不编造** |
| 34 | 在布局中创建并调整视口比例 | `manage_layout_viewports` | `geom/layout.py` | test_general_tools、test_dxf_general（视口比例） | A007 | 11 | 完成 | 只接受标准比例；锁定后不能改比例；视口内冻结图层写入 DXF 为尽力而为，AutoCAD 中的显示未实测 |
| 35 | 配置打印设备、纸张和输出比例 | `configure_plot_and_export` | `cad/render_general.py` | test_general_tools、test_demo_phrases（PDF） | A008 | 11 | 等价实现 | 输出 PDF/PNG/SVG（matplotlib），页面设置记录在布局里；**没有打印机驱动和 .ctb**，用单色/彩色模式代替；放不下时告警 |
| 36 | 创建普通块和带属性的块 | `create_and_insert_block` | `geom/blocks.py` | test_general_tools、test_dxf_general（块与属性） | A009 | 10 | 完成 | 属性值没给就留空并告知；块参照的包围盒按块定义和比例计算 |
| 37 | 制作可调整的动态块 | `manage_dynamic_block` | `geom/blocks.py` | test_general_tools（5 种参数类型） | A010 | — | 等价实现 | 可见性/线性拉伸/翻转/旋转/查询 5 种；**每组参数值生成一个静态块变体，AutoCAD 里不是原生动态块** |
| 38 | 引用外部图形文件 | `manage_external_references` | `general_dxf.py`、`block_tools.py` | test_general_tools、test_dxf_general | A011 | — | 部分 | DXF 外部参照真正附着（导出带 xref 块）；attach/overlay/reload/unload/detach/bind/list 完成。**XCLIP 只保存在 IR 和预览，不写进 DXF；PDF/图片只画占位外框；DWG 外部参照不支持** |
| 39 | 添加几何约束和标注约束 | `apply_parametric_constraints` | `geom/constraints.py`（numpy 阻尼 LM） | test_geom_kernel、test_demo_phrases | A012 | 13 | 等价实现 | 过约束/无解给出诊断；后选对象向先选对象靠拢（与 AutoCAD 一致）；只支持点/直线/圆/圆弧/多段线；**只导出求解后的几何，约束保存在内嵌 IR 里，AutoCAD 不识别** |
| 40 | 将二维轮廓拉伸为三维实体 | `extrude_2d_to_3d` | `geom/solid.py` | test_general_tools、test_solid_boolean、test_demo_phrases | A013 | 12 | 完成 | 带孔轮廓一次成型（孔自动成为通孔）、锥度（简单轮廓）、双向、路径扫掠；布尔并/差/交：两个 z 向棱柱走精确路径，**其它实体用可选依赖 manifold3d**（2026-10-20 经你同意引入；没装报 `BOOLEAN_UNSUPPORTED` 并提示安装）。限制：只“擦边接触”的共面输入可能带零厚度碎片（体积正确）；锥度只支持简单截面 |
| 41 | 将二维轮廓旋转成回转体 | `revolve_2d_to_3d` | `geom/solid.py` | test_general_tools | A014 | — | 完成 | 四种轴定义、任意角度；体积按网格散度定理计算 |
| 42 | 切换三维视图并观察模型 | `switch_3d_view_and_visual_style` | `cad/render_general.py` | test_general_tools、test_general_api | A015 | 12 | 等价实现 | 标准视图/自定义轨道角；视觉样式映射成 matplotlib 着色，隐藏线靠画家算法，复杂遮挡可能不准 |
| 43 | 机械制图涉及公差标注 | `create_mechanical_tolerances` | `geom/mech.py`、`cad/codes/iso286.json` | test_general_tools | A016 | 8、9 | 完成 | 配合代号按 IT 等级和基本偏差计算，查不到（如 `H7/zc9`）**反问用户，不编造**；形位公差框格用线和文字绘制，粗糙度按 GB/T 131；**没有写 AutoCAD 的 TOLERANCE 实体**（需求里写“可选”）；偏差数值已和已知值核对，使用前仍应对照现行标准 |
| 44 | 工程制图涉及专业工具集 | `invoke_engineering_toolset` | `geom/mech.py`、`cad/toolsets/`、`cad/codes/mech_std.json` | test_general_tools（16 个参数化用例） | A017 | — | 完成 | 12 个生成器全有；规格表里没有的规格报错（`SIZE_UNKNOWN`），关键尺寸缺失反问；`chem`/`water` 切换到流程图模式，不在通用图里重写；标准表为手工录入，须对照现行标准核对 |
| 45 | 工程制图涉及符号库管理 | `manage_symbol_library` | `cad/symbols_general.py` | test_general_tools | A018 | — | 完成 | 8 个类别，自定义符号存 `data/symbols/custom/`；内置符号不可覆盖/删除 |
| 46 | （补充）删除对象 | `erase_entities` | `edit_tools.py` | test_general_tools、test_plan_executor | D018 | 6 | 完成 | **提示词的 45 项里漏了“删除”**，演示口令 6 又需要它，所以补了这个函数；超过 20 个对象要确认 |

**统计**：45 项中 完成 34、部分 6（2、9、10、16、23、38）、等价实现 5（1、35、37、39、42）、未做 0。

## 2. 系统函数与协作协议

| 项目 | 实现 | 状态 | 说明 |
|---|---|---|---|
| `cad_batch_execute` | `tools/general/system_tools.py` | 完成 | 全成功才提交、只落一个版本；任一步失败整体回滚（版本号和图元数都不变）；子步骤都是“安静”函数时整批不落版本；批内用 `$this[i]` 引用（test_general_tools、test_plan_executor、A020/A021） |
| `history_undo_redo` | `system_tools.py`、`registry.py` | 完成 | 以版本为单位；新操作清掉重做栈（A019、test_general_tools） |
| `ask_user` | 沿用 `WAITING_CONFIRMATION`，选项里带 `candidates` | 等价实现 | 前端高亮候选，可点图作答（test_plan_executor 的 pick / confirm / unlock 三种续接） |
| 6 个感知函数 | `tools/general/view_tools.py` | 完成 | `query_entities_by_filter`、`resolve_contextual_reference`、`get_entity_snap_points`、`find_closed_boundary`、`get_entity_grips_and_topology`、`inspect_drawing_context` |
| 计划协议与变量引用 | `agent/general/plan.py`、`tools/general/refs.py` | 完成 | `$sN…`、`$this[i]…`、`$LAST_CREATED`、`step_results[i]`；解析不了报 `REF_UNRESOLVED` |
| 识别 Agent | `agent/general/recognizer*.py` | 完成 | 规则解析优先，大模型兜底且只能调感知函数；resolved/ambiguous/not_found；74 条参考用例（test_recognizer） |
| Executor | `agent/general/executor.py` | 完成 | 拓扑序、引用解析、pydantic 校验、预算 60 次、可选 LLM 修参（`ZHIHUI_EXECUTOR_LLM=1`） |
| Planner / 护栏 | `agent/general/planner.py`、`guards.py`、`mock_planner.py` | 完成 | 数值必须有出处、不许写死需计算的坐标、破坏性操作确认；离线规则 Planner 覆盖演示口令及常见变体 |
| 口语规范化 | `agent/speech_normalize.py` + `speech_lexicon.json` | 完成 | 中文数字、符号前缀、误识别纠正，不改“统一/三维/一般”这类词（test_speech_normalize） |
| 命令快速通道 | `agent/command_line.py` | 完成 | AutoCAD 风格命令和中文模板（test_command_line） |
| 通用校验 | `cad/general_validate.py` | 完成 | `check_profile="general"` |
| 导入第三方 DXF | `general_dxf.read_general_doc` | 完成 | LINE/RAY/XLINE/MLINE/LWPOLYLINE/POLYLINE/SPLINE/CIRCLE/ARC/ELLIPSE/TEXT/MTEXT/DIMENSION/INSERT/HATCH/MESH + 块、图层、布局、视口；线性/对齐/半径/直径标注转成本系统标注，其余标注分解成线和文字并**告知**；不支持的对象计数后写进说明 |
| 前端交互视图 | `05_前端/web/cad.html/cad.css/cad.js`；与流程图页共用的外壳 `shell.js/shell.css` | 部分 | 缩放平移、捕捉标记、正交/极轴提示、窗选/交叉选、夹点、图层/特性面板、命令行、计划进度与点图作答、布局标签与视口、三维图片视图、语音按钮完成；与流程图页是同一个应用（共用页签和设置对话框，口令自动送到对的页签，有接口测试和脚本语法检查）；**追踪虚线、光标旁动态输入框没做；前端没有自动化测试，靠浏览器手动验证** |
| 语音输入 | 前端 Web Speech API；`/api/asr` | 部分 | 浏览器语音按钮已接入（依赖浏览器及其语音服务，**不是离线**）；`/api/asr` 的 faster-whisper 路径写好了，**本机未安装，没有实测**；规范化部分有 79 条单测（命令通道另有 96 条） |
| 上传图纸的语义识别（P2） | — | 未做 | 上传的第三方 DXF 能导入并被口述修改，但没有“这是哪类图纸/零件”的语义理解 |

## 3. 等价实现清单（ezdxf / 本系统做不到原生效果的部分）

| 需求 | 本项目的实现 | 导出 DXF 后的形态 |
|---|---|---|
| 工作空间、Ribbon、工具栏 | 前端布局 + 说明文字 | 不涉及 |
| .dwt 样板、.ctb | 自有 JSON 模板；单色/彩色模式 | 普通 DXF，不带 .ctb |
| 背景色、实时缩放平移、视觉样式 | 前端交互 + IR 的 `view`/`background` | 只有命名视图会写入 |
| 动态块 | 参数化块，改参数时程序重生成 | 每组参数值一个静态块，**不是**原生动态块 |
| 几何/标注约束 | 自研 numpy 求解器 | 只导出求解后的几何；约束随内嵌 IR（DXF 根字典 XRecord）保存，AutoCAD 不识别 |
| 三维实体 | 网格拉伸/旋转（带孔轮廓一次成型） | MESH + 可选 STL，**无 ACIS 3DSOLID** |
| 三维布尔 | z 向棱柱：shapely 二维布尔后重新拉伸；其它实体：可选依赖 manifold3d 的网格布尔 | MESH |
| conceptual / realistic 视觉样式 | matplotlib 着色 | 不涉及 |
| PDF 外部参照 | 只画外框和文件名 | PDFUNDERLAY |
| XCLIP | 预览与 IR | 不写入 |
| TOLERANCE 实体 | 用线和文字画框格 | 普通线和文字 |

## 4. 测试与基准

| 内容 | 位置 | 规模 |
|---|---|---|
| 坐标表达式 / 几何内核 / 口语规范化 / 命令通道 | `tests/test_coords.py`、`test_geom_kernel.py`、`test_speech_normalize.py`、`test_command_line.py` | 39 + 38 + 79 + 96 |
| 函数（每个至少一正常一错误；事务/撤销/锁定） | `tests/test_general_tools.py` | 50 |
| DXF 读写（回读图层/线型/表头/图元/块/属性/布局/视口比例；第三方 DXF 导入再导出；ezdxf 自检 0 问题） | `tests/test_dxf_general.py` | 13 |
| 识别（≥40 条中文指代） | `tests/test_recognizer.py` | 74 |
| 计划执行与状态机 | `tests/test_plan_executor.py` | 28 |
| 14 条演示口令（几何断言）+ 语音误识别写法 | `tests/test_demo_phrases.py` | 25 |
| 基准用例完整性与 scripted/mock 通过 | `tests/test_bench_general.py` | 141 |
| 三维布尔（网格闭合性、体积解析值、包含-排除、内腔保持、60 对随机棱柱的 slab/网格差分、没装 manifold3d 的降级） | `tests/test_solid_boolean.py` | 约 50（没装 manifold3d 时整个文件跳过） |
| 基准用例 | `06_测试与基准用例/cases/B001–B010、D001–D019、A001–A021` | 50 条，覆盖 46 个函数、6 个感知函数、2 个系统函数 |

基准三种模式：`--llm scripted`（执行用例写好的步骤，**50/50 通过**）、`--llm mock`（离线规则 Planner 从口令和语音误识别口令生成计划，**36 条可跑的全部通过，14 条规则没覆盖的如实标 SKIP**）、`--llm real`（真实 DeepSeek，只跑口令写法：修复前 32/50，修复后三次整轮 46、48、45/50；有随机波动，详见《变更总结》第 11 节）。

## 5. 怎么复核

```bash
.venv/Scripts/python -m pytest -q                                  # 全量
.venv/Scripts/python -m zhihui.bench.run                           # 全部类别 scripted
.venv/Scripts/python -m zhihui.bench.run --llm mock --category BDA # 通用 CAD 类，离线规则 Planner
.venv/Scripts/python -m zhihui.api                                 # 需要已配置 API Key；浏览器打开 http://127.0.0.1:8000/ 验证前端
```

## 6. 已知问题与没做的事

- 导出的 DXF 通过了 ezdxf 的自检（R2010/R2013/R2018 全部 0 问题），但**没有在 AutoCAD/浩辰/中望里打开过**；人工抽检仍需要做。
- 公差表和机械标准表是手工录入或按公式计算的，已核对若干已知值，使用前应对照现行国标。
- DWG 读写依赖外部转换器；未装时只能走 DXF。
- 真实大模型只测了 DeepSeek（deepseek-flash）和口令写法：B/D/A 50 条修复后 45–48/50，每次结果有波动；语音误识别写法、Qwen/GLM 没在真实模型下测。
- 三维布尔：manifold3d 是可选依赖；只“擦边接触”的共面输入可能带零厚度碎片（体积、封闭性正确，包围盒/渲染线条可能偏大）；三维实体不能用点选/栅栏/穿过边的交叉窗选中（窗选正常，这是原有限制，不是布尔引入的）。
