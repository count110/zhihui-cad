"""封头的回转轮廓（纯数学，确定性）：以封头切线（直边与曲面的分界）为 0、朝封头外为正，给出内/外表面在半径 x 处的高度。

EHA：2:1 标准椭圆，内曲面深度 = Di/4（定义）；外表面按 (a+t, b+t) 的椭圆画（制图上的常用近似，在切线处与筒体外壁连续）。
HHA：半球。THA：碟形（球面 + 过渡区两段圆弧，内半径由 rules.json 的 tha_crown_ratio / tha_knuckle_ratio 给出，外表面为等距偏移）。
SDH：球冠（无过渡区）。FLAT：平盖。CONE（P2）不支持。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from zhihui.cad.geom.errors import GeomError

from . import std_tables as T


@dataclass
class HeadProfile:
    kind: str
    a: float              # 内半径 Ri
    t: float              # 名义厚度（真实值，不夸大）
    hsf: float            # 直边高度
    hc: float             # 内曲面深度（切线到内表面顶点）
    rc: float = 0.0       # THA/SDH 球面内半径
    rk: float = 0.0       # THA 过渡区内半径

    # ---- 内外表面：切线以上的高度；x 为到轴线的距离（取绝对值）
    def inner_y(self, x: float) -> float:
        return self._y(abs(x), 0.0)

    def outer_y(self, x: float) -> float:
        return self._y(abs(x), self.t)

    def _y(self, x: float, off: float) -> float:
        a, k = self.a, self.kind
        if k == "EHA":
            A, B = a + off, self.hc + off
            return B * math.sqrt(max(0.0, 1 - (x / A) ** 2)) if x <= A else 0.0
        if k == "HHA":
            R = a + off
            return math.sqrt(max(0.0, R * R - x * x)) if x <= R else 0.0
        if k == "THA":
            rc, rk = self.rc + off, self.rk + off
            yc = self._yc()
            xt = (a - self.rk) * rc / (self.rc - self.rk)               # 球面与过渡区的切点（两圆心连线上）
            if x <= xt:
                return yc + math.sqrt(max(0.0, rc * rc - x * x))
            dx = x - (a - self.rk)
            return math.sqrt(max(0.0, rk * rk - dx * dx)) if dx <= rk else 0.0
        if k == "SDH":
            rc = self.rc + off
            yc = -math.sqrt(max(0.0, self.rc ** 2 - a * a))
            return max(0.0, yc + math.sqrt(max(0.0, rc * rc - x * x)))
        if k == "FLAT":
            return off if x <= a + off else 0.0
        raise GeomError("HEAD_UNSUPPORTED", f"封头型式 {k} 在 P0 不支持", "P0 支持 EHA/HHA/THA/SDH/FLAT")

    def _yc(self) -> float:
        """THA 球面圆心（切线以下）。"""
        return -math.sqrt(max(0.0, (self.rc - self.rk) ** 2 - (self.a - self.rk) ** 2))

    @property
    def outer_height(self) -> float:
        """切线到外表面顶点（不含直边）。"""
        return self.outer_y(0.0)

    @property
    def total_height(self) -> float:
        """环焊缝到外表面顶点：直边 + 内曲面深度 + 厚度（FLAT 为厚度）。"""
        return self.hsf + self.outer_y(0.0)

    @property
    def inner_depth(self) -> float:
        """环焊缝到内表面顶点。"""
        return self.hsf + self.hc


def inner_depth_of(kind: str, di: float) -> tuple[float, float, float]:
    """(内曲面深度 hc, 球面内半径 rc, 过渡区内半径 rk)。"""
    a = di / 2
    if kind == "EHA":
        return di / 4, 0.0, 0.0
    if kind == "HHA":
        return a, 0.0, 0.0
    if kind == "THA":
        rc, rk = T.rule_value("tha_crown_ratio") * di, T.rule_value("tha_knuckle_ratio") * di
        yc = -math.sqrt(max(0.0, (rc - rk) ** 2 - (a - rk) ** 2))
        return yc + rc, rc, rk
    if kind == "SDH":
        rc = T.rule_value("sdh_crown_ratio") * di
        return rc - math.sqrt(max(0.0, rc * rc - a * a)), rc, 0.0
    if kind == "FLAT":
        return 0.0, 0.0, 0.0
    raise GeomError("HEAD_UNSUPPORTED", f"封头型式 {kind} 在 P0 不支持（锥形封头放在 P2）", "改用 EHA（标准椭圆）、HHA、THA、SDH 或 FLAT")


def profile(kind: str, di: float, t: float, hsf: float) -> HeadProfile:
    hc, rc, rk = inner_depth_of(kind, di)
    return HeadProfile(kind, di / 2, t, hsf, hc, rc, rk)


def head_profile(spec, head) -> HeadProfile:
    """从参数取封头轮廓（参数必须已经补齐：kind、thickness_mm、straight_flange_mm）。"""
    return profile(head.kind or "EHA", float(spec.shell.inner_diameter_mm), float(head.thickness_mm or spec.shell.thickness_mm or 0.0), float(head.straight_flange_mm or 0.0))
