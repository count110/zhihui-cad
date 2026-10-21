"""正交管线布线。

做法：用所有设备包围盒的边线、通道中线和端口坐标拼出一张非均匀网格
（正交可见性图），在上面跑带转弯惩罚的 Dijkstra。端口坐标一定在网格上，
所以管线总是严格水平/垂直地接到端口。结果确定：同一输入永远得到同一路径。
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass

from zhihui.ir import Connection, Drawing

from .symbols import CLEARANCE, DIRS, WATER, Box, inflate, label_box, port_point, symbol_box, tag_strip, usable_area

Pt = tuple[float, float]
EPS = 1e-6

# 代价（图纸 mm，会乘 scale）
BEND_COST = 12.0
CROSS_COST = 6.0
OVERLAP_FACTOR = 20.0  # 与已有管线共线重叠时，长度代价放大倍数
OVERLAP_FIXED = 60.0  # 与已有管线完全共线重合（哪怕很短）再加的固定代价：短段重合按长度罚不住（C001 放空管汇合处 1.5 mm）
STUB = CLEARANCE + 1.0  # 端口引出段长度


@dataclass
class Segments:
    horizontal: list[tuple[float, float, float]]  # (y, x0, x1)
    vertical: list[tuple[float, float, float]]  # (x, y0, y1)


def path_segments(path: list[Pt]) -> list[tuple[Pt, Pt]]:
    return list(zip(path, path[1:]))


def collect_segments(conns: list[Connection]) -> Segments:
    segs = Segments([], [])
    for c in conns:
        if not c.path:
            continue
        for (x0, y0), (x1, y1) in path_segments(c.path):
            if abs(y0 - y1) < EPS and abs(x0 - x1) > EPS:
                segs.horizontal.append((y0, min(x0, x1), max(x0, x1)))
            elif abs(x0 - x1) < EPS and abs(y0 - y1) > EPS:
                segs.vertical.append((x0, min(y0, y1), max(y0, y1)))
    return segs


def simplify(path: list[Pt]) -> list[Pt]:
    out: list[Pt] = []
    for p in path:
        if out and abs(out[-1][0] - p[0]) < EPS and abs(out[-1][1] - p[1]) < EPS:
            continue
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            if (abs(a[0] - b[0]) < EPS and abs(b[0] - p[0]) < EPS) or (
                abs(a[1] - b[1]) < EPS and abs(b[1] - p[1]) < EPS
            ):
                out[-1] = p
                continue
        out.append(p)
    return out


def equipment_obstacles(d: Drawing) -> list[Box]:
    """障碍区：每台设备本体外扩净距，文字框单独作为一块（不合并，免得盖住端口）。"""
    s = d.unit
    out = []
    for e in d.equipment:
        if e.placed:
            out.append(inflate(symbol_box(e, s), CLEARANCE * s))
            out.append(inflate(label_box(e, s, d.template_id), 0.5 * s))
    return out


class Router:
    def __init__(self, d: Drawing):
        self.d = d
        self.s = d.unit
        self.obstacles = equipment_obstacles(d)
        if d.template_id == WATER:
            from .water import legend_box, system_regions  # 管线不穿过右上角图例和子系统标题

            leg = legend_box(d)
            if leg:
                self.obstacles.append(inflate(leg, 1.0 * self.s))
            self.obstacles += [inflate(r["title_box"], 0.5 * self.s) for r in system_regions(d)]
        else:
            from .chem import nozzle_boxes  # 施工流程图的设备管口，管线让开

            self.obstacles += nozzle_boxes(d)
        ux0, uy0, ux1, _ = usable_area(d)
        m = 3 * self.s  # 管线可以略微越出设备区，但不进位号栏、不压标题栏
        self.bounds = (ux0 - m, uy0 - m, ux1 + m, tag_strip(d)[1] - 1 * self.s)
        self.stub_boxes = self._stub_boxes() if d.template_id != WATER else []
        self._skip: set = set()
        self._base_xs, self._base_ys = self._base_coords()
        for (x0, y0, x1, y1), _ in self.stub_boxes:  # 让路径能从别的端口引出段旁边绕过去
            self._base_xs.update((x0, x1))
            self._base_ys.update((y0, y1))

    def _stub_boxes(self) -> list:
        """每个连接端口的引出段（端口 → 引出段末端，再往外 1 mm）：别的管线穿过它要加罚（C001：一根管线正好压过另一根的引出段末端，
        后者只能沿着它走，两根重合）。只罚不挡，端口密的地方仍能布通。"""
        out, seen = [], set()
        stub = STUB * self.s
        for c in self.d.connections:
            for eid, port in ((c.from_equip, c.from_port), (c.to_equip, c.to_port)):
                e = self.d.get_equipment(eid)
                if e is None or not e.placed or (eid, port) in seen:
                    continue
                seen.add((eid, port))
                try:
                    (px, py), dr = port_point(e, port, self.s)
                except KeyError:
                    continue
                vx, vy = DIRS[dr]
                ex, ey = px + vx * (stub + 1.0 * self.d.scale), py + vy * (stub + 1.0 * self.d.scale)
                w = 0.5 * self.d.scale
                out.append(((min(px, ex) - (w if vx == 0 else 0), min(py, ey) - (w if vy == 0 else 0),
                             max(px, ex) + (w if vx == 0 else 0), max(py, ey) + (w if vy == 0 else 0)), (eid, port)))
        return out

    def _base_coords(self) -> tuple[set[float], set[float]]:
        xs: set[float] = {self.bounds[0], self.bounds[2]}
        ys: set[float] = {self.bounds[1], self.bounds[3]}
        for x0, y0, x1, y1 in self.obstacles:
            xs.update((x0, x1))
            ys.update((y0, y1))
        # 通道中线：相邻两条障碍边线之间的中点，让管线走在空隙正中
        for coords in (xs, ys):
            srt = sorted(coords)
            coords.update((a + b) / 2 for a, b in zip(srt, srt[1:]) if b - a > 2 * self.s)
        return xs, ys

    def _blocked(self, a: Pt, b: Pt) -> bool:
        (xa, ya), (xb, yb) = a, b
        lo_x, hi_x = min(xa, xb), max(xa, xb)
        lo_y, hi_y = min(ya, yb), max(ya, yb)
        for x0, y0, x1, y1 in self.obstacles:
            if lo_x < x1 - EPS and hi_x > x0 + EPS and lo_y < y1 - EPS and hi_y > y0 + EPS:
                return True
            # 零长度方向上落在障碍内部
            if abs(xa - xb) < EPS and x0 + EPS < xa < x1 - EPS and lo_y < y1 - EPS and hi_y > y0 + EPS:
                return True
            if abs(ya - yb) < EPS and y0 + EPS < ya < y1 - EPS and lo_x < x1 - EPS and hi_x > x0 + EPS:
                return True
        return False

    def _inside(self, p: Pt) -> bool:
        x, y = p
        return any(x0 + EPS < x < x1 - EPS and y0 + EPS < y < y1 - EPS for x0, y0, x1, y1 in self.obstacles)

    def _edge_penalty(self, a: Pt, b: Pt, segs: Segments) -> float:
        (xa, ya), (xb, yb) = a, b
        length = abs(xa - xb) + abs(ya - yb)
        cost = 0.0
        if abs(ya - yb) < EPS:  # 水平
            lo, hi = min(xa, xb), max(xa, xb)
            for x, y0, y1 in segs.vertical:
                if lo + EPS < x < hi - EPS and y0 + EPS < ya < y1 - EPS:
                    cost += CROSS_COST * self.s
            for y, x0, x1 in segs.horizontal:
                if abs(y - ya) < CLEARANCE * self.s / 2 and min(hi, x1) - max(lo, x0) > EPS:
                    cost += length * OVERLAP_FACTOR + (OVERLAP_FIXED * self.s if abs(y - ya) < EPS else 0.0)
        else:
            lo, hi = min(ya, yb), max(ya, yb)
            for y, x0, x1 in segs.horizontal:
                if lo + EPS < y < hi - EPS and x0 + EPS < xa < x1 - EPS:
                    cost += CROSS_COST * self.s
            for x, y0, y1 in segs.vertical:
                if abs(x - xa) < CLEARANCE * self.s / 2 and min(hi, y1) - max(lo, y0) > EPS:
                    cost += length * OVERLAP_FACTOR + (OVERLAP_FIXED * self.s if abs(x - xa) < EPS else 0.0)
        for (x0, y0, x1, y1), key in self.stub_boxes:
            if key in self._skip:
                continue
            if min(xa, xb) < x1 - EPS and max(xa, xb) > x0 + EPS and min(ya, yb) < y1 - EPS and max(ya, yb) > y0 + EPS:
                cost += OVERLAP_FIXED * self.s
            elif abs(xa - xb) < EPS and x0 + EPS < xa < x1 - EPS and min(ya, yb) < y1 - EPS and max(ya, yb) > y0 + EPS:
                cost += OVERLAP_FIXED * self.s
            elif abs(ya - yb) < EPS and y0 + EPS < ya < y1 - EPS and min(xa, xb) < x1 - EPS and max(xa, xb) > x0 + EPS:
                cost += OVERLAP_FIXED * self.s
        return cost

    def route(self, c: Connection, segs: Segments) -> list[Pt] | None:
        d = self.d
        self._skip = {(c.from_equip, c.from_port), (c.to_equip, c.to_port)}
        fe, te = d.get_equipment(c.from_equip), d.get_equipment(c.to_equip)
        if fe is None or te is None or not fe.placed or not te.placed:
            return None
        (p0, dir0), (p1, dir1) = port_point(fe, c.from_port, self.s), port_point(te, c.to_port, self.s)
        v0, v1 = DIRS[dir0], DIRS[dir1]
        stub = STUB * self.s
        s0 = (p0[0] + v0[0] * stub, p0[1] + v0[1] * stub)
        s1 = (p1[0] + v1[0] * stub, p1[1] + v1[1] * stub)
        if self._inside(s0) or self._inside(s1):
            return None

        xs = sorted(self._base_xs | {s0[0], s1[0]})
        ys = sorted(self._base_ys | {s0[1], s1[1]})
        xi = {x: i for i, x in enumerate(xs)}
        yi = {y: i for i, y in enumerate(ys)}
        start, goal = (xi[s0[0]], yi[s0[1]]), (xi[s1[0]], yi[s1[1]])
        bend = BEND_COST * self.s
        arrive_dir = (-v1[0], -v1[1])  # 最后一段进入端口的方向

        counter = itertools.count()
        best: dict[tuple, float] = {}
        prev: dict[tuple, tuple | None] = {}
        st = (start, v0)
        best[st] = 0.0
        prev[st] = None
        heap = [(0.0, next(counter), st)]
        found = None
        while heap:
            cost, _, state = heapq.heappop(heap)
            if cost > best.get(state, float("inf")) + EPS:
                continue
            (i, j), cur_dir = state
            if (i, j) == goal:
                found = state
                break
            for dv in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if dv == (-cur_dir[0], -cur_dir[1]):
                    continue
                ni, nj = i + dv[0], j + dv[1]
                if not (0 <= ni < len(xs) and 0 <= nj < len(ys)):
                    continue
                a, b = (xs[i], ys[j]), (xs[ni], ys[nj])
                if self._inside(b) or self._blocked(a, b):
                    continue
                step = abs(a[0] - b[0]) + abs(a[1] - b[1]) + self._edge_penalty(a, b, segs)
                if dv != cur_dir:
                    step += bend
                if (ni, nj) == goal and dv != arrive_dir:
                    step += bend
                nst = ((ni, nj), dv)
                ncost = cost + step
                if ncost < best.get(nst, float("inf")) - EPS:
                    best[nst] = ncost
                    prev[nst] = state
                    heapq.heappush(heap, (ncost, next(counter), nst))
        if found is None:
            return None
        pts: list[Pt] = []
        node = found
        while node is not None:
            (i, j), _ = node
            pts.append((xs[i], ys[j]))
            node = prev[node]
        pts.reverse()
        return simplify([p0, *pts, p1])


def route_connections(d: Drawing, conn_ids: set[str] | None = None) -> list[str]:
    """给指定连接（默认全部）重新布线，其他管线保持不动并作为避让对象。

    返回布线失败的连接 id 列表。
    """
    targets = [c for c in d.connections if conn_ids is None or c.id in conn_ids]
    for c in targets:
        c.path = None
    router = Router(d)
    fixed = [c for c in d.connections if c.path]

    def manhattan(c: Connection) -> float:
        fe, te = d.get_equipment(c.from_equip), d.get_equipment(c.to_equip)
        if not (fe and te and fe.placed and te.placed):
            return float("inf")
        return abs(fe.x - te.x) + abs(fe.y - te.y)

    failed = []
    for c in sorted(targets, key=lambda c: (manhattan(c), c.id)):
        segs = collect_segments(fixed)
        path = router.route(c, segs)
        if path is None:
            failed.append(c.id)
            continue
        c.path = [(round(x, 3), round(y, 3)) for x, y in path]
        fixed.append(c)
    merge_shared_ports(d)
    return failed


def _shares(d: Drawing, c: Connection, end: str) -> Connection | None:
    """与 c 共用同一端口（end = from / to）且 id 更小的那条管线（作为干管）。"""
    key = (c.from_equip, c.from_port) if end == "from" else (c.to_equip, c.to_port)
    for o in sorted(d.connections, key=lambda x: x.id):
        if o.id == c.id or not o.path:
            continue
        if ((o.from_equip, o.from_port) if end == "from" else (o.to_equip, o.to_port)) == key:
            return o if o.id < c.id else None
    return None


def _on_path(p: Pt, path: list[Pt], tol: float = 1e-3) -> bool:
    for (x0, y0), (x1, y1) in path_segments(path):
        if abs(x0 - x1) < EPS and abs(p[0] - x0) < tol and min(y0, y1) - tol <= p[1] <= max(y0, y1) + tol:
            return True
        if abs(y0 - y1) < EPS and abs(p[1] - y0) < tol and min(x0, x1) - tol <= p[0] <= max(x0, x1) + tol:
            return True
    return False


def _first_touch(path: list[Pt], other: list[Pt]) -> tuple[int, Pt] | None:
    """沿 path 走，第一次碰到 other 的位置：(所在段序号, 交点)。"""
    for k, (a, b) in enumerate(path_segments(path)):
        cands = [a] + [v for v in other if _on_seg(v, a, b)] + [q for q in _seg_cross(a, b, other)]
        hits = [q for q in cands if _on_path(q, other)]
        if hits:
            return k, min(hits, key=lambda q: abs(q[0] - a[0]) + abs(q[1] - a[1]))
    return None


def _on_seg(p: Pt, a: Pt, b: Pt, tol: float = 1e-3) -> bool:
    return _on_path(p, [a, b], tol)


def _seg_cross(a: Pt, b: Pt, other: list[Pt]) -> list[Pt]:
    out = []
    for c0, c1 in path_segments(other):
        if abs(a[1] - b[1]) < EPS and abs(c0[0] - c1[0]) < EPS:  # 横 × 竖
            x, y = c0[0], a[1]
        elif abs(a[0] - b[0]) < EPS and abs(c0[1] - c1[1]) < EPS:  # 竖 × 横
            x, y = a[0], c0[1]
        else:
            continue
        if _on_seg((x, y), a, b) and _on_seg((x, y), c0, c1):
            out.append((x, y))
    return out


def merge_shared_ports(d: Drawing) -> None:
    """两条管线接同一个端口时，后一条在第一次碰到前一条的地方并入（三通），不再重叠画到端口（GB/T 4457.4：图线重合只画一条）。

    接同一个进口：后一条的尾巴截到交点；从同一个出口出发：后一条的开头从离开前一条的地方算起。
    """
    for c in sorted(d.connections, key=lambda x: x.id):
        if not c.path or len(c.path) < 2:
            continue
        trunk = _shares(d, c, "to")
        if trunk is not None:
            hit = _first_touch(c.path, trunk.path)
            if hit is not None and hit[1] != tuple(c.path[0]):
                k, q = hit
                c.path = simplify([*c.path[: k + 1], q])
        trunk = _shares(d, c, "from")
        if trunk is not None:
            rev = list(reversed(c.path))
            hit = _first_touch(rev, trunk.path)
            if hit is not None and hit[1] != tuple(rev[0]):
                k, q = hit
                c.path = simplify(list(reversed([*rev[: k + 1], q])))


def endpoint_ok(d: Drawing, c: Connection, end: str, port_xy: Pt, tol: float) -> bool:
    """管线端点接在端口上，或者（共用端口时）接在共用这个端口的另一条管线上（三通）。"""
    p = c.path[0] if end == "from" else c.path[-1]
    if abs(p[0] - port_xy[0]) + abs(p[1] - port_xy[1]) <= tol:
        return True
    key = (c.from_equip, c.from_port) if end == "from" else (c.to_equip, c.to_port)
    for o in d.connections:
        if o.id != c.id and o.path and ((o.from_equip, o.from_port) if end == "from" else (o.to_equip, o.to_port)) == key:
            if _on_path(p, o.path, tol):
                return True
    return False


def tee_points(d: Drawing) -> list[tuple[str, Pt]]:
    """三通（并管处）的位置，出图时画实心圆点。"""
    out = []
    for c in d.connections:
        if not c.path:
            continue
        for end, p in (("from", c.path[0]), ("to", c.path[-1])):
            key = (c.from_equip, c.from_port) if end == "from" else (c.to_equip, c.to_port)
            others = [o for o in d.connections if o.id != c.id and o.path and ((o.from_equip, o.from_port) if end == "from" else (o.to_equip, o.to_port)) == key]
            if any(_on_path(p, o.path) and tuple(p) not in (tuple(o.path[0]), tuple(o.path[-1])) for o in others):
                out.append((c.id, (p[0], p[1])))
    return out
