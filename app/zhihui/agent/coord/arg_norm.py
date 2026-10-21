"""Tool-argument normalisation and user-facing question hygiene.

A model that writes {"ref": "P-201", "dx": 800} instead of equipment_ref must not turn a fully specified request into a
question the user cannot answer, and internal validator text must never reach the user.
"""
import re

TAG = r'[A-Z]{1,3}-\d{2,4}[A-Z]?'
TAG_RE = re.compile(r'(?<![A-Za-z0-9])(' + TAG + r')(?![A-Za-z0-9])')
NUM_RE = re.compile(r'\d+(?:\.\d+)?')

ALIASES = {
    'move_equipment': {'equipment_ref': ('ref', 'tag', 'equipment', 'equipment_tag', 'target', 'device', 'name', 'id', '设备', '位号'),
                       'dx': ('x', 'delta_x', 'move_x', 'distance_x'), 'dy': ('y', 'delta_y', 'move_y', 'distance_y')},
    'edit_equipment': {'equipment_ref': ('ref', 'tag', 'equipment', 'equipment_tag', 'target', 'device', 'name', 'id', '设备', '位号')},
    'add_annotation': {'anchor_ref': ('anchor', 'anchor_id', 'target', 'equipment_ref', 'equipment', 'ref', 'tag'), 'text': ('content', 'label', 'value', '文字')},
    'edit_annotation': {'annotation_ref': ('ref', 'id', 'annotation_id', 'annotation', 'target')},
    'delete_annotation': {'annotation_ref': ('ref', 'id', 'annotation_id', 'annotation', 'target')},
    'add_hatch': {'target': ('equipment_ref', 'equipment', 'ref', 'tag', 'anchor_ref')},
    'edit_hatch': {'hatch_ref': ('ref', 'id', 'hatch_id', 'hatch')},
    'edit_title_block': {'fields': ('title_block', 'values', 'data')},
}
DIRECTIONS = {'右': (1, 0), '左': (-1, 0), '上': (0, 1), '下': (0, -1), 'right': (1, 0), 'left': (-1, 0), 'up': (0, 1), 'down': (0, -1),
              '+x': (1, 0), '-x': (-1, 0), '+y': (0, 1), '-y': (0, -1)}
MOVE_RE = re.compile('(' + TAG + r')[^，。；,;\d]{0,10}?(?:向|往)?\s*([左右上下])\s*(?:侧)?\s*(?:移动|平移|挪动|移|挪)\s*(\d+(?:\.\d+)?)')
INTERNAL_RE = re.compile(r'Error executing tool|validation error|pydantic|Traceback|Field required|E_[A-Z_]{3,}|\bJSON\b|\{.*".*\}|input_value')


def parse_move(text: str):
    """'把 P-201 向右移动 800 mm' -> ('P-201', 800.0, 0.0); None when direction or distance is missing."""
    m = MOVE_RE.search(text or '')
    if not m:
        return None
    dx, dy = DIRECTIONS[m.group(2)]
    d = float(m.group(3))
    return m.group(1), dx * d, dy * d


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def normalize_args(tool: str, args: dict, snapshot: dict | None, said: str, schema: dict | None = None):
    """Returns (args, missing_required_names). Never invents a target: aliases and a unique name/tag match only."""
    args = dict(args)
    for canon, names in ALIASES.get(tool, {}).items():
        if canon in args:
            continue
        for n in names:
            if n in args:
                args[canon] = args.pop(n)
                break
    if tool == 'move_equipment':
        if 'dx' not in args and 'dy' not in args:
            direction = str(args.pop('direction', '') or '').strip().lower()
            distance = args.pop('distance', None)
            if direction in DIRECTIONS and distance is not None:
                ux, uy = DIRECTIONS[direction]
                d = _as_float(distance)
                if isinstance(d, float):
                    args['dx'], args['dy'] = ux * d, uy * d
        for k in ('dx', 'dy'):
            if k in args:
                args[k] = _as_float(args[k])
        if 'equipment_ref' not in args:
            mv = parse_move(said)
            if mv and not any(k in args for k in ('dx', 'dy')):
                args['equipment_ref'], args['dx'], args['dy'] = mv[0], mv[1], mv[2]
            elif mv:
                args['equipment_ref'] = mv[0]
    ref = args.get('equipment_ref')
    if isinstance(ref, str) and snapshot:
        eq = snapshot.get('equipment') or []
        tags = {str(e.get('ref', '')).upper(): e.get('ref') for e in eq}
        key = ref.strip().upper()
        if key in tags:
            args['equipment_ref'] = tags[key]
        else:  # a unique name match ("输送泵") is acceptable, anything ambiguous stays unresolved
            hits = [e.get('ref') for e in eq if ref.strip() and (ref.strip() == e.get('name') or e.get('id') == ref.strip())]
            if len(hits) == 1:
                args['equipment_ref'] = hits[0]
    required = [r for r in ((schema or {}).get('required') or []) if r not in ('drawing_id',)]
    return args, [r for r in required if r not in args or args[r] in (None, '')]


def question_for_missing(tool: str, missing: list[str]) -> str:
    what = {'equipment_ref': '要修改的设备位号', 'dx': '水平移动距离', 'dy': '垂直移动距离', 'anchor_ref': '标注所在的设备或位置', 'text': '标注文字',
            'annotation_ref': '标注编号', 'fields': '要修改的标题栏内容', 'target': '修改对象', 'hatch_ref': '填充编号'}
    names = '、'.join(what.get(m, m) for m in missing)
    return f'还缺少：{names}。请补充后继续（例如：把 P-201 向右移动 800 mm）。'


def _strip_numbers(option: str, allowed: set[float]) -> tuple[str, bool]:
    changed = False

    def repl(m):
        nonlocal changed
        if float(re.search(r'\d+(?:\.\d+)?', m.group(0)).group(0)) in allowed:
            return m.group(0)
        changed = True
        return ''
    parts = TAG_RE.split(option)  # odd indexes are tags and are never touched
    out = [p if i % 2 else re.sub(r'\s*[+-]?\d+(?:\.\d+)?\s*(?:mm|毫米)?', repl, p) for i, p in enumerate(parts)]
    return ''.join(out).strip(), changed


def sanitize_question(message: str, options: list[str] | None, said: str):
    """-> (message, options, debug_text). Internal error text is replaced; options never carry numbers the user did not give."""
    debug = ''
    message = str(message or '')
    if INTERNAL_RE.search(message):
        debug, message = message, '操作参数不完整，系统自动重试后仍无法确定。请说明要修改的设备位号、方向和距离（mm）。'
    allowed = {float(n) for n in NUM_RE.findall(TAG_RE.sub(' ', said or ''))}
    clean, stripped = [], False
    for opt in options or []:
        text, changed = _strip_numbers(str(opt), allowed)
        stripped = stripped or changed
        if text and text not in clean:
            clean.append(text)
    if not stripped:
        return message, (list(options) if options else None), debug
    return message + '（选好方向后，请在回答框里写上距离，单位 mm）', (clean if len(clean) >= 2 else None), debug
