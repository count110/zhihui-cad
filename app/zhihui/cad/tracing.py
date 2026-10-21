"""伴热管：带符号的复合线（GB/T 17450-1998 第 3.3.4 条 a：在实线上规则分布几何图形要素、实线不间断，图 5）。

文章第 5 页、3.2.1 节：有伴热的设备和管道要在相应部位画出一段伴热管，必要时标注伴热类型和介质代号（表 2-10）。
画法：在管道一侧 1.2 mm 处画一条细实线，每隔 6 mm 画一个朝向管道的短钩（「L」形，长 1.2 mm），首尾各让开 3 mm；
伴热代号只能来自用户（Connection.tracing），图例里列出含义。
"""

from __future__ import annotations

import math

from zhihui.ir import Drawing

from .standards import LY

OFFSET, PITCH, TICK, END_GAP = 1.2, 6.0, 1.2, 3.0


def draw(msp, d: Drawing, c) -> None:
    if not c.tracing or not c.path or len(c.path) < 2:
        return
    draw_path(msp, d, c.path, [(1000, "tracing"), (1000, c.id), (1000, c.tracing)])


def draw_path(msp, d: Drawing, path, xd) -> None:
    """沿折线 path 的左侧画伴热复合线（设备上的一段伴热管也用它）。"""
    from .dxf_io import APPID

    s = d.scale
    for a, b in zip(path, path[1:]):
        L = math.dist(a, b)
        if L < 2 * END_GAP * s + PITCH * s:
            continue
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        nx, ny = -uy, ux  # 走向的左侧
        o = OFFSET * s
        p0 = (a[0] + ux * END_GAP * s + nx * o, a[1] + uy * END_GAP * s + ny * o)
        p1 = (b[0] - ux * END_GAP * s + nx * o, b[1] - uy * END_GAP * s + ny * o)
        msp.add_line(p0, p1, dxfattribs={"layer": LY["flow_arrow"]}).set_xdata(APPID, xd)
        t = END_GAP * s + PITCH * s / 2
        while t < L - END_GAP * s:
            q = (a[0] + ux * t + nx * o, a[1] + uy * t + ny * o)  # 短钩：从伴热线垂直指向管道，再顺走向折一小段
            r = (q[0] - nx * TICK * s * 0.6, q[1] - ny * TICK * s * 0.6)
            msp.add_lwpolyline([q, r, (r[0] + ux * TICK * s * 0.6, r[1] + uy * TICK * s * 0.6)], dxfattribs={"layer": LY["flow_arrow"]}).set_xdata(APPID, xd)
            t += PITCH * s
