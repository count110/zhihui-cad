"""Source-aware process specification and deterministic tool compiler."""
from typing import Literal
from pydantic import BaseModel,Field,ConfigDict,model_validator


class Contract(BaseModel):
    model_config=ConfigDict(extra='forbid',populate_by_name=True)


class DrawingSpec(Contract):
    kind:Literal['pfd','water_elevation']='pfd'
    stage:Literal['scheme','material','construction']='scheme'
    standard:str='hg_pfd'
    paper_hint:str='A1'
    title:str='工艺流程图'
    title_block:dict=Field(default_factory=dict)


class EquipmentSpec(Contract):
    tag:str
    name:str
    symbol_id:str
    symbol_candidates:list[dict]=Field(default_factory=list)
    family:str=''
    params:dict=Field(default_factory=dict)
    source:Literal['user','kb','inferred']='user'
    confidence:float=Field(default=1,ge=0,le=1)

    @model_validator(mode='after')
    def sourced(self):
        if self.params and self.source!='user': raise ValueError('E_UNSOURCED_VALUE: equipment.params require user source')
        return self


class StreamSpec(Contract):
    from_:str=Field(alias='from')
    to:str
    medium:str=''
    material_code:str=''
    line_class:str='process'
    label:str=''
    source:Literal['user','kb','inferred']='user'


class TerminalSpec(Contract):
    id:str
    name:str
    direction:Literal['in','out']
    medium:str=''
    connected_to:str
    source:Literal['user','kb','inferred']='user'


class HatchSpec(Contract):
    target:str
    region:str
    pattern:str|None=None
    source:Literal['user','kb','inferred']='user'


class ChecklistItem(Contract):
    id:str
    text_span:str
    requirement:str
    checked:bool=False
    generic:bool=False     # descriptive wording that could not be mapped to a checkable requirement


class ProcessSpec(Contract):
    drawing:DrawingSpec=Field(default_factory=DrawingSpec)
    equipment:list[EquipmentSpec]=Field(default_factory=list)
    streams:list[StreamSpec]=Field(default_factory=list)
    terminals:list[TerminalSpec]=Field(default_factory=list)
    hatches:list[HatchSpec]=Field(default_factory=list)
    annotations:list[dict]=Field(default_factory=list)
    instruments:list[dict]=Field(default_factory=list)
    fittings:list[dict]=Field(default_factory=list)
    checklist:list[ChecklistItem]=Field(default_factory=list)
    assumptions:list[str]=Field(default_factory=list)

    @model_validator(mode='after')
    def topology(self):
        tags=[e.tag for e in self.equipment]; ids=[t.id for t in self.terminals]
        if len(set(tags+ids))!=len(tags+ids): raise ValueError('Duplicate equipment/terminal IDs')
        known=set(tags+ids)
        for s in self.streams:
            if s.from_.split('.')[0] not in known or s.to.split('.')[0] not in known: raise ValueError('Stream endpoint missing')
            if s.from_.split('.')[0]==s.to.split('.')[0]: raise ValueError('E_SELF_LOOP: use terminal')
        for t in self.terminals:
            if t.connected_to.split('.')[0] not in tags: raise ValueError('Terminal target missing')
        return self

    def needs_confirmation(self):
        inferred=sum(getattr(x,'source','user')=='inferred' for seq in (self.equipment,self.streams,self.terminals,self.hatches) for x in seq)
        return inferred>3 or any(e.confidence<.6 for e in self.equipment)


