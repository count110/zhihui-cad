import time
from fastapi.testclient import TestClient


def wait(c,tid):
    for _ in range(3000):
        t=c.get('/api/tasks/'+tid).json()
        if t['status'] in {'READY','READY_WITH_WARNINGS','FAILED','WAITING_CONFIRMATION'}:return t
        time.sleep(.02)
    raise TimeoutError(tid)


def test_plan_edit_confirm_persist_report_and_gaps(monkeypatch):
    import zhihui.api.app as api
    monkeypatch.setenv('ZHIHUI_LLM','mock');monkeypatch.setenv('ZHIHUI_AGENT_MODE','multi')
    with TestClient(api.app) as c:
        tid=c.post('/api/tasks',data={'prompt':'画一个小型餐饮污水处理流程'}).json()['id']
        task=wait(c,tid);assert task['status']=='WAITING_CONFIRMATION'
        spec=task['result']['spec'];assert len(spec['equipment'])>=4
        spec['equipment'][0]['name']='用户确认隔油设备'
        spec['equipment'][0]['source']='user'
        assert c.post('/api/tasks/'+tid+'/confirm',json={'equipment':[{'tag':'bad','x':40}]}).status_code==422
        assert c.post('/api/tasks/'+tid+'/confirm',json=spec).status_code==200
        task=wait(c,tid);assert task['status'] in {'READY','READY_WITH_WARNINGS'},task
        saved=api.db.get_task(tid)['state']
        assert saved['confirmed'] and saved['user_facts']['confirmed_spec']['equipment'][0]['name']=='用户确认隔油设备'
        assert task['result']['spec']['equipment'][0]['name']=='用户确认隔油设备'
        assert c.get('/api/tasks/'+tid+'/report').json()['markdown'].startswith('# 智绘CAD')
        assert c.get('/api/knowledge-gaps').json()['items'][0]['task_id']==tid
        assert c.post('/api/knowledge-gaps',json={}).status_code==405
        assert any(e.get('agent')=='builder' for e in task['events'])
        assert all(c.get(a['url']).status_code==200 for a in task['result']['artifacts'])
        assert c.get('/api/health').json()['agent_mode']=='multi'
