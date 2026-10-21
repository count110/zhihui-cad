import asyncio
import json
import pytest
from zhihui.agent.coord.spec import ProcessSpec,EquipmentSpec,compile_spec
from zhihui.agent.coord.roles import SCHEMAS,RoleRunner,RouterOutput
from zhihui.agent.coord.benchmark import evaluate
from zhihui.agent.replay import CASE_DIR


@pytest.mark.parametrize('sid',[f'S{i:03d}' for i in range(1,11)])
def test_multi_robustness_cases(sid,data_dir):
 case=json.loads((CASE_DIR/(sid+'.json')).read_text(encoding='utf-8'))
 result=asyncio.run(evaluate(case,'multi',data_dir/sid,mock=True))
 assert result['passed'],{k:v for k,v in result.items() if k!='events'}


def test_schema_forbids_self_loops_coordinates_and_unsourced_params():
 from pydantic import ValidationError
 with pytest.raises(ValidationError):EquipmentSpec(tag='V-101',name='罐',symbol_id='tank',params={'volume':50},source='kb')
 with pytest.raises(ValidationError):EquipmentSpec(tag='V-101',name='罐',symbol_id='tank',x=50)
 with pytest.raises(ValidationError):ProcessSpec(equipment=[EquipmentSpec(tag='V-101',name='罐',symbol_id='tank')],streams=[{'from':'V-101.out','to':'V-101.in'}])
 for schema in SCHEMAS.values():assert schema.model_json_schema()['type']=='object'


def test_confirmation_threshold_and_compiler():
 spec=ProcessSpec(equipment=[EquipmentSpec(tag='V-101',name='罐',symbol_id='tank',source='inferred',confidence=.5)])
 assert spec.needs_confirmation()
 assert [n for n,a in compile_spec(spec)]==['create_drawing','place_equipment','auto_layout']


def test_invalid_role_json_retries_once_then_falls_back(monkeypatch):
 import zhihui.agent.llm as llm
 class Client:
  async def close(self):return None
 class Broken:
  calls=0
  def __init__(self,*a,**k):self.client=Client()
  async def chat(self,messages,tools):
   Broken.calls+=1
   assert len(messages)<=3
   return llm.Turn(content='invalid JSON')
 monkeypatch.setenv('DEEPSEEK_API_KEY','sk-test')      # 已接入大模型，只是它给出了不合格的输出：重试一次，仍不合格才用确定性结果
 monkeypatch.setenv('ZHIHUI_LLM_PRIMARY','deepseek')
 monkeypatch.setattr(llm,'OpenAICompatLLM',Broken)
 runner=RoleRunner(mock=False)
 out=asyncio.run(runner.run('router',RouterOutput,{'prompt':'画图'},lambda:{'task_type':'new_drawing'}))
 assert out.task_type=='new_drawing' and Broken.calls==2


def test_role_runner_requires_a_connected_model_no_silent_rules(monkeypatch):
    """没填 API Key / 网络鉴权失败：明确抛 LLMUnavailable，不用规则结果冒充（产品里没有离线版）。"""
    import zhihui.agent.llm as llm
    from zhihui.agent.llm_required import LLMUnavailable
    for k in ('DEEPSEEK_API_KEY','DASHSCOPE_API_KEY','ZHIPUAI_API_KEY','OPENAI_API_KEY'): monkeypatch.delenv(k,raising=False)
    monkeypatch.setenv('ZHIHUI_LLM_PRIMARY','deepseek')
    runner=RoleRunner(mock=False)
    with pytest.raises(LLMUnavailable) as ex: asyncio.run(runner.run('router',RouterOutput,{'prompt':'画图'},lambda:{'task_type':'new_drawing'}))
    assert '还没有接入大模型' in str(ex.value)
    class Client:
        async def close(self):return None
    class Down:
        def __init__(self,*a,**k):self.client=Client()
        async def chat(self,messages,tools):raise ConnectionError('network unreachable')
    monkeypatch.setenv('DEEPSEEK_API_KEY','sk-test'); monkeypatch.setattr(llm,'OpenAICompatLLM',Down)
    with pytest.raises(LLMUnavailable) as ex2: asyncio.run(RoleRunner(mock=False).run('router',RouterOutput,{'prompt':'画图'},lambda:{'task_type':'new_drawing'}))
    assert 'ConnectionError' in str(ex2.value)
    assert asyncio.run(RoleRunner(mock=True).run('router',RouterOutput,{'prompt':'画图'},lambda:{'task_type':'new_drawing'})).task_type=='new_drawing'      # mock 只给测试用
