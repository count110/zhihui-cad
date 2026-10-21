"""Independent requirement review.

The checks start from the user's own words (re-extracted by requirements.py) and look at the finished drawing.
Nothing here trusts the planner's spec.checklist, so a planner omission can no longer inflate coverage.
Inventory/geometry rules that are real checks are kept; "sounds like a process sentence" keyword passes are gone.
"""
import re
from zhihui.cad.symbols import get_symbol,footprint,usable_area,load_symbols
from .requirements import extract_requirements,pick_port,split_clauses,POS_RE

STRUCTURAL=re.compile('回流|返回|分成|分为|汇合|汇入|边界|再沸|循环|并联|旁路|串联|进入|送入|送至|流向|信号线')


def _index(drawing):
    tags={e.tag:e for e in drawing.equipment};by_id={e.id:e.tag for e in drawing.equipment}
    return tags,by_id


def _conns(drawing,by_id):
    return [(by_id.get(c.from_equip,''),c.from_port,by_id.get(c.to_equip,''),c.to_port,c.medium,c) for c in drawing.connections]


def describe(r):
    a=r.args;pos={'top':'（塔顶）','bottom':'（塔底）','shell':'（壳侧）'}
    if r.kind in ('edge','loop'):
        label='回流/循环' if r.kind=='loop' else '连接'
        return f"{label}：{a['from']}{pos.get(a['from_hint'],'')} → {a['to']}{pos.get(a['to_hint'],'')}"+(f"，介质「{a['medium']}」" if a.get('medium') else '')
    if r.kind=='terminal':
        return f"边界「{a['name']}」（{'进入' if a['direction']=='in' else '离开'}系统）与 {a['to']}{pos.get(a['hint'],'')} 的连接"
    if r.kind=='side_stream':return f"{a['tag']} 两侧物流（壳侧进出口）分开表示"
    if r.kind in ('instrument','alarm'):return f"仪表 {a['tag']}"
    if r.kind=='fitting':return f"{a['purpose']}（{ {'pump_in':'泵入口侧','pump_out':'泵出口侧'}.get(a['where'],a['where']) }）"
    if r.kind=='signal_line':return a.get('text','信号线')
    if r.kind=='elevation':return f"{a['tag']} 标高 池顶 {a.get('top','—')} m / 水位 {a.get('water','—')} m"
    if r.kind=='title_block':return f"标题栏 {a['field']} = {a['value']}"
    if r.kind=='parallel':return f"{' / '.join(a['tags'])} 并联"
    if r.kind=='equipment':return f"设备 {a['tag']} {a['name']}"
    return r.clause


