"""Generated equipment symbols (图元) for equipment the library does not know.

Design choice: a model never writes free-form geometry. It picks a *template* (vertical vessel, tower, horizontal
vessel, rectangular machine, round machine, exchanger), a size, a few decorations and the ports it wants. Code then
builds the primitives and port positions, so every generated symbol is closed, in range, orthogonal and loadable by
the same loader as the built-in library. Generated symbols live in <data_dir>/symbols_generated/ and are always
marked "自动生成·待专业确认" until a person confirms the plan that uses them.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GEN_DIR_NAME = 'symbols_generated'
TEMPLATES = ('vertical_vessel', 'horizontal_vessel', 'tower', 'rect_machine', 'circle_machine', 'exchanger')
DECORATIONS = ('diagonal', 'cross', 'stirrer', 'jacket', 'trays', 'tubes', 'zigzag', 'packing')
SIDE_DIR = {'left': 'left', 'right': 'right', 'top': 'up', 'bottom': 'down'}
W_RANGE, H_RANGE = (10.0, 40.0), (10.0, 52.0)


class PortDraft(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: str
    kind: Literal['in', 'out']
    side: Literal['left', 'right', 'top', 'bottom']
    pos: float = 0.5
    semantic: str = ''


class SymbolDraft(BaseModel):
    """What the designer role may decide. Anything else (coordinates, primitives) is computed."""
    model_config = ConfigDict(extra='ignore')
    name_cn: str
    aliases: list[str] = Field(default_factory=list)
    category: str = 'X'
    template: Literal['vertical_vessel', 'horizontal_vessel', 'tower', 'rect_machine', 'circle_machine', 'exchanger'] = 'rect_machine'
    width: float = 20
    height: float = 28
    decorations: list[Literal['diagonal', 'cross', 'stirrer', 'jacket', 'trays', 'tubes', 'zigzag', 'packing']] = Field(default_factory=list)
    ports: list[PortDraft] = Field(default_factory=list)
    rationale: str = ''
    evidence_urls: list[str] = Field(default_factory=list)
    confidence: float = Field(default=.5, ge=0, le=1)


def generated_dir() -> Path:
    from zhihui.tools.store import data_dir
    p = data_dir() / GEN_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def symbol_id_for(name: str) -> str:
    return 'gen_' + hashlib.sha1(name.strip().encode('utf-8')).hexdigest()[:8]


def _clamp(v, lo, hi):
    return max(lo, min(hi, float(v)))


# ------------------------------------------------------------------ geometry
def _rect(w, h):
    x, y = w / 2, h / 2
    return [[-x, -y], [x, -y], [x, y], [-x, y], [-x, -y]]


def _ellipse(c, rx, ry, a0, a1):
    return {'type': 'ellipse', 'c': [round(c[0], 3), round(c[1], 3)], 'rx': round(rx, 3), 'ry': round(ry, 3), 'start': a0, 'end': a1}


def _line(a, b):
    return {'type': 'polyline', 'points': [[round(a[0], 3), round(a[1], 3)], [round(b[0], 3), round(b[1], 3)]]}


def _poly(points):
    return {'type': 'polyline', 'points': [[round(x, 3), round(y, 3)] for x, y in points]}


def _body(template: str, w: float, h: float):
    """-> (graphics, edge) ; edge(side,pos) gives the point on the body outline."""
    if template in ('vertical_vessel', 'tower'):
        ry = min(w / 4, h / 6)
        g = [_line((-w / 2, -h / 2 + ry), (-w / 2, h / 2 - ry)), _line((w / 2, -h / 2 + ry), (w / 2, h / 2 - ry)),
             _ellipse((0, h / 2 - ry), w / 2, ry, 0, 180), _ellipse((0, -h / 2 + ry), w / 2, ry, 180, 360)]

        def edge(side, pos):
            pos = _clamp(pos, .1, .9)
            if side in ('left', 'right'):
                y = _clamp(-h / 2 + pos * h, -h / 2 + ry + 1, h / 2 - ry - 1)
                return (-w / 2 if side == 'left' else w / 2), y
            x = (pos - .5) * w
            dy = ry * math.sqrt(max(0.0, 1 - (x / (w / 2)) ** 2))
            return x, (h / 2 - ry + dy) if side == 'top' else (-h / 2 + ry - dy)
        return g, edge
    if template in ('horizontal_vessel', 'exchanger'):
        rx = min(h / 2, w / 4)
        g = [_line((-w / 2 + rx, h / 2), (w / 2 - rx, h / 2)), _line((-w / 2 + rx, -h / 2), (w / 2 - rx, -h / 2)),
             _ellipse((-w / 2 + rx, 0), rx, h / 2, 90, 270), _ellipse((w / 2 - rx, 0), rx, h / 2, -90, 90)]

        def edge(side, pos):
            pos = _clamp(pos, .1, .9)
            if side in ('top', 'bottom'):
                x = _clamp(-w / 2 + pos * w, -w / 2 + rx + 1, w / 2 - rx - 1)
                return x, (h / 2 if side == 'top' else -h / 2)
            y = (pos - .5) * h
            dx = rx * math.sqrt(max(0.0, 1 - (y / (h / 2)) ** 2))
            return ((-w / 2 + rx - dx) if side == 'left' else (w / 2 - rx + dx)), y
        return g, edge
    if template == 'circle_machine':
        r = min(w, h) / 2
        g = [{'type': 'circle', 'c': [0, 0], 'r': round(r, 3)}]

        def edge(side, pos):
            pos = _clamp(pos, .1, .9)
            if side in ('left', 'right'):
                y = (pos - .5) * 2 * r * .8
                dx = math.sqrt(max(0.0, r * r - y * y))
                return (-dx if side == 'left' else dx), y
            x = (pos - .5) * 2 * r * .8
            dy = math.sqrt(max(0.0, r * r - x * x))
            return x, (dy if side == 'top' else -dy)
        return g, edge
    g = [_poly(_rect(w, h))]   # rect_machine

    def edge(side, pos):
        pos = _clamp(pos, .1, .9)
        if side in ('left', 'right'):
            return (-w / 2 if side == 'left' else w / 2), -h / 2 + pos * h
        return -w / 2 + pos * w, (h / 2 if side == 'top' else -h / 2)
    return g, edge


def _decorate(template: str, w: float, h: float, decos: list[str]):
    g = []
    x, y = w / 2, h / 2
    inner = min(x, y) * .75
    for d in decos:
        if d == 'diagonal':
            g.append(_line((-x, -y), (x, y)) if template == 'rect_machine' else _line((-inner * .7, -inner * .7), (inner * .7, inner * .7)))
        elif d == 'cross':
            k = 1 if template == 'rect_machine' else .7
            g += [_line((-x * k, -y * k), (x * k, y * k)), _line((-x * k, y * k), (x * k, -y * k))]
        elif d == 'stirrer':
            g += [_line((0, y * .9), (0, -y * .5)), _line((-x * .45, -y * .5), (x * .45, -y * .5)), _line((-x * .3, -y * .25), (x * .3, -y * .25))]
        elif d == 'jacket':
            g.append(_poly(_rect(w * .78, h * .78)) if template in ('rect_machine', 'circle_machine') else _line((-x * .78, y * .35), (x * .78, y * .35)))
        elif d == 'trays':
            n = 5
            g += [_line((-x, -y * .7 + i * (y * 1.4 / (n - 1))), (x, -y * .7 + i * (y * 1.4 / (n - 1)))) for i in range(n)]
        elif d == 'tubes':
            n = 4
            g += [_line((-x * .85, -y * .5 + i * (y / (n - 1))), (x * .85, -y * .5 + i * (y / (n - 1)))) for i in range(n)]
        elif d == 'zigzag':
            g.append(_poly([(-x * .45, y * .45), (-x * .1, 0), (x * .1, y * .15), (x * .45, -y * .45)]))
        elif d == 'packing':
            g += [_line((-x * .8, -y * .6 + i * y * .4), (x * .8, -y * .35 + i * y * .4)) for i in range(4)]
    return g


def _clean_name(name: str, taken: set[str], kind: str) -> str:
    n = re.sub(r'[^a-z0-9_]', '_', name.strip().lower()) or kind
    if not re.match(r'[a-z]', n):
        n = kind + '_' + n
    base, i = n, 2
    while n in taken:
        n, i = f'{base}{i}', i + 1
    return n


def build_symbol(draft: SymbolDraft, symbol_id: str, sources: list[str], searched: bool) -> dict:
    """Draft -> library JSON (same schema as zhihui/cad/symbols/*.json). Pure, deterministic, always valid."""
    template = draft.template if draft.template in TEMPLATES else 'rect_machine'
    w, h = _clamp(draft.width, *W_RANGE), _clamp(draft.height, *H_RANGE)
    if template == 'tower':
        w, h = _clamp(w, 10, 22), _clamp(max(h, 36), 30, H_RANGE[1])
    if template == 'circle_machine':
        w = h = _clamp(min(w, h), 10, 30)
    decos = [d for d in dict.fromkeys(draft.decorations) if d in DECORATIONS]
    if template == 'tower' and not decos:
        decos = ['trays']
    if template == 'exchanger' and not decos:
        decos = ['tubes']
    graphics, edge = _body(template, w, h)
    graphics += _decorate(template, w, h, decos)
    taken: set[str] = set()
    ports = []
    wanted = list(draft.ports)
    if not any(p.kind == 'in' for p in wanted):
        wanted.insert(0, PortDraft(name='in', kind='in', side='left', pos=.5, semantic='主物料入口'))
    if not any(p.kind == 'out' for p in wanted):
        wanted.append(PortDraft(name='out', kind='out', side='right', pos=.5, semantic='主物料出口'))
    for p in wanted[:8]:
        name = _clean_name(p.name, taken, p.kind)
        taken.add(name)
        x, y = edge(p.side, p.pos)
        ports.append({'name': name, 'kind': p.kind, 'x': round(x, 3), 'y': round(y, 3), 'dir': SIDE_DIR[p.side], 'semantics': p.semantic or ('主物料入口' if p.kind == 'in' else '主物料出口')})
    xs, ys = [], []
    for g in graphics:
        if g['type'] == 'polyline':
            xs += [p[0] for p in g['points']]
            ys += [p[1] for p in g['points']]
        elif g['type'] == 'circle':
            xs += [g['c'][0] - g['r'], g['c'][0] + g['r']]
            ys += [g['c'][1] - g['r'], g['c'][1] + g['r']]
        elif g['type'] == 'ellipse':
            xs += [g['c'][0] - g['rx'], g['c'][0] + g['rx']]
            ys += [g['c'][1] - g['ry'], g['c'][1] + g['ry']]
    xs += [p['x'] for p in ports]
    ys += [p['y'] for p in ports]
    bbox = [math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))]
    category = (draft.category or 'X').strip().upper()[:1]
    from zhihui.cad.symbols import CATEGORIES
    if category not in CATEGORIES:
        category = 'X'
    name = draft.name_cn.strip()[:20]
    aliases = list(dict.fromkeys([name, *[a.strip() for a in draft.aliases if a.strip()]]))[:8]
    family = symbol_id   # its own family: never treated as interchangeable with a library family
    return {
        'id': symbol_id, 'name_cn': name, 'aliases': aliases, 'category': category, 'family': family,
        'status': 'generated', 'verified': False,
        'source': '自动生成·待专业确认（' + ('依据检索：' + '；'.join(sources[:3]) if sources else '未检索到可引用的依据，仅按设备名称与通用画法') + '）',
        'bbox': bbox, 'graphics': graphics, 'ports': ports,
        'label': {'x': bbox[2] + 1, 'y': bbox[3], 'align': 'left'},
        'generated': {'created_at': time.time(), 'template': template, 'sources': sources[:5], 'searched': searched,
                      'confidence': draft.confidence, 'rationale': draft.rationale[:300], 'user_confirmed': False},
    }


def heuristic_draft(name: str, tag: str = '') -> SymbolDraft:
    """Offline / failure fallback: template by name keywords. Deliberately plain."""
    n = name
    prefix = (tag.split('-')[0][:1] or 'X').upper()
    if re.search('塔', n):
        t, w, h, d = 'tower', 14, 44, []
    elif re.search('罐|槽|池|箱|储|桶|釜', n) and re.search('釜|反应', n):
        t, w, h, d = 'vertical_vessel', 22, 30, ['stirrer', 'jacket']
    elif re.search('卧式|分离|缓冲', n):
        t, w, h, d = 'horizontal_vessel', 34, 16, []
    elif re.search('罐|槽|池|箱|储|桶|仓', n):
        t, w, h, d = 'vertical_vessel', 22, 28, []
    elif re.search('泵|风机|压缩|鼓风', n):
        t, w, h, d = 'circle_machine', 16, 16, ['cross'] if re.search('风机|压缩|鼓风', n) else []
    elif re.search('换热|冷却|冷凝|加热|蒸发|再沸|预热', n):
        t, w, h, d = 'exchanger', 34, 14, ['tubes']
    elif re.search('发生|电解|臭氧|紫外|放电', n):
        t, w, h, d = 'rect_machine', 22, 30, ['zigzag']
    elif re.search('反应|聚合|发酵', n):
        t, w, h, d = 'vertical_vessel', 22, 30, ['stirrer', 'jacket']
    elif re.search('过滤|吸附|干燥|分离|除尘', n):
        t, w, h, d = 'rect_machine', 22, 28, ['packing']
    else:
        t, w, h, d = 'rect_machine', 22, 22, ['diagonal']
    return SymbolDraft(name_cn=name, aliases=[name], category=prefix, template=t, width=w, height=h, decorations=d, confidence=.3,
                       rationale='按设备名称关键词选用通用画法（无检索依据）。')


# ------------------------------------------------------------------ storage
def validate_raw(raw: dict) -> list[str]:
    """Cheap structural checks on top of builder guarantees (also used on files read back from disk)."""
    errs = []
    if not re.fullmatch(r'gen_[0-9a-f]{8}', str(raw.get('id', ''))):
        errs.append('id 格式不对')
    ports = raw.get('ports') or []
    if not any(p.get('kind') == 'in' for p in ports) or not any(p.get('kind') == 'out' for p in ports):
        errs.append('缺少入口或出口端口')
    x0, y0, x1, y1 = raw.get('bbox', [0, 0, 0, 0])
    if not (4 <= x1 - x0 <= 80 and 4 <= y1 - y0 <= 80):
        errs.append('尺寸超出合理范围')
    for p in ports:
        if not (x0 - .01 <= p['x'] <= x1 + .01 and y0 - .01 <= p['y'] <= y1 + .01):
            errs.append(f"端口 {p['name']} 在图元外")
    for g in raw.get('graphics', []):
        if g.get('type') not in ('polyline', 'circle', 'ellipse', 'arc'):
            errs.append('含不支持的图形类型 ' + str(g.get('type')))
    return errs


def save_symbol(raw: dict) -> dict:
    errs = validate_raw(raw)
    if errs:
        raise ValueError('生成的图元不合格：' + '；'.join(errs))
    path = generated_dir() / (raw['id'] + '.json')
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
    preview = render_preview(raw, generated_dir() / (raw['id'] + '.png'))
    return {'id': raw['id'], 'name': raw['name_cn'], 'path': str(path), 'preview': preview}


def find_generated(name: str) -> dict | None:
    p = generated_dir() / (symbol_id_for(name) + '.json')
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else None


def load_generated(symbol_id: str) -> dict | None:
    p = generated_dir() / (str(symbol_id) + '.json')
    return json.loads(p.read_text(encoding='utf-8')) if str(symbol_id).startswith('gen_') and p.is_file() else None


def confirm_generated(symbol_ids) -> list[str]:
    done = []
    for sid in set(symbol_ids):
        p = generated_dir() / (str(sid) + '.json')
        if sid and str(sid).startswith('gen_') and p.is_file():
            raw = json.loads(p.read_text(encoding='utf-8'))
            if not raw.get('generated', {}).get('user_confirmed'):
                raw['generated']['user_confirmed'] = True
                raw['generated']['confirmed_at'] = time.time()
                p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
                done.append(sid)
    return done


def is_unconfirmed(symbol_id: str) -> bool:
    p = generated_dir() / (str(symbol_id) + '.json')
    if not str(symbol_id).startswith('gen_') or not p.is_file():
        return False
    return not json.loads(p.read_text(encoding='utf-8')).get('generated', {}).get('user_confirmed')


def render_preview(raw: dict, out: Path) -> str:
    """Small PNG of the symbol with its ports (no Chinese text, so it never depends on a CJK font)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Arc, Circle
    x0, y0, x1, y1 = raw['bbox']
    pad = 6
    fig, ax = plt.subplots(figsize=(2.6, 2.6 * max(.6, min(1.8, (y1 - y0 + 2 * pad) / (x1 - x0 + 2 * pad)))), dpi=110)
    ax.set_xlim(x0 - pad, x1 + pad)
    ax.set_ylim(y0 - pad, y1 + pad)
    ax.set_aspect('equal')
    ax.axis('off')
    for g in raw['graphics']:
        if g['type'] == 'polyline':
            xs, ys = zip(*g['points'])
            ax.plot(xs, ys, color='#222', lw=1.2)
        elif g['type'] == 'circle':
            ax.add_patch(Circle(g['c'], g['r'], fill=False, ec='#222', lw=1.2))
        elif g['type'] == 'ellipse':
            ax.add_patch(Arc(g['c'], 2 * g['rx'], 2 * g['ry'], theta1=g['start'], theta2=g['end'], ec='#222', lw=1.2))
    for p in raw['ports']:
        ax.plot(p['x'], p['y'], 'o', color='#0b6' if p['kind'] == 'in' else '#d52', ms=4)
        ax.annotate(p['name'], (p['x'], p['y']), fontsize=6, color='#555', xytext=(3, 3), textcoords='offset points')
    fig.savefig(out, facecolor='white', bbox_inches='tight', pad_inches=.05)
    plt.close(fig)
    return out.name
