"""网页设置：API Key 只写不读、写回 .env、立即生效、只允许本机。"""
import os

import pytest
from fastapi.testclient import TestClient

import zhihui.api.app as api
from zhihui.api import settings as st


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    p = tmp_path / '.env'
    p.write_text('# 注释保留\nDEEPSEEK_API_KEY=\nDEEPSEEK_MODEL=deepseek-flash\nZHIHUI_WORKERS=2\n', encoding='utf-8')
    monkeypatch.setattr(st, 'ENV_FILE', p)
    for k in ('DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'DEEPSEEK_MODEL', 'ZHIHUI_LLM_PRIMARY', 'ZHIHUI_ADMIN_TOKEN'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('ZHIHUI_LLM', 'real')
    before = dict(os.environ)
    yield p
    for k in set(os.environ) - set(before):        # settings write os.environ directly; leave no trace for other tests
        os.environ.pop(k)
    os.environ.update({k: v for k, v in before.items() if os.environ.get(k) != v})


def test_save_key_never_echoes_it_and_takes_effect(env_file):
    with TestClient(api.app) as c:
        assert c.get('/api/health').json()['key_missing'] is True
        r = c.put('/api/settings', json={'values': {'DEEPSEEK_API_KEY': 'sk-test-1234567890abcd', 'DEEPSEEK_MODEL': 'deepseek-pro', 'ZHIHUI_LLM_PRIMARY': 'deepseek'}})
        assert r.status_code == 200 and 'sk-test-1234567890abcd' not in r.text
        ds = next(p for p in r.json()['settings']['providers'] if p['id'] == 'deepseek')
        assert ds['key_set'] and ds['key_hint'] == '…abcd' and ds['model'] == 'deepseek-pro'
        assert os.environ['DEEPSEEK_API_KEY'] == 'sk-test-1234567890abcd'          # no restart needed
        assert c.get('/api/health').json()['key_missing'] is False
        assert 'sk-test' not in c.get('/api/settings').text
    text = env_file.read_text(encoding='utf-8')
    assert '# 注释保留' in text and 'ZHIHUI_WORKERS=2' in text and 'DEEPSEEK_API_KEY=sk-test-1234567890abcd' in text and 'DEEPSEEK_MODEL=deepseek-pro' in text


def test_blank_key_keeps_old_one_and_clear_removes_it(env_file):
    with TestClient(api.app) as c:
        c.put('/api/settings', json={'values': {'DASHSCOPE_API_KEY': 'sk-qwen-abcdefgh'}})
        c.put('/api/settings', json={'values': {'DASHSCOPE_API_KEY': '', 'QWEN_MODEL': 'qwen-max'}})
        assert os.environ['DASHSCOPE_API_KEY'] == 'sk-qwen-abcdefgh'
        c.put('/api/settings', json={'clear': ['DASHSCOPE_API_KEY']})
        assert 'DASHSCOPE_API_KEY' not in os.environ


def test_rejects_unknown_or_bad_values(env_file):
    with TestClient(api.app) as c:
        assert c.put('/api/settings', json={'values': {'PATH': 'x'}}).status_code == 400
        assert c.put('/api/settings', json={'values': {'DEEPSEEK_API_KEY': 'has space in key'}}).status_code == 400
        assert c.put('/api/settings', json={'values': {'DEEPSEEK_BASE_URL': 'file:///etc/passwd'}}).status_code == 400
        assert c.put('/api/settings', json={'values': {'DEEPSEEK_MODEL': 'x\nZHIHUI_LLM=mock'}}).status_code == 400   # no .env line injection
        assert c.put('/api/settings', json={'values': {'ZHIHUI_LLM_PRIMARY': 'evil'}}).status_code == 400
        assert c.put('/api/settings', json={'clear': ['ZHIHUI_LLM']}).status_code == 400


def test_only_local_or_admin_token(env_file, monkeypatch):
    assert st.allowed_client('127.0.0.1', None) and not st.allowed_client('192.168.1.20', None)
    monkeypatch.setenv('ZHIHUI_ADMIN_TOKEN', 'secret-token')
    assert not st.allowed_client('127.0.0.1', None) and st.allowed_client('192.168.1.20', 'secret-token')
    with TestClient(api.app) as c:
        assert c.get('/api/settings').status_code == 403
        assert c.get('/api/settings', headers={'X-Zhihui-Admin': 'secret-token'}).status_code == 200


def test_connection_test_without_key_is_friendly(env_file):
    with TestClient(api.app) as c:
        r = c.post('/api/settings/test', json={'provider': 'qwen'}).json()
        assert r['ok'] is False and 'Key' in r['message']
        assert c.post('/api/settings/test', json={'provider': 'nope'}).status_code == 400
