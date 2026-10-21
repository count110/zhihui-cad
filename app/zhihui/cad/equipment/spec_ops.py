"""容器参数的确定性操作（任务书 5.3）：合并补丁、解析选择器、路径白名单、单位换算、用途同义词、来源、默认值、缺项。

· sources 的键是内部路径：shell.length_mm、design.design_pressure_mpa、heads[top].kind、nozzles[n3].dn（n3 是管口的内部稳定编号 uid）、
  supports[s1].count、drawing.sheet。展示给人看时 display_path() 把 uid 换成管口符号。
· 来源：user:<turn_id>（用户说的）、user:<turn_id>:add（用户说的增量加到原值上，结果由程序算）、rule:<规则号>、table:<表>、calc:<规则号>。
· 每次合并补丁后，rule/table/calc 来源的值全部清掉、按新参数重算（所以改了设计压力后默认 PN 会重新取档）；用户值永远不被规则覆盖。
  例外：管口符号（R-MARK-01）一旦分配就保持稳定，只有用户明确要求“重新编号”才重排。
"""
from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, get_args

from zhihui.cad.geom.errors import GeomError
from zhihui.ir.vessel import P0_KINDS, Head, HeadKind, Nozzle, NozzleIn, NozzleKind, NozzleLoc, Quantity, Support, SupportIn, SupportKind, VesselPatchIn, VesselSpec

DATA = Path(__file__).resolve().parent / "data"
DERIVED = ("rule:", "table:", "calc:")
KEEP_RULES = ("rule:R-MARK-01",)

# ------------------------------------------------------------------ 字段表
DESIGN_FIELDS = ("design_standard", "vessel_category", "working_pressure_mpa", "design_pressure_mpa", "working_temp_c", "design_temp_c", "medium", "medium_property", "main_material",
                 "corrosion_allowance_mm", "weld_joint_coeff", "heat_treatment", "ndt", "test_type", "test_pressure_mpa", "leak_test", "insulation_thickness_mm", "fill_coeff",
                 "design_life_years", "wind_pressure_kpa", "seismic", "safety_valve_set_mpa")
SHELL_FIELDS = ("inner_diameter_mm", "length_mm", "tangent_length_mm", "thickness_mm", "material")
HEAD_FIELDS = ("kind", "thickness_mm", "straight_flange_mm", "material")
NOZZLE_FIELDS = ("mark", "dn", "purpose", "purpose_text", "kind", "location", "axial_mm", "radial_mm", "orientation_deg", "projection_mm", "pipe_series", "pipe_od_mm", "pipe_thk_mm",
                 "pipe_material", "pn", "flange_std", "flange_type", "facing", "std_part")
SUPPORT_FIELDS = ("kind", "count", "model", "height_mm", "material")
DRAWING_FIELDS = ("sheet", "scale", "orientation_convention", "mark_style")
TEXT_FIELDS = {"design_standard", "vessel_category", "medium", "medium_property", "main_material", "heat_treatment", "ndt", "test_type", "leak_test", "seismic", "material", "mark",
               "purpose", "purpose_text", "kind", "location", "pipe_series", "pipe_material", "flange_std", "flange_type", "facing", "std_part", "model", "sheet", "scale",
               "orientation_convention", "mark_style", "tag", "name"}
COUNT_FIELDS = {"count"}

LABELS = {
    "kind": "设备类型（立式/卧式容器）", "shell.inner_diameter_mm": "筒体内径 Di（mm）", "shell.length": "筒体长度或切线长（mm）", "shell.length_mm": "筒体长度（mm）",
    "shell.tangent_length_mm": "切线长（mm）", "shell.thickness_mm": "筒体名义厚度 δ（mm）", "supports.kind": "支座型式", "nozzles": "有哪些管口（用途和 DN）",
    "drawing_type": "要画的图种",
}


# ------------------------------------------------------------------ 单位换算
_LEN = {"mm": 1.0, "毫米": 1.0, "cm": 10.0, "厘米": 10.0, "m": 1000.0, "米": 1000.0, "dm": 100.0}
_PRES = {"mpa": 1.0, "兆帕": 1.0, "kpa": 0.001, "千帕": 0.001, "pa": 1e-6, "帕": 1e-6, "bar": 0.1, "巴": 0.1}
_TEMP = {"℃": 1.0, "°c": 1.0, "c": 1.0, "度": 1.0, "摄氏度": 1.0}
_KPA = {"kpa": 1.0, "千帕": 1.0, "pa": 0.001, "帕": 0.001, "mpa": 1000.0}
_DEG = {"°": 1.0, "度": 1.0, "deg": 1.0, "degree": 1.0, "degrees": 1.0}


def unit_class(field: str) -> str:
    if field.endswith("_mm") or field in ("length",):
        return "len"
    if field.endswith("_mpa"):
        return "pres"
    if field.endswith("_c"):
        return "temp"
    if field.endswith("_kpa"):
        return "kpa"
    if field.endswith("_deg"):
        return "deg"
    return "plain"


def _conv(v: float, unit: str, cls: str, field: str) -> float:
    u = unit.strip().lower().replace(" ", "")
    table = {"len": _LEN, "pres": _PRES, "temp": _TEMP, "kpa": _KPA, "deg": _DEG}.get(cls)
    if not u:
        return v
    if table is None or u not in table:
        raise GeomError("BAD_UNIT", f"{field} 的单位「{unit}」不认识", f"{field} 可用的单位：{', '.join(table) if table else '不带单位'}")
    f = table[u]
    return v * f if f >= 1.0 else v / round(1.0 / f)         # 小于 1 的系数用除法：600*0.001 这类乘法会带 1ulp 误差，p ≥ 0.1 这样的边界比较会被带偏


_NUM_UNIT = re.compile(r"^\s*(?:DN|PN|δ|Φ|φ|⌀)?\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z℃°一-龥]*)\s*$", re.I)


