"""填充（HATCH），对标 AutoCAD 的 HATCH / BHATCH。纯函数，不依赖 CAD-IR，单独可测。

两种定边界方式：
  find_boundary          「拾取点」：把拾取点附近的线条打散成折线、求交、拼面（shapely.polygonize），
                          取包含拾取点的最小面；面的外环是边界，内环是孤岛。
  boundary_from_entities 「选对象」：闭合图形（圆、闭合多段线、整椭圆、闭合样条）直接作边界。
孤岛方式（AutoCAD 的 Normal / Outer / Ignore）：
  normal  奇偶交替（默认）：最外层填，孤岛不填，孤岛里的孤岛再填 …
  outer   只填最外层，孤岛里面全不填
  ignore  忽略孤岛，整块填满
坐标单位由调用方决定（智绘CAD 里是模型 mm）；unit = 1 图纸 mm 对应多少模型单位，只用于换算提示和默认精度。
"""

from __future__ import annotations

import difflib
import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

STYLES = {"normal": 0, "outer": 1, "ignore": 2}  # → HATCH 的 hatch_style
FLATTEN_MM = 0.05  # 曲线打散误差（图纸 mm）
TEXT_MARGIN_MM = 1.0  # 文字包围盒外扩（图纸 mm）
SOLID = "SOLID"


