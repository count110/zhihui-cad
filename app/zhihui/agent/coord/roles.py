"""Role contracts, isolated model contexts, retries and offline rules."""
import json
import os
import re
import time
from pathlib import Path
from typing import Literal
from pydantic import Field
from .spec import Contract,ProcessSpec,DrawingSpec,EquipmentSpec,StreamSpec,TerminalSpec,HatchSpec,ChecklistItem
from zhihui.cad.symgen import SymbolDraft


class RouterOutput(Contract):
    task_type:Literal['new_drawing','edit_geometry','edit_annotation','edit_title_block','add_hatch','qa','out_of_scope']
    kind:Literal['pfd','water_elevation']='pfd'
    stage:Literal['scheme','material','construction']='scheme'
    handoff:Literal['equipment']|None=None      # 化工设备图请求：流程图链路不套工艺模板，交给“化工设备图”工作空间
    user_facts:dict=Field(default_factory=dict)


class KnowledgeOutput(Contract):
    status:Literal['found','partial','gap']
    items:list[dict]=Field(default_factory=list)
    top_score:float=0
    source:str='local'


class CapabilityOutput(Contract):
    supported:bool
    mappings:list[dict]=Field(default_factory=list)
    unsupported:list[dict]=Field(default_factory=list)


class Action(Contract):
    name:str
    arguments:dict=Field(default_factory=dict)


class EditorOutput(Contract):
    actions:list[Action]=Field(default_factory=list)
    question:str=''
    options:list[str]=Field(default_factory=list)


class RepairOutput(Contract):
    action:str
    reason:str=''


class ReviewOutput(Contract):
    issues:list[dict]=Field(default_factory=list)
    coverage:float=0


class ReporterOutput(Contract):
    markdown:str


class QueryOutput(Contract):
    query:str


SCHEMAS={'router':RouterOutput,'planner':ProcessSpec,'knowledge':KnowledgeOutput,'capability':CapabilityOutput,
         'symbol_designer':SymbolDraft,'builder':EditorOutput,'editor':EditorOutput,'layout_fixer':ReviewOutput,'reviewer':ReviewOutput,'repairer':RepairOutput,'reporter':ReporterOutput}


def equipment_handoff(prompt):
    """设备图请求（图种词 / 设备名 + 结构词，和入口路由是同一套词表）→ 'equipment'，否则 None。在流程图工作空间里只认强信号，“画一个储罐”仍按加设备处理。"""
    from zhihui.agent.general.router import route as entry_route
    return 'equipment' if entry_route(prompt,'pfd').mode=='equipment' else None


def route(prompt,file_ref=None):
    from zhihui.agent.guards import detect_stage
    task='new_drawing'
    if re.search('整体旋转|整图旋转|旋转整张|整张图纸?.{0,6}旋转|整个图纸?.{0,6}旋转|图纸.{0,4}(?:顺|逆)时针',prompt): task='out_of_scope'
    elif file_ref:
        task='edit_title_block' if re.search('设计单位|标题栏|工程名称|图号',prompt) else 'add_hatch' if re.search('填充|剖面线',prompt) else 'edit_annotation' if re.search('标注|文字|烟气.*压线',prompt) else 'qa' if re.search('审图|检查|校验',prompt) else 'edit_geometry'
    return RouterOutput(task_type=task,kind='water_elevation' if re.search('高程|净水厂|水厂',prompt) else 'pfd',stage=detect_stage(prompt) or 'scheme',handoff=equipment_handoff(prompt),user_facts={'text':prompt})


from zhihui.agent.llm_required import NO_KEY_MESSAGE,LLMUnavailable


