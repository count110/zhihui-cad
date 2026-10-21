"""标注与对象的关联（《CATIA V5-6 R2014 工程图教程》5.8.3 节 文本位置链接）。

标注记下相对锚点（设备 / 管线）的偏移 rel（图纸 mm）；每次工具改完图都按锚点当前位置重算标注坐标，
设备移动后设备名、位号、高程、管道规格、说明等标注跟着走。没有锚点的自由文字在校验时给警告。
"""

from __future__ import annotations

from zhihui.ir import Annotation, Drawing

from .hatch_ir import anchor_point

DRAWING_ANCHORS = ("title", "notes")


def attach(d: Drawing, an: Annotation) -> None:
    """按当前坐标记下相对锚点的偏移。"""
    a = anchor_point(d, an.anchor_ref) if an.anchor_ref not in DRAWING_ANCHORS else None
    if a is not None and an.x is not None:
        an.rel = (round((an.x - a[0]) / d.unit, 4), round((an.y - a[1]) / d.unit, 4))


def refresh(d: Drawing) -> list[str]:
    """锚点移动后重算标注位置；返回位置变了的标注 id。"""
    changed = []
    for an in d.annotations:
        if an.rel is None:
            continue
        a = anchor_point(d, an.anchor_ref)
        if a is None:
            continue
        x, y = round(a[0] + an.rel[0] * d.unit, 3), round(a[1] + an.rel[1] * d.unit, 3)
        if (x, y) != (an.x, an.y):
            an.x, an.y = x, y
            changed.append(an.id)
    return changed


def free_texts(d: Drawing) -> list[str]:
    """没有关联到任何对象的标注（锚在 title 上，或锚点已不存在）。"""
    out = []
    for an in d.annotations:
        if an.anchor_ref == "title" or (an.anchor_ref not in DRAWING_ANCHORS and anchor_point(d, an.anchor_ref) is None):
            out.append(an.id)
    return out
