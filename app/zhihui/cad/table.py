"""通用表格引擎（《CATIA V5-6 R2014 工程图教程》第 6、7 章；GB/T 10609.1 标题栏、GB/T 10609.2 明细栏）。

标题栏、明细栏、设备一览表、管道一览表都用它画：
- 列宽、行高（图纸 mm），单元格可跨行跨列（合并单元格），对齐方式 center / left / right；
- 字高按角色从标准配置取（GB/T 14691 字高系列，汉字 ≥ 3.5），放不下按系列降档；
- 外框粗实线、内部细实线（GB/T 10609.1 第 4 条）；与图框线重合的边不重复画（GB/T 4457.4 图线重合只画一条）；
- 只在两侧属于不同单元格的格线处画线，合并单元格内部不画线；同一直线上相邻的线段并成一条。

坐标：表格内部用图纸 mm，原点在左下角，y 向上；画到模型空间时乘出图比例。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from zhihui.ir import Drawing

from .standards import LY, fit_h, text_width

APPID = "ZHIHUI"
PAD = 1.5  # 文字离格线的距离（图纸 mm）


@dataclass
class Cell:
    r: int  # 行号，从上往下 0 起
    c: int  # 列号，从左往右 0 起
    text: str = ""
    rs: int = 1  # 跨行
    cs: int = 1  # 跨列
    role: str = "table_cell"
    align: str = "center"  # center / left / right
    placeholder: str = ""  # text 为空时显示的格子说明（如「（设计单位名称）」）
    key: str = ""  # 字段名（标题栏）或列名（一览表），给检查和读回用


@dataclass
class Table:
    cols: list[float]  # 列宽，从左往右
    rows: list[float]  # 行高，从上往下
    cells: list[Cell] = field(default_factory=list)
    skip_edges: tuple[str, ...] = ()  # 与图框线重合不画的外边：left / right / top / bottom
    kind: str = "table"

    @property
    def width(self) -> float:
        return sum(self.cols)

    @property
    def height(self) -> float:
        return sum(self.rows)

    def xs(self) -> list[float]:
        out = [0.0]
        for w in self.cols:
            out.append(out[-1] + w)
        return out

    def ys(self) -> list[float]:
        """行的上边线 y（从表格底边量，向上为正）：ys[0] = 顶边，ys[-1] = 0。"""
        out = [self.height]
        for h in self.rows:
            out.append(out[-1] - h)
        return out

    def owner(self) -> dict[tuple[int, int], int]:
        """每个网格格子属于哪个单元格（cells 的下标）；没有定义的格子各自独立（下标为负）。"""
        own: dict[tuple[int, int], int] = {}
        for i, cl in enumerate(self.cells):
            if cl.r < 0 or cl.c < 0 or cl.r + cl.rs > len(self.rows) or cl.c + cl.cs > len(self.cols):
                raise ValueError(f"单元格 ({cl.r},{cl.c}) 跨出表格")
            for r in range(cl.r, cl.r + cl.rs):
                for c in range(cl.c, cl.c + cl.cs):
                    if (r, c) in own:
                        raise ValueError(f"单元格 ({cl.r},{cl.c}) 与 {self.cells[own[(r, c)]].r, self.cells[own[(r, c)]].c} 重叠")
                    own[(r, c)] = i
        k = -1
        for r in range(len(self.rows)):
            for c in range(len(self.cols)):
                if (r, c) not in own:
                    own[(r, c)] = k
                    k -= 1
        return own

    def box(self, cl: Cell) -> tuple[float, float, float, float]:
        xs, ys = self.xs(), self.ys()
        return xs[cl.c], ys[cl.r + cl.rs], xs[cl.c + cl.cs], ys[cl.r]

    def segments(self) -> list[tuple[tuple[float, float], tuple[float, float], bool]]:
        """要画的格线：[(起点, 终点, 是否外框)]，同一直线上相邻的段已并成一条。"""
        own = self.owner()
        xs, ys = self.xs(), self.ys()
        nr, nc = len(self.rows), len(self.cols)
        raw: list[tuple[str, float, float, float, bool]] = []  # (方向, 定值, 起, 止, 外框)
        for r in range(nr + 1):  # 横线：第 r 条在第 r-1 行和第 r 行之间
            for c in range(nc):
                outer = r in (0, nr)
                if outer:
                    if ("top" if r == 0 else "bottom") in self.skip_edges:
                        continue
                elif own[(r - 1, c)] == own[(r, c)]:
                    continue
                raw.append(("h", ys[r], xs[c], xs[c + 1], outer))
        for c in range(nc + 1):
            for r in range(nr):
                outer = c in (0, nc)
                if outer:
                    if ("left" if c == 0 else "right") in self.skip_edges:
                        continue
                elif own[(r, c - 1)] == own[(r, c)]:
                    continue
                raw.append(("v", xs[c], ys[r + 1], ys[r], outer))
        raw.sort()
        merged: list[list] = []
        for d, k, a, b, outer in raw:
            last = merged[-1] if merged else None
            if last and last[0] == d and abs(last[1] - k) < 1e-9 and last[4] == outer and abs(last[3] - a) < 1e-9:
                last[3] = b
            else:
                merged.append([d, k, a, b, outer])
        out = []
        for d, k, a, b, outer in merged:
            out.append((((a, k), (b, k)) if d == "h" else ((k, a), (k, b))) + (outer,))
        return out


def draw(msp, d: Drawing, t: Table, origin: tuple[float, float], xd_tag: str | None = None,
         inner_layer: str | None = None, outer_layer: str | None = None) -> None:
    """把表格画到模型空间；origin 为表格左下角（模型坐标）。"""
    from ezdxf.enums import TextEntityAlignment as TA

    from .dxf_io import _text

    s = d.scale
    x0, y0 = origin
    tag = xd_tag or t.kind
    xd = [(1000, "table"), (1000, tag)]
    inner = {"layer": inner_layer or LY["table"]}
    outer = {"layer": outer_layer or LY["border"]}
    for (a, b, is_outer) in t.segments():
        ln = msp.add_line((x0 + a[0] * s, y0 + a[1] * s), (x0 + b[0] * s, y0 + b[1] * s), dxfattribs=outer if is_outer else inner)
        ln.set_xdata(APPID, xd)
    for cl in t.cells:
        text = cl.text or cl.placeholder
        if not text:
            continue
        bx0, by0, bx1, by1 = t.box(cl)
        # 宽度和行高都要放得下：行高的限制换算成「这么高的字有多宽」，交给 fit_h 按字高系列降档
        max_w = min((bx1 - bx0 - 2 * PAD) * s, text_width(text, (by1 - by0 - 1.6) * s))
        h = fit_h(d, cl.role, text, max_w)
        yc = y0 + (by0 + by1) / 2 * s
        if cl.align == "left":
            pos, al = (x0 + (bx0 + PAD) * s, yc), TA.MIDDLE_LEFT
        elif cl.align == "right":
            pos, al = (x0 + (bx1 - PAD) * s, yc), TA.MIDDLE_RIGHT
        else:
            pos, al = (x0 + (bx0 + bx1) / 2 * s, yc), TA.MIDDLE_CENTER
        e = _text(msp, text, pos, h, inner["layer"], al)
        e.set_xdata(APPID, xd + [(1000, cl.key), (1000, "placeholder" if not cl.text else "value")])


# ---------------------------------------------------------------- 模板（标题栏）

def from_template(tpl: dict, values: dict[str, str]) -> Table:
    """标题栏模板 → Table。模板里的单元格用图纸 mm 的矩形写（x: [左, 右]，y: [下, 上]，原点在标题栏左下角），
    引擎从所有边界推出网格，矩形覆盖多个网格格子就是合并单元格。"""
    W, H = (float(v) for v in tpl["size"])
    xs = sorted({0.0, W} | {float(v) for cl in tpl["cells"] for v in cl["x"]})
    ys = sorted({0.0, H} | {float(v) for cl in tpl["cells"] for v in cl["y"]}, reverse=True)
    cols = [b - a for a, b in zip(xs, xs[1:])]
    rows = [a - b for a, b in zip(ys, ys[1:])]
    cells = []
    for cl in tpl["cells"]:
        (xa, xb), (ya, yb) = sorted(cl["x"]), sorted(cl["y"])
        c0, c1 = xs.index(float(xa)), xs.index(float(xb))
        r0, r1 = ys.index(float(yb)), ys.index(float(ya))
        key = cl.get("field", "")
        text = cl.get("text", "")
        if key:
            text = values.get(key, "") or cl.get("default", "")
            if cl.get("format"):  # 如「第 {} 张」：有值套格式，没值写 blank（「第　　张」）
                text = cl["format"].replace("{}", str(text)) if text else cl.get("blank", "")
        cells.append(Cell(r=r0, c=c0, rs=r1 - r0, cs=c1 - c0, text=text, role=cl.get("role", "title_block_value"),
                          align=cl.get("align", "center"), placeholder=cl.get("placeholder", "") if key else "", key=key))
    return Table(cols=cols, rows=rows, cells=cells, skip_edges=tuple(tpl.get("skip_edges", ("right", "bottom"))), kind="title_block")


def validate_template(tpl: dict) -> list[str]:
    """用户上传的标题栏模板：返回问题列表（空 = 可用）。"""
    errs = []
    try:
        W, H = (float(v) for v in tpl["size"])
        if not (50 <= W <= 300 and 20 <= H <= 120):
            errs.append(f"size={tpl['size']} 不合理（宽 50～300、高 20～120 mm）")
        cells = tpl["cells"]
        if not isinstance(cells, list) or not cells:
            errs.append("cells 为空")
        for i, cl in enumerate(cells):
            (xa, xb), (ya, yb) = sorted(cl["x"]), sorted(cl["y"])
            if not (0 <= xa < xb <= W and 0 <= ya < yb <= H):
                errs.append(f"第 {i + 1} 个单元格超出标题栏范围")
            if not (cl.get("text") or cl.get("field")):
                errs.append(f"第 {i + 1} 个单元格既没有 text 也没有 field")
        if not errs:
            from_template(tpl, {}).owner()
    except (KeyError, TypeError, ValueError) as ex:
        errs.append(f"模板格式不对：{ex}")
    return errs