class RoleRunner:
    def __init__(self,mock=False,usage=None):
        self.mock=mock;self.usage=usage if usage is not None else {'prompt_tokens':0,'completion_tokens':0,'models':set()}
        self.config=json.loads((Path(__file__).parent/'agents.json').read_text(encoding='utf-8'))

    async def run(self,role,schema,context,fallback):
        if self.mock: return schema.model_validate(fallback())
        from zhihui.agent.llm import OpenAICompatLLM,FailoverLLM,PROVIDERS
        cfg=self.config[role];provider=os.getenv(cfg['provider_env'],os.getenv('ZHIHUI_LLM_PRIMARY','deepseek'))
        if provider not in PROVIDERS or not os.getenv(PROVIDERS[provider]['key_env']): raise LLMUnavailable(NO_KEY_MESSAGE,'no_key')      # 没有离线版：没接入大模型就明确失败
        try:
            primary=OpenAICompatLLM(provider,timeout=30)
            primary.generation={'temperature':cfg['temperature'],'max_tokens':cfg['max_tokens']}
            override=os.getenv(cfg['model_env'])
            if override: primary.model=override
            backup_provider=os.getenv('ZHIHUI_LLM_BACKUP','qwen')
            backup=OpenAICompatLLM(backup_provider,timeout=20) if backup_provider in PROVIDERS and backup_provider!=provider and os.getenv(PROVIDERS[backup_provider]['key_env']) else None
            if backup: backup.generation={'temperature':cfg['temperature'],'max_tokens':cfg['max_tokens']}
            llm=FailoverLLM(primary,backup)
        except RuntimeError as error:
            raise LLMUnavailable(f'大模型初始化失败：{str(error)[:160]}（请检查 ⚙ 设置里的 API Key、模型名和接口地址）','init') from error
        text=(Path(__file__).parent/'prompts'/(role+'.md')).read_text(encoding='utf-8')
        messages=[{'role':'system','content':text+'\n只输出 JSON。输出契约：'+json.dumps(schema.model_json_schema(),ensure_ascii=False)},
                  {'role':'user','content':json.dumps(context,ensure_ascii=False)}]
        try:
            for attempt in range(2):
                started=time.monotonic()
                try:
                    turn=await llm.chat(messages,[])
                    self.usage['prompt_tokens']+=turn.prompt_tokens;self.usage['completion_tokens']+=turn.completion_tokens;self.usage['models'].add(turn.model)
                    content=turn.content.strip()
                    if content.startswith('```'): content=re.sub(r'^```(?:json)?\s*|\s*```$','',content)
                    validated=schema.model_validate_json(content)
                    self.usage.setdefault('role_attempts',[]).append({'role':role,'attempt':attempt+1,'model':turn.model,'seconds':round(time.monotonic()-started,3),'result':'valid'})
                    return validated
                except Exception as e:
                    self.usage.setdefault('role_attempts',[]).append({'role':role,'attempt':attempt+1,'seconds':round(time.monotonic()-started,3),'result':type(e).__name__})
                    if not isinstance(e,ValueError):      # 网络/鉴权/超时：模型没接通，明确报错，不用规则结果冒充；只有“输出不合格”才重试
                        raise LLMUnavailable(f'调用大模型失败（{type(e).__name__}: {str(e)[:160]}）。请检查网络、API Key、模型名和接口地址（⚙ 设置里可以「测试连接」）。','api_error') from e
                    messages.append({'role':'user','content':'输出不符合契约，请修正一次。错误类型：'+type(e).__name__})
            return schema.model_validate(fallback())
        finally:
            await primary.client.close()
            if backup: await backup.client.close()


def _resolves_name(name,syms):
    # a plain container/tower/pump word is already covered by the library's generic symbol; anything else with no alias hit is a real gap
    return name.endswith(('槽','罐','池','箱','仓','桶','坑','塔','泵','釜','炉')) or any(a and a in name for s in syms.values() for a in (s.name_cn,*s.aliases))


def _free_id(spec,prefix):
    used={e.tag for e in spec.equipment}|{t.id for t in spec.terminals};n=101
    while f'{prefix}-{n}' in used:n+=1
    return f'{prefix}-{n}'


def merge_user_requirements(proposed,baseline):
    """A model may enrich the proposal but cannot drop what the user asked for: re-add missing user edges, boundaries, instruments, elevations, title block."""
    base=lambda r:r.split('.')[0]
    have={(base(s.from_),base(s.to)) for s in proposed.streams}
    for s in baseline.streams:
        if s.source=='user' and (base(s.from_),base(s.to)) not in have:proposed.streams.append(s)
    names={(t.name,t.connected_to.split('.')[0]) for t in proposed.terminals}
    ids={t.id for t in proposed.terminals}|{e.tag for e in proposed.equipment}
    for t in baseline.terminals:
        if (t.name,t.connected_to.split('.')[0]) not in names:
            if t.id in ids:t=t.model_copy(update={'id':f'X-{101+len(ids)}'})
            proposed.terminals.append(t);ids.add(t.id)
    have_i={(i.get('letters'),i.get('loop_no')) for i in proposed.instruments}
    for i in baseline.instruments:
        if (i.get('letters'),i.get('loop_no')) not in have_i:proposed.instruments.append(i)
    if baseline.fittings and not proposed.fittings:proposed.fittings=list(baseline.fittings)
    eq={e.tag:e for e in proposed.equipment}
    for e in baseline.equipment:
        if e.tag in eq and e.params.get('elevations') and not eq[e.tag].params.get('elevations'):eq[e.tag].params=dict(e.params)
    proposed.drawing.title_block={**proposed.drawing.title_block,**baseline.drawing.title_block}
    if baseline.drawing.title!='工艺流程图':proposed.drawing.title=baseline.drawing.title
    return proposed


