"""制图标准配置（对应 CATIA 的 GB.xml）：所有制图规则集中在 standards/*.json，出图时统一读取。

内置三套：
  gb_general    技术制图通用：GB/T 14689 图幅、GB/T 14690 比例、GB/T 14691 字体、GB/T 17450 / GB/T 4457.4 图线、GB/T 4458.4 尺寸
  hg_pfd        化工流程图：在通用基础上叠加 HG/T 20519
  water_supply  给水排水：具体条文以用户提供的标准为准，没有依据的项沿用 gb_general
配置用 "extends" 继承，子配置里写的键覆盖父配置（字典逐层合并）。用户可以放自己的 JSON 到 standards/ 下。
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

STD_DIR = Path(__file__).with_name("standards")
DEFAULT_BY_TEMPLATE = {"pfd_default": "hg_pfd", "water_elevation": "water_supply"}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def is_cjk(text: str) -> bool:
    return any(ord(ch) > 0x2E80 for ch in text or "")


class Standard:
    def __init__(self, raw: dict):
        self.raw = raw
        self.id: str = raw["id"]
        self.name: str = raw.get("name", self.id)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    # ---------------------------------------------------------------- 图幅
    def paper_sizes(self) -> dict[str, tuple[float, float]]:
        return {k: tuple(v) for k, v in self.raw["paper"]["sizes"].items()}

    def base_format(self, paper: str) -> str:
        """加长幅面按基本幅面取留边、字高和图元大小（A3×3 → A3；按文章每次加长 1/4 的 A1+2/4 → A1）。"""
        import re

        m = re.fullmatch(r"(A[0-4])\+[1-9]/4", str(paper))
        if m:
            return m.group(1)
        return self.raw["paper"]["base"].get(paper, paper)

    def is_paper(self, paper: str) -> bool:
        """GB/T 14689 基本 / 加长幅面，或文章「每次加长图样宽度的 1/4」的加长幅面。"""
        return paper in self.paper_sizes() or self.base_format(paper) != paper

    def margins(self, paper: str, binding: bool | None = None) -> tuple[float, float, float, float]:
        """(左, 下, 右, 上) 留边，图纸 mm。留装订边时左边 a、其余 c；不留时四边 e。"""
        m = self.raw["paper"]["margins"][self.base_format(paper)]
        binding = self.raw["paper"].get("binding", False) if binding is None else binding
        return (m["a"], m["c"], m["c"], m["c"]) if binding else (m["e"],) * 4

    # ---------------------------------------------------------------- 字体
    @property
    def heights(self) -> list[float]:
        return list(self.raw["text"]["heights"])

    @property
    def cjk_min(self) -> float:
        return float(self.raw["text"]["cjk_min"])

    @property
    def width_factor(self) -> float:
        return float(self.raw["text"]["width_factor"])

    def text_height(self, role: str, paper: str = "A3", text: str = "") -> float:
        """某类文字的字高（图纸 mm，取自字高系列）：按图幅升档（A3/A4 不升，A2/A1 升一档，A0 升两档），含汉字时不低于 3.5。"""
        t = self.raw["text"]
        hs = self.heights
        base = float(t["roles"].get(role, t["roles"]["default"]))
        idx = min(range(len(hs)), key=lambda i: abs(hs[i] - base))
        step = t["paper_step"].get(self.base_format(paper), 0)
        h = hs[min(idx + step, len(hs) - 1)]
        if is_cjk(text) and h < self.cjk_min:
            h = min(x for x in hs if x >= self.cjk_min)
        return h

    def min_height(self, text: str) -> float:
        return self.cjk_min if is_cjk(text) else self.heights[0]

    def fit_height(self, role: str, paper: str, text: str, max_width: float) -> float:
        """放进给定宽度（图纸 mm）的最大字高：从该类文字的字高往下一档档试，不低于最小字高。"""
        h0 = self.text_height(role, paper, text)
        for h in sorted((x for x in self.heights if x <= h0), reverse=True):
            if h < self.min_height(text):
                break
            if self.text_width(text, h) <= max_width:
                return h
        return max(self.min_height(text), min(self.heights))

    def text_width(self, text: str, height: float, width_factor: float | None = None) -> float:
        """估算单行文字宽度（与字高同单位）：汉字 = 字高 × 宽度因子 × cjk_advance；字母数字 = 字高 × latin_advance
        （字宽约 0.5h + 字母间距 a=0.2h，GB/T 14691 表 2 B 型）；空格按词间距 e=0.6h。"""
        t = self.raw["text"]
        wf = self.width_factor if width_factor is None else width_factor
        cjk, latin = height * wf * t["cjk_advance"], height * t["latin_advance"]
        e = height * self.spacing("e")
        return sum(cjk if ord(ch) > 0x2E80 else (e if ch == " " else latin) for ch in text)

    def spacing(self, key: str) -> float:
        """GB/T 14691 表 1 / 表 2 的比例（乘字高）：a 字母间距、b2 / b3 基准线最小间距、e 词间距、d 笔画宽。"""
        t = self.raw["text"]
        return float(t["b_type" if t.get("latin_type", "B") == "B" else "a_type"][key])

    def line_pitch(self, height: float, caps_only: bool = False) -> float:
        """多行文字的基准线间距（GB/T 14691 表 2）：只有大写字母时 b3=13/10h，否则 b2=15/10h（无发音符号）。"""
        return height * self.spacing("b3" if caps_only else "b2")

    # ---------------------------------------------------------------- 比例
    def scale_series(self, lo: float = 1e-3, hi: float = 1e6) -> list[float]:
        """比例系列里所有的 N（1:N；N < 1 为放大比例），按大小排序。"""
        s = self.raw["scale"]
        out = {1.0}
        for n in range(-4, 8):
            for m in s["preferred"] + s["allowed"]:
                v = m * 10**n
                if v >= 1 and lo <= v <= hi:
                    out.add(round(v, 9))
            for m in s.get("enlarge_preferred", []) + s.get("enlarge_allowed", []):
                v = 1 / (m * 10**n)
                if v < 1 and lo <= v <= hi:
                    out.add(round(v, 12))
        return sorted(out)

    def is_series_scale(self, n: float) -> bool:
        return any(abs(n - v) <= 1e-6 * max(v, 1.0) for v in self.scale_series())

    def nearest_scales(self, n: float, k: int = 2) -> list[float]:
        vals = self.scale_series()
        return sorted(sorted(vals, key=lambda v: abs(math.log(v / n)))[:k]) if n > 0 else vals[:k]

    # ---------------------------------------------------------------- 图线
    def lineweight(self, kind: str) -> float:
        """thick / medium / thin → 线宽（mm）。"""
        return float(self.raw["lines"]["group"][kind])

    def lw100(self, kind: str) -> int:
        """DXF 线宽（0.01 mm）。"""
        return int(round(self.lineweight(kind) * 100))


@lru_cache(maxsize=16)
def load(std_id: str) -> Standard:
    f = STD_DIR / f"{std_id}.json"
    if not f.exists():
        raise KeyError(std_id)
    raw = json.loads(f.read_text(encoding="utf-8"))
    parent = raw.get("extends")
    if parent:
        raw = _merge(load(parent).raw, raw)  # 子配置写了的键覆盖父配置，id / name / extends 都取子配置的
    return Standard(raw)


def available() -> list[str]:
    return sorted(p.stem for p in STD_DIR.glob("*.json"))


def for_drawing(d) -> Standard:
    sid = getattr(d, "standard", "") or DEFAULT_BY_TEMPLATE.get(getattr(d, "template_id", ""), "gb_general")
    return load(sid)


def text_h(d, role: str, text: str = "") -> float:
    """出图用的字高（模型单位）：字高系列里的值 × 出图比例，不再跟着图幅放大系数 d.unit 走。"""
    return for_drawing(d).text_height(role, d.paper_size, text) * d.scale


def fit_h(d, role: str, text: str, max_width_model: float) -> float:
    """放进给定宽度（模型单位）的最大合规字高（模型单位）。"""
    return for_drawing(d).fit_height(role, d.paper_size, text, max_width_model / d.scale) * d.scale


def text_width(text: str, height: float, width_factor: float | None = None) -> float:
    return load("gb_general").text_width(text, height, width_factor)


# 图层：角色 → 层名（ZH-*），出图代码只用角色，层名、颜色、线型、线宽都在配置里（GB/T 14665 图层约定）
LY: dict[str, str] = dict(load("gb_general").raw["layers"]["roles"])
PIPE_CODE_PREFIX: str = load("gb_general").raw["layers"]["pipe_code_prefix"]


def layer_table(std: Standard) -> dict[str, dict]:
    """层名 → {color, linetype, lineweight(0.01 mm)}；线宽按本标准的线宽组。"""
    out = {}
    suffix = std.raw["layers"].get("split_suffix", "-分段")
    for name, spec in std.raw["layers"]["table"].items():
        lw = std.lw100(spec["weight"])
        no = spec["linetype"]
        lt = linetype_name(no, lw) if no in std.raw["linetypes"] else no
        out[name] = {"color": spec["color"], "linetype": lt, "lineweight": lw}
        if no in std.raw["linetypes"]:  # 按线素相位拆成的短线段放这里：同色同宽、实线（GB/T 17450 第 5.2.1 条）
            out[name + suffix] = {"color": spec["color"], "linetype": "Continuous", "lineweight": lw, "split_of": name, "kind": linetype_kind(lt)}
    return out


def linetype_name(no: str, lw100: int) -> str:
    """GB/T 17450 线型 No. + 线宽 → DXF 线型名，如 ZH_02_d070（0.7 mm 线上的虚线）。"""
    return f"ZH_{no}_d{lw100:03d}"


def linetype_elements(std: Standard, no: str, d: float) -> list[float]:
    """线素长度（图纸 mm）：GB/T 17450 表 3 的 d 的倍数乘线宽 d；点（0）保持为点。"""
    return [e * d for e in std.raw["linetypes"][no]["elements_d"]]


def linetype_patterns(std: Standard) -> dict[str, tuple[str, list[float]]]:
    """线型名 → (说明, 线素长度 mm)。每个用到的「线型 No. × 所在图层线宽」各生成一份（GB/T 17450 第 4.3 条表 3，d 为线宽），
    另外保留旧名 CENTER / DASHED / PHANTOM（按细线宽），兼容以前导出的图和用户手工加的图线。"""
    out = {}
    for spec in std.raw["layers"]["table"].values():
        no = spec["linetype"]
        if no in std.raw["linetypes"]:
            lw = std.lw100(spec["weight"])
            name = linetype_name(no, lw)
            out[name] = (f"GB/T 17450 No.{no} {std.raw['linetypes'][no]['name']} d={lw / 100:g}", linetype_elements(std, no, lw / 100))
    thin = std.lineweight("thin")
    for legacy, no in (("CENTER", "04"), ("DASHED", "02"), ("PHANTOM", "05")):
        out[legacy] = (f"GB/T 17450 No.{no} {std.raw['linetypes'][no]['name']}（旧名）", linetype_elements(std, no, thin))
    return out


def linetype_kind(name: str) -> str:
    """线型 → 图线类别（重合优先级用）：04 点画线 = center，02 / 03 / 05 = hidden，其余 visible。"""
    n = (name or "").upper()
    if n.startswith("ZH_04") or n == "CENTER":
        return "center"
    if n.startswith(("ZH_02", "ZH_03", "ZH_05")) or n in ("DASHED", "PHANTOM"):
        return "hidden"
    return "visible"
