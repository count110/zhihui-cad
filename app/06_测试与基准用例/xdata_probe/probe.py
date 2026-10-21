"""XDATA 实测（10.17 闸门）：CAD 软件打开 → 编辑 → 另存后，语义标签还在不在？

用法：
  python probe.py make                 生成 out/probe.dxf（垃圾焚烧示例图）
  python probe.py check <另存后的.dxf>  逐项检查语义标签，并给出结论
"""

from __future__ import annotations

import sys
from pathlib import Path

import ezdxf

OUT = Path(__file__).with_name("out")


def make() -> None:
    from zhihui.agent.llm import DEMO_PLAN
    from zhihui.cad.dxf_io import write_dxf
    from zhihui.cad.layout import auto_layout
    from zhihui.ir import Connection, Drawing, Equipment

    d = Drawing(id="dwg_probe", title="XDATA 实测图")
    for e in DEMO_PLAN["equipment"]:
        d.equipment.append(Equipment(id=d.new_id("E"), **e))
    for c in DEMO_PLAN["connections"]:
        (a, ap), (b, bp) = c["from_port"].split("."), c["to_port"].split(".")
        d.connections.append(Connection(id=d.new_id("C"), from_equip=d.get_equipment(a).id, from_port=ap, to_equip=d.get_equipment(b).id, to_port=bp, medium=c.get("medium", "")))
    auto_layout(d)
    OUT.mkdir(exist_ok=True)
    write_dxf(d, OUT / "probe.dxf")
    print("已生成", OUT / "probe.dxf", "（7 台设备、6 条管线）")


def check(path: str) -> int:
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    inserts = [r for r in msp.query("INSERT") if r.dxf.name.startswith("ZH_")]
    pipes = [p for p in msp.query("LWPOLYLINE") if p.dxf.layer.startswith("PIPE")]

    def has_xdata(e) -> bool:
        try:
            return bool(e.get_xdata("ZHIHUI"))
        except Exception:
            return False

    hidden = sum(1 for r in inserts if {"SYMBOL", "EID"} <= {a.dxf.tag for a in r.attribs})
    root = doc.rootdict.get("ZHIHUI")
    rows = [
        ("设备块参照 ZH_*", f"{len(inserts)} / 7", len(inserts) == 7),
        ("设备 INSERT 的 XDATA", f"{sum(map(has_xdata, inserts))} / {len(inserts)}", inserts and all(map(has_xdata, inserts))),
        ("设备不可见属性 SYMBOL/EID", f"{hidden} / {len(inserts)}", inserts and hidden == len(inserts)),
        ("管线多段线（PIPE_* 图层）", f"{len(pipes)} / 6", len(pipes) == 6),
        ("管线 XDATA", f"{sum(map(has_xdata, pipes))} / {len(pipes)}", pipes and all(map(has_xdata, pipes))),
        ("根字典 ZHIHUI / META", "存在" if root is not None and "META" in root else "丢失", root is not None and "META" in root),
    ]
    for name, val, ok in rows:
        print(f"{'✓' if ok else '✗'} {name:<26} {val}")
    try:
        from zhihui.cad.dxf_io import read_dxf

        _, rep = read_dxf(path)
        print("拓扑一致：", rep.topology_consistent, "| 需重连管线：", rep.stale_pipes, "| 推断连接：", rep.inferred_connections)
    except Exception as ex:
        print("read_dxf 失败：", type(ex).__name__, ex)
    if all(ok for *_, ok in rows):
        print("结论：语义标签完整保留 → 方案 A（XDATA）可用")
        return 0
    if all(ok for name, _, ok in rows if "XDATA" not in name):
        print("结论：XDATA 丢失但块属性在 → 切换方案 B：拓扑改存块属性 / 按端点距离重建（见方案书 7.2）")
        return 1
    print("结论：语义标签严重丢失，记录软件名称与版本，在群里讨论")
    return 2


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "make":
        make()
    elif len(sys.argv) >= 3 and sys.argv[1] == "check":
        sys.exit(check(sys.argv[2]))
    else:
        print(__doc__)
