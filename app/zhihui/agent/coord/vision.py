"""Optional image-only review, with object/region evidence required."""
import base64
import json
from zhihui.agent.llm import OpenAICompatLLM


async def inspect_image(path,provider,model,spec,usage):
    llm=OpenAICompatLLM(provider,timeout=18);llm.model=model
    checklist=[c.model_dump() for c in spec.checklist] if spec else []
    messages=[{'role':'system','content':'独立审图。只输出 JSON {"issues":[{"object_id":"对象ID或区域编号", "message":"可见问题"}]}。每项必须有对象ID或区域编号；禁止编辑指令、坐标或未经图像证实的结论。'},
              {'role':'user','content':[{'type':'text','text':json.dumps(checklist,ensure_ascii=False)},
               {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()}}]}]
    try:
        turn=await llm.chat(messages,[])
        usage['prompt_tokens']+=turn.prompt_tokens;usage['completion_tokens']+=turn.completion_tokens;usage['models'].add(turn.model)
        payload=json.loads(turn.content)
        return [{'id':'vl:'+str(x['object_id']),'target_id':str(x['object_id']),'message':str(x['message']),
                 'code':'W_VISUAL_REVIEW','source':'vision','status':'unresolved','attempts':0}
                for x in payload.get('issues',[]) if x.get('object_id') and x.get('message')]
    finally:await llm.client.close()
