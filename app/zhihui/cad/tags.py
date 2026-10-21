"""位号自动编号。

两种格式（create_drawing 的 tag_style）：
- hg（化工流程图，文章图 2-4）：设备分类号 + 车间或工段号（两位，用户给）+ 设备序号（两位）+ 相同设备尾号，如 V0408A。
  没给工段号时退回 legacy 格式（工段号只能来自用户）。
- legacy：类别代号-1xx（P-101），旧用例保持不变。

用户没给位号的设备，登记时先给一个临时位号，记在 Drawing.auto_tags；auto_layout 时按流程顺序（schedule.flow_order）在这批临时序号里重排。
hg 格式重排一次后就定下来（移出 auto_tags）：同一台设备在三个设计阶段位号不变；删除设备后它的序号留空、不再复用
（Drawing.used_seqs 记每类用过的最大序号），新增设备接在同类原有序号后面。用户给的位号一律不动，但会登记它占用的序号。

相同设备（文章图 2-4 的尾号 A、B、C…）：
- params.units = n：n 台相同设备只画一台，位号带全部尾号（V0605AB，文章图 3-1）；
- place_equipment 的 same_as：分开画的相同设备，和那一台同序号、尾号顺延（V0605A、V0605B，文章图 3-4）。
"""

from __future__ import annotations

import re
import string

from zhihui.ir import Drawing

from .symbols import get_symbol

HG_RE = re.compile(r"^([A-Z])(\d{2})(\d{2})([A-Z]*)$")


def prefix(symbol_id: str) -> str:
    return (get_symbol(symbol_id).category or "X")[:1].upper()


def hg_mode(d: Drawing) -> bool:
    return d.tag_style == "hg" and bool(d.section_no)


def suffix_for(e) -> str:
    """尾号：same_as 组里的第几台（A、B…）；units = n 时写全部尾号（AB、ABC）。"""
    n = int(e.params.get("units", 1) or 1) if str(e.params.get("units", "")).isdigit() else 1
    k = e.params.get("_unit_no")
    if isinstance(k, int):
        return string.ascii_uppercase[k]
    return string.ascii_uppercase[:n] if n > 1 else ""


def parse_hg(tag: str) -> tuple[str, str, int, str] | None:
    m = HG_RE.match(tag.strip().upper())
    return (m.group(1), m.group(2), int(m.group(3)), m.group(4)) if m else None


def register(d: Drawing, tag: str) -> None:
    """用户给的 hg 位号也占用序号（以后自动编号不会撞上）。"""
    p = parse_hg(tag)
    if p and p[1] == d.section_no:
        d.used_seqs[p[0]] = max(d.used_seqs.get(p[0], 0), p[2])


def _hg_taken(d: Drawing, cls: str) -> set[int]:
    out = set()
    for e in d.equipment:
        p = parse_hg(e.tag)
        if p and p[0] == cls and p[1] == d.section_no:
            out.add(p[2])
    return out


def next_free(d: Drawing, pfx: str, taken: set[str] | None = None) -> str:
    if hg_mode(d):
        seq = max([d.used_seqs.get(pfx, 0), *_hg_taken(d, pfx)]) + 1
        d.used_seqs[pfx] = seq
        return f"{pfx}{d.section_no}{seq:02d}"
    taken = {e.tag.upper() for e in d.equipment} if taken is None else taken
    n = 101
    while f"{pfx}-{n}" in taken:
        n += 1
    return f"{pfx}-{n}"


def base_of(tag: str) -> str:
    """去掉尾号的位号：V0605B → V0605，P-101A → P-101。"""
    p = parse_hg(tag)
    if p:
        return f"{p[0]}{p[1]}{p[2]:02d}"
    return re.sub(r"(?<=\d)[A-Z]+$", "", tag.strip())


def same_as(d: Drawing, e, ref) -> None:
    """e 和 ref 是相同设备：同序号，尾号按登记顺序 A、B、C…；ref 原来没有尾号的补上 A。"""
    group = [o for o in d.equipment if o.params.get("_same") == ref.id or o.id == ref.id]
    if ref.params.get("_unit_no") is None:
        ref.params["_unit_no"] = 0
        ref.params["_same"] = ref.id
        ref.tag = base_of(ref.tag) + "A"
    e.params["_same"] = ref.id
    e.params["_unit_no"] = len(group)
    e.tag = base_of(ref.tag) + suffix_for(e)


def renumber(d: Drawing) -> dict[str, str]:
    """按流程顺序重编自动位号；返回 {旧位号: 新位号}（没变的不列）。"""
    from .schedule import flow_order

    auto = set(d.auto_tags)
    if not auto:
        return {}
    renamed = {}
    if hg_mode(d):
        # 只在这批临时序号里按流程顺序重排，然后定下来（不再重编、不复用）
        by_cls: dict[str, list] = {}
        for e in flow_order(d):
            if e.id in auto and not (e.params.get("_same") and e.params.get("_same") != e.id):
                by_cls.setdefault(prefix(e.symbol_id), []).append(e)
        for cls, eqs in by_cls.items():
            seqs = sorted(p[2] for p in (parse_hg(e.tag) for e in eqs) if p)
            for e, seq in zip(eqs, seqs):
                new = f"{cls}{d.section_no}{seq:02d}{suffix_for(e)}"
                for o in d.equipment:  # 相同设备跟着改
                    if o.id != e.id and o.params.get("_same") == e.id:
                        t = f"{cls}{d.section_no}{seq:02d}{suffix_for(o)}"
                        if t != o.tag:
                            renamed[o.tag] = t
                            o.tag = t
                if new != e.tag:
                    renamed[e.tag] = new
                    e.tag = new
        d.auto_tags = [i for i in d.auto_tags if i not in auto]
        return renamed
    taken = {e.tag.upper() for e in d.equipment if e.id not in auto}
    for e in flow_order(d):
        if e.id not in auto or (e.params.get("_same") and e.params.get("_same") != e.id):
            continue
        new = next_free(d, prefix(e.symbol_id), taken)
        taken.add(new)
        sfx = suffix_for(e)
        for o in d.equipment:
            if o.id != e.id and o.params.get("_same") == e.id:
                t = new + suffix_for(o)
                if t != o.tag:
                    renamed[o.tag] = t
                    o.tag = t
        if new + sfx != e.tag:
            renamed[e.tag] = new + sfx
            e.tag = new + sfx
    return renamed
