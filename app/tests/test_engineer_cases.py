"""Regression for the chemical-engineer field test (E01-E16): what is actually IN the drawing, not whether the pipeline ran."""
import asyncio
import json
from pathlib import Path

import pytest

from zhihui.agent.coord import Coordinator
from zhihui.agent.coord.arg_norm import normalize_args, parse_move, sanitize_question
from zhihui.agent.coord.requirements import extract_requirements, tag_names
from zhihui.agent.coord.review import review_spec
from zhihui.agent.coord.roles import Action, EditorOutput
from zhihui.agent.coord.spec import plan_issues
from zhihui.tools.store import DrawingStore

CASES = json.loads((Path(__file__).parent / 'fixtures' / 'engineer_cases.json').read_text(encoding='utf-8'))


def build(prompt, **kw):
    async def go():
        c = Coordinator(mock=True)
        out = await c.run(prompt, **kw)
        return c, out
    c, out = asyncio.run(go())
    drawing = DrawingStore().load(out.drawing_id) if out.drawing_id else None
    return c, out, drawing


def links(d):
    by = {e.id: e.tag for e in d.equipment}
    return {(by[c.from_equip], c.from_port, by[c.to_equip], c.to_port, c.medium) for c in d.connections}


def pairs(d):
    return {(a, c) for a, _, c, _, _ in links(d)}


# ---------------------------------------------------------------- extraction
def test_equipment_names_are_clean():
    names = tag_names(CASES['E01'])
    assert names['P-102'] == '回流泵' and names['E-101'] == '冷凝器' and names['E-102'] == '再沸器'
    assert not any(w in n for n in names.values() for w in ('分成', '底部', '进入', '返回'))
    assert tag_names(CASES['E15'])['P-101B'] == '备用泵'


def test_instrument_tags_are_not_equipment():
    assert 'FT-101' not in tag_names(CASES['E04']) and 'LAL-101' not in tag_names(CASES['E04'])


def test_requirement_extraction_e01_has_loops_and_boundaries():
    reqs = extract_requirements(CASES['E01'])
    loops = {(r.args['from'], r.args['to']) for r in reqs if r.kind == 'loop'}
    assert loops == {('P-102', 'T-101'), ('E-102', 'T-101')}
    assert {r.args['name'] for r in reqs if r.kind == 'terminal'} == {'塔顶产品', '塔底产品'}


# ---------------------------------------------------------------- process semantics in the drawing
def test_e01_distillation_has_reflux_reboil_and_both_products():
    c, out, d = build(CASES['E01'])
    got = links(d)
    assert ('P-102', 'out', 'T-101', 'reflux_in', '回流') in got
    assert ('T-101', 'bottom_out', 'E-102', 'in', '') in got and ('E-102', 'out', 'T-101', 'reboil_in', '') in got
    assert {e.name for e in d.equipment if e.symbol_id == 'pipe_terminal'} == {'塔顶产品', '塔底产品'}
    assert ('E-102', 'P-102') not in pairs(d)                     # the old order-of-appearance chain
    assert out.status == 'READY' and c.state.metrics['requirements']['verified'] == c.state.metrics['requirements']['total']


def test_e02_exchanger_sides_and_media():
    c, out, d = build(CASES['E02'])
    got = links(d)
    assert any(x[2] == 'E-101' and x[3] == 'shell_in' and x[4] == '冷却水供水' for x in got)
    assert any(x[0] == 'E-101' and x[1] == 'shell_out' and x[4] == '冷却水回水' for x in got)
    assert {x[4] for x in got if x[0] in ('V-101', 'P-101', 'E-101') and x[2] in ('P-101', 'E-101', 'V-102')} >= {'热料', '冷却后物料'}
    assert ('X-101', 'V-102') not in pairs(d)                      # utility water never joins the product line