def to_number(v: Any, field: str) -> float:
    """数 / {"value","unit"} / "600kPa" → 本字段的标准单位（mm、MPa、℃、kPa）。模型不做换算，这里做。"""
    cls = unit_class(field)
    if isinstance(v, bool):
        raise GeomError("INVALID_ARGS", f"{field} 必须是数", "")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Quantity):
        return _conv(float(v.value), v.unit, cls, field)
    if isinstance(v, dict) and "value" in v:
        return _conv(float(v["value"]), str(v.get("unit", "")), cls, field)
    if isinstance(v, str):
        m = _NUM_UNIT.match(v)
        if m:
            return _conv(float(m.group(1)), m.group(2), cls, field)
    raise GeomError("INVALID_ARGS", f"{field} 的值 {v!r} 不是数", "写成数、{\"value\":1.6,\"unit\":\"m\"} 或 \"600kPa\"")


def fmt_value(v: Any) -> str:
    """上图字段的统一格式（比较前也用它）：整数不带小数点，其余去掉末尾 0。"""
    if v is None:
        return "待填"
    if isinstance(v, float):
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s if s not in ("-0", "") else "0"
    return str(v)


# ------------------------------------------------------------------ 用途同义词
@lru_cache(maxsize=1)
def purposes() -> list[dict]:
    return json.loads((DATA / "nozzle_purposes.json").read_text(encoding="utf-8"))["rows"]


_LOC_WORDS = re.compile(r"[上下左右]?封头|筒体|筒身|顶部|底部|上部|下部|侧面")
_GENERIC_SYN = {"进口", "出口"}              # 太泛：“冷却水进口”“蒸汽出口”不是进料口、出料口，只在整句等于它时才认


def canonical_purpose(text: str | None) -> tuple[str | None, bool]:
    """原话 → (规范名, 是否在词表里)。词表外的说法原样返回（False），不猜。"""
    if not text:
        return None, True
    s = re.sub(r"\s|DN\d+|\d+|个|一|两|根|只", "", str(text), flags=re.I)
    s0 = _LOC_WORDS.sub("", s) or s                            # “上封头进料”→“进料”：位置修饰词不属于用途
    for cand in dict.fromkeys((s, s0)):
        for row in purposes():
            if cand == row["name"] or cand in row["synonyms"]:
                return row["name"], True
    best = None                                                # “原料进料口”“安全阀接口”这类带修饰的说法：包含规范名或同义词，取最长的
    for row in purposes():
        for w in [row["name"]] + [x for x in row["synonyms"] if len(x) >= 2 and x not in _GENERIC_SYN]:
            if w in s0 and (best is None or len(w) > len(best[0])):
                best = (w, row["name"])
    if best:
        return best[1], True
    return str(text).strip(), False


def purpose_row(name: str | None) -> dict | None:
    return next((r for r in purposes() if r["name"] == name), None)


# ------------------------------------------------------------------ 路径
_SEG = re.compile(r"^(?P<root>[a-z_]+)(?:\[(?P<sel>[^\]]*)\])?(?:\.(?P<field>[a-z_]+))?$")


def allowed_paths(kind: str | None = None) -> list[str]:
    """Planner / 表单能写的路径（选择器写法见 resolve_selector）。"""
    out = ["tag", "name"] + [f"design.{f}" for f in DESIGN_FIELDS] + [f"shell.{f}" for f in SHELL_FIELDS] + ["shell.length"]
    heads = ("top", "bottom") if kind != "horizontal_vessel" else ("left", "right")
    out += [f"heads.{f}" for f in HEAD_FIELDS] + [f"heads[{p}].{f}" for p in heads for f in HEAD_FIELDS]
    out += [f"nozzles[<选择器>].{f}" for f in NOZZLE_FIELDS] + ["nozzles"]
    out += [f"supports[<选择器>].{f}" for f in SUPPORT_FIELDS] + [f"supports.{f}" for f in SUPPORT_FIELDS]
    out += [f"drawing.{f}" for f in DRAWING_FIELDS] + ["drawing_type"]
    return out


def path_allowed(path: str, kind: str | None = None) -> bool:
    m = _SEG.match(path.strip())
    if not m:
        return False
    root, sel, field = m.group("root"), m.group("sel"), m.group("field")
    if root in ("tag", "name", "drawing_type", "kind") and sel is None and field is None:
        return True
    if root == "nozzles" and field is None:
        return sel is None
    table = {"design": DESIGN_FIELDS, "shell": SHELL_FIELDS + ("length",), "heads": HEAD_FIELDS, "nozzles": NOZZLE_FIELDS, "supports": SUPPORT_FIELDS, "drawing": DRAWING_FIELDS}
    if root not in table or field not in table[root]:
        return False
    if root in ("design", "shell", "drawing") and sel is not None:
        return False
    if root == "nozzles" and sel is None:
        return False
    if root == "heads" and sel is not None:
        allowed = ("top", "bottom") if kind != "horizontal_vessel" else ("left", "right")
        return sel.strip() in allowed
    return True


def _bad_path(path: str, kind: str | None) -> GeomError:
    sample = ["shell.length_mm", "shell.inner_diameter_mm", "design.design_pressure_mpa", "heads.kind", "nozzles[b].dn", "nozzles[purpose=进料口].orientation_deg",
              "nozzles[location=top_head].projection_mm", "supports[0].count", "drawing.sheet"]
    return GeomError("BAD_PATH", f"路径 {path} 不在可用路径里", "可用路径举例：" + "、".join(sample) + "；管口选择器：nozzles[符号] / nozzles[purpose=用途] / nozzles[location=位置] / nozzles[kind=类型]",
                     allowed=allowed_paths(kind))


