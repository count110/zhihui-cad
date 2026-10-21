"""三维：二维轮廓 → 拉伸 / 旋转 / 路径扫掠网格，z 向棱柱（slab）布尔运算，体积与包围盒，STL 导出。

实体表示（GeoEntity.kind == "solid3d"）：
  data = {"vertices": [[x,y,z]...], "faces": [[i,j,k]...], "volume": V, "kind": "prism"|"tapered"|"revolve"|"sweep"|"surface",
          "slabs": [{"z0","z1","polys":[{"outer":[[x,y]...],"holes":[[[x,y]...]]}]}]  # 仅 prism：z 向分层的棱柱，两个都是这类实体时布尔走精确的 slab 路径
         }
布尔运算两条路：
  1. 两个实体都是 z 向棱柱：把它们按 z 区间切成若干层，层内是二维截面，直接用 shapely 的二维布尔求解，结果仍是 slab 叠层（精确，不需要额外依赖）。
  2. 其它情况（旋转体、锥度拉伸、路径扫掠、旋转过的实体、上一次网格布尔的结果）：用可选依赖 manifold3d 做任意三角网格的并/差/交（2026-10-20 经用户同意引入，
     见 pyproject 的 optional-dependencies.solid）；没装时报 BOOLEAN_UNSUPPORTED，提示安装命令。结果是普通网格（solid_kind="boolean"），没有 slab。
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import unary_union

from .boundary import find_closed_boundary
from .entities import EPS, Arc, P, Pt, is_closed, primitives, sample
from .errors import GeomError

CIRCLE_SEG = 72


# ------------------------------------------------------------------ 轮廓
def _closed_loop(e, tol: float = 0.02) -> list[tuple[float, float]] | None:
    """自身闭合的图元 → 多边形顶点（圆按 CIRCLE_SEG 段，弧按弦高 tol）。"""
    if e.kind == "circle":
        c, r = e.data["c"], float(e.data["r"])
        return [(c[0] + r * math.cos(2 * math.pi * i / CIRCLE_SEG), c[1] + r * math.sin(2 * math.pi * i / CIRCLE_SEG)) for i in range(CIRCLE_SEG)]
    if not is_closed(e):
        return None
    pts: list[Pt] = []
    for pr in primitives(e):
        s = sample(pr, tol)
        pts += s[:-1]
    out = [(p[0], p[1]) for p in pts]
    return out if len(out) >= 3 else None


def section_from_entities(ents: list, include_islands: bool = True) -> MultiPolygon:
    """闭合图元（和可连成闭环的线、弧）→ 带孔的截面。嵌套按奇偶深度：偶数层是实体，奇数层是孔。"""
    loops: list[list[tuple[float, float]]] = []
    open_ents = []
    for e in ents:
        lp = _closed_loop(e)
        if lp:
            loops.append(lp)
        elif e.kind in ("line", "arc", "polyline", "spline", "ellipse", "mline"):
            open_ents.append(e)
    if open_ents:
        res = find_closed_boundary(open_ents, candidate_ids=[e.id for e in open_ents])
        if not res["is_closed"]:
            raise GeomError("BOUNDARY_NOT_CLOSED", res.get("message", "轮廓不闭合，不能拉伸"), "先补上缺口，或改选已经闭合的图元", gaps=res.get("gaps", []))
        loops.append([(p[0], p[1]) for p in res["outer_loop_points"]])
        loops += [[(p[0], p[1]) for p in i["points"]] for i in res["island_loops"]]
    if not loops:
        raise GeomError("NO_PROFILE", "选中的图元里没有闭合轮廓", "拉伸需要闭合的圆、多段线、矩形等，或能连成闭环的线和弧")
    polys = [Polygon(l).buffer(0) for l in loops]
    polys = [p for p in polys if p.area > EPS]
    # 嵌套深度
    depth = []
    for i, p in enumerate(polys):
        depth.append(sum(1 for j, q in enumerate(polys) if i != j and q.contains(p.representative_point()) and q.area > p.area))
    solids = [p for p, dp in zip(polys, depth) if dp % 2 == 0]
    holes = [p for p, dp in zip(polys, depth) if dp % 2 == 1]
    if not include_islands:
        return _as_multi(unary_union(solids))
    geom = unary_union(solids)
    if holes:
        geom = geom.difference(unary_union(holes))
    return _as_multi(geom)


def _as_multi(g) -> MultiPolygon:
    if g.is_empty:
        return MultiPolygon()
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    return MultiPolygon([x for x in getattr(g, "geoms", []) if isinstance(x, Polygon) and not x.is_empty])


def poly_to_json(p: Polygon) -> dict:
    p = shapely.geometry.polygon.orient(p, 1.0)
    return {"outer": [[round(x, 6), round(y, 6)] for x, y in list(p.exterior.coords)[:-1]], "holes": [[[round(x, 6), round(y, 6)] for x, y in list(r.coords)[:-1]] for r in p.interiors]}


def poly_from_json(d: dict) -> Polygon:
    return Polygon(d["outer"], d.get("holes", []))


# ------------------------------------------------------------------ 网格
class MeshBuilder:
    def __init__(self):
        self.v: list[list[float]] = []
        self.f: list[list[int]] = []
        self._idx: dict[tuple, int] = {}

    def vert(self, x: float, y: float, z: float) -> int:
        key = (round(x, 6), round(y, 6), round(z, 6))
        i = self._idx.get(key)
        if i is None:
            i = len(self.v)
            self._idx[key] = i
            self.v.append([key[0], key[1], key[2]])
        return i

    def tri(self, a: int, b: int, c: int) -> None:
        if a != b and b != c and a != c:
            self.f.append([a, b, c])

    def quad(self, a: int, b: int, c: int, d: int) -> None:
        self.tri(a, b, c)
        self.tri(a, c, d)


def _triangles(poly: Polygon) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    out = []
    for t in shapely.constrained_delaunay_triangles(poly).geoms:
        c = list(t.exterior.coords)[:3]
        out.append((c[0], c[1], c[2]))
    return out


def _cap(mb: MeshBuilder, poly: Polygon, z: float, up: bool) -> None:
    for a, b, c in _triangles(poly):
        ia, ib, ic = mb.vert(a[0], a[1], z), mb.vert(b[0], b[1], z), mb.vert(c[0], c[1], z)
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if (cross > 0) == up:
            mb.tri(ia, ib, ic)
        else:
            mb.tri(ia, ic, ib)


def _walls(mb: MeshBuilder, poly: Polygon, z0: float, z1: float) -> None:
    p = shapely.geometry.polygon.orient(poly, 1.0)               # 外环逆时针、孔顺时针 → 法线朝外
    for ring in [p.exterior, *p.interiors]:
        c = list(ring.coords)
        for (x0, y0), (x1, y1) in zip(c[:-1], c[1:]):
            mb.quad(mb.vert(x0, y0, z0), mb.vert(x1, y1, z0), mb.vert(x1, y1, z1), mb.vert(x0, y0, z1))


def mesh_from_slabs(slabs: list[dict]) -> tuple[list[list[float]], list[list[int]]]:
    mb = MeshBuilder()
    secs = [unary_union([poly_from_json(p) for p in s["polys"]]) for s in slabs]
    for k, s in enumerate(slabs):
        sec = secs[k]
        below = secs[k - 1] if k > 0 and abs(slabs[k - 1]["z1"] - s["z0"]) < 1e-9 else None
        above = secs[k + 1] if k + 1 < len(slabs) and abs(slabs[k + 1]["z0"] - s["z1"]) < 1e-9 else None
        for poly in _as_multi(sec).geoms:
            _walls(mb, poly, s["z0"], s["z1"])
        bot = sec.difference(below) if below is not None else sec
        top = sec.difference(above) if above is not None else sec
        for g in _as_multi(bot).geoms:
            if g.area > 1e-9:
                _cap(mb, g, s["z0"], up=False)
        for g in _as_multi(top).geoms:
            if g.area > 1e-9:
                _cap(mb, g, s["z1"], up=True)
    return mb.v, mb.f


def slabs_volume(slabs: list[dict]) -> float:
    return sum(sum(poly_from_json(p).area for p in s["polys"]) * (s["z1"] - s["z0"]) for s in slabs)


def mesh_volume(v: list[list[float]], f: list[list[int]]) -> float:
    """闭合三角网格的体积（散度定理）；非闭合时只是近似。"""
    V = np.array(v, dtype=float)
    tot = 0.0
    for a, b, c in f:
        tot += float(np.dot(V[a], np.cross(V[b], V[c]))) / 6.0
    return abs(tot)


# ------------------------------------------------------------------ 拉伸
def extrude(section: MultiPolygon, height: float, direction: str = "+z", taper_deg: float = 0.0, base_z: float = 0.0) -> dict:
    """直拉伸（taper=0）→ slab 棱柱；带锥度 → 逐环缩放（只对凸、简单截面可靠，其余报错）。"""
    if section.is_empty:
        raise GeomError("NO_PROFILE", "截面为空", "")
    if height <= 0:
        raise GeomError("INVALID_ARGS", "拉伸高度必须大于 0", "")
    z0, z1 = {"+z": (base_z, base_z + height), "-z": (base_z - height, base_z), "both": (base_z - height / 2, base_z + height / 2)}[direction]
    if abs(taper_deg) < 1e-9:
        slabs = [{"z0": z0, "z1": z1, "polys": [poly_to_json(p) for p in section.geoms]}]
        v, f = mesh_from_slabs(slabs)
        return {"vertices": v, "faces": f, "volume": slabs_volume(slabs), "solid_kind": "prism", "slabs": slabs}
    off = -math.tan(math.radians(taper_deg)) * (z1 - z0)           # 正锥度向内收
    mb = MeshBuilder()
    for poly in section.geoms:
        top = poly.buffer(off, join_style=2, mitre_limit=10.0)
        if top.is_empty or not isinstance(top, Polygon):
            raise GeomError("TAPER_TOO_LARGE", "锥度太大，顶面退化为空或分裂", "减小锥角或拉伸高度", max_offset=round(poly.buffer(-1e-6).area ** 0.5, 3))
        po, to = shapely.geometry.polygon.orient(poly, 1.0), shapely.geometry.polygon.orient(top, 1.0)
        rings0 = [po.exterior, *po.interiors]
        rings1 = [to.exterior, *to.interiors]
        if len(rings0) != len(rings1) or any(len(a.coords) != len(b.coords) for a, b in zip(rings0, rings1)):
            raise GeomError("TAPER_UNSUPPORTED", "这个截面做带锥度拉伸会改变顶点数（含圆弧或复杂孔洞）", "改用不带锥度的拉伸，或先把截面简化为多边形")
        for a, b in zip(rings0, rings1):
            ca, cb = list(a.coords), list(b.coords)
            for i in range(len(ca) - 1):
                mb.quad(mb.vert(ca[i][0], ca[i][1], z0), mb.vert(ca[i + 1][0], ca[i + 1][1], z0), mb.vert(cb[i + 1][0], cb[i + 1][1], z1), mb.vert(cb[i][0], cb[i][1], z1))
        _cap(mb, po, z0, up=False)
        _cap(mb, to, z1, up=True)
    mb.f = orient_mesh(mb.v, mb.f)                                    # 统一朝向、法线朝外（布尔运算和渲染都依赖）
    return {"vertices": mb.v, "faces": mb.f, "volume": mesh_volume(mb.v, mb.f), "solid_kind": "tapered"}


# ------------------------------------------------------------------ 旋转
def _axis_frame(a: Pt, b: Pt) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    o = np.array([a[0], a[1], 0.0])
    u = np.array([b[0] - a[0], b[1] - a[1], 0.0])
    L = np.linalg.norm(u)
    if L < EPS:
        raise GeomError("INVALID_ARGS", "旋转轴的两个点重合", "")
    u /= L
    n = np.array([-u[1], u[0], 0.0])                                 # 平面内垂直于轴
    return o, u, n


def revolve(section: MultiPolygon, a: Pt, b: Pt, angle_deg: float = 360.0, segments: int = 48) -> dict:
    """截面绕轴（平面内的一条直线）旋转：轮廓必须整个在轴的一侧，否则自交。"""
    o, u, n = _axis_frame(a, b)
    z = np.array([0.0, 0.0, 1.0])
    full = abs(angle_deg) >= 360 - 1e-9
    nseg = max(segments if full else max(3, int(segments * abs(angle_deg) / 360)), 8)
    th = [math.radians(angle_deg) * i / nseg for i in range(nseg + (0 if full else 1))]
    mb = MeshBuilder()
    for poly in section.geoms:
        p = shapely.geometry.polygon.orient(poly, 1.0)
        sides = []
        for ring in [p.exterior, *p.interiors]:
            pts = [np.array([x, y, 0.0]) for x, y in list(ring.coords)[:-1]]
            rr = [float(np.dot(q - o, n)) for q in pts]
            if min(rr) < -1e-6 and max(rr) > 1e-6:
                raise GeomError("PROFILE_CROSSES_AXIS", "轮廓跨过了旋转轴，旋转后会自交", "把轮廓整体放在轴的一侧，或改选另一条轴")
            sides.append((pts, rr))
        sgn = 1.0 if all(max(r) >= 0 for _, r in sides[:1]) and sum(sum(r) for _, r in sides[:1]) >= 0 else -1.0
        for pts, rr in sides:
            ss = [float(np.dot(q - o, u)) for q in pts]
            rings = []
            for t in th:
                rings.append([mb.vert(*(o + s * u + (abs(r) * (math.cos(t) * n * sgn + math.sin(t) * z))).tolist()) for s, r in zip(ss, rr)])
            m = len(pts)
            for k in range(len(th) if full else len(th) - 1):
                r0, r1 = rings[k], rings[(k + 1) % len(th)]
                for i in range(m):
                    j = (i + 1) % m
                    mb.quad(r0[i], r0[j], r1[j], r1[i])
        if not full:                                                  # 两端盖
            for t, up in ((th[0], sgn > 0), (th[-1], sgn < 0)):
                for tri in _triangles(p):
                    ids = []
                    for x, y in tri:
                        q = np.array([x, y, 0.0])
                        s_, r_ = float(np.dot(q - o, u)), abs(float(np.dot(q - o, n)))
                        ids.append(mb.vert(*(o + s_ * u + r_ * (math.cos(t) * n * sgn + math.sin(t) * z)).tolist()))
                    mb.tri(*(ids if up else ids[::-1]))
    mb.f = orient_mesh(mb.v, mb.f)                                    # 两端盖朝向随旋转方向/轮廓所在侧变化，统一成法线朝外
    vol = mesh_volume(mb.v, mb.f)
    return {"vertices": mb.v, "faces": mb.f, "volume": vol, "solid_kind": "revolve"}


# ------------------------------------------------------------------ 路径扫掠
def sweep(section: MultiPolygon, path: list[Pt]) -> dict:
    """截面（局部坐标 = 截面质心为原点的 x,y）沿折线路径扫掠；截面放在路径起点、垂直于起点切线，平行移动标架。"""
    if len(path) < 2:
        raise GeomError("INVALID_ARGS", "路径至少需要两个点", "")
    pts = [np.array(p, dtype=float) for p in path]
    pts = [p for i, p in enumerate(pts) if i == 0 or np.linalg.norm(p - pts[i - 1]) > 1e-9]
    tang = []
    for i in range(len(pts)):
        a = pts[max(i - 1, 0)]
        b = pts[min(i + 1, len(pts) - 1)]
        t = (pts[i] - a) / max(np.linalg.norm(pts[i] - a), 1e-12) + (b - pts[i]) / max(np.linalg.norm(b - pts[i]), 1e-12) if 0 < i < len(pts) - 1 else (b - a)
        tang.append(t / np.linalg.norm(t))
    t0 = tang[0]
    ref = np.array([0.0, 0.0, 1.0]) if abs(t0[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    nrm = np.cross(ref, t0)
    nrm /= np.linalg.norm(nrm)
    bn = np.cross(t0, nrm)
    frames = [(nrm, bn)]
    for i in range(1, len(pts)):                                      # 平行移动标架
        n_prev = frames[-1][0]
        n_new = n_prev - np.dot(n_prev, tang[i]) * tang[i]
        if np.linalg.norm(n_new) < 1e-9:
            n_new = frames[-1][1]
        n_new /= np.linalg.norm(n_new)
        frames.append((n_new, np.cross(tang[i], n_new)))
    mb = MeshBuilder()
    for poly in section.geoms:
        p = shapely.geometry.polygon.orient(poly, 1.0)
        c = np.array(p.centroid.coords[0])
        rings = [list(p.exterior.coords)[:-1]] + [list(r.coords)[:-1] for r in p.interiors]
        for ring in rings:
            rows = []
            for i, (n, b) in enumerate(frames):
                rows.append([mb.vert(*(pts[i] + (x - c[0]) * n + (y - c[1]) * b).tolist()) for x, y in ring])
            m = len(ring)
            for i in range(len(pts) - 1):
                for j in range(m):
                    k = (j + 1) % m
                    mb.quad(rows[i][j], rows[i][k], rows[i + 1][k], rows[i + 1][j])
        for idx, up in ((0, False), (len(pts) - 1, True)):
            n, b = frames[idx]
            for tri in _triangles(p):
                (ax, ay), (bx, by), (cx, cy) = tri
                if (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0:      # 三角剖分不保证逆时针：先统一成逆时针，再按端面朝向翻转
                    tri = (tri[0], tri[2], tri[1])
                ids = [mb.vert(*(pts[idx] + (x - c[0]) * n + (y - c[1]) * b).tolist()) for x, y in tri]
                mb.tri(*(ids if up else ids[::-1]))
    mb.f = orient_mesh(mb.v, mb.f)
    return {"vertices": mb.v, "faces": mb.f, "volume": mesh_volume(mb.v, mb.f), "solid_kind": "sweep"}


# ------------------------------------------------------------------ 布尔（z 向棱柱：精确 slab 路径；其它：manifold3d 网格路径）
def _cuts(slabs_a: list[dict], slabs_b: list[dict]) -> list[float]:
    zs = sorted({round(s[k], 9) for s in slabs_a + slabs_b for k in ("z0", "z1")})
    return zs


def _section_at(slabs: list[dict], z: float):
    for s in slabs:
        if s["z0"] - 1e-9 <= z <= s["z1"] + 1e-9:
            return unary_union([poly_from_json(p) for p in s["polys"]])
    return Polygon()


def boolean_slabs(a: list[dict], b: list[dict], op: str) -> list[dict]:
    """a op b；op ∈ union/subtract/intersect（subtract = a − b）。"""
    zs = _cuts(a, b)
    out: list[dict] = []
    for z0, z1 in zip(zs[:-1], zs[1:]):
        if z1 - z0 < 1e-9:
            continue
        zm = (z0 + z1) / 2
        A, B = _section_at(a, zm), _section_at(b, zm)
        g = {"union": A.union(B), "subtract": A.difference(B), "intersect": A.intersection(B)}[op]
        g = g.buffer(0)
        if g.is_empty or g.area < 1e-9:
            continue
        polys = [poly_to_json(p) for p in _as_multi(g).geoms if p.area > 1e-9]
        if out and abs(out[-1]["z1"] - z0) < 1e-9 and _same(out[-1]["polys"], polys):
            out[-1]["z1"] = z1
        else:
            out.append({"z0": z0, "z1": z1, "polys": polys})
    return out


def _same(pa: list[dict], pb: list[dict]) -> bool:
    ga = unary_union([poly_from_json(p) for p in pa])
    gb = unary_union([poly_from_json(p) for p in pb])
    return ga.symmetric_difference(gb).area < 1e-7


def manifold_available() -> bool:
    """可选依赖 manifold3d 是否可用（任意网格布尔需要它）。"""
    try:
        import manifold3d  # noqa: F401
        return True
    except ImportError:
        return False


def mesh_problems(v: list[list[float]], f: list[list[int]]) -> dict:
    """闭合流形检查：每条有向边只出现一次，且反向边存在（= 每条无向边恰好被两个朝向一致的面共用）。"""
    ed: dict[tuple[int, int], int] = {}
    for a, b, c in f:
        for e in ((a, b), (b, c), (c, a)):
            ed[e] = ed.get(e, 0) + 1
    return {"open_edges": sum(1 for (u, w) in ed if (w, u) not in ed), "same_direction_edges": sum(1 for n in ed.values() if n > 1)}


def _ray_inside(V: np.ndarray, tri: np.ndarray, p: np.ndarray) -> bool:
    """点 p 是否在一个闭合壳（tri：(n,3) 顶点序号）里面：沿一个不规则方向发射线，数穿过的三角形个数，奇数 = 在里面（向量化的 Möller–Trumbore）。"""
    d = np.array([0.5772156649, 0.3141592653, 0.2718281828])
    d /= np.linalg.norm(d)
    a, b, c = V[tri[:, 0]], V[tri[:, 1]], V[tri[:, 2]]
    e1, e2 = b - a, c - a
    h = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, h)
    ok = np.abs(det) > 1e-14
    f = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    sv = p - a
    u = f * np.einsum("ij,ij->i", sv, h)
    q = np.cross(sv, e1)
    v = f * (q @ d)
    t = f * np.einsum("ij,ij->i", e2, q)
    hit = ok & (u >= 0.0) & (u <= 1.0) & (v >= 0.0) & (u + v <= 1.0) & (t > 1e-12)
    return int(hit.sum()) % 2 == 1


def orient_mesh(v: list[list[float]], f: list[list[int]]) -> list[list[int]]:
    """统一朝向并让法线朝外。
    ① 沿共享边传播，让每个连通块内部朝向一致；
    ② 按嵌套深度决定每个连通块是“实体壳”（深度为偶数，体积应为正）还是“内腔壳”（深度为奇数，体积应为负：孔、空腔本来就朝内），
       符号不对的整块翻面。这样带孔截面旋转 360° 得到的内腔、布尔运算挖出的封闭空腔不会被误翻成朝外。非流形的边不参与传播。"""
    faces = [list(x) for x in f]
    by_edge: dict[tuple[int, int], list[int]] = {}
    for i, (a, b, c) in enumerate(faces):
        for u, w in ((a, b), (b, c), (c, a)):
            by_edge.setdefault((min(u, w), max(u, w)), []).append(i)
    seen = [False] * len(faces)
    V = np.array(v, dtype=float)

    def directed(i: int, u: int, w: int) -> bool:
        a, b, c = faces[i]
        return (a, b) == (u, w) or (b, c) == (u, w) or (c, a) == (u, w)

    comps: list[list[int]] = []
    for start in range(len(faces)):
        if seen[start]:
            continue
        comp, stack = [start], [start]
        seen[start] = True
        while stack:
            i = stack.pop()
            a, b, c = faces[i]
            for u, w in ((a, b), (b, c), (c, a)):
                nb = by_edge[(min(u, w), max(u, w))]
                if len(nb) != 2:
                    continue
                j = nb[0] if nb[1] == i else nb[1]
                if seen[j]:
                    continue
                if directed(j, u, w):                       # 邻面沿同一方向走这条边 = 朝向相反，翻它
                    faces[j] = [faces[j][0], faces[j][2], faces[j][1]]
                seen[j] = True
                comp.append(j)
                stack.append(j)
        comps.append(comp)

    def vol(comp: list[int]) -> float:
        return sum(float(np.dot(V[faces[i][0]], np.cross(V[faces[i][1]], V[faces[i][2]]))) for i in comp) / 6.0

    vols = [vol(c) for c in comps]
    if len(comps) == 1 or len(comps) > 64:                  # 单个壳：体积为负就整体翻面；壳太多（>64）不做嵌套分析
        for comp, vl in zip(comps, vols):
            if vl < 0:
                for i in comp:
                    faces[i] = [faces[i][0], faces[i][2], faces[i][1]]
        return faces
    tris = [np.array([faces[i] for i in comp], dtype=int) for comp in comps]
    for k, comp in enumerate(comps):
        probe = V[faces[comp[0]][0]]
        depth = sum(1 for j in range(len(comps)) if j != k and _ray_inside(V, tris[j], probe))
        want_positive = depth % 2 == 0
        if (vols[k] > 0) != want_positive and abs(vols[k]) > 0:
            for i in comp:
                faces[i] = [faces[i][0], faces[i][2], faces[i][1]]
    return faces


def _to_manifold(mf, data: dict, who: str):
    """实体数据 → manifold3d 的 Manifold。棱柱按层挤出再求并（避免阶梯面上的 T 形接缝），其它实体用网格（先检查封闭性、统一朝向）。"""
    if data.get("slabs"):
        parts = []
        for sl in data["slabs"]:
            contours = []
            for pj in sl["polys"]:
                contours.append(np.array(pj["outer"], dtype=float))
                contours += [np.array(h, dtype=float) for h in pj.get("holes", [])]
            cs = mf.CrossSection(contours, mf.FillRule.EvenOdd)
            parts.append(mf.Manifold.extrude(cs, float(sl["z1"] - sl["z0"])).translate((0.0, 0.0, float(sl["z0"]))))
        m = mf.Manifold.batch_boolean(parts, mf.OpType.Add) if len(parts) > 1 else parts[0]
    else:
        faces = orient_mesh(data["vertices"], data["faces"])
        prob = mesh_problems(data["vertices"], faces)
        if prob["open_edges"] or prob["same_direction_edges"]:
            raise GeomError("BOOLEAN_FAILED", f"{who}的网格不是封闭流形（开放边 {prob['open_edges']} 条、朝向冲突 {prob['same_direction_edges']} 条），不能做布尔运算",
                            "重新生成这个实体；旋转体请用 360° 或检查轮廓是否贴着旋转轴", **prob)
        m = mf.Manifold(mf.Mesh64(vert_properties=np.array(data["vertices"], dtype=float), tri_verts=np.array(faces, dtype=np.uint32)))
    if m.status() != mf.Error.NoError:
        raise GeomError("BOOLEAN_FAILED", f"{who}不能转成封闭实体（{m.status().name}）", "检查实体是否自交、是否有重合顶点导致的非流形边")
    if m.is_empty():
        raise GeomError("BOOLEAN_FAILED", f"{who}是空实体", "")
    return m


def boolean_mesh(a_data: dict, b_data: dict, op: str) -> dict:
    """任意实体的并/差/交（manifold3d）。a op b；subtract = a − b。"""
    import manifold3d as mf
    A, B = _to_manifold(mf, a_data, "目标实体"), _to_manifold(mf, b_data, "新实体")
    R = {"union": A + B, "subtract": A - B, "intersect": A ^ B}[op]
    if R.status() != mf.Error.NoError:
        raise GeomError("BOOLEAN_FAILED", f"布尔运算失败（{R.status().name}）", "两个实体可能有自交或非流形的面；换一个位置/尺寸再试")
    parts = R.decompose()                                   # 两个实体只是共面接触时，结果里可能带独立的零体积薄片：丢掉（内腔壳的体积是负的、不小，不会被丢）
    if len(parts) > 1:
        total = max(abs(R.volume()), 1e-300)
        kept = [x for x in parts if abs(x.volume()) > 1e-9 * total]
        if kept and len(kept) != len(parts):
            R = mf.Manifold.compose(kept)
    if R.is_empty() or R.volume() <= 1e-12 * max(A.volume(), B.volume()):               # 相对阈值：小尺寸（如 0.001 的立方体）的真实结果不能被当成“空”
        raise GeomError("EMPTY_RESULT", "布尔运算的结果为空（两个实体没有重叠部分）" if op == "intersect" else "布尔运算的结果为空", "检查两个实体的位置和 z 范围")
    mesh = R.to_mesh64()
    V = np.asarray(mesh.vert_properties, dtype=float)[:, :3]
    F = np.asarray(mesh.tri_verts, dtype=int)
    verts = [[round(float(x), 6), round(float(y), 6), round(float(z), 6)] for x, y, z in V]
    faces = [[int(a), int(b), int(c)] for a, b, c in F]
    return {"vertices": verts, "faces": faces, "volume": float(R.volume()), "solid_kind": "boolean"}


def boolean(a_data: dict, b_data: dict, op: str) -> dict:
    if not a_data.get("slabs") or not b_data.get("slabs"):
        if manifold_available():
            return boolean_mesh(a_data, b_data, op)
        bad = [n for n, d in (("目标实体", a_data), ("新实体", b_data)) if not d.get("slabs")]
        raise GeomError("BOOLEAN_UNSUPPORTED", f"{'、'.join(bad)}不是 z 向棱柱（旋转体、带锥度拉伸、路径扫掠或做过旋转/布尔），而本机没有安装可选依赖 manifold3d，只能对 z 向棱柱做布尔运算",
                        "安装可选依赖后即可对任意实体做布尔：pip install manifold3d（pyproject 的 optional-dependencies.solid）；或改成不带锥度的直拉伸后再布尔", reason="no_manifold3d")
    slabs = boolean_slabs(a_data["slabs"], b_data["slabs"], op)
    if not slabs:
        raise GeomError("EMPTY_RESULT", "布尔运算的结果为空（两个实体没有重叠部分）" if op == "intersect" else "布尔运算的结果为空", "检查两个实体的位置和 z 范围")
    v, f = mesh_from_slabs(slabs)
    return {"vertices": v, "faces": f, "volume": slabs_volume(slabs), "solid_kind": "prism", "slabs": slabs}


def feature_edges(V, F, angle_deg: float = 25.0) -> list[tuple[int, int]]:
    """网格的棱线：只属于一个面的边，或相邻两个面法线夹角大于 angle_deg 的边（顶点按坐标合并后判断）。"""
    import numpy as np
    key = {}
    ids = []
    for v in V:
        k = (round(v[0], 5), round(v[1], 5), round(v[2], 5))
        ids.append(key.setdefault(k, len(key)))
    edges: dict[tuple[int, int], list[int]] = {}
    normals = []
    for fi, f in enumerate(F):
        p = [np.array(V[i]) for i in f[:3]]
        n = np.cross(p[1] - p[0], p[2] - p[0])
        L = np.linalg.norm(n)
        normals.append(n / L if L > 0 else n)
        for i in range(len(f)):
            a, b = ids[f[i]], ids[f[(i + 1) % len(f)]]
            if a != b:
                edges.setdefault((min(a, b), max(a, b)), []).append(fi)
    rep = {}
    for i, k in enumerate(ids):
        rep.setdefault(k, i)
    cosine = math.cos(math.radians(angle_deg))
    out = []
    for (a, b), fs in edges.items():
        if len(fs) == 1 or (len(fs) >= 2 and float(np.dot(normals[fs[0]], normals[fs[1]])) < cosine):
            out.append((rep[a], rep[b]))
    return out


# ------------------------------------------------------------------ 导出
def to_stl_ascii(name: str, meshes: Iterable[tuple[list[list[float]], list[list[int]]]]) -> str:
    lines = [f"solid {name}"]
    for v, f in meshes:
        V = np.array(v, dtype=float)
        for a, b, c in f:
            n = np.cross(V[b] - V[a], V[c] - V[a])
            L = np.linalg.norm(n)
            n = n / L if L > 0 else n
            lines.append(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n    outer loop")
            for i in (a, b, c):
                lines.append(f"      vertex {V[i][0]:.6f} {V[i][1]:.6f} {V[i][2]:.6f}")
            lines.append("    endloop\n  endfacet")
    lines.append(f"endsolid {name}")
    return "\n".join(lines) + "\n"


def mesh_bbox(v: list[list[float]]) -> tuple[float, float, float, float, float, float]:
    xs, ys, zs = zip(*v)
    return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)


def translate_slabs(slabs: list[dict], dx: float, dy: float, dz: float) -> list[dict]:
    out = []
    for s in slabs:
        polys = []
        for p in s["polys"]:
            polys.append({"outer": [[x + dx, y + dy] for x, y in p["outer"]], "holes": [[[x + dx, y + dy] for x, y in h] for h in p["holes"]]})
        out.append({"z0": s["z0"] + dz, "z1": s["z1"] + dz, "polys": polys})
    return out
