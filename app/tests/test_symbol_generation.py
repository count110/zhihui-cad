"""Search + auto-generated equipment symbols (图元)."""
import asyncio
import json
import urllib.error

import pytest

from zhihui.agent.coord import Coordinator
from zhihui.cad import symgen
from zhihui.cad.symbols import load_symbols
from zhihui.cad.symgen import PortDraft, SymbolDraft
from zhihui.kb import websearch
from zhihui.tools.store import DrawingStore

PROMPT = '绘制臭氧消毒工艺方案：原料水罐 V-101 → 输送泵 P-101 → 臭氧发生器 M-101 → 接触塔 T-101。不填写任何未提供的设计数值。'


def run_two_steps(prompt, patch=None):
    async def go():
        from zhihui.api.db import DB
        from zhihui.tools.store import data_dir
        db = DB(data_dir() / 'zhihui.db')
        a = Coordinator(mock=True, task_id='t_gen', db=db)
        if patch:
            patch(a)
        first = await a.run(prompt)
        b = Coordinator(mock=True, task_id='t_gen', db=db)
        second = await b.run(prompt, answer='确认当前方案')
        return a, first, b, second
    return asyncio.run(go())


# ---------------------------------------------------------------- templates are always valid
@pytest.mark.parametrize('template', symgen.TEMPLATES)
def test_every_template_builds_a_valid_loadable_symbol(template):
    d = SymbolDraft(name_cn='测试设备', template=template, width=20, height=28, decorations=list(symgen.DECORATIONS)[:3],
                    ports=[PortDraft(name='Feed In', kind='in', side='top', pos=.3), PortDraft(name='out', kind='out', side='bottom', pos=.5),
                           PortDraft(name='steam_in', kind='in', side='left', pos=.8)])
    raw = symgen.build_symbol(d, symgen.symbol_id_for('测试设备'), [], False)
    assert symgen.validate_raw(raw) == []
    x0, y0, x1, y1 = raw['bbox']
    assert all(x0 <= p['x'] <= x1 and y0 <= p['y'] <= y1 for p in raw['ports'])
    names = [p['name'] for p in raw['ports']]
    assert len(set(names)) == len(names) and 'feed_in' in names           # sanitised, unique
    symgen.save_symbol(raw)
    sym = load_symbols()[raw['id']]
    assert sym.status == 'generated' and sym.port('out') is not None


def test_garbage_draft_is_clamped_not_trusted():
    d = SymbolDraft(name_cn='怪设备', template='tower', width=9999, height=-5, decorations=['stirrer'] * 3, category='z')
    raw = symgen.build_symbol(d, 'gen_deadbeef', [], False)
    x0, y0, x1, y1 = raw['bbox']
    assert x1 - x0 <= 40 and y1 - y0 <= 60 and raw['category'] == 'X'
    assert {p['kind'] for p in raw['ports']} == {'in', 'out'}                # default ports added
    assert load_symbols().get('pump') is not None                              # builtin untouched


def test_generated_symbol_cannot_override_builtin(data_dir):
    d = SymbolDraft(name_cn='假泵', template='circle_machine')
    raw = symgen.build_symbol(d, 'gen_aaaaaaaa', [], False)
    raw['id'] = 'pump'
    (data_dir / 'symbols_generated').mkdir(exist_ok=True)
    (data_dir / 'symbols_generated' / 'gen_aaaaaaaa.json').write_text(json.dumps(raw), encoding='utf-8')
    assert load_symbols()['pump'].name_cn == '离心泵'


def test_corrupt_generated_file_does_not_break_library(data_dir):
    (data_dir / 'symbols_generated').mkdir(exist_ok=True)
    (data_dir / 'symbols_generated' / 'gen_bad00000.json').write_text('{not json', encoding='utf-8')
    assert len(load_symbols()) >= 61


