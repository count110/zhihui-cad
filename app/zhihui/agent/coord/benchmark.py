"""Shared fair evaluation harness for offline and real single/multi runs."""
import json
import os
import shutil
import time
from pathlib import Path


async def evaluate(case,mode,work,mock=True):
    from zhihui.agent.replay import CASE_DIR,sha256
    from zhihui.agent.llm import make_llm,ScriptedLLM,demo_steps
    from zhihui.agent.orchestrator import Orchestrator
    from .coordinator import Coordinator
    from zhihui.bench.run import load_cases,build_base
    from zhihui.tools.core import ToolService
    work=Path(work).resolve();work.mkdir(parents=True,exist_ok=True)
    old={k:os.environ.get(k) for k in ('ZHIHUI_DATA_DIR','ZHIHUI_MCP','ZHIHUI_LLM')}
    os.environ.update(ZHIHUI_DATA_DIR=str(work),ZHIHUI_MCP='inproc',ZHIHUI_LLM='mock' if mock else 'real')
    try:
        replacements={};file_ref=None
        for item in case.get('inputs',[]):
            if not item['available']:raise FileNotFoundError(item['original_file_ref'])
            source=CASE_DIR/item['path']
            if sha256(source)!=item['sha256']:raise ValueError('Replay input checksum mismatch')
            dest=work/'uploads'/case['id']/source.name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,dest)
            replacements[item['token']]=dest.relative_to(work).as_posix()
        if replacements:file_ref=replacements.get('$file_ref')
        elif case.get('base_case'):
            dest=build_base(ToolService(),load_cases(),case['base_case'],work/'uploads'/case['id']/'input.dxf')
            file_ref=dest.relative_to(work).as_posix()
        def substitute(x):
            if isinstance(x,str):return replacements.get(x,x)
            if isinstance(x,list):return [substitute(y) for y in x]
            if isinstance(x,dict):return {k:substitute(v) for k,v in x.items()}
            return x
        events=[];started=time.monotonic()
        if mode=='multi':agent=Coordinator(mock=mock,on_event=lambda k,p:events.append({'kind':k,**p}))
        else:agent=Orchestrator(ScriptedLLM(substitute(case.get('steps') or demo_steps(case['prompt'],file_ref))) if mock else make_llm(case['prompt'],file_ref),lambda k,p:events.append({'kind':k,**p}))
        out=await agent.run(case['prompt'],file_ref)
        elapsed=time.monotonic()-started
        accepted=case['expected'].get('outcome',['READY','READY_WITH_WARNINGS','WAITING_CONFIRMATION'])
        passed=out.status in accepted and out.usage['tool_calls']<=40 and elapsed<=180
        if out.status=='WAITING_CONFIRMATION':passed=passed and bool(out.question and 2<=len(out.question.get('options',[]))<=4)
        coverage=out.usage.get('checklist_coverage',0)
        if mode=='single' and out.drawing_id:
            from .roles import rule_plan,route
            from .review import review_spec
            from zhihui.kb.search import SearchEngine
            from zhihui.tools.store import DrawingStore
            baseline=rule_plan(case['prompt'],route(case['prompt'],file_ref),SearchEngine().search(case['prompt'],section='process_templates'))
            coverage=review_spec(baseline,DrawingStore().load(out.drawing_id),out.validation or {})['coverage']
        if mode=='multi' and case['expected'].get('checklist_min'):passed=passed and coverage>=case['expected']['checklist_min']
        if out.status in {'READY','READY_WITH_WARNINGS'}:passed=passed and any(a['format']=='dxf' and (work/a['file_ref']).is_file() for a in out.artifacts)
        return {'id':case['id'],'category':'S','mode':mode,'mock':mock,'status':out.status,'passed':bool(passed),'reason':out.message,'seconds':round(elapsed,3),'usage':out.usage,'coverage':coverage,'question':out.question,'report':out.report,'events':events}
    finally:
        for k,v in old.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