def _topology(spec,prompt,syms,plan):
    """Streams / boundaries / instruments from what the user actually wrote; falls back to the order-of-appearance chain only when no edge is stated."""
    from .requirements import parse_topology,pick_port,extract_title_block,extract_requirements
    from zhihui.agent.llm import GAS_SOURCES
    names,ctx=parse_topology(prompt,syms)
    tags=[e.tag for e in spec.equipment];sym_of={e.tag:e.symbol_id for e in spec.equipment}
    edges=[e for e in ctx.edges if e['from'] in sym_of and e['to'] in sym_of]
    gas='烟气' in prompt
    if edges:
        gas_started=False
        for e in edges:
            fs,ts=syms.get(sym_of[e['from']]),syms.get(sym_of[e['to']])
            fp=pick_port(fs,'out',e['from_hint']);tp=pick_port(ts,'in',e['to_hint'],'reboil' if e['role']=='reboil' else '')
            medium=e['medium']
            if gas:
                if not fp and sym_of[e['from']] in ('furnace','boiler'):fp='flue_out'
                gas_started=gas_started or sym_of[e['from']] in GAS_SOURCES
                if gas_started and not medium:medium='烟气'
            spec.streams.append(StreamSpec(from_=e['from']+('.'+fp if fp else ''),to=e['to']+('.'+tp if tp else ''),medium=medium))
        used={e['from'] for e in edges}|{e['to'] for e in edges}|{t['to'] for t in ctx.terms}
        for i,tag in enumerate(tags):
            if tag in used:continue
            if i>0:spec.streams.append(StreamSpec(from_=tags[i-1],to=tag,source='inferred'))
            elif len(tags)>1:spec.streams.append(StreamSpec(from_=tag,to=tags[1],source='inferred'))
            used.add(tag)
    else:
        for c in plan['connections']:
            spec.streams.append(StreamSpec(from_=c['from_port'],to=c['to_port'],medium=c['medium']))
    for t in ctx.terms:
        if t['to'] not in sym_of:continue
        port=pick_port(syms.get(sym_of[t['to']]),'in' if t['direction']=='in' else 'out',t['hint'])
        spec.terminals.append(TerminalSpec(id=t['id'],name=t['name'][:20],direction=t['direction'],medium=(t.get('medium') or t['name'])[:20],
            connected_to=t['to']+('.'+port if port else ''),source='inferred' if t['inferred'] else 'user'))
        if t['inferred']:spec.assumptions.append(f"边界「{t['name']}」默认接 {t['to']}，请核对")
    fit={}
    for r in extract_requirements(prompt,syms):
        if r.kind=='elevation' and r.args['tag'] in sym_of:
            e=next(x for x in spec.equipment if x.tag==r.args['tag'])
            e.params={'elevations':{k:r.args[k] for k in ('top','water') if k in r.args}}
        elif r.kind in ('instrument','alarm'):
            a=r.args;spec.instruments.append({'letters':a['letters'],'loop_no':a['loop_no'],'attach_conn':a['attach']['kind'],
                'at':{'FT':.65,'FCV':.85}.get(a['letters'],.5)})
        elif r.kind=='fitting':
            a=r.args;at={'闸阀':.4,'止回阀':.2,'调节阀':.85}.get(a['type'],.5)
            fit.setdefault(a['where'],[]).append({'type':a['type'],'at':at})
            if a.get('tag'):spec.annotations.append({'anchor_conn':a['where'],'text':a['tag']})
    for where,items in fit.items():spec.fittings.append({'items':[{'ref_conn':where,'fittings':items}]})
    tb=extract_title_block(prompt)
    if tb.get('drawing_name'):spec.drawing.title=tb['drawing_name']
    spec.drawing.title_block.update(tb)


