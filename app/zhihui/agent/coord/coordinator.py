"""Pure Python stage machine; models never schedule stages or compute geometry."""
import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import asdict

from zhihui.agent.orchestrator import Orchestrator,Outcome,mcp_client
from zhihui.agent.guards import user_text,_numbers
from zhihui.agent.execution import usable
from zhihui.api.db import DB
from zhihui.tools.store import data_dir
from zhihui.kb.search import SearchEngine
from zhihui.kb.gaps import GapStore
from zhihui.kb.capability import check_capability
from .blackboard import TaskState
from .spec import ProcessSpec,compile_spec
from .roles import RoleRunner,RouterOutput,EditorOutput,RepairOutput,ReporterOutput,QueryOutput,route,rule_plan,merge_user_requirements
from .requirements import parse_topology,parse_conn_refs,resolve_attach,extract_title_block
from zhihui.cad import symgen
from zhihui.kb import websearch
from .arg_norm import normalize_args,parse_move,question_for_missing,sanitize_question
from .review import review_spec

STATUS_CN={'READY':'已交付','READY_WITH_WARNINGS':'带警告交付','WAITING_CONFIRMATION':'等待确认','FAILED':'失败'}
GAP_CN={'symbol_generated':'图元库没有，已检索并自动生成示意图元，待专业确认','family_substitution':'用同类图元替代，待专业确认','ask_user':'需要你补充或确认','skip_hatch':'已跳过填充','unsupported':'当前不支持，已记录'}
STAGES=('router','planner','knowledge','symbol','capability','confirmation','builder','editor','layout_fixer','reviewer','repairer','export','reporter')
EDITOR_TOOLS={'inspect_drawing','find_entities','move_equipment','edit_equipment','connect_ports','place_equipment','edit_title_block','delete_annotation','edit_annotation','add_hatch','edit_hatch'}