def verify(r,drawing,syms=None):
    """-> ('verified'|'missing'|'unsupported', reason)"""
    syms=syms or load_symbols();tags,by_id=_index(drawing);conns=_conns(drawing,by_id);a=r.args
    if r.support=='unsupported':return 'unsupported',r.reason
    if r.kind=='equipment':
        return ('verified','') if a['tag'] in tags else ('missing','图中没有该设备')
    if r.kind in ('edge','loop'):
        if a['from'] not in tags or a['to'] not in tags:return 'missing','端点设备不在图中'
        fp=pick_port(get_symbol(tags[a['from']].symbol_id),'out',a['from_hint']);tp=pick_port(get_symbol(tags[a['to']].symbol_id),'in',a['to_hint'],'reboil' if a['role']=='reboil' else '')
        hits=[c for c in conns if c[0]==a['from'] and c[2]==a['to']]
        if not hits:return 'missing','图中没有这条连接'
        if (fp or tp) and not any((not fp or c[1]==fp) and (not tp or c[3]==tp) for c in hits):
            return 'missing','连接存在，但没有接在指定的端口（'+'/'.join(x for x in (fp,tp) if x)+'）'
        if a.get('medium') and not any(c[4] and (c[4] in a['medium'] or a['medium'].startswith(c[4])) for c in hits):
            return 'missing','连接存在，但介质「'+a['medium']+'」没有标注'
        return 'verified',''
    if r.kind=='terminal':
        t=next((e for e in drawing.equipment if e.symbol_id=='pipe_terminal' and (e.name==a['name'][:20] or e.tag==a['id'])),None)
        if t is None:return 'missing','图中没有该边界'
        if a['to'] not in tags:return 'missing','边界所连设备不在图中'
        ok=any((c[0]==t.tag and c[2]==a['to']) if a['direction']=='in' else (c[0]==a['to'] and c[2]==t.tag) for c in conns)
        return ('verified','') if ok else ('missing','边界没有接到 '+a['to'])
    if r.kind=='side_stream':
        e=tags.get(a['tag'])
        if e is None:return 'missing','设备不在图中'
        used={c[1] for c in conns if c[0]==a['tag']}|{c[3] for c in conns if c[2]==a['tag']}
        need=[p for p in ('shell_in','shell_out','water_in','water_out') if get_symbol(e.symbol_id).port(p)]
        return ('verified','') if need and sum(p in used for p in need)>=2 else ('missing','另一侧介质的进出口没有同时接出')
    if r.kind=='parallel':
        x,y=a['tags']
        if x not in tags or y not in tags:return 'missing','并联设备不在图中'
        return ('verified','') if not any({c[0],c[2]}=={x,y} for c in conns) else ('missing','两台设备被串联')
    if r.kind in ('instrument','alarm'):
        ok=any(i.letters==a['letters'] and i.loop_no==a['loop_no'] for i in drawing.instruments)
        return ('verified','') if ok else ('missing','图中没有该仪表')
    if r.kind=='fitting':
        from zhihui.cad.fittings import resolve
        want=resolve(a['type']);pump=next((e.tag for e in drawing.equipment if get_symbol(e.symbol_id).family=='pump'),None)
        if not pump:return 'missing','图中没有泵'
        for c in conns:
            side=('pump_out' if c[0]==pump else 'pump_in' if c[2]==pump else '')
            if side==a['where'] and any(f.type==want for f in c[5].fittings):
                if a.get('tag') and not any(x.text==a['tag'] for x in drawing.annotations):return 'missing','阀门已画，但位号 '+a['tag']+' 没有标注'
                return 'verified',''
        return 'missing','该位置的管线上没有这个阀门'
    if r.kind=='elevation':
        e=tags.get(a['tag']);ev=(e.params.get('elevations') if e else None) or {}
        texts={x.text for x in drawing.annotations if x.style=='elevation'}
        for k in ('top','water'):
            if k in a:
                if abs(float(ev.get(k,1e9))-a[k])>1e-6:return 'missing',f'设备参数里没有 {k}={a[k]}'
                if f'{a[k]:.2f}' not in texts:return 'missing',f'图上没有标出 {a[k]:.2f} m'
        return 'verified',''
    if r.kind=='title_block':
        got=drawing.title_block.get(a['field'])
        if a['field']=='drawing_name' and not got:got=drawing.title if drawing.title==a['value'] else got
        return ('verified','') if got==a['value'] else ('missing','标题栏没有写入「'+a['value']+'」')
    return 'missing','无法核验'