# ------------------------------------------------------------------ 选择器
def _match_nozzle(n: Nozzle, cond: str) -> bool:
    cond = cond.strip()
    if "=" not in cond:
        c = cond.lower().lstrip("#")
        return (n.mark or "").lower() == c or (n.uid or "").lower() == c
    k, v = (x.strip() for x in cond.split("=", 1))
    if k in ("mark", "符号"):
        return (n.mark or "").lower() == v.lower()
    if k in ("purpose", "用途"):
        cp, _ = canonical_purpose(v)
        return n.purpose == cp or (n.purpose_text or "") == v
    if k in ("location", "位置"):
        return n.location == v
    if k in ("kind", "类型"):
        return n.kind == v
    if k == "dn":
        try:
            return n.dn is not None and abs(float(n.dn) - to_number(v, "dn")) < 1e-9
        except GeomError:
            return False
    if k == "uid":
        return n.uid == v
    raise GeomError("BAD_PATH", f"管口选择器不认识条件 {k}", "可用：符号、purpose=用途、location=位置、kind=类型、dn=公称尺寸")


def describe_nozzle(n: Nozzle) -> str:
    loc = {"shell": "筒体", "top_head": "上封头", "bottom_head": "下封头", "left_head": "左封头", "right_head": "右封头"}.get(n.location or "", "位置待定")
    return f"{n.mark or n.uid}（{n.purpose or n.purpose_text or '用途未说'}，DN{fmt_value(n.dn)}，{loc}）"


def resolve_selector(spec: VesselSpec, sel: str, root: str = "nozzles", allow_many: bool = False) -> list:
    """nozzles[b] / nozzles[purpose=进料口] / nozzles[location=top_head] / heads[top] / supports[0] / supports[kind=lug]。0 个报 NOT_FOUND，多个报 AMBIGUOUS（附候选）。"""
    if root == "heads":
        hs = [h for h in spec.heads if h.position == sel.strip()]
        if not hs:
            raise GeomError("NOT_FOUND", f"没有封头 {sel}", f"现有：{[h.position for h in spec.heads]}", candidates=[h.position for h in spec.heads])
        return hs
    if root == "supports":
        s = sel.strip()
        if s.isdigit():
            i = int(s)
            if i >= len(spec.supports):
                raise GeomError("NOT_FOUND", f"没有第 {i} 组支座", f"现有 {len(spec.supports)} 组", candidates=[str(k) for k in range(len(spec.supports))])
            return [spec.supports[i]]
        k, _, v = s.partition("=")
        hits = [x for x in spec.supports if (k.strip() in ("kind", "类型") and x.kind == v.strip()) or x.uid == s]
        if not hits:
            raise GeomError("NOT_FOUND", f"没有支座 {s}", "", candidates=[x.kind or "?" for x in spec.supports])
        return hits
    conds = [c for c in re.split(r"[,，;；&]", sel) if c.strip()]
    hits = [n for n in spec.nozzles if all(_match_nozzle(n, c) for c in conds)]
    if not hits:
        raise GeomError("NOT_FOUND", f"没有找到管口 {sel}", "现有管口：" + "；".join(describe_nozzle(n) for n in spec.nozzles), candidates=[n.mark or n.uid for n in spec.nozzles],
                        options=[describe_nozzle(n) for n in spec.nozzles][:4])
    if len(hits) > 1 and not allow_many:
        raise GeomError("AMBIGUOUS", f"「{sel}」对应 {len(hits)} 个管口：" + "、".join(describe_nozzle(n) for n in hits) + "，要改哪一个？", "用管口符号指定，例如 nozzles[" + (hits[0].mark or hits[0].uid or "b") + "]",
                        candidates=[n.mark or n.uid for n in hits], options=[describe_nozzle(n) for n in hits][:3] + (["全部"] if len(hits) > 1 else []))
    return hits


# ------------------------------------------------------------------ 内部路径读写
def nkey(n: Nozzle, f: str) -> str:
    return f"nozzles[{n.uid}].{f}"


def skey(s: Support, f: str) -> str:
    return f"supports[{s.uid}].{f}"


def hkey(h: Head, f: str) -> str:
    return f"heads[{h.position}].{f}"


def _obj_for_key(spec: VesselSpec, key: str):
    m = _SEG.match(key)
    if not m:
        return None, None
    root, sel, field = m.group("root"), m.group("sel"), m.group("field")
    if root in ("design", "shell"):
        return getattr(spec, root), field
    if root == "drawing":
        return spec.drawing, field
    if root == "heads":
        return next((h for h in spec.heads if h.position == sel), None), field
    if root == "nozzles":
        return next((n for n in spec.nozzles if n.uid == sel), None), field
    if root == "supports":
        return next((s for s in spec.supports if s.uid == sel), None), field
    if root in ("tag", "name"):
        return spec, root
    return None, None


def get_key(spec: VesselSpec, key: str):
    obj, f = _obj_for_key(spec, key)
    if obj is None:
        return None
    return obj.get(f) if isinstance(obj, dict) else getattr(obj, f, None)


def set_key(spec: VesselSpec, key: str, value, source: str | None) -> None:
    obj, f = _obj_for_key(spec, key)
    if obj is None:
        return
    if isinstance(obj, dict):
        if value is None:
            obj.pop(f, None)
        else:
            obj[f] = value
    else:
        if value is None and isinstance(obj, Nozzle) and f in ("kind", "reinforcement"):
            value = "pipe" if f == "kind" else "auto"                     # 有缺省值的字段清回缺省值，不能是 None
        setattr(obj, f, value)
    if source:
        spec.sources[key] = source
    else:
        spec.sources.pop(key, None)


def display_path(spec: VesselSpec, key: str) -> str:
    m = _SEG.match(key)
    if m and m.group("root") == "nozzles":
        n = next((x for x in spec.nozzles if x.uid == m.group("sel")), None)
        if n is not None and n.mark:
            return f"nozzles[{n.mark}].{m.group('field')}"
    if m and m.group("root") == "supports":
        i = next((i for i, x in enumerate(spec.supports) if x.uid == m.group("sel")), None)
        if i is not None:
            return f"supports[{i}].{m.group('field')}"
    return key