class HatchError(Exception):
    def __init__(self, code: str, message: str, hint: str = "", detail: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.hint, self.detail = code, message, hint, detail or {}

    def to_dict(self) -> dict:
        return {"ok": False, "error_code": self.code, "message": self.message, "hint": self.hint, **self.detail}


@dataclass
class Ring:
    coords: list[tuple[float, float]]  # 打散后的闭合折线（首尾不重复）
    exact: Any = None  # 正好是一整个原图形时，记下原实体（写出时用精确圆弧 / bulge）
    is_text: bool = False


@dataclass
class Region:
    """要填的区域：geometry 是按孤岛方式算好的实际填充范围，rings 是要写进 HATCH 的边界环。"""

    geometry: Polygon | MultiPolygon
    rings: list[Ring]
    exterior_count: int  # rings 里前几个是外边界
    islands: int  # 孤岛数（不含文字挖掉的）
    text_holes: int = 0
    style: str = "normal"
    detail: dict = field(default_factory=dict)

    @property
    def area(self) -> float:
        return self.geometry.area

    @property
    def exact(self) -> bool:
        return all(r.exact is not None or r.is_text for r in self.rings)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.geometry.bounds


# ---------------------------------------------------------------- 图形 → 线

def _entity_points(e, tol: float) -> list[tuple[float, float]]:
    from ezdxf import path as ezpath

    return [(v.x, v.y) for v in ezpath.make_path(e).flattening(tol)]


def to_lines(source: Iterable, tol: float) -> list[tuple[LineString, Any]]:
    """ezdxf 实体或 shapely 线 → [(折线, 原实体)]。块参照（INSERT）展开成里面的图形。"""
    out: list[tuple[LineString, Any]] = []
    for s in source:
        if isinstance(s, (LineString, shapely.LinearRing)):
            out.append((LineString(s.coords), None))
        elif isinstance(s, Polygon):
            out += [(LineString(r.coords), None) for r in (s.exterior, *s.interiors)]
        elif hasattr(s, "dxftype"):
            t = s.dxftype()
            if t == "INSERT":
                out += to_lines(list(s.virtual_entities()), tol)
                continue
            if t in ("TEXT", "MTEXT", "HATCH", "SOLID", "ATTRIB", "ATTDEF", "POINT", "DIMENSION"):
                continue
            try:
                pts = _entity_points(s, tol)
            except TypeError:  # 不支持转路径的实体
                continue
            if len(pts) >= 2:
                out.append((LineString(pts), s))
    return out


def _closed_polygon(e, tol: float) -> Polygon | None:
    """闭合图形 → 多边形；不闭合返回 None。"""
    t = e.dxftype()
    if t == "CIRCLE":
        closed = True
    elif t == "LWPOLYLINE":
        closed = e.closed or (len(e) > 2 and math.dist(e[0][:2], e[-1][:2]) < tol)
    elif t == "POLYLINE":
        closed = e.is_closed
    elif t == "ELLIPSE":
        closed = abs((e.dxf.end_param - e.dxf.start_param) % (2 * math.pi)) < 1e-9
    elif t == "SPLINE":
        pts = _entity_points(e, tol)
        closed = e.closed or (len(pts) > 2 and math.dist(pts[0], pts[-1]) < tol)
    else:
        closed = False
    if not closed:
        return None
    pts = _entity_points(e, tol)
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    if poly.area <= 0:
        return None
    return poly if poly.is_valid else poly.buffer(0)


# ---------------------------------------------------------------- 缺口

def _nodes(merged) -> dict[tuple[float, float], int]:
    deg: dict[tuple[float, float], int] = {}
    geoms = merged.geoms if hasattr(merged, "geoms") else [merged]
    for g in geoms:
        if g.is_empty or not isinstance(g, LineString):
            continue
        cs = list(g.coords)
        if math.dist(cs[0], cs[-1]) < 1e-12:  # 闭合环没有端点
            continue
        for p in (cs[0], cs[-1]):
            k = (round(p[0], 9), round(p[1], 9))
            deg[k] = deg.get(k, 0) + 1
    return deg


def dangling_ends(lines: list[LineString]) -> list[tuple[float, float]]:
    """求交之后只连着一根线的端点（悬空端点）。"""
    merged = unary_union(lines)
    return [p for p, n in _nodes(merged).items() if n == 1]


def find_gaps(lines: list[LineString], near: tuple[float, float] | None = None, limit: int = 5) -> list[dict]:
    """每个悬空端点到别的线最近处就是一个缺口。按离 near（拾取点）由近到远排。"""
    ends = dangling_ends(lines)
    merged = unary_union(lines)
    pieces = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    gaps = []
    for p in ends:
        pt = Point(p)
        best = None
        for g in pieces:
            cs = list(g.coords)
            if any(math.dist(p, c) < 1e-9 for c in (cs[0], cs[-1])):  # 端点自己所在的那根线
                continue
            q = shapely.ops.nearest_points(pt, g)[1]
            dist = pt.distance(g)
            if best is None or dist < best[0]:
                best = (dist, (q.x, q.y))
        if best is None:
            continue
        dist, q = best
        mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        gaps.append({"at": mid, "from": p, "to": q, "length": dist})
    uniq: list[dict] = []
    for g in gaps:  # 两个端点互为最近时只留一个
        if not any(math.dist(g["at"], u["at"]) < 1e-6 for u in uniq):
            uniq.append(g)
    if near is not None:
        uniq.sort(key=lambda g: math.dist(g["at"], near))
    return uniq[:limit]


def close_gaps(lines: list[LineString], gap_tol: float) -> list[LineString]:
    """把距离 < gap_tol 的悬空端点接到最近的线上（AutoCAD 的 HPGAPTOL）。"""
    if gap_tol <= 0:
        return lines
    extra = [LineString([g["from"], g["to"]]) for g in find_gaps(lines, limit=10**6) if 0 < g["length"] < gap_tol]
    return lines + extra


# ---------------------------------------------------------------- 孤岛方式

def _styled(face: Polygon, faces: list[Polygon], style: str) -> tuple[Polygon | MultiPolygon, list, int]:
    """按孤岛方式算实际填充范围；返回 (填充范围, 要写的环 [(坐标, 是否外环)], 第一层孤岛数)。"""
    outer = Polygon(face.exterior)
    islands = list(face.interiors)
    if style == "ignore":
        return outer, [(list(face.exterior.coords), True)], len(islands)
    if style == "outer":
        return face, [(list(face.exterior.coords), True)] + [(list(r.coords), False) for r in islands], len(islands)
    # normal：逐层交替。第 d 层孤岛围成的面（减去它自己的孤岛）d 为偶数时填
    filled = face
    rings = [(list(face.exterior.coords), True)] + [(list(r.coords), False) for r in islands]
    level, depth = [Polygon(r) for r in islands], 1
    while level:
        nxt = []
        for hole in level:
            inner = next((f for f in faces if f.exterior.equals(hole.exterior)), None)
            if inner is None:
                continue
            if depth % 2 == 0:
                filled = filled.union(inner)
            rings += [(list(r.coords), False) for r in inner.interiors]
            nxt += [Polygon(r) for r in inner.interiors]
        level, depth = nxt, depth + 1
    return filled, rings, len(islands)


def _match_exact(coords, closed_polys: list[tuple[Polygon, Any]], tol_area: float):
    ring = Polygon(coords)
    for poly, ent in closed_polys:
        if abs(poly.area - ring.area) <= tol_area and poly.symmetric_difference(ring).area <= tol_area:
            return ent
    return None


def _finish(geometry, rings_raw, islands, style, closed_polys, text_boxes, unit, tol) -> Region:
    text_holes = 0
    rings = []
    tol_area = max(geometry.length * tol * 2, 1e-9)
    if text_boxes:
        m = TEXT_MARGIN_MM * unit
        boxes = [box(b[0] - m, b[1] - m, b[2] + m, b[3] + m) for b in text_boxes]
        hits = [b for b in boxes if b.intersects(geometry)]
        if hits:
            geometry = geometry.difference(unary_union(hits))
            text_holes = len(hits)
            # 文字挖掉后环可能与边界相交，直接用最终几何的环写出
            polys = list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]
            ext = [Ring(list(p.exterior.coords)[:-1], _match_exact(list(p.exterior.coords), closed_polys, tol_area)) for p in polys]
            holes = []
            for p in polys:
                for r in p.interiors:
                    cs = list(r.coords)
                    ent = _match_exact(cs, closed_polys, tol_area)
                    holes.append(Ring(cs[:-1], ent, is_text=ent is None))
            return Region(geometry, ext + holes, len(ext), islands, text_holes, style)
    for coords, is_ext in rings_raw:
        rings.append(Ring(coords[:-1] if coords[0] == coords[-1] else coords, _match_exact(coords, closed_polys, tol_area)))
    n_ext = sum(1 for _, is_ext in rings_raw if is_ext)  # rings_raw 里外环排在最前
    return Region(geometry, rings, n_ext, islands, text_holes, style)