def test_e05_absorption_has_loop_and_four_boundaries():
    c, out, d = build(CASES['E05'])
    assert ('T-101', 'V-101') in pairs(d) and ('P-101', 'T-101') in pairs(d)
    assert len([e for e in d.equipment if e.symbol_id == 'pipe_terminal']) == 4
    assert {'补水', '排污', '净化气体'} <= {e.name for e in d.equipment}


def test_e15_parallel_pumps_kept():
    c, out, d = build(CASES['E15'])
    p = pairs(d)
    assert ('V-101', 'P-101A') in p and ('V-101', 'P-101B') in p and ('P-101A', 'V-102') in p and ('P-101B', 'V-102') in p
    assert ('P-101A', 'P-101B') not in p and {e.tag for e in d.equipment} >= {'P-101A', 'P-101B'}


def test_e04_pid_objects_and_honest_unsupported_list():
    c, out, d = build(CASES['E04'])
    assert {(i.letters, i.loop_no) for i in d.instruments} >= {('FT', '101'), ('LAL', '101')}
    types = [f.type for cn in d.connections for f in cn.fittings]
    assert types.count('gate_valve') == 2 and 'swing_check' in types and 'globe_valve' in types
    assert any(a.text == 'FCV-101' for a in d.annotations)
    assert out.status == 'READY_WITH_WARNINGS'
    assert any('信号线' in i['message'] and i['code'] == 'W_REQ_UNSUPPORTED' for i in c.state.issues)
    assert '当前不支持' in out.report and '信号线' in out.report


def test_e07_title_block_and_unknown_facts_left_empty():
    c, out, d = build(CASES['E07'])
    assert d.title == '转料换热流程图' and d.title_block['drawing_no'] == 'TEST-201'
    assert 'unit_name' not in d.title_block and 'project' not in d.title_block


def test_e09_elevations_reach_the_drawing():
    c, out, d = build(CASES['E09'])
    assert [e.params['elevations'] for e in d.equipment] == [{'top': 12.0, 'water': 11.0}, {'top': 11.5, 'water': 10.5}, {'top': 11.0, 'water': 10.0}, {'top': 10.5, 'water': 9.5}]
    texts = {a.text for a in d.annotations if a.style == 'elevation'}
    assert {'12.00', '11.50', '11.00', '10.50', '10.00', '9.50'} <= texts


def test_e14_substitution_confirm_then_draw():
    async def go():
        from zhihui.api.db import DB
        from zhihui.tools.store import data_dir
        db = DB(data_dir() / 'zhihui.db')
        a = Coordinator(mock=True, task_id='t_e14', db=db)
        first = await a.run(CASES['E14'])
        b = Coordinator(mock=True, task_id='t_e14', db=db)
        second = await b.run(CASES['E14'], answer='确认使用通用设备示意')
        return first, second, b
    first, second, b = asyncio.run(go())
    assert first.status == 'WAITING_CONFIRMATION' and second.status in ('READY', 'READY_WITH_WARNINGS')
    assert second.usage['total']['tool_calls'] >= second.usage['tool_calls']      # cumulative, not just this round


def test_model_commentary_in_params_does_not_block():
    """E14 on the real model: a substitute_note mentioning ClO2 used to trip the 'unsourced number' guard and dead-end the task."""
    async def go():
        c = Coordinator(mock=True)
        original = c.role_call

        async def planner(role, schema, context, fallback):
            if role == 'planner':
                spec = fallback()
                spec['equipment'][0]['params'] = {'symbol_substitute': True, 'substitute_note': '以 ClO2 发生器图元替代臭氧发生器'}
                spec['equipment'][0]['source'] = 'user'
                return schema.model_validate(spec)
            return await original(role, schema, context, fallback)
        c.role_call = planner
        out = await c.run(CASES['E14'])
        return c, out
    c, out = asyncio.run(go())
    assert out.status == 'WAITING_CONFIRMATION' and '图元' in out.question['question']
    assert c.state.spec.equipment[0].params == {}      # commentary never becomes a drawing parameter


