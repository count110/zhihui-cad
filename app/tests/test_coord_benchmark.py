from zhihui.agent.coord.metrics import summarize,percentile
from zhihui.agent.extended_guards import ExtendedGuard


def test_metric_denominators_and_percentiles():
    rows=[{'status':s,'seconds':n,'coverage':c,'usage':{'tool_calls':n,'prompt_tokens':100}}
          for s,n,c in [('READY',1,1),('READY_WITH_WARNINGS',3,.5),('WAITING_CONFIRMATION',5,0),('FAILED',7,0)]]
    m=summarize(rows)
    assert m['ready_rate']==m['warning_delivery_rate']==m['failed_rate']==.25
    assert m['waiting_confirmation']==1 and m['total_tokens']==400
    assert m['tool_calls_mean']==4 and m['seconds_p50']==4 and m['tool_calls_p95']==6.7
    assert percentile([], .95) is None


def test_chinese_title_field_is_normalized_without_trusting_model_facts():
    args={'fields':{'设计单位':'广东石油化工学院'},'user_facts':{'text':'model claims'}}
    ExtendedGuard().normalize('edit_title_block',args,[{'role':'user','content':'设计单位写广东石油化工学院'}])
    assert args['fields']=={'unit_name':'广东石油化工学院'}
    assert args['user_facts']=={'text':'设计单位写广东石油化工学院'}
