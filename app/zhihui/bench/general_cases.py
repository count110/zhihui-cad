"""通用 CAD 基准用例（B 基础环境 / D 绘制与编辑 / A 注释、出图与进阶）的运行与判定。

三种模式：
  scripted  执行用例里写好的 steps（经过护栏和 Executor，不经过 Planner），检验函数、几何内核、识别和断言本身；
  mock      离线规则 Planner 从口令（以及带语音误识别的口令）生成计划再执行；只跑 mock_ok=true 的用例，其余如实标 SKIP（规则没覆盖，不冒充通过）；
  real      真实大模型 Planner（需要 API Key），口令和语音误识别口令各跑一遍，这才是答辩用的成功率。

用例格式（JSON）：
  id / category(B|D|A) / functions(覆盖的函数) / prompt / prompt_asr(带误识别的写法) / intents(期望的意图类别)
  setup    {"template": "gb_a3_mech", "calls": [{"tool","args"}...], "xref_source": true}  预置图纸（确定性，不经过模型）
  steps    期望的函数调用序列：{"tool","args"} 或 {"rec": "指代短语", "need": [...]}，按顺序成 s1、s2…，每步依赖上一步
  assert   结果断言列表，见 check()；"scripted_only": true 的断言在 mock/real 里跳过（例如只有口令里没提的参数）
  status   期望的最终状态，默认 READY / READY_WITH_WARNINGS

化工设备图（E 类，任务书 10.2–10.4）由 run_vessel_case 运行：先过入口路由 entry_decision，期望 pfd 的到路由就判定（不进流程图协调器）；
期望 equipment 的在 setup 新建的 hg_equipment_a1 上跑 GeneralAgent(route_hint=路由结果)。每条 E 用例都必须有 setup（否则自动新建图纸会被算成一个新版本），
改图类用例用 setup.calls 里确定性的 vessel_define 预置参数（不过护栏）。E 类另有字段：
  expect_route    "equipment" | "pfd" | "general"
  route_drawing   路由时传哪张图："none"（新建场景，默认）| "setup"（改图场景，setup.calls 里有 vessel_define 时默认）| "both"
  expect_ask_kind true：路由结果必须带 ask_kind（只有设备名，由设备图 Planner 一次问图种和必问项）
  expect_clarify  {"paths": [...]}：期望 WAITING_CONFIRMATION，反问的待问路径覆盖这些路径，新增版本数 = 0（不要求有 drawing_id）
  expect_error    "错误码"：期望出现这个错误码（question.error_code、事件或结果里），默认状态 WAITING_CONFIRMATION
E 类断言（见 check_equip）：equip_spec / equip_sources / equip_values_from_user / equip_nozzles / equip_views / equip_table / equip_check /
equip_dim / clarify_covers / route_mode / artifact_source / no_vessel / equip_user_kept（任务书 10.3 之外多加的一种：成功判据③“其余用户字段不变”，
和 setup 预置的参数比，except 里列允许变的路径），另有已有的 revisions_added、artifact、layout、text。
mock 模式：规则 Planner 没有设备图规则（P0 的 E 用例 mock_ok=false），所以只跑路由（确定性、不调模型）：期望 pfd 的用例完整判定，
其余先查路由，路由对了标 SKIP（不冒充通过），路由错了标 FAIL。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from pathlib import Path
from typing import Any

import zhihui.tools.general  # noqa: F401  注册全部函数
from zhihui.agent.general.backend import LocalBackend
from zhihui.agent.general.engine import GeneralAgent
from zhihui.agent.general.plan import Plan
from zhihui.tools.core import ToolService
from zhihui.tools.general.registry import REGISTRY, run_tool
from zhihui.tools.store import DrawingStore

OK_STATUS = ["READY", "READY_WITH_WARNINGS"]


# ------------------------------------------------------------------ 预置与计划
def _sub(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace(k, v)
        return obj
    if isinstance(obj, list):
        return [_sub(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _sub(v, mapping) for k, v in obj.items()}
    return obj


def setup_drawing(svc: ToolService, case: dict) -> tuple[str | None, dict[str, str]]:
    s = case.get("setup")
    if s is None:
        return None, {}
    mapping: dict[str, str] = {}
    if s.get("xref_source"):
        r = run_tool(svc, None, "manage_document_file", {"action": "new", "template_name": "blank_a3"})
        other = r["drawing_id"]
        for tool, args in (("draw_linear_primitives", {"primitive_type": "line", "points": ["0,0", "50,0"]}),
                           ("draw_circular_primitives", {"curve_type": "circle", "method": "center_radius", "center": "25,10", "radius": 5})):
            assert run_tool(svc, other, tool, args)["ok"]
        saved = run_tool(svc, other, "manage_document_file", {"action": "save", "drawing_id": other, "format": "dxf"})
        mapping["$XREF_FILE"] = saved["file"]["file_ref"]
    r = run_tool(svc, None, "manage_document_file", {"action": "new", "template_name": s.get("template", "blank_a3")})
    assert r["ok"], r
    did = r["drawing_id"]
    for call in s.get("calls", []):
        out = run_tool(svc, did, call["tool"], _sub(call.get("args", {}), mapping))
        assert out.get("ok"), f"{case['id']} 预置失败 {call['tool']}: {out}"
    return did, mapping


def steps_to_plan(case: dict, mapping: dict[str, str]) -> Plan:
    steps = []
    for i, st in enumerate(_sub(case["steps"], mapping), 1):
        sid, deps = f"s{i}", ([f"s{i - 1}"] if i > 1 else [])
        if "perceive" in st:                                     # 直接调用感知函数的识别步骤
            steps.append({"step_id": sid, "target_agent": "entity_recognition_agent", "depends_on": deps, "action_summary": st["perceive"], "payload": {"tool": st["perceive"], "args": st.get("args", {})}})
        elif "rec" in st:
            payload: dict = {"query": st["rec"], "need": st.get("need") or ["entity_ids"]}
            steps.append({"step_id": sid, "target_agent": "entity_recognition_agent", "depends_on": deps, "action_summary": f"识别「{st['rec']}」", "payload": payload})
        else:
            steps.append({"step_id": sid, "target_agent": "tool_execution_agent", "depends_on": deps, "action_summary": st.get("summary") or st["tool"], "payload": {"tool": st["tool"], "args": st.get("args", {})}})
    return Plan.model_validate({"thought": "基准脚本", "intent_categories": case.get("intents", []), "workflow_steps": steps})


def expected_calls(case: dict, flatten: bool = True) -> list[str]:
    """期望的函数调用序列。flatten=True 时把 cad_batch_execute 里的子步骤也展开（覆盖统计用）。"""
    out: list[str] = []
    for st in case["steps"]:
        name = st.get("tool") or st.get("perceive")
        if not name:
            continue
        out.append(name)
        if flatten and name == "cad_batch_execute":
            out += [sub["tool"] for sub in st["args"].get("steps", [])]
    return out


# ------------------------------------------------------------------ 断言
def approx(a: Any, b: Any, tol: float = 1e-6) -> bool:
    if isinstance(b, (list, tuple)):
        return isinstance(a, (list, tuple)) and len(a) == len(b) and all(approx(x, y, tol) for x, y in zip(a, b))
    if isinstance(b, (int, float)) and not isinstance(b, bool) and isinstance(a, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= tol * max(1.0, abs(b))
    return a == b


def dig(obj: Any, path: str) -> Any:
    """点路径取值：支持 dict 键、对象属性、列表下标。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur[part]
        elif isinstance(cur, (list, tuple)):
            cur = cur[int(part)]
        else:
            cur = getattr(cur, part)
    return cur


