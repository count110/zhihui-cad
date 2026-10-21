"""通用模式的护栏（7.4）：在计划执行之前拦截。

1. 破坏性操作（删除、修剪、分解、purge/delete 图层）的对象超过 20 个，或涉及锁定图层 → 要求确认；
2. 关键几何数值必须有出处：出现在用户原话（规范化后）里，或来自变量引用 / 坐标表达式 / 上一步结果，或是它们的简单换算（半、倍）和固定常量；
3. 计划里写死了需要计算才能得到的坐标 → 拦截，提示改用识别结果的引用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zhihui.tools.general.refs import is_ref

from .plan import Plan

NUM = re.compile(r"(?<![A-Za-z_\d])-?\d+(?:\.\d+)?")
NUM_ANY = re.compile(r"-?\d+(?:\.\d+)?")           # 用户原话里的数：Ra3.2、R8、H7、M8 里的数字也算用户给的
SKIP_KEYS = {"limit", "dpi", "segments", "degree", "steps", "transaction_name", "offset_index", "max_distance", "snap_spacing", "grid_spacing", "reference_length"}
CONST = {0.0, 1.0, -1.0, 2.0, 0.5, 90.0, 180.0, 270.0, 360.0, 3.5, 2.5}
DESTRUCTIVE = {"erase_entities", "explode_compound_entities"}


@dataclass
class GuardIssue:
    code: str                  # E_UNSOURCED_VALUE / E_HARDCODED_COORD / E_CONFIRM_REQUIRED
    message: str
    step_id: str = ""
    kind: str = "block"        # block 要重新规划；confirm 要问用户

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "step_id": self.step_id, "kind": self.kind}


def utterance_numbers(text: str) -> set[float]:
    return {float(x) for x in NUM_ANY.findall(text)}


def _allowed(v: float, src: set[float]) -> bool:
    if v in CONST or abs(v) in CONST:
        return True
    for u in src:
        for cand in (u, -u, u / 2, u * 2, u / 3, u * 3, u / 10):
            if abs(abs(v) - abs(cand)) < 1e-9:
                return True
    return False


def _walk(obj: Any, key: str = ""):
    """生成 (key, value)：所有数值叶子，以及带数字的坐标/表达式字符串。变量引用跳过。"""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield key, obj
    elif isinstance(obj, str):
        if not is_ref(obj):
            yield key, obj
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk(x, key)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k in SKIP_KEYS:
                continue
            yield from _walk(v, k)


def _numbers_in_arg(key: str, v: Any) -> list[float]:
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, str):
        if key in ("layer", "layer_name", "block_name", "pattern_name", "text", "content", "style_name", "name", "transaction_name", "new_layout_name", "layout_name", "view_name", "iso_fit_code",
                   "datum_letter", "tag", "attribute_values", "title", "file_ref", "template_name", "symbol_keyword_or_id", "standard_scale", "plot_scale", "dim_param_name", "dim_expression", "domain", "tool_name",
                   "xref_path_or_name", "source_block_name", "block_ref_id", "viewport_id", "linetype", "bg_color", "color", "text_override", "mline_style", "workspace", "topic"):
            return []
        if re.fullmatch(r"[A-Za-z]+\d+(?:\.[\w()]+)*", v.strip()):            # C8.center、L2.mid 这类特征点引用
            return []
        return [float(x) for x in NUM.findall(v)]
    return []


def _flatten_calls(plan: Plan):
    for st in plan.workflow_steps:
        if st.target_agent != "tool_execution_agent":
            continue
        tool, args = st.payload.get("tool"), st.payload.get("args", {}) or {}
        if tool == "cad_batch_execute":
            for sub in args.get("steps", []):
                yield st.step_id, sub.get("tool"), sub.get("args", {}) or {}
        else:
            yield st.step_id, tool, args


def check_plan(plan: Plan, utterance: str, original: str = "", prior_numbers: set[float] | None = None) -> list[GuardIssue]:
    issues: list[GuardIssue] = []
    src = utterance_numbers(utterance) | utterance_numbers(original) | (prior_numbers or set())
    for sid, tool, args in _flatten_calls(plan):
        if tool in VESSEL_TOOLS:                                    # 设备图参数走专用的 check_vessel_patch（不用常量表、不放行倍数）
            continue
        # 1 破坏性
        if tool in DESTRUCTIVE and isinstance(args.get("entity_ids"), list) and len(args["entity_ids"]) > 20 and not args.get("confirmed"):
            issues.append(GuardIssue("E_CONFIRM_REQUIRED", f"{tool} 一次处理 {len(args['entity_ids'])} 个对象（超过 20 个），需要先确认", sid, "confirm"))
        if tool == "manage_layers" and args.get("action") in ("delete", "purge"):
            issues.append(GuardIssue("E_CONFIRM_REQUIRED", f"{'删除' if args['action'] == 'delete' else '清理'}图层需要先确认", sid, "confirm"))
        # 2/3 数值出处
        for key, v in _walk(args):
            for n in _numbers_in_arg(key, v):
                if not _allowed(n, src):
                    kind = "坐标" if isinstance(v, str) and ("," in v or "<" in v) else "数值"
                    code = "E_HARDCODED_COORD" if kind == "坐标" else "E_UNSOURCED_VALUE"
                    issues.append(GuardIssue(code, f"{tool} 的 {key or '参数'} 里有{kind} {n:g}，用户原话和上一步结果里都没有这个数；要计算得到的{kind}请用识别结果的引用，缺少的数值要反问用户", sid))
                    break
    return issues


def blocking(issues: list[GuardIssue]) -> list[GuardIssue]:
    return [i for i in issues if i.kind == "block"]


# ------------------------------------------------------------------ 设备补丁护栏（任务书 9.3，只用于 vessel_define / vessel_generate_drawing）
VESSEL_TOOLS = {"vessel_define", "vessel_generate_drawing", "vessel_query", "vessel_lookup_standard", "vessel_check"}
_CLAUSE = re.compile(r"[，,；;。\n、]")
_UNIT_NUM = re.compile(r"(-?\d+(?:\.\d+)?)\s*(mm|毫米|cm|厘米|dm|m(?![a-zA-Z])|米|MPa|mpa|兆帕|kPa|kpa|千帕|Pa|帕|bar|巴|℃|°C|度)", re.I)
_MI_SHORT = re.compile(r"(?<![\d.])(\d+)米(\d{1,3})(?![\d.]|毫|厘|分|[A-Za-z])")
_LEN_UNITS = {"cm": 10.0, "厘米": 10.0, "dm": 100.0, "m": 1000.0, "米": 1000.0}
_PRES_UNITS = {"kpa": 0.001, "千帕": 0.001, "pa": 1e-6, "帕": 1e-6, "bar": 0.1, "巴": 0.1}
_LEN_WORDS = r"筒体|筒身|筒长|筒高|直段|筒节"
_TAN_WORDS = r"切线"
# 触发词里的单字在这些词里不算数：“设计温度80℃”的“度”、“80°C”的“°”都不是方位的说法
_TRIG_MASK = {"orientation_deg": r"[温厚高长宽深精强硬速密浓粘]度|°\s*[CF]"}
# 设备类型（必问）：立式/卧式的说法，或只有一种容器用的支座型式
_VESSEL_KIND = {"vertical_vessel": r"立式|立罐|耳座|耳式|支腿|裙座|塔", "horizontal_vessel": r"卧式|卧罐|鞍座|鞍式"}


def vessel_sources(text: str) -> tuple[set[float], set[float]]:
    """原话里的数（含反问的回答）：(原样的数, 原样的数 ∪ 按单位换算成 mm / MPa 之后的数)。"""
    raw = utterance_numbers(text)
    conv = set(raw)
    for m in _UNIT_NUM.finditer(text):
        v, u = float(m.group(1)), m.group(2).lower()
        if u == "米" and text[m.end():m.end() + 1].isdigit():              # “1米6”里的“1米”不是 1000
            continue
        if u in _LEN_UNITS:
            conv.add(round(v * _LEN_UNITS[u], 9))
        elif u in _PRES_UNITS:
            conv.add(round(v * _PRES_UNITS[u], 12))
    for m in _MI_SHORT.finditer(text):                                 # 口语“一米六”“两米四”（规范化后是“1米6”“2米4”）：米后面的数字是小数部分
        conv.add(round((float(m.group(1)) + float("0." + m.group(2))) * 1000, 9))
    return raw, conv


def _only_with_other_unit(v: float, text: str, cls: str) -> bool:
    """原话里这个数只带着非标准单位出现（“1.6米”“600千帕”），模型却写成裸数：单位没换算。"""
    hits = [m for m in _UNIT_NUM.finditer(text) if abs(float(m.group(1)) - v) < 1e-9]
    if not hits:
        return False
    plain = [m for m in re.finditer(r"(?<![\d.])" + re.escape(f"{v:g}") + r"(?![\d.])", text)]
    if len(plain) > len(hits):
        return False
    units = _LEN_UNITS if cls == "len" else _PRES_UNITS if cls == "pres" else None
    return bool(units) and all(m.group(2).lower() in units for m in hits)


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE.split(text) if c.strip()]


def _has_num(clause: str, v: float) -> bool:
    return any(abs(x - v) < 1e-9 for x in utterance_numbers(clause))


def _path_class(path: str) -> str:
    m = re.match(r"^([a-z_]+)(?:\[[^\]]*\])?(?:\.([a-z_]+))?$", path.strip())
    if not m:
        return path
    root, field = m.group(1), m.group(2)
    return f"{root}.{field}" if field else root


def _leaves(tool: str, args: dict):
    """(容器, 键, 字段分类名, 显示路径)。字段分类名形如 shell.length_mm / nozzles.dn / heads.kind / design.medium。"""
    if tool == "vessel_define":
        spec = args.get("spec") or {}
        if isinstance(spec, dict):
            if "kind" in spec:
                yield spec, "kind", "vessel.kind", "spec.kind"
            for k in ("tag", "name"):
                if k in spec:
                    yield spec, k, k, k
            for grp in ("design", "shell", "drawing"):
                g = spec.get(grp)
                if isinstance(g, dict):
                    for k in list(g):
                        yield g, k, f"{grp}.{k}", f"spec.{grp}.{k}"
            for grp in ("heads", "nozzles", "supports"):
                for i, it in enumerate(spec.get(grp) or []):
                    if isinstance(it, dict):
                        for k in list(it):
                            yield it, k, f"{grp}.{k}", f"spec.{grp}[{i}].{k}"
        for k in list((args.get("set") or {})):
            yield args["set"], k, _path_class(k), f"set[{k}]"
        for k in list((args.get("add") or {})):
            yield args["add"], k, "add", f"add[{k}]"
        for grp, cls in (("upsert_nozzles", "nozzles"), ("upsert_supports", "supports")):
            for i, it in enumerate(args.get(grp) or []):
                if isinstance(it, dict):
                    for k in list(it):
                        yield it, k, f"{cls}.{k}", f"{grp}[{i}].{k}"
        for k in ("sheet", "scale"):
            if k in args:
                yield args, k, f"drawing.{k}", k
    elif tool == "vessel_generate_drawing":
        for k in ("sheet", "scale"):
            if k in args:
                yield args, k, f"drawing.{k}", k


def _nums_of(v, field: str):
    """数值叶子 → [(原样的数, 换算后的数, 是否带单位)]；文字返回 None。"""
    from zhihui.cad.equipment import spec_ops as S
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return [(float(v), float(v), False)]
    if isinstance(v, dict) and isinstance(v.get("value"), (int, float)):
        try:
            return [(float(v["value"]), S.to_number(v, field), bool(str(v.get("unit", "")).strip()))]
        except Exception:
            return [(float(v["value"]), float(v["value"]), True)]
    if isinstance(v, str):
        m = re.fullmatch(r"\s*(?:DN|PN)?\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z℃°一-龥]*)\s*", v, re.I)
        if m and (S.unit_class(field) != "plain" or re.match(r"\s*(?:DN|PN)", v, re.I) or not m.group(2)):
            try:
                return [(float(m.group(1)), S.to_number(v, field), bool(m.group(2)))]
            except Exception:
                return None
    return None


def _sub(v: str, text: str) -> bool:
    a = re.sub(r"\s", "", str(v)).lower()
    return bool(a) and a in re.sub(r"\s", "", text).lower()


def check_vessel_patch(plan: Plan, utterance: str, original: str = "") -> dict:
    """设备补丁护栏（确定性）：
    1 数值必须与（规范化后）原话里的某个数精确相等；带单位的写法（{"value":1.6,"unit":"m"}、"600kPa"）换算后再比对；不用常量表，不放行倍数；
    2 可默认字段：值必须出现在触发词所在的分句里，否则剔除这个字段并警告 E_DEFAULTABLE_FIELD（不重规划）；
    3 必问字段和照抄字段里来源不明的值 → 拦截（E_INVENTED_VALUE）并重规划；照抄的文字必须是原话的子串（或用途同义词）；
    4 管口符号只有原话里出现了才能写；
    5 用户只说“高/长/总高”而没说筒体长度还是切线长：剔除并转成反问（clarify）。
    返回 {"block": [GuardIssue], "warn": [GuardIssue], "clarify": [待问项]}；被剔除的字段直接从 plan 里删掉。"""
    from zhihui.cad.equipment import spec_ops as S
    text = f"{utterance}；{original}"
    raw, conv = vessel_sources(text)
    clauses = _clauses(text)
    out: dict = {"block": [], "warn": [], "clarify": []}

    def strip(box, key, shown, why, sid):
        box.pop(key, None)
        out["warn"].append(GuardIssue("E_DEFAULTABLE_FIELD", f"{shown}：{why}，已剔除（由默认规则决定）", sid, "warn"))

    for sid, tool, args in _flatten_calls(plan):
        if tool not in ("vessel_define", "vessel_generate_drawing"):
            continue
        for box, key, cls, shown in list(_leaves(tool, args)):
            if key not in box:
                continue
            v = box[key]
            field = cls.split(".")[-1] if cls != "add" else key.split(".")[-1]
            if v is None or field in ("turn_id", "confirmed", "regenerate", "renumber", "op", "position"):
                continue
            if cls == "vessel.kind":                                               # 设备类型是必问字段：原话里要有立式/卧式（或只配一种容器的支座）
                pat = _VESSEL_KIND.get(str(v))
                if pat is None or not re.search(pat, text):
                    out["block"].append(GuardIssue("E_INVENTED_VALUE", f"{shown}：设备类型 {v} 原话里没有说（立式/卧式），不能替用户选（缺了由程序反问）", sid))
                continue
            if field == "mark":                                                    # 4 管口符号
                if not re.search(r"(?<![A-Za-z])" + re.escape(str(v)) + r"(?![A-Za-z0-9])", text):
                    strip(box, key, shown, f"管口符号 {v} 原话里没有（符号由程序按 R-MARK-01 分配）", sid)
                continue
            tkey = cls if cls in ("heads.kind", "supports.kind") else ("location" if field == "location" else "kind" if cls == "nozzles.kind" else None)
            if tkey and isinstance(v, str):                                        # 翻译类文字字段
                pat = S.TEXT_TRIGGERS.get(tkey, {}).get(v)
                if pat is None or (pat and not re.search(pat, text, re.I)):
                    if tkey == "supports.kind":
                        out["block"].append(GuardIssue("E_INVENTED_VALUE", f"{shown}：支座型式 {v} 原话里没有说，不能替用户选（缺了由程序反问）", sid))
                    else:
                        strip(box, key, shown, f"{v} 在原话里没有相应说法", sid)
                continue
            nums = _nums_of(v, field)
            if nums is None:                                                       # 文字
                if cls in ("drawing.scale", "drawing.sheet"):
                    if not all(any(abs(x - n) < 1e-9 for x in raw) for n in utterance_numbers(str(v))):
                        strip(box, key, shown, f"{v} 原话里没有", sid)
                    continue
                if field in ("flange_type", "facing", "flange_std", "std_part", "model", "pipe_series"):
                    if not re.search(S.DEFAULTABLE_TRIGGERS.get(field, "^$"), text, re.I):
                        strip(box, key, shown, f"{v} 原话里没有说（设计条件齐全时由规则初选）", sid)
                    continue
                if field in ("purpose_text", "purpose"):
                    cp, _ = S.canonical_purpose(str(v))
                    row = S.purpose_row(cp)
                    words = [cp] + (row["synonyms"] if row else [])
                    if not (_sub(str(v), text) or any(w and w in text for w in words)):
                        out["block"].append(GuardIssue("E_INVENTED_VALUE", f"{shown}：管口用途「{v}」原话里没有", sid))
                    continue
                if isinstance(v, str) and not _sub(v, text):
                    out["block"].append(GuardIssue("E_INVENTED_VALUE", f"{shown}：「{v}」不是用户原话里的说法（照抄字段只能照抄）", sid))
                continue
            if field == "count" and all(abs(c - 1) < 1e-9 for _, c, _ in nums):
                continue                                                           # 数量 1 可以不说
            ucls = S.unit_class(field)
            defaultable = (field in S.DEFAULTABLE_TRIGGERS or cls in S.DEFAULTABLE_TRIGGERS) and cls != "add" and not cls.startswith(("shell.", "design.")) and cls != "nozzles.dn"
            for orig, val, has_unit in nums:
                ok = any(abs(val - x) < 1e-9 for x in conv)
                if ok and not has_unit and _only_with_other_unit(orig, text, ucls):
                    out["block"].append(GuardIssue("E_INVENTED_VALUE", f"{shown}={orig:g}：原话里这个数带着单位（如“米”“千帕”），请写成 {{\"value\":{orig:g},\"unit\":\"…\"}} 交给程序换算", sid))
                    break
                if not ok:
                    if defaultable:
                        strip(box, key, shown, f"{orig:g} 原话里没有", sid)
                    else:
                        out["block"].append(GuardIssue("E_INVENTED_VALUE", f"{shown}={orig:g}：用户原话里没有这个数；不要计算、换算或编造，缺的值由程序反问", sid))
                    break
                trig = S.DEFAULTABLE_TRIGGERS.get(cls) or S.DEFAULTABLE_TRIGGERS.get(field)
                mask = _TRIG_MASK.get(field)
                if defaultable and trig and field not in ("count",) and not any(re.search(trig, re.sub(mask, "", c) if mask else c, re.I) and _has_num(c, orig) for c in clauses):
                    strip(box, key, shown, f"{orig:g} 不在“{trig.split('|')[0]}”这类说法所在的分句里", sid)
                    break
            if key in box and cls in ("shell.length_mm", "shell.tangent_length_mm") and nums:   # 5 “高/长”口径
                n0 = nums[0][0]
                cl = [c for c in clauses if _has_num(c, n0)]
                if cl and not any(re.search(_LEN_WORDS if cls == "shell.length_mm" else _TAN_WORDS, c) for c in cl) and any(re.search(r"高|长", c) for c in cl):
                    box.pop(key, None)
                    out["clarify"].append(S.pending_item("shell.length", f"你说的 {n0:g} 是筒体长度（两条环焊缝之间）还是切线长？（P0 不接受总高/总长）", [f"筒体长度 {n0:g}", f"切线长 {n0:g}"]))
    return out


def invented_values(spec, turn_id: str, utterance: str) -> list[str]:
    """引擎侧复核（E_INVENTED_VALUE）：本轮来源为 user:<turn_id> 的每个数值叶子都要等于原话里的某个数（按单位换算后精确相等，不放行倍数）。"""
    from zhihui.cad.equipment import spec_ops as S
    from zhihui.agent.speech_normalize import normalize
    _, conv = vessel_sources(f"{utterance}；{normalize(utterance).text}")      # 调用方传原话也行：“四个”规范化成“4个”再取数
    bad = []
    for key, src in spec.sources.items():
        if src != f"user:{turn_id}":
            continue
        v = S.get_key(spec, key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if key.endswith(".count") and abs(v - 1) < 1e-9:
            continue
        if key.endswith("orientation_deg") and any(abs((v % 360) - (x % 360)) < 1e-6 for x in conv):
            continue
        if not any(abs(float(v) - x) < 1e-6 for x in conv):
            bad.append(f"{S.display_path(spec, key)}={S.fmt_value(v)}")
    return bad
