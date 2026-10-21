"""图元库（图元知识库）加载与几何计算。

图元 JSON 放在 symbols/ 目录，一种设备一个文件，尺寸单位为「图纸 mm」，原点在图元中心附近。
每个图元除图形和端口外，还记录：
  category  设备类别代号（HG/T 20519.2 表 10.0.2），位号前缀由它决定
  family    同类图元的分组（如塔类），评测时同组图元视为等价
  source    图形出处（标准号 + 表号 + 页码），未列入标准的写明「按实物外形简化」
  status    standard（按标准绘制）/ article（按文章表 2-4 扫描图绘制，verified=false）/ simplified（标准未列入，待化工专业确认）/
            generated（检索后自动生成，见 cad/symgen.py，须人工确认）
  nozzles   与配管和外界有关的管口（排液、放空、仪表接口），只在施工流程图画（文章 3.1.3 节），不改 bbox
几何函数返回模型空间 mm：图元尺寸乘 drawing.unit，图框、标题栏乘 drawing.scale。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from zhihui.ir import Drawing, Equipment

SYMBOL_DIR = Path(__file__).with_name("symbols")

DIRS: dict[str, tuple[int, int]] = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}

# HG/T 20519.2-2009 表 10.0.2 设备类别代号
CATEGORIES: dict[str, str] = {
    "T": "塔", "P": "泵", "C": "压缩机、风机", "E": "换热器", "R": "反应器", "F": "工业炉",
    "S": "火炬、烟囱", "V": "容器（槽、罐）", "L": "起重运输设备", "W": "计量设备",
    "M": "其他机械", "X": "其他设备",
}

# 文字高度（图纸 mm，随图幅等比放大）
# 下面几个字高只用于算文字占地（布局、避让），实际出图字高一律取自标准配置（standards.text_h），两者在 A3 上相同
TAG_HEIGHT = 2.5  # 位号（数字及字母 2～3 mm，HG/T 20519.1 表 6.2.2；字高系列 GB/T 14691）
NAME_HEIGHT = 3.5  # 设备名称（汉字不小于 3.5，GB/T 14691）
CLEARANCE = 2.0  # 设备周围留给管线的净距
SYMBOL_SIZE = 1.25  # 图元整体放大系数
TAG_STRIP = 22.0  # 图纸上方位号栏的高度（两行位号 + 名称）

# 图框（GB/T 14689-2008 表 4，不留装订边）与标题栏（GB/T 10609.1-2008，宽 180、高 ≤56），单位为真实图纸 mm
TITLE_W, TITLE_H = 180.0, 40.0

# 出图模板 water_elevation（给水处理 工艺流程及高程布置图）：不画位号栏，构筑物正下方注中文名称，
# 顶部是大标题，右下角是水利行业标题栏
WATER = "water_elevation"
TEMPLATES = ("pfd_default", WATER)
WATER_NAME_HEIGHT = 5.0  # 构筑物名称（字高系列取 5 号，加粗、带下划线）
WATER_NAME_GAP = 1.5  # 名称与图元底边的距离
# 图元 JSON 的 name_style → 下方名称字高：below 构筑物名称（加粗带下划线），note 小字（如「至配水管网」）；
# inside_vertical（药剂罐里竖排药剂名）和 none 不占图元外的位置
WATER_NAME_STYLES = {"below": WATER_NAME_HEIGHT, "note": 3.5}
WATER_TITLE_STRIP = 16.0  # 顶部大标题所占的一条（图纸 mm，乘 unit）
WATER_TITLE_W, WATER_TITLE_H = 180.0, 50.0  # 水利标题栏（真实图纸 mm）

Box = tuple[float, float, float, float]
TERMINALS = ("pipe_terminal",)  # 管道来源 / 去向：流程图上写名称（如「上水」「去残渣受槽」），位号只留在块属性里


@dataclass(frozen=True)
class PortDef:
    name: str
    kind: str  # in / out
    x: float
    y: float
    dir: str
    semantics: str = ''


@dataclass(frozen=True)
class SymbolDef:
    id: str
    name_cn: str
    aliases: tuple[str, ...]
    category: str
    family: str
    source: str
    status: str
    bbox: Box
    graphics: tuple[dict, ...]
    ports: tuple[PortDef, ...]
    label: dict
    elev_ports: dict[str, float]  # 高程布置图：池底 / 水面 / 顶 等水平线相对图元原点的 y
    axes: tuple  # 对称中心线 [((x0, y0), (x1, y1)), ...]，出图时画细点画线（小图元画细实线，GB/T 4457.4）
    name_style: str  # water_elevation 模板下名称注法：below / inside_vertical / none
    nozzles: tuple = ()  # 施工流程图才画的管口：({name, kind, x, y, dir, len}, ...)，已乘图元放大系数

    @property
    def tag_prefix(self) -> str:
        return self.category

    def port(self, name: str) -> PortDef | None:
        return next((p for p in self.ports if p.name == name), None)


def _zoom_graphic(g: dict, k: float) -> dict:
    g = dict(g)
    for key in ("c", "p"):
        if key in g:
            g[key] = [g[key][0] * k, g[key][1] * k]
    for key in ("r", "rx", "ry", "h"):
        if key in g:
            g[key] = g[key] * k
    for key in ("points", "boundary"):
        if key in g:
            g[key] = [[x * k, y * k] for x, y in g[key]]
    if "holes" in g:
        g["holes"] = [[[x * k, y * k] for x, y in hole] for hole in g["holes"]]
    return g


def _symbol_from_raw(raw: dict) -> SymbolDef:
    k = SYMBOL_SIZE
    label = dict(raw.get("label", {"x": raw["bbox"][2] + 1, "y": raw["bbox"][3], "align": "left"}))
    label["x"], label["y"] = label["x"] * k, label["y"] * k
    return SymbolDef(
        id=raw["id"],
        name_cn=raw["name_cn"],
        aliases=tuple(raw.get("aliases", [])),
        category=raw.get("category", "X"),
        family=raw.get("family", raw["id"]),
        source=raw.get("source", ""),
        status=raw.get("status", "simplified"),
        bbox=tuple(v * k for v in raw["bbox"]),
        graphics=tuple(_zoom_graphic(g, k) for g in raw["graphics"]),
        ports=tuple(PortDef(**{**p, "x": p["x"] * k, "y": p["y"] * k}) for p in raw["ports"]),
        label=label,
        elev_ports={n: v * k for n, v in raw.get("elev_ports", {}).items()},
        name_style=raw.get("name_style", "below"),
        axes=tuple(((a[0] * k, a[1] * k), (b[0] * k, b[1] * k)) for a, b in raw.get("axes", [])),
        nozzles=tuple({**n, "x": n["x"] * k, "y": n["y"] * k, "len": n.get("len", 3) * k} for n in raw.get("nozzles", [])),
    )


def _generated_files() -> list[Path]:
    """自动生成的图元（<data_dir>/symbols_generated/*.json）；目录不存在不报错，也不创建。"""
    from zhihui.tools.store import data_dir

    d = data_dir() / "symbols_generated"
    return sorted(d.glob("gen_*.json")) if d.is_dir() else []


_CACHE: dict = {}


def load_symbols() -> dict[str, SymbolDef]:
    """内置图元库 + 自动生成的图元。生成目录有增减或改动就重新加载（API 进程和 MCP 子进程各自按文件签名判断）。"""
    files = _generated_files()
    key = (str(SYMBOL_DIR), tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in files))
    if _CACHE.get("key") == key:
        return _CACHE["value"]
    out: dict[str, SymbolDef] = {}
    for f in sorted(SYMBOL_DIR.glob("*.json")):
        raw = json.loads(f.read_text(encoding="utf-8"))
        out[raw["id"]] = _symbol_from_raw(raw)
    for f in files:
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            if raw["id"] not in out:  # 生成的图元不能覆盖内置图元
                out[raw["id"]] = _symbol_from_raw(raw)
        except (ValueError, KeyError, TypeError):
            continue  # 坏文件只是不可用，不拖垮整个图元库
    _CACHE["key"], _CACHE["value"] = key, out
    return out


def get_symbol(symbol_id: str) -> SymbolDef:
    syms = load_symbols()
    if symbol_id not in syms:
        raise KeyError(symbol_id)
    return syms[symbol_id]


# ---------- 文字尺寸估算 ----------

def text_width(text: str, height: float, width_factor: float | None = None) -> float:
    """估算单行文字宽度：宽度因子取自标准配置（默认 0.7 长仿宋），汉字约 1.05 倍字高、字母数字 0.55 倍。"""
    from .standards import text_width as _tw

    return _tw(text, height, width_factor)


# ---------- 几何（模型 mm） ----------

def symbol_box(eq: Equipment, unit: float) -> Box:
    x0, y0, x1, y1 = get_symbol(eq.symbol_id).bbox
    return (eq.x + x0 * unit, eq.y + y0 * unit, eq.x + x1 * unit, eq.y + y1 * unit)


def body_box(eq: Equipment, unit: float) -> Box:
    """设备本体的外框（模型 mm）：去掉端口引出的短线，隔热层、伴热管贴着它画。"""
    sym = get_symbol(eq.symbol_id)
    ports = {(round(p.x, 3), round(p.y, 3)) for p in sym.ports}
    xs: list[float] = []
    ys: list[float] = []
    for g in sym.graphics:
        t = g["type"]
        if t == "polyline":
            pts = g["points"]
            if len(pts) == 2 and any((round(x, 3), round(y, 3)) in ports for x, y in pts):
                continue
            xs += [x for x, _ in pts]
            ys += [y for _, y in pts]
        elif t in ("circle", "arc"):
            (cx, cy), r = g["c"], g["r"]
            xs += [cx - r, cx + r]
            ys += [cy - r, cy + r]
        elif t == "ellipse":
            (cx, cy), rx, ry = g["c"], g["rx"], g["ry"]
            xs += [cx - rx, cx + rx]
            ys += [cy - ry, cy + ry]
    if not xs:
        return symbol_box(eq, unit)
    return (eq.x + min(xs) * unit, eq.y + min(ys) * unit, eq.x + max(xs) * unit, eq.y + max(ys) * unit)


def label_box(eq: Equipment, unit: float, template: str = "pfd_default") -> Box:
    """设备旁只注位号（带下划线，HG/T 20519.2 第 3.3.1 条）。label.y 是文字顶边。

    water_elevation 模板：不注位号，构筑物正下方居中注中文名称（带下划线）；名称写在图元内或不注时返回图元中心的空框。
    """
    sym = get_symbol(eq.symbol_id)
    if template == WATER:
        x0, y0, x1, _ = sym.bbox
        cx = (x0 + x1) / 2
        if sym.name_style not in WATER_NAME_STYLES or not eq.name:
            cy = (sym.bbox[1] + sym.bbox[3]) / 2
            return (eq.x + cx * unit, eq.y + cy * unit, eq.x + cx * unit, eq.y + cy * unit)
        h = WATER_NAME_STYLES[sym.name_style]
        w = text_width(eq.name, h) + 1.0
        top = y0 - WATER_NAME_GAP
        bottom = top - h - 1.5
        return (eq.x + (cx - w / 2) * unit, eq.y + bottom * unit, eq.x + (cx + w / 2) * unit, eq.y + top * unit)
    lx, ly, align = sym.label["x"], sym.label["y"], sym.label.get("align", "left")
    w = text_width(eq.tag, TAG_HEIGHT) + 1.0
    top, bottom = ly, ly - TAG_HEIGHT - 1.2
    if eq.symbol_id in TERMINALS and eq.name:  # 流程线的起点、终点写物料名称和来去（文章 3.1 节、规范化要求 6.7），不写位号
        w = text_width(eq.name, NAME_HEIGHT) + 1.0
        bottom = ly - NAME_HEIGHT - 1.2
    if align == "center":
        left, right = lx - w / 2, lx + w / 2
    elif align == "left":
        left, right = lx, lx + w
    else:
        left, right = lx - w, lx
    return (eq.x + left * unit, eq.y + bottom * unit, eq.x + right * unit, eq.y + top * unit)


def union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def inflate(b: Box, d: float) -> Box:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def footprint(eq: Equipment, unit: float, template: str = "pfd_default") -> Box:
    """设备本体 + 旁注位号（或下方名称）的占地范围，用于布局和重叠检查。"""
    return union(symbol_box(eq, unit), label_box(eq, unit, template))


def footprint_rel(eq: Equipment, unit: float, template: str = "pfd_default") -> Box:
    probe = eq.model_copy(update={"x": 0.0, "y": 0.0})
    return footprint(probe, unit, template)


def boxes_overlap(a: Box, b: Box, eps: float = 1e-6) -> bool:
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps


def port_point(eq: Equipment, port_name: str, unit: float) -> tuple[tuple[float, float], str]:
    p = get_symbol(eq.symbol_id).port(port_name)
    if p is None:
        raise KeyError(port_name)
    return (eq.x + p.x * unit, eq.y + p.y * unit), p.dir


# ---------- 图框分区（模型 mm） ----------

def inner_frame(d: Drawing) -> Box:
    """图框线（粗实线）所围区域。留边取自标准配置（GB/T 14689 表 4）：不留装订边四边 e，留装订边左 a、其余 c。"""
    from .standards import for_drawing

    w, h = d.frame_size()
    left, bottom, right, top = (v * d.scale for v in for_drawing(d).margins(d.paper_size, d.binding))
    return (left, bottom, w - right, h - top)


def tag_strip(d: Drawing) -> Box:
    """图纸上方的设备位号栏；water_elevation 模板没有位号栏，这一条换成顶部大标题。"""
    x0, _, x1, y1 = inner_frame(d)
    return (x0, y1 - (WATER_TITLE_STRIP if d.template_id == WATER else TAG_STRIP) * d.unit, x1, y1)


def title_h(d: Drawing) -> float:
    """标题栏高度（真实图纸 mm），取自标题栏模板；不画图框和标题栏时为 0。"""
    from .titleblock import size

    return size(d)[1] if d.frame else 0.0


def label_area_ok(d: Drawing, b: Box) -> bool:
    """文字框 b 能不能放：在图框内、不进位号栏、不压标题栏（标题栏左边那一截横带可以用）。"""
    from .titleblock import size as tb_size

    x0, y0, x1, y1 = inner_frame(d)
    m = 1.0 * d.scale
    if b[0] < x0 + m or b[1] < y0 + m or b[2] > x1 - m or b[3] > y1 - m:
        return False
    band = _tag_band_cached(d)  # 位号栏（流程图贴着设备带浮动，见 tagrow.tag_band）
    if band and b[0] < band[2] and band[0] < b[2] and b[1] < band[3] + m and band[1] - m < b[3]:
        return False
    if band is None and b[3] > tag_strip(d)[1] - m:
        return False
    tw, th = tb_size(d) if d.frame else (0.0, 0.0)
    return not (b[2] > x1 - tw * d.scale - m and b[1] < y0 + th * d.scale + m)


_BAND: dict = {}


def _tag_band_cached(d: Drawing):
    """tag_band 按当前设备坐标和管线缓存（文字避让会反复调用）；高程图（water_elevation）没有位号栏，返回整条顶部大标题。"""
    if d.template_id == WATER:
        return tag_strip(d)
    key = (id(d), d.paper_size, tuple((e.id, e.x, e.y, e.tag, e.name, str(e.params.get("feature", ""))) for e in d.equipment),
           tuple(tuple(c.path or ()) for c in d.connections), d.stage)
    if key not in _BAND:
        from .tagrow import tag_band

        if len(_BAND) > 64:
            _BAND.clear()
        _BAND[key] = tag_band(d)
    return _BAND[key]


def usable_area(d: Drawing) -> Box:
    """可放设备和管线的区域：图框内，扣掉上方位号栏和底部标题栏所在的一条。"""
    x0, y0, x1, _ = inner_frame(d)
    m = 4 * d.unit
    return (x0 + m, y0 + title_h(d) * d.scale + m, x1 - m, tag_strip(d)[1] - m)


def pick_port(d: Drawing, eq: Equipment, kind: str, prefer: str | None = None) -> str | None:
    """给只写了位号、没写端口的连接挑一个端口：优先未占用的同类端口。

    prefer：投药线（如「CL→加药点」「PAC→管道混合器」）传 "dose"，有这个端口就直接用（投药口可以接多根）；
    其他连接则把 dose 口排到最后，免得主管线接到投药口上。
    """
    sym = get_symbol(eq.symbol_id)
    used = {c.from_port if c.from_equip == eq.id else c.to_port for c in d.connections_of(eq.id)}
    candidates = [p.name for p in sym.ports if p.kind == kind]
    if prefer and prefer in candidates:
        return prefer
    candidates.sort(key=lambda n: n == "dose")
    for name in candidates:
        if name not in used:
            return name
    return candidates[0] if candidates else None