def ents(d, kind=None, layer=None, tag=None, space="Model"):
    return [e for e in d.entities if (kind is None or e.kind == kind) and (layer is None or e.layer == layer) and (tag is None or tag in e.tags) and (space is None or e.space == space)]


def check(a: dict, d, out, agent, base_rev: int, artifacts: list[dict], mode: str = "scripted", extra: dict | None = None) -> tuple[bool, str]:
    ((kind, spec),) = [(k, v) for k, v in a.items() if k not in ("scripted_only", "note")]
    if kind in EQUIP_KINDS:                                                  # 化工设备图断言（E 类）
        return check_equip(kind, spec, d, out, agent, base_rev, artifacts, extra or {})
    if d is None and kind not in ("revisions_added", "artifact", "result", "result_has"):          # 反问类用例可以没有图纸（E 类）
        return False, f"没有图纸，无法检查 {kind}"
    if kind == "count":
        n = len(ents(d, spec.get("kind"), spec.get("layer"), spec.get("tag"), spec.get("space", "Model")))
        if "n" in spec and n != spec["n"]:
            return False, f"{spec.get('kind', '图元')}应有 {spec['n']} 个，实际 {n}"
        if n < spec.get("min", 0) or n > spec.get("max", 10 ** 9):
            return False, f"{spec.get('kind', '图元')}数量 {n} 不在 [{spec.get('min', 0)}, {spec.get('max', '∞')}]"
        return True, ""
    if kind == "entity":
        cand = ents(d, spec.get("kind"), spec.get("layer"), spec.get("tag"), spec.get("space", "Model"))
        if spec.get("id"):
            cand = [e for e in d.entities if e.id == spec["id"]]
        if len(cand) <= spec.get("nth", 0):
            return False, f"找不到第 {spec.get('nth', 0)} 个 {spec.get('kind', '图元')}（共 {len(cand)} 个）"
        e = cand[spec.get("nth", 0)]
        for f in ("layer", "color", "linetype", "lineweight_mm"):
            if f in spec and getattr(e, f) != spec[f]:
                return False, f"{e.id}.{f} = {getattr(e, f)!r}，应为 {spec[f]!r}"
        for path, want in (spec.get("data") or {}).items():
            try:
                got = dig(e.data, path)
            except (KeyError, IndexError, AttributeError):
                return False, f"{e.id} 没有字段 data.{path}"
            if not approx(got, want, spec.get("tol", 1e-6)):
                return False, f"{e.id}.data.{path} = {got!r}，应为 {want!r}"
        for path, want in (spec.get("len") or {}).items():
            try:
                got = len(dig(e.data, path))
            except (KeyError, IndexError, AttributeError, TypeError):
                return False, f"{e.id} 没有字段 data.{path}"
            if got != want:
                return False, f"{e.id}.data.{path} 长度 {got}，应为 {want}"
        for t in spec.get("tags_has", []):
            if t not in e.tags:
                return False, f"{e.id} 缺标签 {t}"
        return True, ""
    if kind == "drawing":
        for path, want in spec.items():
            try:
                got = dig(d, path)
            except (KeyError, IndexError, AttributeError):
                return False, f"图纸没有字段 {path}"
            if isinstance(want, dict) and "gt" in want:
                if not got > want["gt"]:
                    return False, f"{path} = {got!r}，应大于 {want['gt']}"
            elif isinstance(want, dict) and "ne" in want:
                if got == want["ne"]:
                    return False, f"{path} 仍是 {got!r}，应已改变"
            elif not approx(got, want, 1e-6):
                return False, f"{path} = {got!r}，应为 {want!r}"
        return True, ""
    if kind == "dim_style":
        st = next((s for s in d.dim_styles if s["name"] == d.current_dim_style), None)
        if st is None:
            return False, f"当前标注样式「{d.current_dim_style}」不在样式表里"
        for f, want in spec.items():
            if not approx(st.get(f), want):
                return False, f"当前标注样式 {f} = {st.get(f)!r}，应为 {want!r}"
        return True, ""
    if kind == "layer":
        l = d.get_layer(spec["name"])
        if spec.get("exists", True) is False:
            return (l is None), f"图层「{spec['name']}」不该存在"
        if l is None:
            return False, f"没有图层「{spec['name']}」"
        for f in ("color", "linetype", "lineweight_mm", "on", "frozen", "locked", "true_color", "plot"):
            if f in spec and getattr(l, f) != spec[f]:
                return False, f"图层「{l.name}」{f} = {getattr(l, f)!r}，应为 {spec[f]!r}"
        return True, ""
    if kind == "text":
        want = spec
        hit = [e for e in d.entities if e.kind in ("text", "mtext") and want in str(e.data.get("text", ""))]
        return (bool(hit), f"图上没有包含「{want}」的文字")
    if kind == "layout":
        lays = d.layouts
        lay = next((x for x in lays if x["name"] == spec["name"]), None) if spec.get("name") else (lays[0] if lays else None)
        if lay is None:
            return False, "没有布局"
        if "size" in spec and not approx(lay["page_setup"]["size"], spec["size"]):
            return False, f"布局图幅 {lay['page_setup']['size']}，应为 {spec['size']}"
        if "viewports" in spec and len(lay["viewports"]) != spec["viewports"]:
            return False, f"视口数 {len(lay['viewports'])}，应为 {spec['viewports']}"
        if "scale" in spec or "locked" in spec:
            vp = lay["viewports"][0] if lay["viewports"] else None
            if vp is None:
                return False, "布局里没有视口"
            if "scale" in spec and vp["scale"] != spec["scale"]:
                return False, f"视口比例 {vp['scale']}，应为 {spec['scale']}"
            if "locked" in spec and bool(vp["locked"]) != spec["locked"]:
                return False, f"视口锁定 {vp['locked']}，应为 {spec['locked']}"
        return True, ""
    if kind == "block":
        b = next((x for x in d.blocks if x["name"] == spec["name"]), None)
        if b is None:
            return False, f"没有块「{spec['name']}」"
        if "attdefs" in spec and [x["tag"] for x in b.get("attdefs", [])] != spec["attdefs"]:
            return False, f"块属性 {[x['tag'] for x in b.get('attdefs', [])]}，应为 {spec['attdefs']}"
        if "inserts" in spec:
            n = len([e for e in d.entities if e.kind == "insert" and str(e.data.get("block", "")).split("@")[0] == spec["name"]])
            if n != spec["inserts"]:
                return False, f"块参照 {n} 个，应为 {spec['inserts']}"
        return True, ""
    if kind == "artifact":
        fm = [f for f in artifacts if f.get("format") == spec]
        return (bool(fm), f"没有输出 {spec} 文件")
    if kind == "constraints":
        got = [c["type"] for c in d.constraints]
        return (sorted(got) == sorted(spec), f"约束 {got}，应为 {spec}（不计顺序）")
    if kind == "solid":
        sol = ents(d, "solid3d")
        if not sol:
            return False, "没有三维实体"
        v = sol[0].data["volume"]
        if not approx(v, spec["volume"], spec.get("rel", 0.01)):
            return False, f"体积 {v:.1f}，应为 {spec['volume']}±{spec.get('rel', 0.01) * 100:g}%"
        return True, ""
    if kind == "selected":
        sel = [d.get_entity(i) for i in d.session.selection]
        n = len([e for e in sel if e is not None and (spec.get("kind") is None or e.kind == spec["kind"])])
        return (n == spec["n"], f"选中 {n} 个，应为 {spec['n']}")
    if kind == "result":
        r = agent.results.get(spec["step"])
        if r is None and mode == "real":                                   # 真实模型自己分步，步骤号和脚本不同：在所有步骤结果里找
            for res in agent.results.values():
                try:
                    if approx(dig(res, spec["path"]), spec["value"], spec.get("tol", 1e-6)):
                        return True, ""
                except (KeyError, IndexError, AttributeError, TypeError):
                    continue
            return False, f"所有步骤的结果里都没有 {spec['path']} = {spec['value']!r}"
        if r is None:
            return False, f"没有步骤 {spec['step']} 的结果"
        try:
            got = dig(r, spec["path"])
        except (KeyError, IndexError, AttributeError):
            return False, f"步骤 {spec['step']} 的结果里没有 {spec['path']}"
        return (approx(got, spec["value"], spec.get("tol", 1e-6)), f"{spec['step']}.{spec['path']} = {got!r}，应为 {spec['value']!r}")
    if kind == "result_has":                                                # 任何一步的结果里出现这个数值/坐标（用哪个函数、哪个字段取到都行）
        def found(o: Any) -> bool:
            if isinstance(o, (list, tuple)) and len(o) == len(spec) and all(isinstance(x, (int, float)) for x in o) and approx(list(o), spec, 1e-6):
                return True
            if isinstance(o, dict):
                return any(found(v) for v in o.values())
            if isinstance(o, (list, tuple)):
                return any(found(v) for v in o)
            return False
        return found(agent.results), f"所有步骤的结果里都没有 {spec}"
    if kind == "view_fits_all":
        bb = d.bbox()
        if bb is None:
            return False, "图是空的"
        ok = d.view.height >= (bb[3] - bb[1]) - 1e-6 and abs(d.view.center[0] - (bb[0] + bb[2]) / 2) < 1.0 and abs(d.view.center[1] - (bb[1] + bb[3]) / 2) < 1.0
        return ok, f"视图 中心 {d.view.center} 高 {d.view.height}，没有包住图元范围 {bb}"
    if kind == "variant_block":
        blks = [b for b in d.blocks if b["name"].startswith(spec["prefix"])]
        if not blks:
            return False, f"没有以 {spec['prefix']} 开头的块变体"
        xs = [v["p"][0] for b in blks for en in b["entities"] if en["kind"] == "polyline" for v in en["data"]["vertices"]]
        return (bool(xs) and approx(max(xs), spec["max_x"]), f"块变体最大 x = {max(xs) if xs else None}，应为 {spec['max_x']}")
    if kind == "revisions_added":
        n = (d.revision - base_rev) if d is not None else 0
        return (n == spec, f"新增版本 {n}，应为 {spec}")
    if kind == "history":
        n = len(d.session.history)
        return (n == spec, f"撤销栈 {n} 条，应为 {spec}")
    if kind == "dim":
        dims = [e for e in d.entities if e.kind == "dimension" and (spec.get("dtype") is None or e.data["dtype"] == spec["dtype"])]
        if len(dims) != spec.get("n", len(dims)):
            return False, f"标注 {spec.get('dtype', '')} 有 {len(dims)} 个，应为 {spec['n']}"
        if "measurements" in spec and sorted(round(e.data["measurement"], 6) for e in dims) != sorted(spec["measurements"]):
            return False, f"标注值 {sorted(e.data['measurement'] for e in dims)}，应为 {spec['measurements']}"
        for path, want in (spec.get("data") or {}).items():
            if not all(approx(dig(e.data, path), want) for e in dims):
                return False, f"标注 data.{path} 不是 {want!r}"
        return True, ""
    return False, f"未知断言类型 {kind}"