# ---------------------------------------------------------------- review is independent of the planner
def test_review_ignores_planner_and_reports_missing_loop():
    c, out, d = build(CASES['E01'])
    d.connections = [x for x in d.connections if not (x.from_port == 'out' and x.to_port == 'reflux_in')]
    spec = c.state.spec.model_copy(deep=True)
    spec.streams = [s for s in spec.streams if 'reflux_in' not in s.to]      # planner "forgot" it as well
    spec.checklist = c.state.spec.checklist
    review = review_spec(spec, d, {'errors': [], 'passed': True}, prompt=CASES['E01'])
    assert any(i['code'] == 'E_REQ_MISSING' and '回流' in i['message'] for i in review['issues'])
    assert review['coverage'] < 1 and review['metrics']['requirements']['verified'] < review['metrics']['requirements']['total']


# ---------------------------------------------------------------- editing
def _base_dxf():
    c, out, d = build(CASES['E07'])
    return next(a['file_ref'] for a in out.artifacts if a['format'] == 'dxf')


def test_e10_explicit_move_is_done_in_one_pass_even_with_wrong_arg_names():
    dxf = _base_dxf()

    async def go():
        c = Coordinator(mock=True)
        original = c.role_call

        async def wrong(role, schema, context, fallback):
            if role == 'editor' and 'fix' not in context:
                return EditorOutput(actions=[Action(name='move_equipment', arguments={'ref': 'P-201', 'dx': 800, 'dy': 0})])
            return await original(role, schema, context, fallback)
        c.role_call = wrong
        return await c.run('把 P-201 向右移动 800 mm，其余设备位置、编号和名称不变。', file_ref=dxf)
    out = asyncio.run(go())
    assert out.status in ('READY', 'READY_WITH_WARNINGS') and out.diff['moved'] == [{'id': 'E2', 'tag': 'P-201', 'dx': 800.0, 'dy': 0.0}]


def test_e12_ambiguous_asks_without_guessing_numbers():
    c, out, d = build(CASES['E12'], file_ref=_base_dxf())
    assert out.status == 'WAITING_CONFIRMATION'
    msg, opts, _ = sanitize_question('选择：', ['将 P-201 向 +X 方向平移 2000', '将 P-201 向 -X 方向平移 2000', '向上平移 1000'], CASES['E12'])
    assert opts and not any(ch.isdigit() for o in opts for ch in o.replace('P-201', ''))


def test_e13_whole_drawing_rotation_options_are_whitelisted():
    c, out, d = build(CASES['E13'], file_ref=_base_dxf())
    assert out.status == 'WAITING_CONFIRMATION' and out.question['options'] == ['导出 DXF 后在 CAD 中旋转', '保留当前图纸']


def test_e11_title_block_edit_reports_before_after_and_touches_nothing_else():
    c, out, d = build(CASES['E11'], file_ref=_base_dxf())
    changes = {x['field']: x for x in out.diff['title_block']}
    assert changes['unit_name']['after'] == '化工工程体验组' and changes['drawing_no'] == {'field': 'drawing_no', 'before': 'TEST-201', 'after': 'TEST-202'}
    assert not out.diff['moved'] and not out.diff['added'] and not out.diff['removed']


def test_arg_normalisation_and_question_hygiene():
    assert parse_move(CASES['E10'])[:2] == ('P-201', 800.0) and parse_move(CASES['E16'])[:2] == ('P-301', 300.0)
    assert parse_move('把那个泵挪一下。') is None
    args, missing = normalize_args('move_equipment', {'ref': 'P-201', 'distance': '800', 'direction': '右'}, {'equipment': [{'ref': 'P-201', 'name': '输送泵'}]}, '', {'required': ['drawing_id', 'equipment_ref']})
    assert args['equipment_ref'] == 'P-201' and args['dx'] == 800.0 and not missing
    msg, _, debug = sanitize_question('Error executing tool move_equipment: 1 validation error for move_equipmentArguments', ['a', 'b'], '')
    assert 'validation error' not in msg and 'Error executing' in debug