# ---------------------------------------------------------------- 拾取点

@dataclass
class Prepared:
    """打散好的图形 + 空间索引。同一张图要求多个填充时只准备一次。"""

    items: list[tuple[LineString, Any]]
    tol: float

    def __post_init__(self) -> None:
        self.lines = [ln for ln, _ in self.items]
        self.tree = STRtree(self.lines) if self.lines else None
        self.total = unary_union(self.lines).bounds if self.lines else (0.0, 0.0, 0.0, 0.0)
        self.closed_polys = [(p, e) for _, e in self.items if e is not None for p in [_closed_polygon(e, self.tol)] if p is not None]


def prepare(source: Iterable, tol: float) -> Prepared:
    return Prepared(to_lines(list(source), tol), tol)


def find_boundary(
    segments_source: Iterable,
    pick: tuple[float, float],
    gap_tol: float = 0.0,
    text_boxes: Iterable[tuple[float, float, float, float]] = (),
    style: str = "normal",
    unit: float = 1.0,
    flatten_tol: float | None = None,
) -> Region:
    """拾取点定边界。找不到封闭区域抛 E_HATCH_NO_BOUNDARY（带最近的缺口），拾取点在线上抛 E_HATCH_ON_BOUNDARY。"""
    if style not in STYLES:
        raise HatchError("E_BAD_ARG", f"未知的孤岛方式 {style}", f"可选 {list(STYLES)}")
    if isinstance(segments_source, Prepared):
        prep = segments_source
    else:
        prep = prepare(segments_source, flatten_tol if flatten_tol is not None else FLATTEN_MM * unit)
    tol = prep.tol
    if not prep.lines:
        raise HatchError("E_HATCH_NO_BOUNDARY", "图中没有可作边界的线", "先画出封闭的边界")
    lines, tree, total, closed_polys = prep.lines, prep.tree, prep.total, prep.closed_polys
    pt = Point(pick)
    on_tol = max(tol, 1e-9 * max(1.0, unit))
    near = tree.query(pt.buffer(on_tol))
    if any(lines[i].distance(pt) <= on_tol for i in near):
        raise HatchError("E_HATCH_ON_BOUNDARY", f"拾取点 ({pick[0]:.1f}, {pick[1]:.1f}) 落在线上", "拾取点要落在要填的区域里面，离开边界线一点再试")
    span = max(total[2] - total[0], total[3] - total[1], 1e-9)
    radius = span / 16
    while True:
        win = box(pick[0] - radius, pick[1] - radius, pick[0] + radius, pick[1] + radius)
        full = win.contains(box(*total))
        idx = tree.query(win) if not full else range(len(lines))
        sub = close_gaps([lines[i] for i in idx], gap_tol)
        faces = list(polygonize(unary_union(sub)))
        hits = [f for f in faces if f.contains(pt)]
        if hits:
            face = min(hits, key=lambda f: f.area)
            # 面完全落在搜索窗口里，窗口外的线不可能再切分它，结果可信；否则扩大窗口再求
            if full or win.contains(box(*face.bounds)):
                geometry, rings_raw, islands = _styled(face, faces, style)
                region = _finish(geometry, rings_raw, islands, style, closed_polys, list(text_boxes), unit, tol)
                region.detail = {"pick": pick, "face_bounds": face.bounds}
                return region
        if full:
            break
        radius *= 2
    gaps = find_gaps(close_gaps(lines, gap_tol), near=pick)
    detail = {"gaps": [{"at": [round(g["at"][0], 3), round(g["at"][1], 3)], "from": [round(v, 3) for v in g["from"]],
                        "to": [round(v, 3) for v in g["to"]], "length_mm": round(g["length"] / unit, 3)} for g in gaps]}
    if gaps:
        g = gaps[0]
        hint = (f"({g['at'][0]:.1f}, {g['at'][1]:.1f}) 处有 {g['length'] / unit:.2f} mm 缺口，可用 EXTEND/JOIN 补上，"
                f"或把 gap_tol 调到 {math.ceil(g['length'] / unit * 10 + 1) / 10:g} mm 以上")
    else:
        hint = "拾取点不在任何封闭区域内：换到图形围成的区域里面再拾取，或改用选对象方式"
    raise HatchError("E_HATCH_NO_BOUNDARY", f"拾取点 ({pick[0]:.1f}, {pick[1]:.1f}) 周围没有封闭区域", hint, detail)