def rule_plan(prompt,intent,knowledge):
    from zhihui.agent.llm import heuristic_plan,DEMO_PLAN
    from zhihui.cad.symbols import load_symbols
    syms=load_symbols();plan=heuristic_plan(prompt)
    if plan:
        plan['equipment']=[e for e in plan['equipment'] if e['tag'].split('-')[0] in {'P','V','T','E','R','C','D','S','B','F','M','X','K','XV'}]
        tags={e['tag'] for e in plan['equipment']}
        plan['connections']=[c for c in plan['connections'] if c['from_port'].split('.')[0] in tags and c['to_port'].split('.')[0] in tags]
    if plan and len(plan['equipment'])>=2:
        spec=ProcessSpec(drawing=DrawingSpec(kind=intent.kind,stage=intent.stage,title=plan['title'],paper_hint='A3' if 'A3' in prompt else 'A1',standard='water_supply' if intent.kind=='water_elevation' else 'hg_pfd'))
        for e in plan['equipment']:
            known=_resolves_name(e['name'],syms) or not e['name']
            if not known:e={**e,'symbol_id':'__unknown__'}   # no library symbol at all: the coordinator searches and generates one
            spec.equipment.append(EquipmentSpec(**e,family=syms[e['symbol_id']].family if e['symbol_id'] in syms else '',**({'source':'inferred','confidence':.5} if not known else {})))
        _topology(spec,prompt,syms,plan)
        boundary=[('助燃空气','in','F-101.air_in'),('蒸汽','out','B-101.out'),('急冷水','in','T-101.spray_in'),('飞灰','out','D-101.dust_out')]
        for name,direction,target in boundary:
            if not spec.terminals and name in prompt and target.split('.')[0] in tags:
                spec.terminals.append(TerminalSpec(id=f'X-{101+len(spec.terminals)}',name=name,direction=direction,medium=name,connected_to=target))
        if re.search('填充|堆积区|燃烧区',prompt):
            from zhihui.cad.named_regions import definitions
            for e in spec.equipment:
                if definitions(e.symbol_id).get('fill'):spec.hatches.append(HatchSpec(target=e.tag,region='fill'))
    elif '臭氧发生器' in prompt:
        spec=ProcessSpec(equipment=[EquipmentSpec(tag='M-101',name='臭氧发生器',symbol_id='__unknown__',family='',source='inferred',confidence=.5)],assumptions=['图元库没有臭氧发生器专用图元：将检索并生成示意图元；生成失败时借用同类发生装置图元，均非设备详图，须专业确认。'])
    elif knowledge['items']:
        template=knowledge['items'][0]['data'];spec=ProcessSpec(drawing=DrawingSpec(kind=intent.kind,title=template.get('name','工艺方案'),standard='water_supply' if intent.kind=='water_elevation' else 'hg_pfd'))
        numbers={}
        for e in template.get('equipment',[]):
            prefix=e['tag_prefix'];numbers[prefix]=numbers.get(prefix,100)+1
            spec.equipment.append(EquipmentSpec(tag=f'{prefix}-{numbers[prefix]}',name=e['name'],symbol_id=e['symbol_id'],family=e['family'],source='inferred',confidence=e.get('confidence',.8)))
        for a,b in zip(spec.equipment,spec.equipment[1:]):spec.streams.append(StreamSpec(from_=a.tag,to=b.tag,source='inferred'))
        for t in template.get('terminals',[])+template.get('auxiliary_streams',[]):
            index=t.get('equipment_index')
            if index is None or index>=len(spec.equipment):continue
            spec.terminals.append(TerminalSpec(id=_free_id(spec,'X'),name=t['name'],direction=t['direction'],medium=t.get('medium',''),connected_to=spec.equipment[index].tag+'.'+t.get('port','in' if t['direction']=='in' else 'out'),source='inferred'))
        spec.assumptions.append(template.get('source','团队整理，待专业确认'))
    else:
        spec=ProcessSpec(assumptions=['缺少可确认的核心设备，请提供设备清单或参考工艺。'])
    clauses=[s.strip() for s in re.split(r'[。；\n]+',prompt) if s.strip() and len(s.strip())>3 and not re.fullmatch(r'[#\-*|\s\d]+',s.strip())]
    spec.checklist=[ChecklistItem(id=f'R{i+1}',text_span=s,requirement=s) for i,s in enumerate(clauses)]
    return spec