class Coordinator(Orchestrator):
    def __init__(self,llm=None,on_event=None,task_id=None,db=None,mock=None):
        super().__init__(llm,on_event)
        self.db=db or DB(data_dir()/'zhihui.db');self.task_id=task_id or 'coord_'+uuid.uuid4().hex[:12]
        task=self.db.get_task(self.task_id)
        self.state=TaskState.model_validate(task['state']) if task and task.get('state') else TaskState(task_id=self.task_id)
        self.mock=(os.getenv('ZHIHUI_LLM')=='mock') if mock is None else mock
        self.role_usage={'prompt_tokens':0,'completion_tokens':0,'models':set()}
        self._prior=dict(self.state.usage_total)
        self.roles=RoleRunner(self.mock,self.role_usage)
        self.role='router';self._confirmed_spec=None;self._conn_refs={};self.tool_schemas={};self.search_calls=0
        self.stage_started=time.monotonic()
        self.control.config.stage_calls.update({'planner':6,'knowledge':6,'symbol':2,'capability':2,'builder':15,'editor':15,'validation':20,'layout_fixer':6,'reviewer':6,'repairer':12,'export':4})

    def usage(self):
        return {**super().usage(),'prompt_tokens':self.role_usage['prompt_tokens'],'completion_tokens':self.role_usage['completion_tokens'],
                'models':sorted(self.role_usage['models']),'role_attempts':self.role_usage.get('role_attempts',[]),'checklist_coverage':self.state.coverage,'knowledge_gap_count':len(self.state.knowledge_gaps),
                'confirmation_count':sum(d.get('action')=='confirmation' for d in self.state.decisions),'agent_mode':'multi',
                'search_calls':self.search_calls,'total':self._total_usage()}

    def _total_usage(self):
        """Task-level consumption across confirm/resume rounds: earlier rounds (persisted) + this round."""
        now={'tool_calls':self.tool_calls,'prompt_tokens':self.role_usage['prompt_tokens']+self.prompt_tokens,'completion_tokens':self.role_usage['completion_tokens']+self.completion_tokens,
             'seconds':round(time.monotonic()-self.t0,1)}
        return {k:round(self._prior.get(k,0)+v,1) if k=='seconds' else self._prior.get(k,0)+v for k,v in now.items()}

    async def emit(self,kind,payload):
        if hasattr(self,'role'):
            limit=self.control.config.stage_calls.get(self.control.stage,self.control.config.max_calls)
            payload={'agent':self.role,'stage':self.control.stage,'budget_remaining':max(0,min(limit-self.control.stage_used.get(self.control.stage,0),self.control.config.max_calls-self.control.calls)),**payload}
        await super().emit(kind,payload)

    def persist(self):
        self.state.attempt_log=self.control.attempt_log
        self.state.usage_total=self._total_usage()
        self.state.best_revision=self.control.best.get(self.state.drawing_id,{})
        self.db.set_state(self.task_id,self.state.model_dump(mode='json'))

    async def stage(self,role):
        if role not in STAGES:raise ValueError('Unknown stage '+role)
        self.role=role;self.state.stage=role;self.control.stage=role
        self.stage_started=time.monotonic()
        self.state.decisions.append({'stage':role,'reason':'确定性状态机推进'})
        self.persist()
        await self.emit('stage',{'role':role})

    async def _program_call(self,client,name,args):
        if name=='export_artifacts' and 'png' in (args.get('formats') or []) and 'svg' not in args['formats']:args={**args,'formats':[*args['formats'],'svg']}   # web preview is vector: zoom never blurs
        limit=self.control.config.stage_calls.get(self.control.stage,40)
        seconds=self.roles.config.get(self.role,{}).get('seconds',45)
        if time.monotonic()-self.stage_started>seconds:raise TimeoutError('Stage time budget exhausted: '+self.role)
        if self.control.stage_used.get(self.control.stage,0)>=limit:raise TimeoutError('Stage budget exhausted: '+self.control.stage)
        return await super()._program_call(client,name,args)

    async def _validate(self,client,did,compare=False):
        previous=self.control.stage;self.control.stage='validation'
        try:
            result=await super()._validate(client,did,compare=compare)
            # Construction snapshots are incomplete proposals, not delivery candidates.
            # Keep their validation cache, but select a best deliverable after compilation.
            if previous=='builder':self.control.best.pop(did,None)
            return result
        finally:self.control.stage=previous

    async def role_call(self,role,schema,context,fallback):
        if self.control.budget_reason():return schema.model_validate(fallback())
        remaining_stage=self.roles.config.get(role,{}).get('seconds',45)-(time.monotonic()-self.stage_started)
        try:return await asyncio.wait_for(self.roles.run(role,schema,context,fallback),timeout=max(.1,min(remaining_stage,self.control.remaining_seconds-12)))
        except (TimeoutError,ValueError):return schema.model_validate(fallback())

    async def ask(self,question,messages,options=None):
        self.state.decisions.append({'action':'confirmation','question':question})
        self.persist()
        out=await self._ask(question,self.state.drawing_id,messages,options)
        out.spec=self.state.spec.model_dump(mode='json') if self.state.spec else None
        out.generated_symbols=self.state.generated_symbols
        await self._attach_preview(out)
        return out

    async def _attach_preview(self,out):
        """A non-fatal wait should still show what exists so far: export a PNG of the current working version (best effort, never fails the question)."""
        client=getattr(self,'_client',None)
        if client is None or not self.state.drawing_id or self.control.remaining_seconds<8:return
        previous=self.control.stage
        try:
            self.control.stage='export'
            exported=await self._program_call(client,'export_artifacts',{'drawing_id':self.state.drawing_id,'formats':['png'],'allow_warnings':True,'delivery_notes':['当前为未完成的工作版本，仅供预览']})
            if exported.get('ok'):out.artifacts=[a for a in exported.get('files',[]) if a.get('format')=='png']
        except Exception:pass
        finally:self.control.stage=previous

    def gap(self,query,fallback,context=None):
        gid=GapStore(self.db.path if hasattr(self.db,'path') else data_dir()/'zhihui.db').record(self.task_id,query,context or self.state.user_facts,fallback)
        item={'id':gid,'query':query,'fallback_used':fallback,'status':'open'};self.state.knowledge_gaps.append(item);return item

    async def run(self,prompt,file_ref=None,resume=None,answer=None,confirmed_spec=None):
        if self.db.get_task(self.task_id) is None:self.db.create_task(self.task_id,prompt,file_ref)
        self.state.user_facts.setdefault('text',prompt)
        if answer:self.state.user_facts['answer']=answer
        messages=[{'role':'user','content':prompt+(('\n用户回答：'+answer) if answer else '')}]
        async with mcp_client() as client:
            self._client=client
            listed=(await client.list_tools()).tools
            self.control.available_tools={t.name for t in listed}
            self.tool_schemas={t.name:json.loads(json.dumps(t.input_schema)) for t in listed}
            await self.emit('status',{'status':'PLANNING'})
            try:
                return await self.pipeline(client,prompt,file_ref,answer,confirmed_spec,messages)
            except (TimeoutError,ValueError) as error:
                self.warnings.append('流水线升级或降级原因：'+str(error))
                if self.state.drawing_id:
                    await self.stage('export')
                    out=await self._finish(client,self.state.drawing_id,self.warnings[-1],messages,warning=True)
                    return await self.finish_report(out)
                return await self.ask('需要补充核心设备或确认工艺后才能构图。',messages,['提供设备与连接清单','选择并确认已有工艺模板','暂不生成图纸'])

    async def pipeline(self,client,prompt,file_ref,answer,confirmed_spec,messages):
        await self.stage('router')
        intent=await self.role_call('router',RouterOutput,{'prompt':messages[0]['content'],'has_file':bool(file_ref)},lambda:route(messages[0]['content'],file_ref).model_dump())
        deterministic=route(messages[0]['content'],file_ref)
        if deterministic.task_type=='out_of_scope' or deterministic.handoff:intent=deterministic
        self.state.intent=intent.model_dump();self.persist()
        if intent.handoff=='equipment' and not answer:      # 设备图请求：不套工艺模板，提示去化工设备图工作空间（用户回答后仍坚持画流程图则照常往下走）
            return await self.ask('这是化工设备图请求（设备装配图、管口方位图），不是工艺流程图：请到“化工设备图”工作空间重新描述，这里不会套用工艺模板。',messages,['到化工设备图工作空间重新描述','仍按工艺流程图处理'])
        if intent.task_type=='out_of_scope':
            report=check_capability(prompt);self.state.capability_report=report;self.gap(prompt,'unsupported')
            return await self.ask(report['fallback'],messages,report.get('options') or ['导出 DXF 后在 CAD 中旋转','保留当前图纸'])
        if file_ref or self.state.drawing_id:
            return await self.edit_existing(client,messages[0]['content'],file_ref,intent,messages)
        if self.state.spec is not None and (confirmed_spec is not None or self.state.confirmed or (answer and re.search('^(确认|同意)',answer))):
            self.state.spec=ProcessSpec.model_validate(confirmed_spec) if confirmed_spec else self.state.spec
            self.state.confirmed=True
            self.state.user_facts['confirmed_spec']=self.state.spec.model_dump(mode='json')
            messages.append({'role':'user','content':'用户确认方案：'+json.dumps(self.state.user_facts['confirmed_spec'],ensure_ascii=False)})
        else:
            await self.stage('knowledge')
            engine=SearchEngine();knowledge=engine.search(prompt,section='process_templates')
            if knowledge['status']!='found' and not self.mock:
                for _ in range(2):
                    rewritten=await self.role_call('knowledge',QueryOutput,{'query':prompt,'available_templates':[d['data']['name'] for d in engine.documents('process_templates')]},lambda:{'query':prompt})
                    knowledge=engine.search(rewritten.query,section='process_templates')
                    if knowledge['status']=='found':break
            self.persist()
            await self.stage('planner')
            baseline=rule_plan(prompt,intent,knowledge)
            context={'prompt':prompt,'user_facts':self.state.user_facts,'proposal':baseline.model_dump(mode='json'),'knowledge_status':knowledge['status']}
            explicit={e.tag for e in baseline.equipment if e.source=='user'}
            fully_explicit=len(explicit)>=2 and set(re.findall(r'[A-Z]{1,3}-\d{2,4}',prompt))>=explicit
            # Explicit edges/boundaries parsed from the user's own sentences cover every device: the rule proposal is authoritative.
            _,topo=parse_topology(prompt)
            covered={t for e in topo.edges for t in (e['from'],e['to'])}|{t['to'] for t in topo.terms}
            fully_explicit=fully_explicit or (bool(topo.edges) and len(explicit)>=2 and explicit<=covered)
            # Fully specified tagged chains already have a validated rule proposal.
            # Avoid asking a model to transcribe dozens of verbatim clauses again.
            proposed=baseline if fully_explicit else await self.role_call('planner',ProcessSpec,context,lambda:baseline.model_dump(mode='json'))
            if '臭氧发生器' in prompt and any(e.confidence<.6 for e in baseline.equipment):proposed=baseline
            explicit={e.tag for e in baseline.equipment if e.source=='user'}
            if explicit and not explicit.issubset({e.tag for e in proposed.equipment}):proposed=baseline
            # A model may enrich a proposal, but cannot remove explicit requirements
            # or rewrite the user's clause inventory to inflate checklist coverage.
            proposed.checklist=baseline.checklist
            if proposed is not baseline and not fully_explicit:proposed=merge_user_requirements(proposed,baseline)
            if fully_explicit:
                proposed=baseline
            from zhihui.ir import PAPER_SIZES
            from zhihui.cad.symbols import load_symbols
            symbols=load_symbols()
            for e in proposed.equipment:
                if e.tag not in explicit and e.tag not in prompt:e.source='inferred'
                if e.symbol_id in symbols and symgen.is_unconfirmed(e.symbol_id):
                    e.source='inferred';e.confidence=min(e.confidence,.5);proposed.assumptions.append(e.tag+' '+e.name+' 使用自动生成的示意图元，须核对外形与端口')
                    old=symgen.load_generated(e.symbol_id)
                    if old:self._record_symbol(old,old['generated'].get('sources',[]),bool(old['generated'].get('searched')),True)
                if e.symbol_id not in symbols:
                    made=await self.make_symbol(e)
                    if made:
                        e.symbol_id=made['id'];e.family=made['id'];e.name=made['name'];e.source='inferred';e.confidence=min(e.confidence,.5)
                        proposed.assumptions.append(e.tag+' '+e.name+' 在图元库中没有，已'+('检索并' if made['searched'] else '')+'自动生成示意图元（'+('依据：'+'；'.join(made['sources']) if made['sources'] else '无可引用的检索依据')+'），须专业确认')
                        symbols=load_symbols();continue
                    matches=engine.search(e.name,section='symbols',family=e.family or None)['items']
                    if matches:
                        original=e.name;e.symbol_id=matches[0]['id'];e.name+='(待确认)';e.source='inferred';e.confidence=.5
                        proposed.assumptions.append('未知图元 '+original+' 改用同族 '+e.symbol_id+'，待确认');self.gap(original,'family_substitution')
                    else:return await self.ask('核心设备 '+e.name+' 没有可确认图元，请选择处理方式。',messages,['提供图元/参考图','确认使用通用设备示意','暂停绘图'])
                if e.params and (_numbers(json.dumps(e.params,ensure_ascii=False))-_numbers(prompt) or {'dedicated_symbol','symbol_substitute','substitute_note'}&set(e.params)):
                    # Model commentary (e.g. a substitution note mentioning "ClO2"), not a user fact: keep it as an assumption, never as a drawing parameter.
                    proposed.assumptions.append(e.tag+' 模型备注（未写入图纸参数）：'+json.dumps(e.params,ensure_ascii=False));e.params={}
            if proposed.drawing.paper_hint not in PAPER_SIZES:proposed.drawing.paper_hint=baseline.drawing.paper_hint
            if any(str(v) not in prompt for v in proposed.drawing.title_block.values()):proposed.drawing.title_block=baseline.drawing.title_block
            if not proposed.checklist:proposed.checklist=baseline.checklist
            self.state.spec=proposed
            if '臭氧发生器' in prompt:self.gap('臭氧发生器','family_substitution',{'prompt':prompt})
            if not proposed.equipment:
                self.gap(prompt,'ask_user',{'prompt':prompt})
                return await self.ask('未找到可信工艺与核心设备清单。',messages,['提供设备清单','提供参考工艺图','保留为知识缺口'])
            await self.stage('capability')
            self.state.capability_report={'supported':True,'mappings':[{'operation':n,'tool':n} for n,_ in compile_spec(proposed)],'unsupported':[]}
            self.persist()
            if proposed.needs_confirmation():
                await self.stage('confirmation')
                self.gap(prompt,'ask_user',{'prompt':prompt,'reason':'方案包含推断或低置信度设备'})
                return await self.ask(('方案中有自动生成的图元，请核对其外形与端口，并核对设备、连接及假设后确认。' if self.state.generated_symbols else '方案含推断设备或待确认图元，请核对设备、连接及假设后确认。'),messages,['确认当前方案','修改设备与连接后确认','补充参考工艺'])
        await self.stage('builder')
        for name,raw in compile_spec(self.state.spec):
            args=self._resolve_refs(name,dict(raw))
            if args is None:
                self.warnings.append('仪表/阀门所需的安装位置在图中不存在，已跳过：'+name);continue
            optional=name=='add_instrument' or (name=='connect_ports' and 'fittings' in str(raw.get('items',''))[:400] and any('ref' in it for it in args.get('items',[])))
            if self.state.drawing_id:args['drawing_id']=self.state.drawing_id
            self.guard.normalize(name,args,messages)
            blocked=self.guard.check(name,args,messages)
            if blocked:return await self.ask(blocked['message'],messages)
            data=await self._program_call(client,name,args)
            self.control.observe(name,args,data)
            if name=='connect_ports' and data.get('ok'):self._conn_refs.update(parse_conn_refs(data.get('connections')))
            if not data.get('ok') and optional:
                self.warnings.append('['+str(data.get('error_code',''))+'] '+str(data.get('message','仪表/阀门未画成')));continue
            if not data.get('ok'):
                if name=='add_hatch':self.gap(str(args.get('target'))+':'+str(args.get('region')),'skip_hatch');self.warnings.append(data.get('message','填充未完成'));continue
                if self.state.drawing_id and await self._layout_recovery(client,self.state.drawing_id,messages,data.get('error_code','')):continue
                return await self.ask(data.get('message','构图需要补充条件'),messages)
            self.state.drawing_id=data.get('drawing_id',self.state.drawing_id)
            await self._validate(client,self.state.drawing_id)
            self.persist()
        return await self.review_and_export(client,messages)

    def _resolve_refs(self,name,args):
        """Instrument/valve placement is named by role (pump_out, vessel...) in the spec; connection refs only exist once the pipes are drawn."""
        spec=self.state.spec;equipment={e.tag:e.symbol_id for e in spec.equipment} if spec else {}
        if name=='add_instrument' and 'attach_conn' in args:
            target=resolve_attach(args.pop('attach_conn'),equipment,self._conn_refs)
            if target is None:return None
            args['attach_to']=target
        if name=='add_annotation' and 'anchor_conn' in args:
            target=resolve_attach(args.pop('anchor_conn'),equipment,self._conn_refs)
            if target is None:return None
            args['anchor_ref']=target
        if name=='connect_ports':
            for it in args.get('items') or []:
                if 'ref_conn' in it:
                    target=resolve_attach(it.pop('ref_conn'),equipment,self._conn_refs)
                    if target is None:return None
                    it['ref']=target
        return args

    async def make_symbol(self,e):
        """No library symbol: search the web for the equipment (name only), let the designer role pick a template, build + save the symbol. None on failure."""
        name=re.sub(r'[（(]待确认[）)]','',e.name).strip() or e.tag
        reused=symgen.find_generated(name)
        if reused:
            return self._record_symbol(reused,reused['generated'].get('sources',[]),bool(reused['generated'].get('searched')),True)
        await self.stage('symbol')
        evidence=websearch.Evidence()
        if websearch.enabled() and not self.control.budget_reason():
            await self.emit('execution_control',{'action':f'图元库没有「{name}」，正在联网检索并生成示意图元'})
            try:
                evidence=await asyncio.wait_for(asyncio.to_thread(websearch.gather_equipment_evidence,name),timeout=min(25,max(1,self.control.remaining_seconds-40)))
            except (TimeoutError,asyncio.TimeoutError):
                evidence.error='检索超时'
            self.search_calls+=evidence.calls
            if evidence.error:self.warnings.append(f'检索「{name}」：{evidence.error}')
        ctx={'name':name,'tag_prefix':e.tag.split('-')[0],'templates':list(symgen.TEMPLATES),'decorations':list(symgen.DECORATIONS),
             'evidence':[{'title':h.title,'url':h.url,'summary':h.summary,'score':h.score} for h in evidence.hits]}
        draft=await self.role_call('symbol_designer',symgen.SymbolDraft,ctx,lambda:symgen.heuristic_draft(name,e.tag).model_dump())
        urls={h.url for h in evidence.hits}
        draft.evidence_urls=[u for u in draft.evidence_urls if u in urls]      # a model cannot cite a page it never saw
        draft.name_cn=name
        if not evidence.hits:draft.confidence=min(draft.confidence,.4)
        try:
            raw=symgen.build_symbol(draft,symgen.symbol_id_for(name),draft.evidence_urls,bool(evidence.hits))
            symgen.save_symbol(raw)
        except (ValueError,KeyError,OSError):
            return None
        self.gap(name,'symbol_generated',{'sources':draft.evidence_urls,'template':draft.template})
        await self.emit('execution_control',{'action':f'已生成「{name}」示意图元（{draft.template}），请在确认页核对外形与端口'})
        return self._record_symbol(raw,draft.evidence_urls,bool(evidence.hits),False)

    def _record_symbol(self,raw,sources,searched,reused):
        g=raw['generated']
        rec={'id':raw['id'],'name':raw['name_cn'],'preview_ref':'symbols_generated/'+raw['id']+'.png','sources':list(sources),'searched':searched,'template':g['template'],
             'confidence':g.get('confidence',.3),'rationale':g.get('rationale',''),'reused':reused,'ports':[{'name':p['name'],'kind':p['kind'],'semantic':p['semantics']} for p in raw['ports']]}
        if not any(x['id']==rec['id'] for x in self.state.generated_symbols):self.state.generated_symbols.append(rec)
        return rec

    def _tool_specs(self):
        out=[]
        for name in sorted(EDITOR_TOOLS):
            sc=self.tool_schemas.get(name) or {}
            props={k:{'type':v.get('type',v.get('anyOf') and 'any'),'description':(v.get('description') or '')[:80]} for k,v in (sc.get('properties') or {}).items() if k not in ('drawing_id','restore_revision','include_state')}
            out.append({'name':name,'required':[r for r in sc.get('required',[]) if r!='drawing_id'],'properties':props})
        return out

    def _complete(self,call,snapshot,prompt):
        args={'drawing_id':self.state.drawing_id,**call.arguments}
        args,missing=normalize_args(call.name,args,snapshot,prompt,self.tool_schemas.get(call.name))
        return args,missing

    async def _ask(self,message,did,messages,options=None):
        """Every question passes one gate: no validator text, no option that smuggles in a number the user never gave."""
        message,options,debug=sanitize_question(message,options,user_text(messages))
        if debug:await self.emit('execution_control',{'action':'question_sanitized','debug':debug[:600]})
        return await super()._ask(message,did,messages,options)

    async def edit_existing(self,client,prompt,file_ref,intent,messages):
        await self.stage('editor')
        args={'drawing_id':self.state.drawing_id,'include_state':True} if self.state.drawing_id else {'file_ref':file_ref,'include_state':True}
        snapshot=await self._program_call(client,'inspect_drawing',args)
        if not snapshot.get('ok'):return await self.ask(snapshot.get('message','无法读取图纸'),messages)
        self.state.drawing_id=snapshot['drawing_id']
        await self._validate(client,self.state.drawing_id)
        def fallback():
            if re.search('保留当前|暂不修改|不改了',prompt):return {'actions':[]}
            if intent.task_type=='edit_title_block':
                fields=extract_title_block(prompt)
                if not fields:
                    m=re.search(r'(?:设计单位|单位名称)(?:写为|改为|写|为|[:：])?\s*(.+)',prompt)
                    fields={'unit_name':m.group(1).strip()} if m else {}
                if fields:return {'actions':[{'name':'edit_title_block','arguments':{'fields':fields}}]}
            if intent.task_type=='qa':return {'actions':[]}
            mv=parse_move(prompt)
            if mv:return {'actions':[{'name':'move_equipment','arguments':{'equipment_ref':mv[0],'dx':mv[1],'dy':mv[2]}}]}
            deleted=re.findall(r'A\d+',prompt)
            if deleted and re.search('确认.*删|同意.*删',prompt):return {'actions':[{'name':'delete_annotation','arguments':{'annotation_ref':ref,'confirmed':True}} for ref in deleted]}
            return {'question':'请指定标注 ID，或设备位号、方向和距离。','options':['提供标注 ID 与新文字','提供设备位号、方向和距离','保留当前图纸']}
        ctx={'prompt':prompt,'drawing':{k:snapshot[k] for k in ('equipment','connections')},'annotations':snapshot['state'].get('annotations',[]),'allowed_tools':self._tool_specs()}
        action=await self.role_call('editor',EditorOutput,ctx,fallback)
        if action.question:return await self.ask(action.question,messages,action.options or None)
        for call in action.actions:
            if call.name not in EDITOR_TOOLS:return await self.ask('修改动作超出该角色权限。',messages)
            args,missing=self._complete(call,snapshot,prompt)
            if missing:   # one structured retry with the exact schema, then the deterministic parser, before bothering the user
                retry=await self.role_call('editor',EditorOutput,{**ctx,'fix':{'tool':call.name,'missing':missing,'got':call.arguments}},fallback)
                cand=next((c for c in retry.actions if c.name==call.name),None)
                if cand:args,missing=self._complete(cand,snapshot,prompt)
                if missing:
                    cand=next((c for c in EditorOutput.model_validate(fallback()).actions if c.name==call.name),None)
                    if cand:args,missing=self._complete(cand,snapshot,prompt)
            if missing:return await self.ask(question_for_missing(call.name,missing),messages,['提供缺少的信息后重新提交','保留当前图纸，暂不修改'])
            self.guard.normalize(call.name,args,messages)
            blocked=self.control.before(call.name,args) or self.guard.check(call.name,args,messages)
            if blocked:return await self.ask(blocked['message'],messages)
            result=await self._program_call(client,call.name,args);decision=self.control.observe(call.name,args,result)
            if not result.get('ok'):return await self.ask(result.get('message','操作需要确认'),messages)
            v,change=await self._validate(client,self.state.drawing_id,compare=True)
            if change=='rollback':await self._rollback(client,self.state.drawing_id);self.warnings.append('恢复历史最优版本');break
            if decision:break
        return await self.review_and_export(client,messages)

    async def review_and_export(self,client,messages):
        await self.stage('layout_fixer')
        labels=any(re.search('下方.*标注|下方.*名称',c.text_span) for c in self.state.spec.checklist) if self.state.spec else False
        fix=await self._program_call(client,'apply_layout_fixes',{'drawing_id':self.state.drawing_id,'labels_below':labels})
        if fix.get('ok'):await self._validate(client,self.state.drawing_id)
        await self.stage('reviewer')
        for round_no in range(3):
            validation,_=await self._validate(client,self.state.drawing_id)
            snapshot=await self._program_call(client,'inspect_drawing',{'drawing_id':self.state.drawing_id,'include_state':True})
            if self.state.spec:
                from zhihui.ir import Drawing
                review=review_spec(self.state.spec,Drawing.model_validate(snapshot['state']),validation,prompt=self.state.user_facts.get('text'))
                self.state.metrics=review['metrics'];self.state.requirements=review['requirements']
                self.state.coverage=review['coverage'];self.state.issues=review['issues']
            self.state.issues += [{'id':e['code']+':'+','.join(e.get('refs',[])),'source':'validate','status':'open','attempts':round_no,'target_id':next(iter(e.get('refs',[])),''),**e} for e in validation.get('errors',[])]
            self.persist()
            if validation.get('passed') or round_no==2:break
            issue=next(iter(validation.get('errors',[])),None)
            if not issue:break
            await self.stage('repairer')
            rule=self.control.rule(issue['code'])
            chosen=await self.role_call('repairer',RepairOutput,{'issue':issue,'allowed_actions':rule['actions'],'failed_signatures':[a['signature'] for a in self.control.attempt_log if not a['ok']]},lambda:{'action':rule['actions'][0]})
            if chosen.action not in rule['actions']:raise ValueError('Repair action outside playbook')
            if not await self._layout_recovery(client,self.state.drawing_id,messages,issue['code']):break
            await self.stage('reviewer')
        await self.visual_review(client)
        for issue in self.state.issues:
            if issue.get('status')!='resolved':self.warnings.append('['+issue.get('code','issue')+'] '+issue.get('message','未解决事项'))
        await self.stage('export')
        out=await self._finish(client,self.state.drawing_id,'图纸已生成；请核对交付报告。',messages,warning=bool(self.warnings))
        return await self.finish_report(out)

    async def visual_review(self,client):
        """Optional independent vision review; output is evidence only, never actions."""
        provider=os.getenv('ZHIHUI_VL_PROVIDER');model=os.getenv('ZHIHUI_VL_MODEL')
        if self.mock or not provider or not model:return
        from .vision import inspect_image
        try:
            exported=await self._program_call(client,'export_artifacts',{'drawing_id':self.state.drawing_id,'formats':['png'],'allow_warnings':True,'delivery_notes':self.warnings})
            png=next(a for a in exported.get('files',[]) if a['format']=='png')
            issues=await asyncio.wait_for(inspect_image(data_dir()/png['file_ref'],provider,model,self.state.spec,self.role_usage),timeout=min(20,max(.1,self.control.remaining_seconds-12)))
            self.state.issues.extend(issues);self.persist()
        except Exception as error:
            self.warnings.append('视觉审图未完成：'+type(error).__name__)

    async def finish_report(self,out):
        await self.stage('reporter')
        spec=self.state.spec;m=self.state.metrics or {}
        req=m.get('requirements',{});rules=m.get('drawing_rules',{})
        lines=['# 智绘CAD 交付报告','',f'- 状态：{STATUS_CN.get(out.status,out.status)}']
        if req.get('total'):lines.append(f"- 工艺需求核验：{req.get('verified',0)}/{req.get('total',0)} 项已在图中核实"+(f"，{req['unsupported']} 项当前不支持" if req.get('unsupported') else ''))
        if rules:lines.append('- 图面规则（几何/标准自检）：'+('通过' if rules.get('passed') else f"{rules.get('errors',0)} 项未通过")+'（与工艺需求核验分开统计，通过不代表工艺正确）')
        lines+=['','## 已画内容']
        if spec:lines += ['- '+e.tag+' '+e.name for e in spec.equipment]+['- 边界 '+t.id+' '+t.name+('（进入）' if t.direction=='in' else '（离开）')+' → '+t.connected_to for t in spec.terminals]
        reqs=self.state.requirements
        if reqs:
            mark={'verified':'✓ 已核实','missing':'✗ 未满足','unsupported':'— 不支持'}
            lines+=['','## 需求逐项核验']+[f"- {mark.get(r['status'],r['status'])}：{r['text']}" for r in reqs if r['kind']!='equipment']
        todo=[i for i in self.state.issues if i.get('status')!='resolved' and i.get('source') in ('requirement','spec')]
        lines+=['','## 未满足或不支持的需求']+(['- '+i['message'] for i in todo] or ['- 无'])
        unchecked=m.get('unchecked_clauses') or []
        if unchecked:lines+=['','## 未逐项核验的描述（没有对应的可检查对象，不计入核验数）']+['- '+c for c in unchecked]
        if self.state.generated_symbols:
            lines+=['','## 自动生成的图元（图元库中没有，须专业确认）']+[f"- {g['name']}（{g['id']}）："+('依据检索 '+'；'.join(g['sources']) if g['sources'] else '没有可引用的检索依据，仅按设备名称的通用画法')+f"；模板 {g['template']}，可信度 {g['confidence']}" for g in self.state.generated_symbols]
        lines+=['','## 推断与假设']
        if spec:lines += ['- '+e.tag+' '+e.name+'（推断）' for e in spec.equipment if e.source=='inferred']+['- '+s for s in spec.assumptions]
        lines+=['','## 知识缺口与替代方案']+['- '+g['query']+'：'+GAP_CN.get(g['fallback_used'],g['fallback_used']) for g in self.state.knowledge_gaps]
        lines+=['','## 其他未解决问题']+['- '+w for w in self.warnings if not any(w.endswith(i['message']) or i['message'] in w for i in todo)] or ['- 无']
        lines+=['','## 下一步建议','- 核对推断项与未解决问题；补齐必要设计参数后再作正式工程使用。']
        self.state.report=chr(10).join(lines)   # one structured report; no second prose copy that repeats it
        self.persist();out.report=self.state.report;out.requirements=self.state.requirements;out.metrics=self.state.metrics;out.generated_symbols=self.state.generated_symbols;out.spec=spec.model_dump(mode='json') if spec else None;out.usage=self.usage()
        return out