def test_loop_number_of_instrument_tag_counts_as_user_given():
    from zhihui.agent.extended_guards import ExtendedGuard
    g = ExtendedGuard()
    msgs = [{'role': 'user', 'content': '在泵出口增加流量变送器 FT-101'}]
    assert g.check('add_instrument', {'drawing_id': 'x', 'letters': 'FT', 'loop_no': '101', 'attach_to': 'C5'}, msgs) is None
    assert g.check('add_instrument', {'drawing_id': 'x', 'letters': 'FT', 'loop_no': '202', 'attach_to': 'C5'}, msgs)['error_code'] == 'E_UNSOURCED_VALUE'


# ---------------------------------------------------------------- plan confirmation and API
def test_plan_issues_are_row_level_chinese():
    spec = {'equipment': [{'tag': 'V-101', 'name': '罐', 'symbol_id': 'tank'}, {'tag': 'P-101', 'name': '泵', 'symbol_id': 'pump'}],
            'streams': [{'from': 'V-101.out', 'to': 'R-101.in'}, {'from': 'V-101.foo', 'to': 'P-101'}]}
    msgs = [i['message'] for i in plan_issues(spec)]
    assert any('第 1 条连接' in m and 'R-101' in m and '不存在' in m for m in msgs)
    assert any('V-101.foo' in m and '可用端口' in m for m in msgs)
    assert not any('Error' in m or 'pydantic' in m for m in msgs)


def test_api_surface(monkeypatch):
    import time
    from fastapi.testclient import TestClient
    import zhihui.api.app as api
    monkeypatch.setenv('ZHIHUI_LLM', 'mock'); monkeypatch.setenv('ZHIHUI_AGENT_MODE', 'multi')
    with TestClient(api.app) as c:
        r = c.post('/api/preflight', json={'prompt': CASES['E04']}).json()
        assert r['unsupported'] and any('信号线' in x for x in r['unsupported']) and r['notes']
        syms = c.get('/api/symbols').json()['items']
        tower = next(s for s in syms if s['id'] == 'tower')
        assert any(p['name'] == 'reboil_in' and p['semantic'] == '再沸返回入口' for p in tower['ports'])
        tid = c.post('/api/tasks', data={'prompt': '画一个小型餐饮污水处理流程'}).json()['id']
        for _ in range(3000):
            t = c.get('/api/tasks/' + tid).json()
            if t['status'] == 'WAITING_CONFIRMATION':
                break
            time.sleep(.02)
        assert [x for x in c.get('/api/tasks?limit=5').json()['items'] if x['id'] == tid]
        spec = t['result']['spec']
        spec['streams'].append({'from': spec['equipment'][0]['tag'] + '.out', 'to': 'R-999.in', 'medium': '', 'source': 'user'})
        bad = c.post(f'/api/tasks/{tid}/confirm', json=spec)
        assert bad.status_code == 422 and 'R-999' in bad.json()['detail'] and bad.json()['issues']
        assert 'pydantic' not in bad.text and 'Field required' not in bad.text
        assert c.post(f'/api/tasks/{tid}/confirm', data='{oops').status_code in (400, 422)
        spec['streams'].pop()
        assert c.post(f'/api/tasks/{tid}/confirm', json=spec).status_code == 200
        for _ in range(3000):
            t = c.get('/api/tasks/' + tid).json()
            if t['status'] in ('READY', 'READY_WITH_WARNINGS'):
                break
            time.sleep(.02)
        assert t['status'] in ('READY', 'READY_WITH_WARNINGS')
        z = c.get(t['result']['bundle_url'])
        assert z.status_code == 200 and z.headers['content-type'] == 'application/zip'
        import io, zipfile
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        assert any(n.endswith('.dxf') for n in names) and '交付报告.md' in names
        assert t['result']['report'].count('# 智绘CAD 交付报告') == 1 and '表述补充' not in t['result']['report']
        assert t['result']['usage']['total']['tool_calls'] >= t['result']['usage']['tool_calls']