# ---------------------------------------------------------------- 选对象

def boundary_from_entities(entities: Iterable, style: str = "normal", unit: float = 1.0, text_boxes: Iterable = (), flatten_tol: float | None = None) -> Region:
    """「选对象」：每个对象必须自身闭合。嵌套的对象按孤岛方式处理（normal 奇偶交替）。"""
    if style not in STYLES:
        raise HatchError("E_BAD_ARG", f"未知的孤岛方式 {style}", f"可选 {list(STYLES)}")
    tol = flatten_tol if flatten_tol is not None else FLATTEN_MM * unit
    polys: list[tuple[Polygon, Any]] = []
    for e in entities:
        if e.dxftype() == "INSERT":
            raise HatchError("E_HATCH_NOT_CLOSED", "块参照不能直接作填充边界", "改用拾取点方式，在设备图形里拾取")
        p = _closed_polygon(e, tol)
        if p is None:
            raise HatchError("E_HATCH_NOT_CLOSED", f"{e.dxftype()}（句柄 {e.dxf.handle}）不是闭合图形", "改用拾取点方式，或先把边界连成闭合多段线")
        polys.append((p, e))
    if not polys:
        raise HatchError("E_HATCH_NOT_CLOSED", "没有选中任何对象", "select 里写要作边界的对象")
    depth = [sum(1 for q, _ in polys if q is not p and q.contains(p)) for p, _ in polys]
    if style == "ignore":
        keep = [i for i, dd in enumerate(depth) if dd == 0]
    elif style == "outer":
        keep = [i for i, dd in enumerate(depth) if dd <= 1]
    else:
        keep = list(range(len(polys)))
    geometry = None
    for i in sorted(keep, key=lambda i: depth[i]):
        geometry = polys[i][0] if geometry is None else geometry.symmetric_difference(polys[i][0])
    rings_raw = [(list(polys[i][0].exterior.coords), depth[i] == 0) for i in sorted(keep, key=lambda i: depth[i])]
    region = _finish(geometry, rings_raw, sum(1 for i in keep if depth[i] == 1), style, polys, list(text_boxes), unit, tol)
    return region


# ---------------------------------------------------------------- 图案与写出