def source_of(spec: VesselSpec, path: str) -> str | None:
    """按对外路径（可带选择器）查来源：nozzles[purpose=进料口].orientation_deg → user:t1。"""
    m = _SEG.match(path.strip())
    if not m:
        return None
    root, sel, field = m.group("root"), m.group("sel"), m.group("field")
    if root == "nozzles" and sel is not None:
        n = resolve_selector(spec, sel)[0]
        return spec.sources.get(nkey(n, field))
    if root == "supports" and sel is not None:
        s = resolve_selector(spec, sel, "supports")[0]
        return spec.sources.get(skey(s, field))
    if root == "shell" and field == "length":
        return spec.sources.get(f"shell.{_length_fields(spec)[0]}")
    return spec.sources.get(path)


def value_of(spec: VesselSpec, path: str):
    m = _SEG.match(path.strip())
    if not m:
        raise GeomError("BAD_PATH", f"路径写法不对：{path}", "")
    root, sel, field = m.group("root"), m.group("sel"), m.group("field")
    if root == "nozzles" and sel is not None:
        return getattr(resolve_selector(spec, sel)[0], field)
    if root == "supports" and sel is not None:
        return getattr(resolve_selector(spec, sel, "supports")[0], field)
    if root == "heads" and sel is not None:
        return getattr(resolve_selector(spec, sel, "heads")[0], field)
    if root in ("tag", "name", "kind"):
        return getattr(spec, root)
    if root == "drawing":
        return spec.drawing.get(field)
    if root == "shell" and field == "length":                  # 逻辑路径：用户给的是切线长就取切线长，否则取筒体长度（与 _apply_add 同一口径）
        return spec.shell.tangent_length_mm if _length_fields(spec) == ["tangent_length_mm"] else spec.shell.length_mm
    return getattr(getattr(spec, root), field)


# ------------------------------------------------------------------ 合并
def _next_uid(items, prefix: str) -> str:
    n = 0
    for x in items:
        m = re.fullmatch(prefix + r"(\d+)", x.uid or "")
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}{n + 1}"


def _norm_val(field: str, v):
    if v is None:
        return None
    if field in TEXT_FIELDS:
        return str(v).strip() if not isinstance(v, str) else v.strip()
    if field in COUNT_FIELDS:
        try:
            return int(to_number(v, field))
        except (TypeError, ValueError):
            raise GeomError("INVALID_ARGS", f"{field} 必须是整数", "")
    x = to_number(v, field)
    if field in ("dn", "pn"):
        return float(x)
    return x


def _validate_value(field: str, v, warnings: list[str]):
    if v is None:
        return
    if field in ("inner_diameter_mm", "length_mm", "tangent_length_mm", "dn") and v <= 0:
        raise GeomError("INVALID_VALUE", f"{field} 必须大于 0（给的是 {fmt_value(v)}）", "请给出正确的数值")
    if field == "thickness_mm" and v <= 0:
        raise GeomError("INVALID_VALUE", f"厚度必须大于 0（给的是 {fmt_value(v)}）", "名义厚度是正数，请确认")
    if field in ("projection_mm", "height_mm") and v <= 0:
        raise GeomError("INVALID_VALUE", f"{field} 必须大于 0（给的是 {fmt_value(v)}）", "")
    if field == "count" and v <= 0:
        raise GeomError("INVALID_VALUE", "数量必须大于 0", "")
    if field == "orientation_deg" and not (-360 < v < 720):
        raise GeomError("INVALID_VALUE", f"方位角 {fmt_value(v)}° 超出范围", "方位角 0–360°")


def _enum_of(obj, field: str) -> tuple[str, ...] | None:
    """枚举字段的可选值（与 ir/vessel.py 的 Literal 一致）；不是枚举字段返回 None。"""
    if isinstance(obj, Nozzle):
        return {"location": get_args(NozzleLoc), "kind": get_args(NozzleKind), "pipe_series": ("A", "B")}.get(field)
    if isinstance(obj, Head):
        return get_args(HeadKind) if field == "kind" else None
    if isinstance(obj, Support):
        return get_args(SupportKind) if field == "kind" else None
    if isinstance(obj, dict) and field == "sheet":
        return ("A0", "A1", "A2", "A3")
    return None


def _check_enum(obj, field: str, v):
    """枚举字段只能写模型认的值（不分大小写，存规范写法）。set 路径里的值是自由 JSON，不拦的话存进去会让整张图再也加载不了。"""
    if v is None:
        return v
    if isinstance(obj, dict) and field == "scale":
        from zhihui.cad.geom import layout as LYT
        s = str(v).strip().replace("：", ":")
        if not LYT.is_standard_scale(s):
            raise GeomError("INVALID_VALUE", f"比例 {v} 不是 GB/T 14690 的标准比例", f"可选：{LYT.STANDARD_SCALES + LYT.SECOND_SCALES}")
        return s
    allowed = _enum_of(obj, field)
    if allowed is None:
        return v
    hit = next((a for a in allowed if a.lower() == str(v).strip().lower()), None)
    if hit is None:
        raise GeomError("INVALID_VALUE", f"{field} 的值「{v}」不在可选范围里", "可选：" + "、".join(allowed))
    return hit


