"""Export all role and ProcessSpec JSON contracts."""
import json
from pathlib import Path
from .roles import SCHEMAS
from .spec import ProcessSpec


def main():
    root=Path(__file__).resolve().parents[3]/'01_项目文档'/'接口约定';root.mkdir(parents=True,exist_ok=True)
    (root/'process_spec.schema.json').write_text(json.dumps(ProcessSpec.model_json_schema(),ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'agent_roles.schema.json').write_text(json.dumps({k:v.model_json_schema() for k,v in SCHEMAS.items()},ensure_ascii=False,indent=2),encoding='utf-8')
    print(root/'process_spec.schema.json')


if __name__=='__main__':main()
