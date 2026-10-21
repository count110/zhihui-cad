"""The single persisted state shared only through the coordinator."""
from pydantic import BaseModel,Field
from .spec import ProcessSpec


class TaskState(BaseModel):
    task_id:str
    stage:str='router'
    intent:dict=Field(default_factory=dict)
    user_facts:dict=Field(default_factory=dict)
    spec:ProcessSpec|None=None
    capability_report:dict=Field(default_factory=dict)
    knowledge_gaps:list[dict]=Field(default_factory=list)
    issues:list[dict]=Field(default_factory=list)
    attempt_log:list[dict]=Field(default_factory=list)
    best_revision:dict=Field(default_factory=dict)
    decisions:list[dict]=Field(default_factory=list)
    drawing_id:str|None=None
    confirmed:bool=False
    coverage:float=0
    report:str=''
    metrics:dict=Field(default_factory=dict)        # requirement / drawing-rule metrics kept apart on purpose
    requirements:list[dict]=Field(default_factory=list)
    generated_symbols:list[dict]=Field(default_factory=list)   # equipment symbols created by search + designer role this task
    usage_total:dict=Field(default_factory=dict)    # tool calls / tokens / seconds summed over every confirm-resume round

    def slice(self,*keys):
        return self.model_dump(mode='json',include=set(keys))