def _set_field(spec: VesselSpec, obj, key: str, field: str, raw, src: str, changed: list[str], warnings: list[str]) -> None:
    v = _norm_val(field, raw)
    if field == "orientation_deg" and v is not None:
        v = v % 360.0
    _validate_value(field, v, warnings)
    v = _check_enum(obj, field, v)
    if isinstance(obj, Nozzle) and field == "purpose_text" and v:
        cp, known = canonical_purpose(v)
        obj.purpose_text = v
        if obj.purpose is None or spec.sources.get(nkey(obj, "purpose")) == spec.sources.get(nkey(obj, "purpose_text")):
            obj.purpose = cp                                                  # 用途规范名是对用户原话的翻译，来源同原话（不随规则重算）
            spec.sources[nkey(obj, "purpose")] = src
            changed.append(nkey(obj, "purpose"))
        if not known:
            warnings.append(f"管口用途「{v}」不在受控词表里，按原样保存")
        spec.sources[key] = src
        changed.append(key)
        return
    if isinstance(obj, Nozzle) and field == "purpose" and v:
        cp, known = canonical_purpose(v)
        if obj.purpose_text is None:
            obj.purpose_text = v
        v = cp
        if not known:
            warnings.append(f"管口用途「{cp}」不在受控词表里，按原样保存")
    if isinstance(obj, dict):
        obj[field] = v
    else:
        setattr(obj, field, v)
    spec.sources[key] = src
    changed.append(key)


def _new_nozzle(spec: VesselSpec, nin: NozzleIn, src: str, changed: list[str], warnings: list[str]) -> Nozzle:
    n = Nozzle(uid=_next_uid(spec.nozzles, "n"))
    spec.nozzles.append(n)
    for f in NOZZLE_FIELDS:
        raw = getattr(nin, f, None)
        if raw is not None:
            _set_field(spec, n, nkey(n, f), f, raw, src, changed, warnings)
    return n


def apply_create(patch: VesselPatchIn, turn_id: str) -> tuple[VesselSpec, list[str], list[str]]:
    """新建：只合并用户说了的值（来源 user:<turn_id>）。返回 (spec, 改动的路径, 警告)。kind 缺了报 SPEC_INCOMPLETE。"""
    src = f"user:{turn_id}"
    if patch.kind is None:
        raise GeomError("SPEC_INCOMPLETE", "还缺：设备类型（立式/卧式容器）", "一次问完缺的项", pending=[pending_item("kind")] + missing_for_patch(patch))
    spec = VesselSpec(kind=patch.kind)
    spec.sources["kind"] = src
    changed: list[str] = ["kind"]
    warnings: list[str] = []
    for f in ("tag", "name"):
        v = getattr(patch, f)
        if v:
            setattr(spec, f, str(v))
            spec.sources[f] = src
            changed.append(f)
    if patch.design:
        for f in DESIGN_FIELDS:
            raw = getattr(patch.design, f)
            if raw is not None:
                _set_field(spec, spec.design, f"design.{f}", f, raw, src, changed, warnings)
    if patch.shell:
        for f in SHELL_FIELDS:
            raw = getattr(patch.shell, f)
            if raw is not None:
                _set_field(spec, spec.shell, f"shell.{f}", f, raw, src, changed, warnings)
    ensure_heads(spec)
    for hin in patch.heads or []:
        targets = [h for h in spec.heads if hin.position is None or h.position == hin.position]
        if not targets:
            raise GeomError("INVALID_ARGS", f"{'卧' if not spec.vertical else '立'}式容器没有 {hin.position} 封头", f"可用：{[h.position for h in spec.heads]}")
        for h in targets:
            for f in HEAD_FIELDS:
                raw = getattr(hin, f)
                if raw is not None:
                    _set_field(spec, h, hkey(h, f), f, raw, src, changed, warnings)
    for nin in patch.nozzles or []:
        for _ in range(int(nin.count or 1)):
            _new_nozzle(spec, nin, src, changed, warnings)
    for sin in patch.supports or []:
        s = Support(uid=_next_uid(spec.supports, "s"))
        spec.supports.append(s)
        for f in SUPPORT_FIELDS:
            raw = getattr(sin, f)
            if raw is not None:
                _set_field(spec, s, skey(s, f), f, raw, src, changed, warnings)
    if patch.drawing:
        for f in ("sheet", "scale"):
            v = getattr(patch.drawing, f)
            if v:
                spec.drawing[f] = _check_enum(spec.drawing, f, v)
                spec.sources[f"drawing.{f}"] = src
                changed.append(f"drawing.{f}")
    check_location_kind(spec)
    return spec, changed, warnings


def ensure_heads(spec: VesselSpec) -> None:
    """R-HEAD-01：立式 top+bottom，卧式 left+right（封头对象本身由规则建，字段由规则或用户填）。"""
    want = ("bottom", "top") if spec.vertical else ("left", "right")
    have = {h.position for h in spec.heads}
    for p in want:
        if p not in have:
            spec.heads.append(Head(position=p))
    spec.heads = sorted([h for h in spec.heads if h.position in want], key=lambda h: want.index(h.position))


def check_location_kind(spec: VesselSpec) -> None:
    bad = ("left_head", "right_head") if spec.vertical else ("top_head", "bottom_head")
    for n in spec.nozzles:
        if n.location in bad:
            word = {"left_head": "左封头", "right_head": "右封头", "top_head": "上封头", "bottom_head": "下封头"}[n.location]
            raise GeomError("INVALID_ARGS", f"{'立' if spec.vertical else '卧'}式容器没有{word}", "立式用 top_head/bottom_head，卧式用 left_head/right_head")