@lru_cache(maxsize=1)
def patterns() -> dict:
    from ezdxf.tools import pattern

    return pattern.load()


def base_spacing(name: str) -> float:
    """图案在比例 1 时的基准线距：各组平行线线距的最大值，也就是图案的重复间距。

    ANSI31、EARTH 这类各组线距相同的图案就是线距本身（3.175、6.35）；GRAVEL、AR-SAND、AR-CONC 这类不规则图案
    各组线距差别很大（GRAVEL 0.8～25.4），取最大值后「图纸线距」就是图案在图纸上重复一次的大小，不会被个别密线带偏。
    """
    spacings = []
    for angle, _, offset, _ in patterns()[name]:
        a = math.radians(angle)
        spacings.append(abs(-math.sin(a) * offset[0] + math.cos(a) * offset[1]))
    best = max(spacings, default=0.0)
    return best if best > 1e-9 else 1.0


def check_pattern(name: str) -> str:
    up = name.strip().upper()
    if up == SOLID or up in patterns():
        return up
    # 先把易混的字符归一（l/I→1、O→0），再按相似度排，免得 ANSI3l 推荐成 ANSI38
    norm = lambda t: t.translate(str.maketrans({"L": "1", "I": "1", "O": "0"}))  # noqa: E731
    keys = {norm(k): k for k in patterns()}
    near = [keys[k] for k in difflib.get_close_matches(norm(up), list(keys), n=5, cutoff=0.3)]
    raise HatchError("E_BAD_ARG", f"没有填充图案 {name}", f"最接近的图案：{near or ['ANSI31', 'AR-CONC', 'EARTH', 'GRAVEL', 'AR-SAND']}；纯色用 SOLID")


def paper_scale(pattern: str, unit: float, paper_spacing_mm: float) -> float:
    """「图纸上的线距（mm）」→ 图案比例。模型空间 1 图纸 mm = unit；scale = unit × 图纸线距 / 基准线距。"""
    if check_pattern(pattern) == SOLID:
        return 1.0
    return unit * paper_spacing_mm / base_spacing(check_pattern(pattern))


def _add_ring(h, ring: Ring, flags: int) -> bool:
    """写一个边界环；能用原图形精确边界就用（真圆弧、bulge），返回是否精确。"""
    e = ring.exact
    if e is not None:
        t = e.dxftype()
        if t == "CIRCLE":
            p = h.paths.add_edge_path(flags=flags)
            p.add_arc(center=(e.dxf.center.x, e.dxf.center.y), radius=e.dxf.radius, start_angle=0, end_angle=360)
            return True
        if t == "LWPOLYLINE":
            h.paths.add_polyline_path([(x, y, b) for x, y, b in e.get_points("xyb")], is_closed=True, flags=flags)
            return True
        if t == "ELLIPSE":
            p = h.paths.add_edge_path(flags=flags)
            c, mj = e.dxf.center, e.dxf.major_axis
            p.add_ellipse(center=(c.x, c.y), major_axis=(mj.x, mj.y), ratio=e.dxf.ratio, start_angle=0, end_angle=360)
            return True
    h.paths.add_polyline_path(ring.coords, is_closed=True, flags=flags)
    return False


def write_hatch(layout, region: Region, pattern: str = "ANSI31", scale: float = 1.0, angle: float = 0.0, layer: str = "ZH-剖面线", style: str | None = None, dxfattribs: dict | None = None):
    """把区域写成 HATCH 实体。pattern='SOLID' 为纯色填充。"""
    name = check_pattern(pattern)
    style = style or region.style
    hs = STYLES[style]
    if region.text_holes and hs == STYLES["ignore"]:
        hs = STYLES["outer"]  # 忽略孤岛方式也不能压字：文字挖出的环要生效
    h = layout.add_hatch(color=256, dxfattribs={"layer": layer, **(dxfattribs or {})})  # 颜色随层
    h.dxf.hatch_style = hs
    if name == SOLID:
        h.set_solid_fill(color=256)
    else:
        h.set_pattern_fill(name, scale=scale, angle=angle, color=256)
    for i, ring in enumerate(region.rings):
        flags = 1 if i < region.exterior_count else (8 if ring.is_text else 0)
        _add_ring(h, ring, flags)
    return h
