"""Deterministic capability mapping, independent of any model."""
import json
from pathlib import Path


def check_capability(operation):
    catalog=json.loads((Path(__file__).parent/'capabilities.json').read_text(encoding='utf-8'))
    for entry in catalog:
        if operation==entry['operation'] or any(word in operation for word in entry['phrases']):
            return {**entry,'supported':entry['tool']!='unsupported'}
    return {'operation':operation,'tool':'unsupported','supported':False,'fallback':'当前能力目录没有该操作；请选择已有操作或导出 DXF 后在 CAD 中处理。'}