# ------------------------------------------------------------------ 化工设备图断言（E 类，任务书 10.3）
EQUIP_KINDS = {"equip_spec", "equip_sources", "equip_values_from_user", "equip_nozzles", "equip_views", "equip_table", "equip_check", "equip_dim",
               "clarify_covers", "route_mode", "artifact_source", "no_vessel", "equip_user_kept"}
TABLE_MARKS = {"明细栏": ("TBL-PARTS", "件号"), "管口表": ("TBL-NOZ", "管口表"), "设计数据表": ("TBL-DESIGN", "设计数据表"), "技术要求": ("TECH", "技术要求")}      # 表名 → (部件标签, 表里一定有的文字)


def _fmt_deep(v: Any) -> Any:
    """比较前两边统一用 spec_ops.fmt_value 格式化（1600 和 1600.0、0.6 和 0.60 相同）；列表和字典逐项。"""
    from zhihui.cad.equipment import spec_ops as S
    if isinstance(v, dict):
        return {k: _fmt_deep(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_fmt_deep(x) for x in v]
    if v is None or isinstance(v, (bool, str)):
        return v
    return S.fmt_value(v)


def _equip_value(v, path: str) -> Any:
    """按对外路径取参数值（spec_ops.value_of）；再加两个只给断言用的写法：shell.length（= 筒体长度）、supports[0].positions.orientation_deg（各组位置的这个字段）。"""
    from zhihui.cad.equipment import spec_ops as S
    p = path.strip()
    m = re.match(r"^(.*\.positions)\.(\w+)$", p)
    if m:
        return [x.get(m.group(2)) for x in (S.value_of(v, m.group(1)) or [])]
    if p == "shell.length":
        return v.shell.length_mm
    return S.value_of(v, p)


def _src_kind(src: str | None) -> str:
    """user:t1 / user:t1:add → user；rule:R-PN-01 → rule；table:… → table；calc:… → calc；没有来源 → none。"""
    return (src or "none").split(":", 1)[0]


def _clarify_path(p: str) -> str:
    """反问路径的规范化：去掉 [] 里的选择器；shell.length_mm / shell.tangent_length_mm 都算 shell.length。"""
    p = re.sub(r"\[[^\]]*\]", "", str(p).strip())
    return "shell.length" if p in ("shell.length_mm", "shell.tangent_length_mm") else p


def _internal_keys(pre, path: str) -> list[str]:
    """对外路径（可带选择器）→ 预置参数里的内部键（nozzles[uid].dn 这种），给 equip_user_kept 的 except 用。"""
    from zhihui.cad.equipment import spec_ops as S
    m = re.match(r"^(?P<root>[a-z_]+)(?:\[(?P<sel>[^\]]*)\])?(?:\.(?P<field>[a-z_]+))?$", path.strip())
    if not m:
        return [path.strip()]
    root, sel, field = m.group("root"), m.group("sel"), m.group("field")
    if root == "nozzles" and sel is not None:
        return [S.nkey(n, field) for n in S.resolve_selector(pre, sel, allow_many=True)]
    if root == "supports" and sel is not None:
        return [S.skey(s, field) for s in S.resolve_selector(pre, sel, "supports", allow_many=True)]
    if root == "heads" and sel is not None:
        return [S.hkey(h, field) for h in S.resolve_selector(pre, sel, "heads")]
    if path.strip() == "shell.length":
        return ["shell.length_mm", "shell.tangent_length_mm"]
    return [path.strip()]


def _msg(ex: Exception) -> str:
    return str(getattr(ex, "message", None) or ex)


def _check_artifact_source(spec: dict, artifacts: list[dict], extra: dict) -> tuple[bool, str]:
    fmt = spec["format"]
    fm = [f for f in artifacts if f.get("format") == fmt]
    if not fm:
        return False, f"没有输出 {fmt} 文件"
    hit = [f for f in fm if f.get("source") == spec["source"]] if "source" in spec else fm
    if not hit:
        return False, f"{fmt} 文件的来源是 {[f.get('source') for f in fm]}，应为 {spec['source']!r}"
    svc, f = extra.get("svc"), hit[0]
    if svc is None or not f.get("file_ref"):
        return True, ""
    p = svc.store.root / f["file_ref"]
    if not p.exists() or p.stat().st_size == 0:
        return False, f"{fmt} 文件 {f['file_ref']} 不存在或是空的"
    if fmt == "pdf" and not p.read_bytes()[:5].startswith(b"%PDF"):
        return False, "pdf 文件不是 PDF（文件头不对）"
    if fmt == "dxf" and (spec.get("layouts_has") or spec.get("texts_has")):        # 读回 DXF：布局在，标题栏 / 管口表的文字能从布局里读出来
        import ezdxf
        doc = ezdxf.readfile(p)
        names = [l.name for l in doc.layouts]
        miss = [n for n in spec.get("layouts_has", []) if n not in names]
        if miss:
            return False, f"DXF 里没有布局 {miss}（有 {names}）"
        texts: list[str] = []
        for lay in doc.layouts:
            if lay.name == "Model":
                continue
            for e in lay:
                if e.dxftype() == "TEXT":
                    texts.append(e.dxf.text)
                elif e.dxftype() == "MTEXT":
                    texts.append(e.text)
        miss = [t for t in spec.get("texts_has", []) if not any(t in x for x in texts)]
        if miss:
            return False, f"DXF 布局里读不到文字 {miss}"
    return True, ""


def check_equip(kind: str, spec: Any, d, out, agent, base_rev: int, artifacts: list[dict], extra: dict) -> tuple[bool, str]:
    from zhihui.cad.equipment import check as CHK
    from zhihui.cad.equipment import sheet as SH
    from zhihui.cad.equipment import spec_ops as S
    from zhihui.cad.equipment.profile import head_profile
    if kind == "route_mode":                                                    # 入口路由结果（entry_decision）
        got = (extra.get("route") or {}).get("mode")
        return got == spec, f"入口路由 {got!r}，应为 {spec!r}"
    if kind == "clarify_covers":                                                # 反问的待问路径覆盖期望路径，且这一轮没有落版本
        q = (getattr(out, "question", None) or {}) if out is not None else {}
        items = q.get("items") or []
        if getattr(out, "status", None) != "WAITING_CONFIRMATION":
            return False, f"状态 {getattr(out, 'status', None)}，应为 WAITING_CONFIRMATION"
        if not items:
            return False, "反问里没有结构化的待问项（question.items）"
        got = {_clarify_path(it.get("path", "")) for it in items}
        miss = [p for p in spec["paths"] if _clarify_path(p) not in got]
        if miss:
            return False, f"反问没有覆盖 {miss}（问了 {sorted(got)}）"
        n = (d.revision - base_rev) if d is not None else 0
        return (n == 0, f"反问时新增了 {n} 个版本，应为 0")
    if kind == "artifact_source":
        return _check_artifact_source(spec, artifacts, extra)
    v = d.vessel if d is not None else None
    if kind == "no_vessel":
        return (v is None) == bool(spec), ("图纸上有设备参数（Drawing.vessel）" if v is not None else "图纸上没有设备参数（Drawing.vessel）")
    if v is None:
        return False, "图纸没有设备参数（Drawing.vessel 为空）"
    if kind == "equip_spec":
        for path, want in spec.items():
            try:
                got = _equip_value(v, path)
            except Exception as ex:                                              # noqa: BLE001  选择器没找到 / 不唯一 / 路径写错：都是失败，写清原因
                return False, f"取 {path} 失败：{_msg(ex)}"
            if _fmt_deep(got) != _fmt_deep(want):
                return False, f"{path} = {_fmt_deep(got)!r}，应为 {_fmt_deep(want)!r}"
        return True, ""
    if kind == "equip_sources":
        for path, want in spec.items():
            try:
                src = S.source_of(v, path)
            except Exception as ex:                                              # noqa: BLE001
                return False, f"取 {path} 的来源失败：{_msg(ex)}"
            if _src_kind(src) != want:
                return False, f"{path} 的来源是 {src!r}，应为 {want}"
        return True, ""
    if kind == "equip_values_from_user":                                         # 本轮来源为 user:<turn_id> 的每个数值，都在本轮口令里（按单位换算后精确相等）
        from zhihui.agent.general.guards import invented_values
        turn = getattr(agent, "turn_id", None)
        if not turn:
            return False, "没有本轮编号 turn_id（agent 没有运行？）"
        mine = [k for k, s in v.sources.items() if s == f"user:{turn}" and isinstance(S.get_key(v, k), (int, float)) and not isinstance(S.get_key(v, k), bool)]
        if not mine:
            return False, f"本轮（user:{turn}）没有来源为用户的数值，这条检查是空转"
        bad = invented_values(v, turn, extra.get("prompt", ""))
        return (not bad, f"来源标为用户、口令里却没有的值：{bad}")
    if kind == "equip_user_kept":                                                # 除了 except 里列的路径，setup 预置的用户值（来源 user:*）一个都不能变、不能被规则取代
        pre = extra.get("pre_vessel")
        if pre is None:
            return False, "这条用例的 setup 没有预置设备参数（setup.calls 里要有 vessel_define），没有东西可比"
        skip: set[str] = set()
        for p in spec.get("except", []):
            try:
                skip.update(_internal_keys(pre, p))
            except Exception as ex:                                              # noqa: BLE001
                return False, f"except 里的路径 {p} 在预置参数里找不到：{_msg(ex)}"
        users = [k for k, s in pre.sources.items() if s.startswith("user:") and k not in skip]
        if not users:
            return False, "预置参数里没有可比的用户值，这条检查是空转"
        for k in users:
            was, now = S.get_key(pre, k), S.get_key(v, k)
            shown = S.display_path(pre, k)
            if _fmt_deep(was) != _fmt_deep(now):
                return False, f"用户给的 {shown} 从 {_fmt_deep(was)!r} 变成了 {_fmt_deep(now)!r}"
            if not (v.sources.get(k) or "").startswith("user:"):
                return False, f"用户给的 {shown} 来源变成了 {v.sources.get(k)!r}"
        if getattr(pre, "kind", None) != v.kind:
            return False, f"设备类型从 {pre.kind} 变成了 {v.kind}"
        return True, ""
    if kind == "equip_nozzles":
        ns = v.nozzles
        if "count" in spec and len(ns) != spec["count"]:
            return False, f"管口 {len(ns)} 个，应为 {spec['count']}（{[n.mark for n in ns]}）"
        if "marks" in spec and sorted(n.mark or "" for n in ns) != sorted(spec["marks"]):
            return False, f"管口符号 {sorted(n.mark or '' for n in ns)}，应为 {sorted(spec['marks'])}"
        for field in ("dn", "location", "purpose"):
            for mark, want in (spec.get(field) or {}).items():
                n = next((x for x in ns if x.mark == mark), None)
                if n is None:
                    return False, f"没有管口 {mark}（有 {[x.mark for x in ns]}）"
                if _fmt_deep(getattr(n, field)) != _fmt_deep(want):
                    return False, f"管口 {mark} 的 {field} = {_fmt_deep(getattr(n, field))!r}，应为 {_fmt_deep(want)!r}"
        return True, ""
    if kind == "equip_views":                                                    # ["M","O"] 或 ["M","L"]；也可以 {"layout": "管口方位图", "views": ["O"]}
        name, want = (spec.get("layout", SH.LAYOUT), spec["views"]) if isinstance(spec, dict) else (SH.LAYOUT, spec)
        lay = next((l for l in d.layouts if l["name"] == name), None)
        if lay is None:
            return False, f"没有布局「{name}」（有 {[l['name'] for l in d.layouts]}）"
        got = [vp.get("equip_view") for vp in lay["viewports"]]
        if sorted(got) != sorted(want):
            return False, f"布局「{name}」的视图 {got}，应为 {want}"
        rec = v.drawing.get("record") or {}
        if name == SH.LAYOUT and sorted(x["view"] for x in rec.get("viewports", [])) != sorted(want):
            return False, "自检记录里的视口和布局不一致"
        return True, ""
    if kind == "equip_table":                                                    # ["设计数据表","管口表","明细栏","技术要求"]，加标题栏
        rec = v.drawing.get("record") or {}
        have = {t["name"]: t for t in rec.get("tables", [])}
        for name in spec:
            if name not in have:
                return False, f"布局里没有「{name}」（自检记录里有 {sorted(have)}）"
            if name not in TABLE_MARKS:
                return False, f"用例里的表名「{name}」不认识（可用 {sorted(TABLE_MARKS)}）"
            code, head = TABLE_MARKS[name]
            if not any(e.kind == "text" and e.space == SH.LAYOUT and f"equip_part:{code}" in e.tags and str(e.data.get("text", "")).startswith(head) for e in d.entities):
                return False, f"图纸空间里读不到「{name}」的文字（部件 {code}，应以「{head}」开头）"
        tb = rec.get("title_block")
        if not tb or tb[2] - tb[0] <= 0 or tb[3] - tb[1] <= 0:
            return False, "没有标题栏"
        if not any(e.kind == "text" and e.space == SH.LAYOUT and "equip_part:TB" in e.tags and "方案图" in str(e.data.get("text", "")) for e in d.entities):
            return False, "标题栏里没有「（方案图）」图名"
        return True, ""
    if kind == "equip_check":                                                    # {"errors": 0}
        r = CHK.run(v, d)
        if "errors" in spec and len(r["errors"]) != spec["errors"]:
            return False, f"设备图自检 {len(r['errors'])} 个错误（应为 {spec['errors']}）：" + "；".join(f"{e['code']} {e['message']}" for e in r["errors"][:3])
        return True, ""
    if kind == "equip_dim":                                                      # 总高（卧式总长）：取参数值算 = 筒体长度 + 各封头总高，必须和图上标注的尺寸链一致
        dm = (v.drawing.get("record") or {}).get("dim") or {}
        calc = float(v.shell.length_mm) + sum(head_profile(v, h).total_height for h in v.heads)
        want = spec["overall"]
        if _fmt_deep(dm.get("overall")) != _fmt_deep(want):
            return False, f"图上总尺寸 {_fmt_deep(dm.get('overall'))}，应为 {_fmt_deep(want)}"
        if _fmt_deep(calc) != _fmt_deep(want):
            return False, f"按参数值算的总尺寸 {_fmt_deep(calc)}，应为 {_fmt_deep(want)}"
        if abs(sum(dm.get("chain", [])) - float(dm.get("overall", 0))) > 1e-6:
            return False, f"尺寸链 {dm.get('chain')} 的和不等于总尺寸 {dm.get('overall')}"
        return True, ""
    return False, f"未知断言类型 {kind}"


# ------------------------------------------------------------------ 运行
EQUIVALENT ={"resolve_osnap_point": ("get_entity_snap_points",), "get_entity_snap_points": ("resolve_osnap_point",)}      # 真实模型取捕捉点可以走这两个函数或识别步骤，结果由状态断言检查
VERBOSE = False                                                           # run.py 的 --verbose：打印模型给出的计划和每步工具结果，排查失败用


def _show(kind: str, p: dict) -> None:
    if kind == "plan":
        for s in p.get("steps", []):
            print("    计划", s.get("step_id"), s.get("tool"), json.dumps(s.get("args"), ensure_ascii=False)[:300])
    elif kind == "tool_result":
        print("    结果", p.get("step_id"), p.get("tool"), str(p.get("summary"))[:200])
    elif kind in ("clarify", "guard", "replan", "recognize"):
        print("   ", kind, json.dumps(p, ensure_ascii=False)[:300])


async def _one(case: dict, mode: str, work: Path, prompt: str, variant: str) -> dict:
    t0 = time.monotonic()
    svc = ToolService(DrawingStore(work / f"{case['id']}_{variant}"))
    did, mapping = setup_drawing(svc, case)
    prompt = _sub(prompt, mapping)                                 # 口令里写 $XREF_FILE 的用例：换成预置文件的 file_ref（相当于用户上传后在口令里指明这个文件）
    base_rev = svc.store.load(did).revision if did else 0
    events: list[tuple[str, dict]] = []
    agent = GeneralAgent(on_event=lambda k, p: (events.append((k, p)), _show(k, p) if VERBOSE else None), mock=(mode != "real"), backend=LocalBackend(svc), drawing_id=did)
    try:
        if mode == "scripted":
            out = await agent.run_plan(steps_to_plan(case, mapping), prompt)
        else:
            out = await agent.run(prompt)
    except Exception as ex:                                              # noqa: BLE001  基准里任何异常都算失败并写清原因
        return {"id": case["id"], "category": case["category"], "variant": variant, "passed": False, "reason": f"{type(ex).__name__}: {ex}", "status": "ERROR"}
    want = case.get("status", OK_STATUS)
    res = {"id": case["id"], "category": case["category"], "variant": variant, "status": out.status, "seconds": round(time.monotonic() - t0, 1), "usage": out.usage}
    if out.status not in want:
        return {**res, "passed": False, "reason": f"状态 {out.status}（期望 {want}）：{out.message[:100]}"}
    if not out.drawing_id:
        return {**res, "passed": False, "reason": "没有图纸"}
    d = svc.store.load(out.drawing_id)
    arts = list(out.artifacts or [])
    for a in case.get("assert", []):
        if a.get("scripted_only") and mode != "scripted":
            continue
        ok, why = check(a, d, out, agent, base_rev, arts, mode)
        if not ok:
            return {**res, "passed": False, "reason": f"断言失败 {json.dumps({k: v for k, v in a.items() if k != 'scripted_only'}, ensure_ascii=False)[:100]}：{why}"}
    if mode == "real":                                                     # 真实模型：期望的函数至少要出现（不要求顺序和数量一致）
        used = [p.get("tool") for k, p in events if k == "tool_result"]
        miss = [t for t in expected_calls(case, flatten=False) if t not in used and t != "cad_batch_execute" and not (t in REGISTRY and REGISTRY[t].category == "perception")]
        planned = json.dumps([p for k, p in events if k == "plan"], ensure_ascii=False)
        miss = [t for t in miss if t not in planned]
        miss = [t for t in miss if not (set(EQUIVALENT.get(t, ())) & set(used)) and not (t in EQUIVALENT and any(k == "recognize" and p.get("status") == "resolved" for k, p in events))]
        if miss:
            return {**res, "passed": False, "reason": f"期望的函数没有被调用：{miss}"}
    return {**res, "passed": True, "reason": "ok"}


async def run_general_case(case: dict, mode: str, work: Path, asr: bool = True) -> dict:
    if mode == "scripted":
        return await _one(case, mode, work, case["prompt"], "scripted")
    if mode == "mock" and not case.get("mock_ok"):
        return {"id": case["id"], "category": case["category"], "passed": True, "skipped": True, "reason": "SKIP：离线规则 Planner 没有覆盖这类说法（scripted 模式已覆盖函数本身）", "status": "SKIPPED"}
    r1 = await _one(case, mode, work, case["prompt"], "口令")
    if not r1["passed"] or not asr:                                          # asr=False：只跑口令，不跑语音误识别写法（真实模型要花钱，先省一半）
        return r1
    r2 = await _one(case, mode, work, case["prompt_asr"], "语音误识别")
    if not r2["passed"]:
        r2["reason"] = "语音误识别写法：" + r2["reason"]
        return r2
    return {**r1, "seconds": round(r1.get("seconds", 0) + r2.get("seconds", 0), 1), "reason": "ok（口令 + 语音误识别写法）"}


# ------------------------------------------------------------------ 化工设备图用例（E 类，任务书 10.2）
def entry_route(prompt: str, drawing=None) -> dict:
    """入口路由（router.entry_decision，API 的 _general_target 也用它）：{mode, categories, ask_kind, reason}。"""
    from zhihui.agent.general import router
    fn = getattr(router, "entry_decision", None)
    if fn is None:
        raise RuntimeError("zhihui.agent.general.router 里还没有 entry_decision(prompt, drawing, has_dxf)")
    r = fn(prompt, drawing)
    return {"mode": getattr(r, "mode", None), "categories": list(getattr(r, "categories", None) or []), "ask_kind": bool(getattr(r, "ask_kind", False)), "reason": getattr(r, "reason", "")}


def _route_policy(case: dict) -> str:
    """路由时传不传图：新建场景不传（none），改图场景传 setup 之后的那张设备图（setup），流程图那类两种都要对（both）。"""
    if case.get("route_drawing"):
        return case["route_drawing"]
    calls = (case.get("setup") or {}).get("calls", [])
    return "setup" if any(c.get("tool") == "vessel_define" for c in calls) else "none"


def _route_step(case: dict, prompt: str, d0) -> tuple[dict | None, str]:
    """入口路由并对照 expect_route / expect_ask_kind。返回 (路由结果, 失败原因)，通过时原因为空串。"""
    policy, exp = _route_policy(case), case.get("expect_route")
    routes: dict[str, dict] = {}
    for w in (("none", "setup") if policy == "both" else (policy,)):
        r = routes[w] = entry_route(prompt, d0 if w == "setup" else None)
        where = "当前是设备图" if w == "setup" else "没有图纸"
        if exp and r["mode"] != exp:
            return r, f"入口路由（{where}）是 {r['mode']}，期望 {exp}（{r.get('reason', '')}）"
        if case.get("expect_ask_kind") is not None and r["ask_kind"] != bool(case["expect_ask_kind"]):
            return r, f"入口路由（{where}）ask_kind = {r['ask_kind']}，期望 {bool(case['expect_ask_kind'])}"
    return routes.get("setup") or routes["none"], ""


def _has_code(obj: Any, code: str, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(obj, dict):
        return any((k in ("error_code", "code") and v == code) or _has_code(v, code, depth + 1) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_has_code(x, code, depth + 1) for x in obj)
    return False


def _found_error(code: str, out, events: list, agent) -> bool:
    """期望的错误码出现在：反问（question.error_code）、事件、各步结果、执行报告里。"""
    report = None
    try:
        report = json.loads(out.report) if getattr(out, "report", "") else None
    except ValueError:
        pass
    return any(_has_code(x, code) for x in (getattr(out, "question", None), [p for _, p in events], agent.results, report))


async def _one_vessel(case: dict, mode: str, work: Path, prompt: str, variant: str) -> dict:
    t0 = time.monotonic()
    base = {"id": case["id"], "category": case["category"], "variant": variant}
    svc = ToolService(DrawingStore(work / f"{case['id']}_{variant}"))
    try:
        did, mapping = setup_drawing(svc, case)
    except Exception as ex:                                                  # noqa: BLE001  预置失败是用例或函数的问题，写清原因
        return {**base, "passed": False, "reason": f"预置失败 {type(ex).__name__}: {ex}", "status": "ERROR"}
    d0 = svc.store.load(did) if did else None
    base_rev = d0.revision if d0 is not None else 0
    pre = d0.vessel.model_copy(deep=True) if d0 is not None and d0.vessel is not None else None          # setup 预置的参数（equip_user_kept 用）
    prompt = _sub(prompt, mapping)
    # ---- 入口路由
    try:
        route, why = _route_step(case, prompt, d0)
    except Exception as ex:                                                  # noqa: BLE001
        return {**base, "passed": False, "reason": f"路由出错 {type(ex).__name__}: {ex}", "status": "ERROR"}
    res = {**base, "route": route["mode"] if route else None, "seconds": round(time.monotonic() - t0, 1)}
    if why:
        return {**res, "passed": False, "status": "ROUTE_MISMATCH", "reason": why}
    events: list[tuple[str, dict]] = []
    extra = {"route": route, "prompt": prompt, "svc": svc, "events": events, "variant": variant, "pre_vessel": pre}
    if case.get("expect_route") != "equipment":                              # 流程图 / 通用：到路由这一步就判定，不进智能体
        for a in case.get("assert", []):
            ok, w = check(a, d0, None, None, base_rev, [], mode, extra)
            if not ok:
                return {**res, "passed": False, "status": "ROUTED", "reason": f"断言失败 {json.dumps(a, ensure_ascii=False)[:100]}：{w}"}
        return {**res, "passed": True, "status": "ROUTED", "reason": f"ok（路由到 {route['mode']}，没有生成设备参数）"}
    # ---- 产品路径：GeneralAgent(route_hint=路由结果) 在 setup 新建的设备图上运行
    agent = GeneralAgent(on_event=lambda k, p: (events.append((k, p)), _show(k, p) if VERBOSE else None), mock=(mode != "real"), backend=LocalBackend(svc), drawing_id=did, route_hint=route["mode"])
    try:
        if mode == "scripted":
            out = await agent.run_plan(steps_to_plan(case, mapping), prompt)
        else:
            out = await agent.run(prompt)
    except Exception as ex:                                                  # noqa: BLE001  基准里任何异常都算失败并写清原因
        return {**res, "passed": False, "reason": f"{type(ex).__name__}: {ex}", "status": "ERROR"}
    waiting = bool(case.get("expect_clarify") or case.get("expect_error"))
    want = case.get("status") or (["WAITING_CONFIRMATION"] if waiting else OK_STATUS)
    res.update({"status": out.status, "seconds": round(time.monotonic() - t0, 1), "usage": out.usage})
    if out.status not in want:
        return {**res, "passed": False, "reason": f"状态 {out.status}（期望 {want}）：{out.message[:100]}"}
    if not out.drawing_id and not case.get("expect_clarify"):                # 反问类不要求有 drawing_id
        return {**res, "passed": False, "reason": "没有图纸"}
    d = svc.store.load(out.drawing_id) if out.drawing_id else None
    arts = list(out.artifacts or [])
    asserts = list(case.get("assert", []))
    if case.get("expect_error") and not _found_error(case["expect_error"], out, events, agent):
        return {**res, "passed": False, "reason": f"没有出现期望的错误码 {case['expect_error']}：{out.message[:100]}"}
    if case.get("expect_clarify") and not any("clarify_covers" in a for a in asserts):
        asserts.append({"clarify_covers": case["expect_clarify"]})
    for a in asserts:
        if a.get("scripted_only") and mode != "scripted":
            continue
        ok, w = check(a, d, out, agent, base_rev, arts, mode, extra)
        if not ok:
            return {**res, "passed": False, "reason": f"断言失败 {json.dumps({k: v for k, v in a.items() if k != 'scripted_only'}, ensure_ascii=False)[:100]}：{w}"}
    if mode == "real" and not case.get("expect_clarify"):                    # 真实模型：期望的函数至少要出现（反问类是模型直接问，不要求调函数）
        used = [p.get("tool") for k, p in events if k == "tool_result"]
        planned = json.dumps([p for k, p in events if k == "plan"], ensure_ascii=False)
        miss = [t for t in expected_calls(case, flatten=False) if t not in used and t not in planned]
        if miss:
            return {**res, "passed": False, "reason": f"期望的函数没有被调用：{miss}"}
    return {**res, "passed": True, "reason": "ok"}


async def run_vessel_case(case: dict, mode: str, work: Path, asr: bool = True) -> dict:
    """E 类用例：先路由，再（期望 equipment 时）在设备图上跑智能体。scripted 跑用例写好的 steps；mock 只跑路由（规则 Planner 没有设备图规则）；real 口令 + 语音误识别写法各跑一遍。"""
    if mode == "scripted":
        return await _one_vessel(case, mode, work, case["prompt"], "scripted")
    if mode == "mock" and not case.get("mock_ok") and case.get("expect_route") == "equipment":
        for tag, text in (("口令", case["prompt"]), ("语音误识别", case["prompt_asr"])):          # 路由是确定性代码：先查，路由错了就是真失败；对了也只能标 SKIP
            svc = ToolService(DrawingStore(work / f"{case['id']}_{tag}_route"))
            try:
                did, _ = setup_drawing(svc, case)
                _, why = _route_step(case, text, svc.store.load(did) if did else None)
            except Exception as ex:                                          # noqa: BLE001
                return {"id": case["id"], "category": case["category"], "passed": False, "reason": f"{tag}：路由出错 {type(ex).__name__}: {ex}", "status": "ERROR"}
            if why:
                return {"id": case["id"], "category": case["category"], "passed": False, "reason": f"{tag}：{why}", "status": "ROUTE_MISMATCH"}
        return {"id": case["id"], "category": case["category"], "passed": True, "skipped": True, "status": "SKIPPED",
                "reason": "SKIP：离线规则 Planner 没有设备图规则（口令和语音误识别写法的入口路由已检查通过；scripted 模式已覆盖函数本身）"}
    r1 = await _one_vessel(case, mode, work, case["prompt"], "口令")
    if not r1["passed"] or not asr:
        return r1
    r2 = await _one_vessel(case, mode, work, case["prompt_asr"], "语音误识别")
    if not r2["passed"]:
        r2["reason"] = "语音误识别写法：" + r2["reason"]
        return r2
    return {**r1, "seconds": round(r1.get("seconds", 0) + r2.get("seconds", 0), 1), "reason": "ok（口令 + 语音误识别写法）"}