def compile_spec(spec):
    """No model-generated coordinates or paper dimensions enter these actions."""
    from zhihui.cad.symbols import get_symbol
    steps=[('create_drawing',{'title':spec.drawing.title,'paper_size':spec.drawing.paper_hint,'stage':spec.drawing.stage,
        'standard':spec.drawing.standard,'template_id': 'water_elevation' if spec.drawing.kind=='water_elevation' else 'pfd_default','title_block':spec.drawing.title_block})]
    items=[{'tag':e.tag,'name':e.name,'symbol_id':e.symbol_id,'params':e.params} for e in spec.equipment]
    items += [{'tag':t.id,'name':t.name,'symbol_id':'pipe_terminal'} for t in spec.terminals]
    steps.append(('place_equipment',{'items':items}))
    equipment={e.tag:e for e in spec.equipment}
    def endpoint(ref,direction):
        tag,_,port=ref.partition('.')
        if tag not in equipment: return tag+'.'+direction
        symbol=get_symbol(equipment[tag].symbol_id)
        if port and symbol.port(port): return ref
        candidates=[p.name for p in symbol.ports if p.kind==direction]
        if not candidates: raise ValueError('E_PORT_INVALID: '+ref)
        return tag+'.'+candidates[0]
    streams=[{'from_port':endpoint(s.from_,'out'),'to_port':endpoint(s.to,'in'),'medium':s.medium,'code':s.material_code,'line_type':s.line_class} for s in spec.streams]
    for t in spec.terminals:
        streams.append({'from_port':t.id+'.out' if t.direction=='in' else endpoint(t.connected_to,'out'),
            'to_port':endpoint(t.connected_to,'in') if t.direction=='in' else t.id+'.in','medium':t.medium,'line_type':'auxiliary'})
    if streams: steps.append(('connect_ports',{'items':streams}))
    steps.append(('auto_layout',{}))
    for h in spec.hatches: steps.append(('add_hatch',{'target':h.target,'region':h.region,'pattern':h.pattern or 'ANSI31'}))
    for a in spec.annotations: steps.append(('add_annotation',dict(a)))
    for i in spec.instruments: steps.append(('add_instrument',dict(i)))
    for fitting in spec.fittings: steps.append(('connect_ports',dict(fitting)))
    return steps


def plan_issues(raw) -> list[dict]:
    """Row-level, Chinese problems in an edited plan, found before pydantic's English errors can reach the user."""
    from zhihui.cad.symbols import load_symbols
    out: list[dict] = []
    if not isinstance(raw, dict):
        return [{'field': '', 'message': '方案格式不对，应为一个 JSON 对象'}]
    syms = load_symbols()
    eq = raw.get('equipment') or []
    terms = raw.get('terminals') or []
    tags: dict[str, str] = {}
    for i, e in enumerate(eq, 1):
        tag, name, sid = str(e.get('tag', '')).strip(), str(e.get('name', '')).strip(), str(e.get('symbol_id', '')).strip()
        if not tag:
            out.append({'field': f'equipment[{i-1}].tag', 'message': f'第 {i} 台设备没有位号'})
        elif tag in tags:
            out.append({'field': f'equipment[{i-1}].tag', 'message': f'位号 {tag} 重复，请改成不同的位号'})
        if tag:
            tags[tag] = sid
        if not name or name == '请填写名称':
            out.append({'field': f'equipment[{i-1}].name', 'message': f'设备 {tag or i} 还没有填写名称'})
        if sid not in syms:
            out.append({'field': f'equipment[{i-1}].symbol_id', 'message': f'设备 {tag or i} 的图元「{sid}」不存在，请从下拉列表里选择'})
    ids = {str(t.get('id', '')).strip() for t in terms}
    for t in terms:
        if str(t.get('id', '')).strip() in tags:
            out.append({'field': 'terminals', 'message': f"边界 {t.get('id')} 的编号与设备位号重复"})
        tgt = str(t.get('connected_to', '')).split('.')[0]
        if tgt not in tags:
            out.append({'field': 'terminals', 'message': f"边界「{t.get('name', '')}」连接的设备 {tgt or '（空）'} 不存在，可能已被删除"})
    known = set(tags) | ids
    for j, s in enumerate(raw.get('streams') or [], 1):
        a, b = str(s.get('from', s.get('from_', ''))).strip(), str(s.get('to', '')).strip()
        for role, ref in (('起点', a), ('终点', b)):
            tag, _, port = ref.partition('.')
            if not ref:
                out.append({'field': f'streams[{j-1}]', 'message': f'第 {j} 条连接的{role}没有填写'})
            elif tag not in known:
                out.append({'field': f'streams[{j-1}]', 'message': f'第 {j} 条连接的{role} {tag} 不存在，可能已被删除；请改接其他设备或删除这条连接'})
            elif port and tag in tags and tags[tag] in syms and syms[tags[tag]].port(port) is None:
                names = '、'.join(p.name for p in syms[tags[tag]].ports)
                out.append({'field': f'streams[{j-1}]', 'message': f'第 {j} 条连接的{role}端口 {tag}.{port} 不存在；{tag} 可用端口：{names}'})
        if a and a.split('.')[0] == b.split('.')[0]:
            out.append({'field': f'streams[{j-1}]', 'message': f'第 {j} 条连接把 {a.split(".")[0]} 接到了自己，请改为通过边界表示'})
    return out
