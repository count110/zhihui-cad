"""Resolve authored semantic polygons directly, without guessing a pick point."""
import json
from shapely.geometry import Polygon
from zhihui.cad.symbols import SYMBOL_DIR, SYMBOL_SIZE


def definitions(symbol_id):
    path = SYMBOL_DIR/(symbol_id+'.json')
    if not path.is_file():   # 自动生成的图元没有命名填充区域
        return {}
    return json.loads(path.read_text(encoding='utf-8')).get('regions', {})


def resolve_named(drawing, hatch):
    from zhihui.cad.hatch import Region, Ring, HatchError
    equipment = drawing.get_equipment(hatch.anchor_ref)
    if equipment is None:
        raise HatchError('E_NOT_FOUND', '填充锚点已不存在')
    regions = definitions(equipment.symbol_id)
    if hatch.region not in regions:
        raise HatchError('E_REGION_UNKNOWN', '不存在该命名区域', detail={'available_regions': list(regions)})
    definition = regions[hatch.region]
    def points(poly):
        return [(equipment.x+x*SYMBOL_SIZE*drawing.unit,equipment.y+y*SYMBOL_SIZE*drawing.unit) for x,y in poly]
    exterior = points(definition['points'])
    holes = [points(h) for h in definition.get('holes', [])]
    geometry = Polygon(exterior, holes)
    if not geometry.is_valid or geometry.area <= 0:
        raise HatchError('E_HATCH_NOT_CLOSED', '命名区域定义不是有效闭合多边形')
    return Region(geometry, [Ring(exterior), *[Ring(h) for h in holes]], 1, len(holes), style=hatch.style)