# ---------------------------------------------------------------- end to end (offline: no search, template by name)
def test_missing_symbol_is_generated_confirmed_drawn_and_reused():
    a, first, b, second = run_two_steps(PROMPT)
    assert first.status == 'WAITING_CONFIRMATION' and '自动生成的图元' in first.question['question']
    g = first.generated_symbols[0]
    assert g['name'] == '臭氧发生器' and g['id'].startswith('gen_') and not g['sources'] and not g['searched']
    assert first.spec['equipment'][2]['symbol_id'] == g['id']
    assert second.status in ('READY', 'READY_WITH_WARNINGS')
    d = DrawingStore().load(second.drawing_id)
    assert next(e for e in d.equipment if e.tag == 'M-101').symbol_id == g['id']
    assert ('M-101' in {cn_tag for cn_tag in (e.tag for e in d.equipment)})
    assert len(d.connections) == 3 and second.validation['passed'] is True
    assert '自动生成的图元' in second.report and g['id'] in second.report
    raw = json.loads((symgen.generated_dir() / (g['id'] + '.json')).read_text(encoding='utf-8'))
    assert raw['generated']['user_confirmed'] is False                         # only the API confirm flips it
    # second task with the same equipment name: reused, not regenerated, still needs a person to look
    async def again():
        c = Coordinator(mock=True)
        return c, await c.run(PROMPT)
    c, out = asyncio.run(again())
    assert out.status == 'WAITING_CONFIRMATION' and out.generated_symbols[0]['id'] == g['id'] and out.generated_symbols[0]['reused'] is True
    assert any('自动生成的示意图元' in s for s in out.spec['assumptions'])


def test_generation_failure_falls_back_to_old_behaviour(monkeypatch):
    monkeypatch.setattr(symgen, 'save_symbol', lambda raw: (_ for _ in ()).throw(ValueError('boom')))
    async def go():
        c = Coordinator(mock=True)
        return await c.run('我要画一台臭氧发生器，设备位号 M-101，名称为臭氧发生器。')
    out = asyncio.run(go())
    assert out.status == 'WAITING_CONFIRMATION' and not out.generated_symbols    # substituted + asks, as before


# ---------------------------------------------------------------- search + designer role
def fake_hits(*urls):
    return [websearch.Hit(title='t' + str(i), url=u, summary='臭氧发生器通常由放电室、冷却系统和电源组成，进气口与出气口各一。', score=2.0, authority=3) for i, u in enumerate(urls)]


def test_searched_evidence_reaches_designer_and_unseen_urls_are_dropped(monkeypatch):
    monkeypatch.setattr(websearch, 'enabled', lambda: True)
    seen = {}

    def fake_search(q, count=8, timeout=12.0):
        seen.setdefault('queries', []).append(q)
        return fake_hits('https://example.org/ozone', 'https://example.org/other')
    monkeypatch.setattr(websearch, 'search', fake_search)

    def patch(coord):
        original = coord.role_call

        async def role_call(role, schema, context, fallback):
            if role == 'symbol_designer':
                seen['context'] = context
                return SymbolDraft(name_cn='别的名字', aliases=['O3发生器'], category='M', template='rect_machine', width=22, height=30, decorations=['zigzag'],
                                   ports=[PortDraft(name='gas_in', kind='in', side='left', pos=.3, semantic='原料气入口'), PortDraft(name='o3_out', kind='out', side='right', pos=.5, semantic='臭氧出口')],
                                   evidence_urls=['https://example.org/ozone', 'https://made-up.example/none'], confidence=.9, rationale='放电室矩形设备')
            return await original(role, schema, context, fallback)
        coord.role_call = role_call
    a, first, b, second = run_two_steps(PROMPT, patch)
    g = first.generated_symbols[0]
    assert g['sources'] == ['https://example.org/ozone'] and g['searched'] and g['name'] == '臭氧发生器'      # name stays the user's; fabricated url dropped
    assert {p['name'] for p in g['ports']} == {'gas_in', 'o3_out'}
    assert all('V-101' not in q and 'M-101' not in q and '不填写' not in q for q in seen['queries']) and seen['queries'][0].startswith('臭氧发生器')
    assert seen['context']['evidence'][0]['url'] == 'https://example.org/ozone'
    assert a.search_calls == 2 and first.usage['search_calls'] == 2
    raw = json.loads((symgen.generated_dir() / (g['id'] + '.json')).read_text(encoding='utf-8'))
    assert 'example.org/ozone' in raw['source'] and raw['generated']['confidence'] == .9
    assert second.status in ('READY', 'READY_WITH_WARNINGS')