def apply_patch(spec: VesselSpec, ops: dict, turn_id: str) -> tuple[VesselSpec, list[str], list[str]]:
    """改参数：set / add / upsert_nozzles / remove_nozzles / upsert_supports / remove_supports。返回新的 spec（原对象不动）。"""
    spec = spec.model_copy(deep=True)
    src = f"user:{turn_id}"
    changed: list[str] = []
    warnings: list[str] = []
    for path, raw in (ops.get("set") or {}).items():
        _apply_set(spec, path, raw, src, changed, warnings)
    for path, raw in (ops.get("add") or {}).items():
        _apply_add(spec, path, raw, f"{src}:add", changed, warnings)
    for nin in ops.get("upsert_nozzles") or []:
        nin = nin if isinstance(nin, NozzleIn) else NozzleIn.model_validate(nin)
        if nin.mark and any((n.mark or "").lower() == nin.mark.lower() for n in spec.nozzles):
            n = next(n for n in spec.nozzles if (n.mark or "").lower() == nin.mark.lower())
            for f in NOZZLE_FIELDS:
                raw = getattr(nin, f)
                if raw is not None and f != "mark":
                    _set_field(spec, n, nkey(n, f), f, raw, src, changed, warnings)
        else:
            for _ in range(int(nin.count or 1)):
                _new_nozzle(spec, nin, src, changed, warnings)
    for sel in ops.get("remove_nozzles") or []:
        s = str(sel).strip()
        s = s[len("nozzles["):-1] if s.startswith("nozzles[") and s.endswith("]") else s
        for n in resolve_selector(spec, s):
            spec.nozzles = [x for x in spec.nozzles if x.uid != n.uid]
            for k in [k for k in spec.sources if k.startswith(f"nozzles[{n.uid}].")]:
                spec.sources.pop(k)
            changed.append(f"nozzles[{n.uid}]")
    for sin in ops.get("upsert_supports") or []:
        sin = sin if isinstance(sin, SupportIn) else SupportIn.model_validate(sin)
        hit = next((s for s in spec.supports if sin.kind is not None and s.kind == sin.kind), None)
        if hit is None and len(spec.supports) == 1 and sin.kind is None:
            hit = spec.supports[0]
        if hit is None:
            hit = Support(uid=_next_uid(spec.supports, "s"))
            spec.supports.append(hit)
        for f in SUPPORT_FIELDS:
            raw = getattr(sin, f)
            if raw is not None:
                _set_field(spec, hit, skey(hit, f), f, raw, src, changed, warnings)
    for sel in ops.get("remove_supports") or []:
        s = str(sel).strip()
        s = s[len("supports["):-1] if s.startswith("supports[") and s.endswith("]") else s
        for x in resolve_selector(spec, s, "supports"):
            spec.supports = [y for y in spec.supports if y.uid != x.uid]
            for k in [k for k in spec.sources if k.startswith(f"supports[{x.uid}].")]:
                spec.sources.pop(k)
            changed.append(f"supports[{x.uid}]")
    if ops.get("renumber"):
        for n in spec.nozzles:
            n.mark = None
            spec.sources.pop(nkey(n, "mark"), None)
        spec.drawing.pop("marks_assigned", None)
    check_location_kind(spec)
    return spec, changed, warnings


def _targets(spec: VesselSpec, path: str):
    """对外路径 → [(对象, 内部键, 字段)]。"""
    m = _SEG.match(path.strip())
    if not m or not path_allowed(path, spec.kind):
        raise _bad_path(path, spec.kind)
    root, sel, field = m.group("root"), m.group("sel"), m.group("field")
    if root in ("tag", "name"):
        return [(spec, root, root)]
    if root in ("design", "shell"):
        return [(getattr(spec, root), f"{root}.{field}", field)]
    if root == "drawing":
        return [(spec.drawing, f"drawing.{field}", field)]
    if root == "heads":
        hs = spec.heads if sel is None else resolve_selector(spec, sel, "heads")
        return [(h, hkey(h, field), field) for h in hs]
    if root == "nozzles":
        return [(n, nkey(n, field), field) for n in resolve_selector(spec, sel)]
    if root == "supports":
        if sel is None:
            if len(spec.supports) != 1:
                raise GeomError("AMBIGUOUS", f"有 {len(spec.supports)} 组支座，要改哪一组？", "用 supports[0] 或 supports[kind=lug] 指定", candidates=[str(i) for i in range(len(spec.supports))])
            ss = spec.supports
        else:
            ss = resolve_selector(spec, sel, "supports")
        return [(s, skey(s, field), field) for s in ss]
    raise _bad_path(path, spec.kind)


def _apply_set(spec: VesselSpec, path: str, raw, src: str, changed: list[str], warnings: list[str]) -> None:
    if path.strip() in ("kind",):
        raise GeomError("BAD_PATH", "设备类型（立式/卧式）不能用 patch 改", "要换类型请删除参数后重新 create")
    if path.strip() == "shell.length":
        raise GeomError("BAD_PATH", "shell.length 是逻辑路径，只能用在 add（加长/缩短）里", "改筒体长度写 shell.length_mm，改切线长写 shell.tangent_length_mm")
    for obj, key, field in _targets(spec, path):
        if field == "mark" and raw is not None and any((n.mark or "").lower() == str(raw).lower() and n is not obj for n in spec.nozzles):
            raise GeomError("SPEC_CONFLICT", f"管口符号 {raw} 已被占用", "换一个符号")
        if field == "length_mm" and isinstance(obj, type(spec.shell)) and spec.sources.get("shell.tangent_length_mm", "").startswith("user:"):
            obj.tangent_length_mm = None                                     # 用户改用筒体长度口径：原来的切线长作废
            spec.sources.pop("shell.tangent_length_mm", None)
        if field == "tangent_length_mm" and isinstance(obj, type(spec.shell)) and spec.sources.get("shell.length_mm", "").startswith("user:"):
            obj.length_mm = None
            spec.sources.pop("shell.length_mm", None)
        _set_field(spec, obj, key, field, raw, src, changed, warnings)


def _length_fields(spec: VesselSpec) -> list[str]:
    """逻辑路径 shell.length 落在哪个（些）口径上：用户给过的口径；两个都给过就两个都动（保持一致）；都不是用户给的取筒体长度。"""
    given = [f for f in ("length_mm", "tangent_length_mm") if spec.sources.get(f"shell.{f}", "").startswith("user:")]
    return given or ["length_mm"]


