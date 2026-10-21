"""图幅分区和对中符号（文章第 1 页；GB/T 14689-2008 第 4.4、4.5 条，原文未取得，规则按常见做法：verified=false）。

- 分区数为偶数，每格 25～75 mm；沿上下两边自左向右写阿拉伯数字 1、2、3…，沿左右两边自上而下写大写字母 A、B、C…；
  分区代号字母在前、数字在后，如「C3」。分区线画在图框和纸边之间的留边里（细实线），代号写在留边正中。
- 对中符号：四条边的中点处，从纸边线画到图框线内约 5 mm 的粗实线。
- 自动：A2 及以上（含加长幅面）画分区；A3、A4 不画。find_entities 可按分区查对象（不画分区时也能算）。
"""

from __future__ import annotations

import string

from zhihui.ir import Drawing

from .standards import LY

ZONE_MIN, ZONE_MAX, ZONE_PREF = 25.0, 75.0, 50.0
CENTER_MARK_IN = 5.0


def enabled(d: Drawing) -> bool:
    if d.zones is not None:
        return bool(d.zones) and d.frame
    from zhihui.ir.models import BASE_FORMAT

    base = BASE_FORMAT.get(d.paper_size, d.paper_size)
    return d.frame and base in ("A0", "A1", "A2")


def _count(length_mm: float) -> int:
    """偶数个分区，每格 25～75 mm，尽量接近 50 mm。"""
    best = None
    for n in range(2, 60, 2):
        w = length_mm / n
        if ZONE_MIN <= w <= ZONE_MAX:
            score = abs(w - ZONE_PREF)
            if best is None or score < best[0]:
                best = (score, n)
    return best[1] if best else max(2, int(length_mm // ZONE_PREF) // 2 * 2)


def grid(d: Drawing) -> tuple[list[float], list[float]]:
    """分区边界（模型坐标）：xs 自左向右，ys 自上而下。"""
    from .symbols import inner_frame

    x0, y0, x1, y1 = inner_frame(d)
    s = d.scale
    nx, ny = _count((x1 - x0) / s), _count((y1 - y0) / s)
    xs = [x0 + (x1 - x0) * i / nx for i in range(nx + 1)]
    ys = [y1 - (y1 - y0) * j / ny for j in range(ny + 1)]
    return xs, ys


def zone_of(d: Drawing, x: float, y: float) -> str:
    """点所在的分区代号，如 C3（字母为行、自上而下，数字为列、自左向右）。"""
    xs, ys = grid(d)
    col = next((i for i in range(len(xs) - 1) if xs[i] <= x <= xs[i + 1]), None)
    row = next((j for j in range(len(ys) - 1) if ys[j + 1] <= y <= ys[j]), None)
    if col is None or row is None:
        return ""
    return f"{string.ascii_uppercase[row % 26]}{col + 1}"


def draw(msp, d: Drawing) -> None:
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import APPID, _text
    from .standards import text_h
    from .symbols import inner_frame

    w, h = d.frame_size()
    fx0, fy0, fx1, fy1 = inner_frame(d)
    s = d.scale
    xd = [(1000, "zone")]
    th = text_h(d, "small", "A")
    if enabled(d):
        xs, ys = grid(d)
        mx, my = (fx0 + fx1) / 2, (fy0 + fy1) / 2
        for x in xs[1:-1]:  # 上、下留边里的分区线（落在中点的那条由对中符号代替，不重复画）
            if abs(x - mx) < 1e-6:
                continue
            for a, b in ((fy1, h), (0.0, fy0)):
                msp.add_line((x, a), (x, b), dxfattribs={"layer": LY["zone"]}).set_xdata(APPID, xd)
        for y in ys[1:-1]:  # 左、右留边里的分区线
            if abs(y - my) < 1e-6:
                continue
            for a, b in ((0.0, fx0), (fx1, w)):
                msp.add_line((a, y), (b, y), dxfattribs={"layer": LY["zone"]}).set_xdata(APPID, xd)
        for i in range(len(xs) - 1):
            cx = (xs[i] + xs[i + 1]) / 2
            for cy in ((fy1 + h) / 2, fy0 / 2):
                _text(msp, str(i + 1), (cx, cy), th, LY["zone"], TA.MIDDLE_CENTER).set_xdata(APPID, xd)
        for j in range(len(ys) - 1):
            cy = (ys[j] + ys[j + 1]) / 2
            for cx in (fx0 / 2, (fx1 + w) / 2):
                _text(msp, string.ascii_uppercase[j % 26], (cx, cy), th, LY["zone"], TA.MIDDLE_CENTER).set_xdata(APPID, xd)
    if enabled(d) or d.zones:
        # 对中符号：四边中点，纸边线 → 图框内 5 mm，粗实线
        k = CENTER_MARK_IN * s
        mx, my = (fx0 + fx1) / 2, (fy0 + fy1) / 2
        for a, b in (((mx, h), (mx, fy1 - k)), ((mx, 0.0), (mx, fy0 + k)), ((0.0, my), (fx0 + k, my)), ((w, my), (fx1 - k, my))):
            msp.add_line(a, b, dxfattribs={"layer": LY["border"]}).set_xdata(APPID, [(1000, "center_mark")])
