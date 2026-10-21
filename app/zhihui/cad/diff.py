"""改图前后的差异：几何签名比对 + 差异叠图。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from zhihui.ir import Drawing


def _h(obj) -> str:
    return hashlib.sha1(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def signatures(d: Drawing) -> dict[str, str]:
    """每个对象一个几何签名：设备看位号/图元/坐标/参数，连接看端点和路径。"""
    out = {}
    for e in d.equipment:
        out[e.id] = _h([e.tag, e.symbol_id, e.name, e.params, e.x, e.y])
    for c in d.connections:
        out[c.id] = _h([c.from_equip, c.from_port, c.to_equip, c.to_port, c.line_type, c.medium, c.path])
    for a in d.annotations:
        out[a.id] = _h([a.anchor_ref, a.text, a.x, a.y])
    for h in d.hatches:
        out[h.id] = _h([h.pattern, h.paper_spacing, h.angle, h.style, h.anchor_ref, h.pick_rel, h.boundary_refs, h.rings])
    return out


def changed_ids(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def diff_report(base: Drawing, cur: Drawing) -> dict:
    b_eq = {e.id: e for e in base.equipment}
    c_eq = {e.id: e for e in cur.equipment}
    rep = {"added": [], "removed": [], "moved": [], "modified": [], "connections": {"added": [], "removed": [], "rerouted": [], "modified": []}}
    for eid in sorted(c_eq.keys() - b_eq.keys()):
        rep["added"].append({"id": eid, "tag": c_eq[eid].tag, "symbol_id": c_eq[eid].symbol_id})
    for eid in sorted(b_eq.keys() - c_eq.keys()):
        rep["removed"].append({"id": eid, "tag": b_eq[eid].tag})
    for eid in sorted(b_eq.keys() & c_eq.keys()):
        a, b = b_eq[eid], c_eq[eid]
        if (a.x, a.y) != (b.x, b.y) and a.placed and b.placed:
            rep["moved"].append({"id": eid, "tag": b.tag, "dx": round(b.x - a.x, 1), "dy": round(b.y - a.y, 1)})
        changes = {k: [getattr(a, k), getattr(b, k)] for k in ("tag", "symbol_id", "name", "params") if getattr(a, k) != getattr(b, k)}
        if changes:
            rep["modified"].append({"id": eid, "tag": b.tag, "changes": changes})
    b_c = {c.id: c for c in base.connections}
    c_c = {c.id: c for c in cur.connections}
    tag = {**{e.id: e.tag for e in base.equipment}, **{e.id: e.tag for e in cur.equipment}}
    desc = lambda c: f"{tag.get(c.from_equip, c.from_equip)}.{c.from_port}→{tag.get(c.to_equip, c.to_equip)}.{c.to_port}"  # noqa: E731
    for cid in sorted(c_c.keys() - b_c.keys()):
        rep["connections"]["added"].append({"id": cid, "desc": desc(c_c[cid])})
    for cid in sorted(b_c.keys() - c_c.keys()):
        rep["connections"]["removed"].append({"id": cid, "desc": desc(b_c[cid])})
    for cid in sorted(b_c.keys() & c_c.keys()):
        a, b = b_c[cid], c_c[cid]
        if (a.from_equip, a.from_port, a.to_equip, a.to_port, a.line_type, a.medium) != (b.from_equip, b.from_port, b.to_equip, b.to_port, b.line_type, b.medium):
            rep["connections"]["modified"].append({"id": cid, "before": desc(a), "after": desc(b)})
        elif a.path != b.path:
            rep["connections"]["rerouted"].append({"id": cid, "desc": desc(b)})
    if base.hatches or cur.hatches:  # 填充：增删改、失去关联（没有填充的图不加这一节）
        bh, ch = {h.id: h for h in base.hatches}, {h.id: h for h in cur.hatches}
        keys = ("pattern", "paper_spacing", "angle", "style", "gap_tol")
        rep["hatches"] = {
            "added": [{"id": i, "pattern": ch[i].pattern} for i in sorted(ch.keys() - bh.keys())],
            "removed": [{"id": i, "pattern": bh[i].pattern} for i in sorted(bh.keys() - ch.keys())],
            "modified": [{"id": i, "changes": {k: [getattr(bh[i], k), getattr(ch[i], k)] for k in keys if getattr(bh[i], k) != getattr(ch[i], k)}}
                         for i in sorted(bh.keys() & ch.keys()) if any(getattr(bh[i], k) != getattr(ch[i], k) for k in keys)],
            "moved": [i for i in sorted(bh.keys() & ch.keys()) if bh[i].rings != ch[i].rings],
            "disassociated": [i for i in sorted(ch) if ch[i].disassociated],
        }
    # 标题栏文字不属于几何对象，但改了就要在差异报告里看到前后值
    tb = []
    for k in sorted(set(base.title_block) | set(cur.title_block)):
        a, b = base.title_block.get(k, ""), cur.title_block.get(k, "")
        if a != b:
            tb.append({"field": k, "before": a, "after": b})
    if base.title != cur.title:
        tb.append({"field": "title", "before": base.title, "after": cur.title})
    rep["title_block"] = tb
    sig_b, sig_c = signatures(base), signatures(cur)
    rep["unchanged_count"] = sum(1 for k in sig_b if sig_c.get(k) == sig_b[k])
    rep["changed_ids"] = changed_ids(sig_b, sig_c)
    return rep


def render_overlay(base: Drawing, cur: Drawing, out_path: str | Path) -> Path:
    """差异叠图：灰色 = 未变，浅红 = 改前，绿色 = 改后。直接从 IR 画，不依赖 DXF。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Arc, Circle, Rectangle

    from .symbols import get_symbol, symbol_box

    changed = set(changed_ids(signatures(base), signatures(cur)))
    w, h = cur.frame_size()
    fig = plt.figure(figsize=(16.5, 16.5 * h / w))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, w)
    ax.set_ylim(0, h)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.add_patch(Rectangle((0, 0), w, h, fill=False, ec="#999", lw=0.8))

    def draw(d: Drawing, color_changed: str, only_changed: bool, lw: float, ls: str = "-"):
        for c in d.connections:
            if not c.path or (only_changed and c.id not in changed):
                continue
            col = color_changed if c.id in changed else "#b8b8b8"
            xs, ys = zip(*c.path)
            ax.plot(xs, ys, color=col, lw=lw, ls=ls)
        for e in d.equipment:
            if not e.placed or (only_changed and e.id not in changed):
                continue
            col = color_changed if e.id in changed else "#b8b8b8"
            k = d.unit
            for g in get_symbol(e.symbol_id).graphics:
                if g["type"] == "circle":
                    ax.add_patch(Circle((e.x + g["c"][0] * k, e.y + g["c"][1] * k), g["r"] * k, fill=False, ec=col, lw=lw, ls=ls))
                elif g["type"] == "arc":
                    ax.add_patch(Arc((e.x + g["c"][0] * k, e.y + g["c"][1] * k), 2 * g["r"] * k, 2 * g["r"] * k, theta1=g["start"], theta2=g["end"], ec=col, lw=lw, ls=ls))
                elif g["type"] == "ellipse":
                    ax.add_patch(Arc((e.x + g["c"][0] * k, e.y + g["c"][1] * k), 2 * g["rx"] * k, 2 * g["ry"] * k, theta1=g["start"], theta2=g["end"], ec=col, lw=lw, ls=ls))
                elif g["type"] == "hatch":  # 填充不画进差异叠图
                    continue
                elif g["type"] == "text":
                    ax.text(e.x + g["p"][0] * k, e.y + g["p"][1] * k, g["text"], color=col, ha="center", va="center", fontsize=6)
                else:
                    pts = [(e.x + x * k, e.y + y * k) for x, y in g["points"]]
                    if g.get("closed"):
                        pts.append(pts[0])
                    xs, ys = zip(*pts)
                    ax.plot(xs, ys, color=col, lw=lw, ls=ls)
            x0, y0, x1, y1 = symbol_box(e, d.unit)
            ax.text((x0 + x1) / 2, y0 - 2 * d.unit, e.tag, color=col, ha="center", va="top", fontsize=8)

    draw(cur, "#1a9850", only_changed=False, lw=1.4)
    draw(base, "#e57373", only_changed=True, lw=1.0, ls="--")
    ax.legend(
        handles=[
            Line2D([], [], color="#b8b8b8", lw=1.4, label="unchanged"),
            Line2D([], [], color="#e57373", lw=1.0, ls="--", label="before"),
            Line2D([], [], color="#1a9850", lw=1.4, label="after"),
        ],
        loc="upper right",
        frameon=False,
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110, facecolor="white")
    plt.close(fig)
    return out_path