def _apply_add(spec: VesselSpec, path: str, raw, src: str, changed: list[str], warnings: list[str]) -> None:
    p = path.strip()
    paths = [f"shell.{f}" for f in _length_fields(spec)] if p == "shell.length" else [p]
    for p in paths:
        for obj, key, field in _targets(spec, p):
            if field in TEXT_FIELDS:
                raise GeomError("INVALID_ARGS", f"{p} 不是数值字段，不能用 add", "add 只用于“加长/增加/减少”这类相对改动")
            cur = obj.get(field) if isinstance(obj, dict) else getattr(obj, field)
            if cur is None:
                raise GeomError("MISSING_ARG", f"{display_path(spec, key)} 还没有值，不能在它上面加减", "请直接给出新的值")
            delta = to_number(raw, field) if field not in COUNT_FIELDS else int(to_number(raw, field))
            _set_field(spec, obj, key, field, cur + delta, src, changed, warnings)


# ------------------------------------------------------------------ 清除派生值、默认值、缺项
def reset_derived(spec: VesselSpec) -> None:
    for key, src in list(spec.sources.items()):
        if src.startswith(DERIVED) and src not in KEEP_RULES and key != "kind":
            set_key(spec, key, None, None)
    for s in spec.supports:
        if not any(k.startswith(f"supports[{s.uid}].positions") and v.startswith("user:") for k, v in spec.sources.items()):
            s.positions = None


def fill_defaults(spec: VesselSpec) -> dict:
    """按任务书 4.3 的顺序跑默认规则。返回 {"defaults": [{path, rule_id, value}], "warnings": [...], "notes": [...], "tables": [...]}。"""
    from . import rules
    reset_derived(spec)
    ctx = rules.RuleCtx(spec)
    for rid, fn in rules.RULES.items():
        fn(ctx)
    spec.warnings = list(dict.fromkeys(ctx.warnings))
    spec.drawing["tables_used"] = sorted(set(ctx.tables))
    spec.drawing["unverified_tables"] = sorted(set(ctx.unverified))
    return {"defaults": ctx.defaults, "warnings": spec.warnings, "notes": ctx.notes, "tables": spec.drawing["tables_used"]}


def pending_item(path: str, label: str | None = None, options: list[str] | None = None) -> dict:
    opts = {
        "kind": ["立式容器（耳座/支腿）", "卧式容器（鞍座）"],
        "shell.inner_diameter_mm": ["1000", "1600", "2000", "2400"],
        "shell.length": ["筒体长度 2400", "切线长 3000", "筒体长度 6000"],
        "shell.thickness_mm": ["6", "8", "10", "12"],
        "supports.kind": ["耳式支座", "支腿", "裙座"],
        "nozzles": ["进料 DN80、出料 DN80、人孔 DN500", "进料 DN50、出料 DN50、放空 DN25、液位计口 2×DN25"],
        "drawing_type": ["工艺流程图", "化工设备图（装配图）"],
    }
    return {"path": path, "label": label or LABELS.get(path, path), "options": (options or opts.get(path, []))[:4]}


def missing(spec: VesselSpec) -> list[dict]:
    """必问字段（任务书 4.2）还缺哪些：[{path, label, options}]。"""
    out: list[dict] = []
    sh = spec.shell
    if sh.inner_diameter_mm is None:
        out.append(pending_item("shell.inner_diameter_mm"))
    if sh.length_mm is None and sh.tangent_length_mm is None:
        out.append(pending_item("shell.length"))
    if sh.thickness_mm is None:
        out.append(pending_item("shell.thickness_mm"))
    for h in spec.heads:
        if h.kind in ("FLAT", "CONE") and h.thickness_mm is None:
            out.append(pending_item(f"heads[{h.position}].thickness_mm", f"{'上' if h.position == 'top' else '下' if h.position == 'bottom' else '左' if h.position == 'left' else '右'}封头（{h.kind}）的厚度（mm）", ["与筒体同厚", "20", "30"]))
    if spec.vertical and (not spec.supports or any(s.kind is None for s in spec.supports)):
        out.append(pending_item("supports.kind"))
    for s in spec.supports:
        if s.kind in ("leg", "skirt") and s.height_mm is None:
            out.append(pending_item(f"supports[{s.uid}].height_mm", f"{'支腿' if s.kind == 'leg' else '裙座'}高度（mm，从下环焊缝量到底板底面）", ["600", "800", "1000"]))
    if not spec.nozzles:
        out.append(pending_item("nozzles"))
    for n in spec.nozzles:
        if n.dn is None:
            out.append(pending_item(nkey(n, "dn"), f"{n.purpose or n.purpose_text or '管口'}的公称尺寸 DN", ["DN25", "DN50", "DN80", "DN100"] if n.kind != "manhole" else ["DN450", "DN500", "DN600"]))
    return out


def missing_for_patch(patch: VesselPatchIn) -> list[dict]:
    """kind 都没给时，按入参能看出来的缺项（给反问用）。"""
    out = []
    sh = patch.shell
    if sh is None or sh.inner_diameter_mm is None:
        out.append(pending_item("shell.inner_diameter_mm"))
    if sh is None or (sh.length_mm is None and sh.tangent_length_mm is None):
        out.append(pending_item("shell.length"))
    if sh is None or sh.thickness_mm is None:
        out.append(pending_item("shell.thickness_mm"))
    if not patch.nozzles:
        out.append(pending_item("nozzles"))
    return out


def question_text(pending: list[dict]) -> str:
    """反问文字由代码根据 pending 拼（mock 与 real 一样）。"""
    parts = []
    for i, p in enumerate(pending, 1):
        o = f"（例如 {' / '.join(p['options'][:3])}）" if p.get("options") else ""
        parts.append(f"{i}. {p['label']}{o}")
    return "画设备图还缺这些参数，请一次告诉我：" + "；".join(parts)