def review_spec(spec,drawing,validation,prompt=None):
    syms=load_symbols()
    prompt=prompt or '。'.join(c.text_span for c in spec.checklist)
    tags,by_id=_index(drawing)
    links={(by_id[c.from_equip],by_id[c.to_equip],c.medium) for c in drawing.connections}
    checks=[]
    for e in spec.equipment:
        actual=tags.get(e.tag);checks.append({'id':'equipment:'+e.tag,'checked':bool(actual and actual.symbol_id==e.symbol_id and actual.name==e.name)})
    for s in spec.streams:checks.append({'id':'stream:'+s.from_+'>'+s.to,'checked':(s.from_.split('.')[0],s.to.split('.')[0],s.medium) in links})
    for t in spec.terminals:
        pair=(t.id,t.connected_to.split('.')[0],t.medium) if t.direction=='in' else (t.connected_to.split('.')[0],t.id,t.medium)
        checks.append({'id':'terminal:'+t.id,'checked':t.id in tags and pair in links})
    for h in spec.hatches:
        checks.append({'id':'hatch:'+h.target+':'+h.region,'checked':any(by_id.get(a.anchor_ref)==h.target and a.region==h.region for a in drawing.hatches)})
    for a in spec.annotations:checks.append({'id':'annotation:'+a.get('text',''),'checked':any(x.text==a.get('text') for x in drawing.annotations)})
    errors={e['code'] for e in validation.get('errors',[])}
    all_inventory=all(c['checked'] for c in checks)
    placed=bool(drawing.equipment) and all(e.placed for e in drawing.equipment)
    geometry=not errors.intersection({'E_OVERLAP','E_UNROUTED','E_PIPE_DETACHED','E_PIPE_THROUGH_EQUIP','E_STD_TEXT_LINE','E_STD_TEXT_OVERLAP'})
    # --- typed requirements from the user's own words, verified against the drawing
    reqs=extract_requirements(prompt,syms);results={}
    for r in reqs:results[r.id]=verify(r,drawing,syms)
    clause_index={c:i for i,c in enumerate(split_clauses(prompt))}
    for item in spec.checklist:
        text=item.text_span;item.generic=False
        idx=clause_index.get(text)
        typed=[r for r in reqs if r.clause_idx==idx and r.kind!='equipment'] if idx is not None else []
        referenced=[t for t in re.findall(r'[A-Z]{1,3}-\d{2,4}',text) if not t.startswith('PFD-')]
        if typed:ok=all(results[r.id][0]=='verified' for r in typed)
        elif referenced and STRUCTURAL.search(text):ok=False   # a topology claim we could not turn into a checkable requirement
        elif referenced:ok=all(t in tags for t in referenced) and all_inventory
        elif re.search('75%|85%|75％|85％',text):ok=False
        elif re.search('下方.*标注|下方.*名称',text):
            ok=all(any(a.anchor_ref==e.id and a.y is not None and a.y<e.y and e.tag in a.text for a in drawing.annotations) for e in drawing.equipment if e.symbol_id!='pipe_terminal')
        elif re.search('重叠|遮挡|断线|穿越|正交|压盖|交叉',text):ok=placed and geometry
        elif re.search('不得.*编造|不允许.*编造|不标.*流量|不要补充|不填写|没有提供.*不写|一律不写|不编造',text):ok=all(not e.params or e.source=='user' for e in spec.equipment)
        elif re.search('填充|堆积区|燃烧区',text):ok=bool(drawing.hatches)
        elif re.search('图框|标题栏|图名|图号',text):ok=drawing.frame and bool(drawing.title)
        elif re.search('位号|编号|中文名称|中文名',text):ok=placed and all(e.tag and e.name for e in spec.equipment)
        else:
            item.generic=True;ok=False   # descriptive wording: reported separately, never counted as covered
        item.checked=bool(ok)
    counted=[c for c in spec.checklist if not c.generic]
    count=sum(c.checked for c in counted)
    coverage=count/len(counted) if counted else (sum(c['checked'] for c in checks)/len(checks) if checks else 0)
    issues=[{'id':c['id'],'source':'spec','status':'open','attempts':0,'target_id':c['id'].split(':')[-1],'code':'E_SPEC_MISSING','message':'未覆盖 '+c['id']} for c in checks if not c['checked']]
    for r in reqs:
        status,why=results[r.id]
        if status=='verified' or r.kind=='equipment':continue
        code='W_REQ_UNSUPPORTED' if status=='unsupported' else 'E_REQ_MISSING'
        issues.append({'id':f'{code}:{r.id}','source':'requirement','status':'unresolved' if status=='unsupported' else 'open','attempts':0,'target_id':r.args.get('tag') or r.args.get('from') or r.args.get('to') or r.id,
                       'code':code,'kind':r.kind,'message':('当前不支持：' if status=='unsupported' else '未满足：')+describe(r)+('（'+why+'）' if why else ''),'clause':r.clause})
    issues+=[{'id':c.id,'source':'checklist','status':'unresolved','attempts':0,'target_id':'','code':'W_REQUIREMENT_UNVERIFIED','message':c.text_span} for c in counted if not c.checked and not any(i['clause']==c.text_span for i in issues if i.get('clause'))]
    typed_total=[r for r in reqs if r.kind!='equipment']
    metrics={'requirements':{'verified':sum(results[r.id][0]=='verified' for r in typed_total),'total':len(typed_total),
                             'unsupported':sum(results[r.id][0]=='unsupported' for r in typed_total)},
             'drawing_rules':{'passed':bool(validation.get('passed')),'errors':len(validation.get('errors',[]))},
             'unchecked_clauses':[c.text_span for c in spec.checklist if c.generic]}
    return {'issues':issues,'coverage':coverage,'inventory':checks,'requirements':[{'id':r.id,'kind':r.kind,'status':results[r.id][0],'text':describe(r)} for r in reqs],'metrics':metrics}