def test_search_failure_degrades_to_template_not_to_failure(monkeypatch):
    monkeypatch.setattr(websearch, 'enabled', lambda: True)

    def boom(q, count=8, timeout=12.0):
        raise urllib.error.HTTPError('u', 402, 'Payment Required', {}, None)
    monkeypatch.setattr(websearch, 'search', boom)
    a, first, b, second = run_two_steps(PROMPT)
    g = first.generated_symbols[0]
    assert not g['searched'] and g['confidence'] <= .4 and second.status in ('READY', 'READY_WITH_WARNINGS')
    assert any('检索' in w and '402' in w for w in a.warnings)


def test_websearch_client_contract(monkeypatch):
    calls = []

    def fake_post(path, body, timeout):
        calls.append((path, body))
        return {'results': [{'title': 'a', 'url': 'https://a.example', 'summary': 's', 'rerank_score': 2.1, 'authority': 3},
                            {'title': 'bad', 'url': 'javascript:alert(1)', 'summary': 'x', 'rerank_score': 3},
                            {'title': 'low', 'url': 'https://b.example', 'summary': 'y', 'rerank_score': .2, 'authority': 0}]}
    monkeypatch.setattr(websearch, '_post', fake_post)
    ev = websearch.gather_equipment_evidence('臭氧发生器')
    assert [c[0] for c in calls] == ['/v1/tools/search'] * 2 and all(c[1]['need_content'] is False and len(c[1]['query']) <= 128 for c in calls)
    assert [h.url for h in ev.hits] == ['https://a.example'] and ev.calls == 2 and not ev.error


def test_enabled_rules(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'k')
    monkeypatch.setenv('ZHIHUI_LLM', 'mock')
    monkeypatch.delenv('ZHIHUI_SYMBOL_SEARCH', raising=False)
    assert websearch.enabled() is False                       # mock never goes online
    monkeypatch.setenv('ZHIHUI_LLM', 'real')
    assert websearch.enabled() is True
    monkeypatch.setenv('ZHIHUI_SYMBOL_SEARCH', 'off')
    assert websearch.enabled() is False
    monkeypatch.setenv('ZHIHUI_SYMBOL_SEARCH', 'auto'); monkeypatch.delenv('DEEPSEEK_API_KEY')
    assert websearch.enabled() is False


# ---------------------------------------------------------------- API surface
def test_api_exposes_previews_and_confirm_marks_symbol(monkeypatch):
    import time
    from fastapi.testclient import TestClient
    import zhihui.api.app as api
    monkeypatch.setenv('ZHIHUI_LLM', 'mock'); monkeypatch.setenv('ZHIHUI_AGENT_MODE', 'multi')
    with TestClient(api.app) as c:
        tid = c.post('/api/tasks', data={'prompt': PROMPT}).json()['id']
        for _ in range(3000):
            t = c.get('/api/tasks/' + tid).json()
            if t['status'] == 'WAITING_CONFIRMATION':
                break
            time.sleep(.02)
        g = t['result']['generated_symbols'][0]
        png = c.get(g['preview_url'])
        assert png.status_code == 200 and png.headers['content-type'] == 'image/png'
        assert any(s['id'] == g['id'] for s in c.get('/api/symbols').json()['items'])
        assert c.post(f'/api/tasks/{tid}/confirm', json=t['result']['spec']).status_code == 200
        raw = json.loads((symgen.generated_dir() / (g['id'] + '.json')).read_text(encoding='utf-8'))
        assert raw['generated']['user_confirmed'] is True
        for _ in range(3000):
            t = c.get('/api/tasks/' + tid).json()
            if t['status'] in ('READY', 'READY_WITH_WARNINGS'):
                break
            time.sleep(.02)
        assert t['status'] in ('READY', 'READY_WITH_WARNINGS')