def summary(spec: VesselSpec) -> str:
    sh = spec.shell
    kind = {"vertical_vessel": "立式容器", "horizontal_vessel": "卧式容器"}.get(spec.kind, spec.kind)
    ln = f"L={fmt_value(sh.length_mm)}" if sh.length_mm is not None else "L=待定"
    return f"{kind} {spec.tag or ''} DN{fmt_value(sh.inner_diameter_mm)}×δ{fmt_value(sh.thickness_mm)}，{ln}，{len(spec.nozzles)} 个管口，{sum(int(s.count or 0) for s in spec.supports)} 个{'鞍座' if not spec.vertical else '支座'}".replace("  ", " ")


def copy_spec(spec: VesselSpec) -> VesselSpec:
    return VesselSpec.model_validate(copy.deepcopy(spec.model_dump()))


# ------------------------------------------------------------------ 字段分级（任务书 4.2；护栏、提示词目录、反问都以这里为准）
MUST_ASK = ("kind", "shell.inner_diameter_mm", "shell.length_mm", "shell.tangent_length_mm", "shell.thickness_mm", "nozzles.dn", "supports.kind", "supports.height_mm", "heads.thickness_mm(FLAT/CONE)")
COPY_FIELDS = tuple(f"design.{f}" for f in DESIGN_FIELDS) + ("shell.material", "heads.material", "nozzles.pipe_material", "supports.material", "tag", "name", "nozzles.purpose_text", "nozzles.purpose")
# 可默认字段 → 触发词：用户原话里（同一分句）出现这些词，才允许 Planner 写这个字段；否则护栏确定性地剔除并警告 E_DEFAULTABLE_FIELD
DEFAULTABLE_TRIGGERS: dict[str, str] = {
    "orientation_deg": r"方位|度|°|朝|转到|转至|转向|旋转|角",
    "projection_mm": r"伸出|外伸|伸长|高出|法兰面",
    "axial_mm": r"距|离|高度为|标高|位置|处",
    "radial_mm": r"距|离|偏心|径向|中心",
    "pn": r"PN|公称压力",
    "flange_type": r"SO|WN|PL|平焊|对焊|板式|带颈|法兰型式",
    "facing": r"RF|FF|MFM|TG|突面|凹凸|榫槽|全平面|密封面",
    "flange_std": r"HG/?T|标准",
    "straight_flange_mm": r"直边",
    "pipe_series": r"系列|英制|公制",
    "pipe_od_mm": r"[ΦφØ⌀]|外径",
    "pipe_thk_mm": r"壁厚|[×xX]",
    "std_part": r"HG/?T|型号|标准",
    "model": r"型号|号|[AB]\d",
    "count": r"个|只|座|组|台|件|数量",
    "sheet": r"A[0-4]|图幅|幅面",
    "scale": r"比例|[:：]",
    "mark": r"",
    "heads.thickness_mm": r"封头",
    "heads.kind": r"椭圆|碟形|半球|平盖|平封头|平板|锥|球冠|EHA|HHA|THA|SDH|封头型式",
    "location": r"封头|顶|底|筒体|筒身|侧面|上部|下部",
    "kind": r"人孔|手孔|视镜|视孔|液位计|液面计",
}
TEXT_TRIGGERS: dict[str, dict[str, str]] = {        # 翻译类字段：值 → 原话里必须出现的词
    "location": {"top_head": r"上封头|顶|上部|顶部", "bottom_head": r"下封头|底|下部|底部", "left_head": r"左封头|左端|左侧封头", "right_head": r"右封头|右端|右侧封头", "shell": r"筒体|筒身|侧面|侧壁|筒节"},
    "heads.kind": {"EHA": r"椭圆|EHA", "HHA": r"半球|HHA", "THA": r"碟形|THA", "SDH": r"球冠|SDH", "FLAT": r"平盖|平封头|平板", "CONE": r"锥"},
    "kind": {"manhole": r"人孔", "handhole": r"手孔", "sight_glass": r"视镜|视孔", "level_gauge": r"液位计|液面计|液位", "pipe": r""},
    "supports.kind": {"lug": r"耳座|耳式", "leg": r"支腿|腿", "skirt": r"裙座", "saddle": r"鞍座|鞍式", "ring": r"支承式|环形"},
    "shell.kind": {},
}


def catalog_line() -> str:
    """vessel_define 的工具目录行（不展开 VesselSpec，只列必问/照抄字段、选择器语法；可默认字段一行“说了才写”）。控制在 1000 字以内。"""
    keys = ("orientation_deg", "projection_mm", "pn", "flange_type", "heads.kind", "straight_flange_mm", "axial_mm")
    trig = "；".join(f"{k}←{'/'.join(DEFAULTABLE_TRIGGERS[k].split('|')[:3])}" for k in keys)
    return ("- vessel_define(op*:create|patch|delete, spec:{kind:vertical_vessel|horizontal_vessel, tag, name, shell:{inner_diameter_mm, length_mm|tangent_length_mm, thickness_mm, material}, "
            "heads:[{position?, kind}], nozzles:[{dn, purpose_text, kind:pipe|manhole|level_gauge, count, location:shell|top_head|bottom_head|left_head|right_head}], "
            "supports:[{kind:lug|leg|saddle, count, height_mm}], design:{design_pressure_mpa, design_temp_c, medium, …照抄}}, "
            "set:{路径:值}, add:{路径:增量}, upsert_nozzles, remove_nozzles:[符号], upsert_supports, remove_supports)："
            "设备图参数，程序补齐其余参数并生成整张设备图，缺必问项自动反问。数值可写 {\"value\":1.6,\"unit\":\"m\"} 或 \"600kPa\"。"
            "路径如 shell.length_mm、design.design_pressure_mpa、heads.kind、supports[0].count；add 用 shell.length。"
            "管口选择器：nozzles[b]、nozzles[purpose=进料口]、nozzles[location=top_head]。"
            f"【可默认字段只有用户明确说了才写（字段←触发词）：{trig}；管口符号、支座型号、耳座高度同理】")
