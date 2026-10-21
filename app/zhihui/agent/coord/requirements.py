"""Deterministic requirement extraction from the user's own words.

No model is involved. The same extraction feeds two independent consumers:
  * rule_plan(): turns explicit edges / boundaries / instruments into the ProcessSpec,
    instead of chaining tags in order of appearance;
  * review(): re-extracts from the raw prompt (never from the planner's output) and
    verifies each requirement against the finished drawing.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from .spec import Contract

TAG = r'[A-Z]{1,3}-\d{2,4}[A-Z]?'
TAG_RE = re.compile(r'(?<![A-Za-z0-9])(' + TAG + r')(?![A-Za-z0-9])')
EQUIP_PREFIX = {'P', 'V', 'T', 'E', 'R', 'C', 'D', 'S', 'B', 'F', 'M', 'X', 'K', 'XV'}
PREFIX_SYMBOL = {'P': 'pump', 'V': 'tank', 'T': 'tower', 'E': 'heat_exchanger', 'R': 'reactor', 'C': 'compressor',
                 'D': 'dust_collector', 'S': 'stack', 'B': 'boiler', 'XV': 'valve', 'K': 'compressor'}
ARROW = re.compile(r'→|->|=>|—>|⟶|➜|➔')
POS_TOP = ('塔顶', '顶部', '上部')
POS_BOTTOM = ('塔底', '底部', '下部')
POS_RE = re.compile('(' + '|'.join(POS_TOP + POS_BOTTOM) + ')')
CUTTERS = ('进入', '送入', '送至', '送往', '送到', '返回', '回到', '流入', '流向', '连接', '经过', '经由', '通过', '接入', '分成', '分为',
           '然后', '之后', '再经', '再', '并', '为', '是', '到', '至', '经', '接', '和', '与', '及', '从', '由', '在', '的', '把', '将', '画', '绘制')
LOOP_VERBS = ('返回', '回到', '送回', '回流到', '回流至', '回流')
FWD_VERBS = ('进入', '送入', '送至', '送往', '送到', '去往', '流入', '接入', '输送到', '输送至', '连接到', '连接至', '连接',
             '接到', '接至', '流向', '经由', '经过', '通过', '返回', '回到', '送回', '回流到', '回流至', '经', '接', '至', '到')
SPLIT_RE = re.compile(r'分成两路|分为两路|分两路|分成两股|分为两股|分两股|分出一路|出口分')
ASSIGN = r'(?:写为|写成|改为|改成|定为|设为|为|是|[:：])\s*'
UNKNOWN_WORDS = ('留空', '空白', '不填', '不写', '待定')

Kind = Literal['equipment', 'edge', 'loop', 'terminal', 'side_stream', 'instrument', 'fitting', 'alarm',
               'signal_line', 'elevation', 'title_block', 'parallel', 'rule', 'generic']


class Requirement(Contract):
    id: str
    clause_idx: int = -1
    clause: str = ''
    kind: Kind
    args: dict = Field(default_factory=dict)
    support: Literal['supported', 'unsupported'] = 'supported'
    reason: str = ''


# ------------------------------------------------------------------ helpers
def is_equipment_tag(tag: str) -> bool:
    return tag.split('-')[0] in EQUIP_PREFIX


def is_instrument_tag(tag: str) -> bool:
    prefix = tag.split('-')[0]
    return prefix not in EQUIP_PREFIX and bool(re.fullmatch('[A-Z]{2,3}', prefix))


def split_clauses(prompt: str) -> list[str]:
    return [s.strip() for s in re.split(r'[。；;\n]+', prompt) if s.strip() and len(s.strip()) > 3 and not re.fullmatch(r'[#\-*|\s\d]+', s.strip())]


def _aliases():
    from zhihui.cad.symbols import load_symbols
    syms = load_symbols()
    return syms


def symbol_for(tag: str, name: str, syms=None) -> str:
    syms = syms or _aliases()
    best, best_score = None, (0, False)
    for s in syms.values():
        for alias in (s.name_cn, *s.aliases):
            score = (len(alias), alias in s.name_cn)  # on a tie prefer the symbol whose own name carries the alias
            if alias and alias in name and score > best_score:
                best, best_score = s.id, score
    return best or PREFIX_SYMBOL.get(tag.split('-')[0], 'tank')


def _resolves(name: str, syms) -> bool:
    return any(alias and alias in name for s in syms.values() for alias in (s.name_cn, *s.aliases))


def _clean_before(run: str) -> str:
    cut = 0
    for w in CUTTERS:
        i = run.rfind(w)
        while i >= 0:
            if run[i:i + 2] in ('再沸', '接触', '经济'):   # words that merely contain a cutter
                i = run.rfind(w, 0, i)
                continue
            cut = max(cut, i + len(w))
            break
    return run[cut:]


def tag_names(prompt: str, syms=None) -> dict[str, str]:
    """tag -> Chinese name, in order of first appearance; instrument tags are excluded."""
    syms = syms or _aliases()
    found: dict[str, str] = {}
    for m in TAG_RE.finditer(prompt):
        tag = m.group(1)
        if tag in found or not is_equipment_tag(tag):
            continue
        before = re.search(r'([一-龥]{1,24})[ \t　]*$', prompt[:m.start()])
        before_name = _clean_before(before.group(1)) if before else ''
        after = re.match(r'[ \t　]*([一-龥]{1,24})', prompt[m.end():])
        after_name = ''
        if after:
            run = after.group(1)
            cut = len(run)
            for w in CUTTERS + POS_TOP + POS_BOTTOM + ('出口', '入口', '分'):
                i = run.find(w)
                if 0 <= i < cut:
                    cut = i
            after_name = run[:cut]
        if before_name and _resolves(before_name, syms):
            name = before_name
        elif after_name and _resolves(after_name, syms):
            name = after_name
        else:
            name = before_name or after_name
        found[tag] = name[:20]
    return found


class Mention:
    __slots__ = ('tag', 'start', 'end', 'hint')

    def __init__(self, tag, start, end, hint=''):
        self.tag, self.start, self.end, self.hint = tag, start, end, hint

    def __repr__(self):
        return f'Mention({self.tag},{self.hint})'


def _tail_hint(text: str, end: int) -> str:
    tail = text[end:end + 8]
    if re.match(r'[ \t　]*(?:' + '|'.join(POS_TOP) + ')', tail):
        return 'top'
    if re.match(r'[ \t　]*(?:' + '|'.join(POS_BOTTOM) + ')', tail):
        return 'bottom'
    return ''


def find_mentions(text: str, names: dict[str, str]) -> list[Mention]:
    spans: list[tuple[int, int, str]] = []
    for m in TAG_RE.finditer(text):
        tag = m.group(1)
        if tag not in names:
            continue
        s, e = m.start(), m.end()
        name = names[tag]
        if name:
            mm = re.search(re.escape(name) + r'[ \t　]*$', text[:s])
            if mm:
                s = mm.start()
            else:
                mm = re.match(r'[ \t　]*' + re.escape(name), text[e:])
                if mm:
                    e += mm.end()
        spans.append((s, e, tag))
    by_name: dict[str, list[str]] = {}
    for tag, name in names.items():
        if len(name) >= 3:
            by_name.setdefault(name, []).append(tag)
    for name, tags in by_name.items():
        if len(tags) != 1:
            continue
        for m in re.finditer(re.escape(name), text):
            if any(a < m.end() and m.start() < b for a, b, _ in spans):
                continue
            spans.append((m.start(), m.end(), tags[0]))
    spans.sort()
    return [Mention(tag, s, e, _tail_hint(text, e)) for s, e, tag in spans]


def pick_port(sym, kind: str, hint: str = '', role: str = '') -> str:
    """Port name by physical meaning, '' when the symbol has no specific port (compiler then auto-selects)."""
    if sym is None:
        return ''
    names = [p.name for p in sym.ports if p.kind == kind]

    def first(*cands):
        return next((c for c in cands if c in names), '')
    if kind == 'in':
        if hint == 'shell':
            return first('shell_in', 'water_in')
        if role == 'reboil':
            return first('reboil_in', 'feed')
        if hint == 'top':
            return first('spray_in', 'reflux_in')
        if hint == 'bottom':
            return first('feed')
    else:
        if hint == 'shell':
            return first('shell_out', 'water_out')
        if hint == 'top':
            return first('top_out', 'vapor_out', 'gas_out')
        if hint == 'bottom':
            return first('bottom_out', 'liquid_out')
    return ''


# ------------------------------------------------------------------ extraction
class _Ctx:
    def __init__(self, names, syms):
        self.names, self.syms = names, syms
        self.order = list(names)
        self.sym = {t: symbol_for(t, n, syms) for t, n in names.items()}
        self.tower = None
        self.exchanger = None
        self.prev = None          # (tag, hint) of the last edge target
        self.split = None         # tag whose outlet was split into branches
        self.branches: list[str] = []
        self.edges: list[dict] = []
        self.terms: list[dict] = []

    def family(self, tag):
        s = self.syms.get(self.sym.get(tag, ''))
        return s.family if s else ''

    def note(self, m: Mention):
        fam = self.family(m.tag)
        if fam == 'tower':
            self.tower = m.tag
        elif fam == 'exchanger':
            self.exchanger = m.tag

    def add_edge(self, src, dst, clause_idx, clause, *, from_hint='', to_hint='', medium='', loop=False, role=''):
        if not src or not dst or src == dst:
            return None
        key = (src, dst, from_hint, to_hint)
        for e in self.edges:
            if (e['from'], e['to'], e['from_hint'], e['to_hint']) == key:
                return e
        if not loop and src in self.order and dst in self.order:
            loop = self.order.index(dst) < self.order.index(src) and bool(role == 'loop')
        e = {'from': src, 'to': dst, 'from_hint': from_hint, 'to_hint': to_hint, 'medium': medium, 'loop': loop,
             'role': role, 'clause_idx': clause_idx, 'clause': clause}
        self.edges.append(e)
        return e

    def add_term(self, name, direction, to, hint, clause_idx, clause, *, inferred=False):
        name = name.strip()
        if not name or not to:
            return None
        for t in self.terms:
            if t['name'] == name and t['to'] == to and t['direction'] == direction:
                return t
        t = {'id': f'X-{101 + len(self.terms)}', 'name': name, 'direction': direction, 'to': to, 'hint': hint,
             'inferred': inferred, 'clause_idx': clause_idx, 'clause': clause}
        self.terms.append(t)
        return t


def _terminal_direction(name: str) -> str:
    if re.search('入|进|补|供|给', name) and not re.search('产品|排|出', name):
        return 'in'
    return 'out'


def _clean_boundary_name(item: str) -> str:
    item = re.sub(r'(边界|接口|入口|出口)$', '', item.strip())
    return item.strip('、，, ')


def _vessel_for_boundary(ctx: _Ctx) -> str | None:
    for tag in ctx.order:
        if ctx.family(tag) == 'vessel':
            return tag
    return ctx.order[-1] if ctx.order else None


def _parse_sentence(ctx: _Ctx, sent: str, idx: int):
    # exchanger cooling-water supply / return boundaries: "冷却水供水、回水"
    m = re.search(r'(冷却水|冷媒|循环水|冷水)?(供水|进水)[、,和及](冷却水|冷媒|循环水|冷水)?(回水|出水)', sent)
    side_target = None
    for mm in find_mentions(sent, ctx.names):
        ctx.note(mm)
    if m:
        side_target = ctx.exchanger
        prefix = m.group(1) or m.group(3) or ''
        if side_target:
            ctx.add_term((m.group(1) or prefix) + m.group(2), 'in', side_target, 'shell', idx, sent)
            ctx.add_term((m.group(3) or prefix) + m.group(4), 'out', side_target, 'shell', idx, sent)
    for sub in re.split(r'[，,]', sent):
        sub = sub.strip()
        if not sub:
            continue
        ms = find_mentions(sub, ctx.names)
        for mm in ms:
            ctx.note(mm)
        # --- arrow chains
        if ARROW.search(sub):
            nodes = []
            for seg in ARROW.split(sub):
                sm = find_mentions(seg, ctx.names)
                nodes.append((sm[0].tag, sm[0].hint) if sm else None)
            for a, b in zip(nodes, nodes[1:]):
                if a and b:
                    ctx.add_edge(a[0], b[0], idx, sent, from_hint=a[1], to_hint=b[1])
            last = next((n for n in reversed(nodes) if n), None)
            if last:
                ctx.prev = last
            continue
        # --- boundary entering the system: "气体边界进入吸收塔 T-101 底部"
        m = re.match(r'^(.*?)(边界|来源)(?:进入|接入|送入|送至|送到)', sub)
        if m and m.group(1).strip() and ms:
            tgt = ms[0]
            ctx.add_term(re.split(r'[：:\s]', m.group(1).strip())[-1], 'in', tgt.tag, tgt.hint, idx, sent)
            ctx.prev = (tgt.tag, tgt.hint)
            continue
        # --- stream leaving a tower end: "净化气体从塔顶排出"
        m = re.match(r'^(.*?)从(塔顶|塔底|顶部|底部)(?:排出|送出|引出|排放|离开|流出|出)', sub)
        if m and ctx.tower:
            ctx.add_term(m.group(1).strip() or '产品', 'out', ctx.tower, 'top' if m.group(2) in POS_TOP else 'bottom', idx, sent)
            continue
        # --- other branch used as a product boundary: "另一路为塔顶产品边界"
        m = re.match(r'^(?:其中)?另一路(?:则)?(?:为|是|作为|送出为|去)?(.*?)(?:边界|出口|接口)?$', sub)
        if m and not ms and ctx.split and m.group(1).strip():
            ctx.add_term(_clean_boundary_name(m.group(1)), 'out', ctx.split, '', idx, sent)
            continue
        # --- verb edges
        done = False
        for vm in re.finditer('(' + '|'.join(sorted(FWD_VERBS, key=len, reverse=True)) + ')', sub):
            verb = vm.group(1)
            before, after = sub[:vm.start()], sub[vm.end():]
            before_ms = [x for x in ms if x.end <= vm.start()]
            after_ms = [x for x in ms if x.start >= vm.end()]
            pos_after = POS_RE.search(after[:6])
            if not after_ms and not pos_after:
                continue
            if len(verb) == 1 and not (after_ms and after_ms[0].start - vm.end() <= 8 or pos_after):
                continue
            src = None
            src_hint = ''
            medium = ''
            if before_ms:
                src, src_hint = before_ms[-1].tag, before_ms[-1].hint
            else:
                pm = POS_RE.search(before)
                lead = re.match(r'^\s*(?:再|其中)?(?:另)?一路', before)
                if pm and ctx.tower:
                    src, src_hint = ctx.tower, 'top' if pm.group(1) in POS_TOP else 'bottom'
                    medium = before[pm.end():].strip()
                    medium = medium if 1 <= len(medium) <= 6 and re.fullmatch('[一-龥]+', medium) else ''
                elif lead and ctx.split:
                    src = ctx.split
                elif ctx.prev:
                    src, src_hint = ctx.prev[0], ''
                if src is None and ctx.split:
                    src = ctx.split
            if src is None:
                continue
            targets: list[tuple[str, str]] = [(x.tag, x.hint) for x in after_ms]
            if not targets and pos_after and ctx.tower:
                targets = [(ctx.tower, 'top' if pos_after.group(1) in POS_TOP else 'bottom')]
            if not targets:
                continue
            # a sub-clause like "分别接 A 和 B" fans out; others link to the first target only
            fan = '分别' in before or (len(targets) > 1 and re.search(r'[和、及]', after))
            if not fan:
                targets = targets[:1]
            is_loop = verb in LOOP_VERBS
            src_sym = ctx.sym.get(src, '')
            for dst, dst_hint in targets:
                role = ''
                if src_sym == 'kettle_reboiler' and ctx.family(dst) == 'tower':
                    role = 'reboil'
                med = medium
                if is_loop and dst_hint == 'top' and ctx.family(dst) == 'tower':
                    med = med or '回流'
                if re.search('汇合|汇入|合并', sub) and ctx.branches:
                    for b in ctx.branches:
                        ctx.add_edge(b, dst, idx, sent, to_hint=dst_hint, medium=med)
                else:
                    ctx.add_edge(src, dst, idx, sent, from_hint=src_hint, to_hint=dst_hint, medium=med,
                                 loop=is_loop, role=role or ('loop' if is_loop else ''))
            if fan and len(targets) > 1:
                ctx.branches = [t for t, _ in targets]
            ctx.prev = targets[-1]
            if ctx.family(targets[-1][0]) == 'exchanger':
                ctx.exchanger = targets[-1][0]
            done = True
            break
        # --- branch point
        if SPLIT_RE.search(sub):
            if ms:
                ctx.split = ms[-1].tag
                ctx.prev = (ms[-1].tag, '')
            continue
        if done:
            continue
        # --- "另画 / 增加 ... 边界 / 入口 / 出口"
        m = re.match(r'^(?:另?画|增加|设置|加上?|补充)(.+)$', sub)
        if m:
            for item in re.split('和|及|、', m.group(1)):
                item = item.strip()
                if not re.search('(边界|入口|出口|接口)$', item):
                    continue
                name = _clean_boundary_name(item)
                direction = _terminal_direction(item)
                hint, target, inferred = '', None, False
                pm = POS_RE.match(name)
                if pm and ctx.tower:
                    target, hint = ctx.tower, 'top' if pm.group(1) in POS_TOP else 'bottom'
                elif re.search('补水|供水|进水|补液', name):
                    target, inferred = _vessel_for_boundary(ctx), True
                elif re.search('排污|排水|排液', name):
                    target, inferred = _vessel_for_boundary(ctx), True
                else:
                    target, inferred = (ctx.split or (ctx.prev[0] if ctx.prev else None) or ctx.tower), True
                ctx.add_term(name, direction, target, hint, idx, sent, inferred=inferred)
    # --- annotations list: "管线标注热料、冷却后物料、..."
    m = re.search(r'(?:管线|物流|管道)?标注(?:为|：|:)?([^。；]+)', sent)
    if m:
        items = [i.strip() for i in re.split(r'[、,，和及]', m.group(1)) if i.strip()]
        items = [i for i in items if i not in ('介质', '物料', '物料名称', '介质名称') and len(i) <= 10 and not re.search('不|无|勿', i)]
        _assign_mediums(ctx, items)


def _assign_mediums(ctx: _Ctx, items: list[str]):
    process = []
    for it in items:
        if '供水' in it or '进水' in it:
            t = next((t for t in ctx.terms if t['direction'] == 'in' and t['hint'] == 'shell'), None)
            if t:
                t['medium'] = it
                continue
        if '回水' in it or '出水' in it:
            t = next((t for t in ctx.terms if t['direction'] == 'out' and t['hint'] == 'shell'), None)
            if t:
                t['medium'] = it
                continue
        process.append(it)
    edges = [e for e in ctx.edges if not e['loop'] and not e['medium']]
    if not process or not edges:
        return
    k, m = len(edges), len(process)
    if m == 1:
        for e in edges:
            e['medium'] = process[0]
    elif m == k:
        for e, med in zip(edges, process):
            e['medium'] = med
    else:
        cut = next((i for i, e in enumerate(edges) if ctx.family(e['to']) == 'exchanger'), None)
        if m == 2 and cut is not None:
            for i, e in enumerate(edges):
                e['medium'] = process[0] if i <= cut else process[1]
        else:
            for i, e in enumerate(edges):
                e['medium'] = process[min(m - 1, i * m // k)]


def parse_topology(prompt: str, syms=None):
    """Returns (names, ctx) with explicit edges / boundary terminals found in the prompt."""
    syms = syms or _aliases()
    names = tag_names(prompt, syms)
    ctx = _Ctx(names, syms)
    for i, sent in enumerate(split_clauses(prompt)):
        _parse_sentence(ctx, sent, i)
    return names, ctx


# ------------------------------------------------------------------ instruments, valves, elevations, title block
INSTR_RE = re.compile(r'(?<![A-Za-z0-9])([A-Z]{2,3})-(\d{3,5})(?![A-Za-z0-9])')


def _instrument_reqs(prompt: str, names: dict, ctx: _Ctx, base: int) -> list[Requirement]:
    out = []
    clauses = split_clauses(prompt)
    for idx, sent in enumerate(clauses):
        for m in INSTR_RE.finditer(sent):
            letters, loop_no = m.group(1), m.group(2)
            if letters in EQUIP_PREFIX or f'{letters}-{loop_no}' in names:
                continue
            before = sent[:m.start()]
            seg = re.split(r'[，,、]', before)[-1] if False else before
            attach = {'kind': 'pump_out'}
            window = sent[max(0, m.start() - 24):m.start()]
            if re.search('储罐|罐|槽|池', window) and re.search('报警|液位', sent):
                attach = {'kind': 'vessel'}
            elif re.search('泵出口|泵后|出口', window):
                attach = {'kind': 'pump_out'}
            elif re.search('泵入口|泵前|入口', window):
                attach = {'kind': 'pump_in'}
            if re.fullmatch('[FLPT][CI]V', letters):   # control valve: not an instrument bubble (table 2-13 has no V), drawn as a valve fitting + tag text
                out.append(Requirement(id=f'{base + len(out)}', clause_idx=idx, clause=sent, kind='fitting',
                                       args={'type': '调节阀', 'where': attach['kind'] if attach['kind'] != 'vessel' else 'pump_out',
                                             'purpose': f'调节阀 {letters}-{loop_no}', 'tag': f'{letters}-{loop_no}'}))
                continue
            kind = 'alarm' if re.fullmatch('[LPTF]A[LH]?', letters) or re.fullmatch('[LPTF][LH]A', letters) else 'instrument'
            out.append(Requirement(id=f'{base + len(out)}', clause_idx=idx, clause=sent, kind=kind,
                                   args={'tag': f'{letters}-{loop_no}', 'letters': letters, 'loop_no': loop_no, 'attach': attach}))
        if re.search('信号线|控制信号|控制回路', sent):
            out.append(Requirement(id=f'{base + len(out)}', clause_idx=idx, clause=sent, kind='signal_line',
                                   args={'text': re.search('[^，,。；]*(?:信号线|控制信号|控制回路)', sent).group(0)},
                                   support='unsupported', reason='当前工具链没有仪表信号线对象，未绘制'))
        if re.search('隔离阀', sent):
            where = ['pump_in', 'pump_out'] if re.search('泵前后|泵进出口|泵两侧|泵前、后', sent) else ['pump_out'] if '泵后' in sent else ['pump_in'] if '泵前' in sent else ['pump_out']
            for w in where:
                out.append(Requirement(id=f'{base + len(out)}', clause_idx=idx, clause=sent, kind='fitting', args={'type': '闸阀', 'where': w, 'purpose': '隔离阀'}))
        if re.search('止回阀|单向阀', sent):
            w = 'pump_in' if '泵前' in sent and '泵后' not in sent else 'pump_out'
            out.append(Requirement(id=f'{base + len(out)}', clause_idx=idx, clause=sent, kind='fitting', args={'type': '止回阀', 'where': w, 'purpose': '止回阀'}))
    return out


def _clause_idx(prompt: str, *needles: str) -> int:
    for i, c in enumerate(split_clauses(prompt)):
        if all(n in c for n in needles):
            return i
    return -1


def _elevation_reqs(prompt: str, names: dict, base: int) -> list[Requirement]:
    def series(label):
        m = re.search(label + r'(?:依次|分别)?(?:为|是|：|:)?\s*([0-9.\s、,，和及]+?)\s*(m|米|M)', prompt)
        if not m:
            return None
        return [float(x) for x in re.findall(r'\d+(?:\.\d+)?', m.group(1))], m.group(2)
    top, water = series('池顶标高'), series('设计水位')
    if not (top or water):
        return []
    tags = list(names)
    out = []
    for i, tag in enumerate(tags):
        args = {'tag': tag, 'unit': 'm'}
        if top and i < len(top[0]):
            args['top'] = top[0][i]
        if water and i < len(water[0]):
            args['water'] = water[0][i]
        if len(args) > 2:
            out.append(Requirement(id=f'{base + len(out)}', kind='elevation', clause_idx=_clause_idx(prompt, '标高'), clause='标高', args=args))
    return out


TITLE_FIELDS = {'设计单位': 'unit_name', '单位名称': 'unit_name', '工程名称': 'project', '项目名称': 'project', '图名': 'drawing_name', '图号': 'drawing_no'}


def extract_title_block(prompt: str) -> dict[str, str]:
    out: dict[str, str] = {}
    named = re.search(r'图纸名称[^“"「]*[“"「]([^”"」]{2,40})', prompt)
    if named:
        out['drawing_name'] = named.group(1).replace('（PFD）', '').replace('(PFD)', '')
    for label, key in TITLE_FIELDS.items():
        m = re.search(label + ASSIGN + r'[“"「]?([^，,。；;\n”"」]+)', prompt)
        if not m:
            continue
        value = m.group(1).strip()
        if not value or value.startswith(UNKNOWN_WORDS) or re.search('留空|空白', value):
            continue
        if key == 'drawing_no':
            mm = re.match(r'[A-Za-z0-9][A-Za-z0-9\-_/.]{1,19}', value)
            if not mm:
                continue
            value = mm.group(0)
        out.setdefault(key, value)
    if 'PFD-01' in prompt:
        out.setdefault('drawing_no', 'PFD-01')
    return out


def extract_requirements(prompt: str, syms=None) -> list[Requirement]:
    syms = syms or _aliases()
    names, ctx = parse_topology(prompt, syms)
    clauses = split_clauses(prompt)
    reqs: list[Requirement] = []

    def add(kind, args, e=None, **extra):
        reqs.append(Requirement(id=f'R{len(reqs) + 1}', clause_idx=(e or {}).get('clause_idx', -1), clause=(e or {}).get('clause', ''),
                                kind=kind, args=args, **extra))
    for tag, name in names.items():
        add('equipment', {'tag': tag, 'name': name})
    for e in ctx.edges:
        add('loop' if e['loop'] else 'edge', {k: e[k] for k in ('from', 'to', 'from_hint', 'to_hint', 'medium', 'role')}, e)
    for t in ctx.terms:
        add('terminal', {k: t[k] for k in ('id', 'name', 'direction', 'to', 'hint', 'inferred')}, t)
    if ctx.terms and any(t['hint'] == 'shell' for t in ctx.terms):
        ex = next(t['to'] for t in ctx.terms if t['hint'] == 'shell')
        add('side_stream', {'tag': ex}, next(t for t in ctx.terms if t['hint'] == 'shell'))
    pairs = re.findall(r'(' + TAG + r')[ \t　]*(?:与|和|及)[ \t　]*(' + TAG + r')', prompt)
    for a, b in pairs:
        if re.sub(r'[A-Z]$', '', a) == re.sub(r'[A-Z]$', '', b) and a != b and re.search('并联', prompt):
            i = _clause_idx(prompt, a, b, '并联')
            add('parallel', {'tags': [a, b]}, {'clause_idx': i, 'clause': split_clauses(prompt)[i] if i >= 0 else ''})
    reqs += _instrument_reqs(prompt, names, ctx, len(reqs) + 1)
    reqs += _elevation_reqs(prompt, names, len(reqs) + 1)
    for field, value in extract_title_block(prompt).items():
        i = _clause_idx(prompt, value)
        add('title_block', {'field': field, 'value': value}, {'clause_idx': i, 'clause': split_clauses(prompt)[i] if i >= 0 else ''})
    # numbered ids
    for i, r in enumerate(reqs, 1):
        r.id = f'R{i}'
    return reqs


_DESC_RE = re.compile(r'^([^.→]+)\.[^→]*→([^.→]+)\.')


def parse_conn_refs(connections: list[dict]) -> dict[tuple[str, str], str]:
    """connect_ports result items [{ref, desc: 'A.out→B.in'}] -> {(A, B): ref}"""
    out: dict[tuple[str, str], str] = {}
    for c in connections or []:
        m = _DESC_RE.match(str(c.get('desc', '')))
        if m and c.get('ref'):
            out.setdefault((m.group(1), m.group(2)), c['ref'])
    return out


def resolve_attach(kind: str, equipment: dict[str, str], refs: dict[tuple[str, str], str], syms=None) -> str | None:
    """Where a P&ID instrument / valve goes: a connection ref (pump side) or a vessel tag. None when the drawing has no such place."""
    syms = syms or _aliases()
    fam = {t: (syms[s].family if s in syms else '') for t, s in equipment.items()}
    if kind == 'vessel':
        return next((t for t, f in fam.items() if f == 'vessel'), None)
    pump = next((t for t, f in fam.items() if f == 'pump'), None)
    if not pump:
        return None
    for (a, b), ref in refs.items():
        if (kind == 'pump_out' and a == pump) or (kind == 'pump_in' and b == pump):
            return ref
    return None
