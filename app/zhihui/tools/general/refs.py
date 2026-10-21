"""变量引用解析（确定性）：$s1.created[0]、$s2.points.geometric_center、$this[2].created[0]、$LAST_CREATED …

只替换“整个字符串恰好是一个引用”的参数值；解析不了统一报 REF_UNRESOLVED。
"""
from __future__ import annotations

import re
from typing import Any

from zhihui.tools.core import ToolError

_REF = re.compile(r"^\$(?P<root>s\d+|this\[\d+\]|LAST_CREATED|LAST_MODIFIED|SELECTION|LAST_POINT)(?P<rest>(?:\.[A-Za-z_]\w*|\[\d+\])*)$")
_TOKEN = re.compile(r"\.([A-Za-z_]\w*)|\[(\d+)\]")


def is_ref(v: Any) -> bool:
    return isinstance(v, str) and v.startswith("$") and _REF.match(v.strip()) is not None


def _fail(ref: str, why: str) -> ToolError:
    return ToolError("REF_UNRESOLVED", f"引用 {ref} 解析不了：{why}", "检查步骤编号和字段；前一步必须先成功并且产生这个结果")


def resolve_ref(ref: str, steps: dict[str, dict], this: list[dict] | None, session) -> Any:
    m = _REF.match(ref.strip())
    if not m:
        raise _fail(ref, "写法不对")
    root, rest = m["root"], m["rest"]
    # 起点值
    if root.startswith("this["):
        idx = int(root[5:-1])
        if this is None or idx >= len(this):
            raise _fail(ref, f"批量内还没有第 {idx} 个子步骤的结果")
        val: Any = this[idx]
    elif root.startswith("s"):
        if root not in steps:
            raise _fail(ref, f"步骤 {root} 没有结果（还没执行，或执行失败）")
        val = steps[root]
    elif root == "LAST_POINT":
        if session.last_point is None:
            raise _fail(ref, "还没有上一点")
        val = list(session.last_point)
    elif root == "SELECTION":
        val = list(session.selection)
        if not val:
            raise _fail(ref, "当前没有选择集")
    else:
        op = "created" if root == "LAST_CREATED" else "modified"
        items = [r for r in session.recent_entities if r["op"] == op]
        val = {"_recent": items}
    toks = _TOKEN.findall(rest)
    i = 0
    while i < len(toks):
        name, idx = toks[i]
        i += 1
        if name:
            if isinstance(val, dict) and "_recent" in val:           # $LAST_CREATED.circle / $LAST_MODIFIED.polyline
                items = [r for r in val["_recent"] if r["type"] == name]
                if not items:
                    raise _fail(ref, f"最近没有 {name} 类型的图元")
                val = {"_recent": items}
                continue
            if isinstance(val, dict) and name == "created" and "created_kinds" in val and i < len(toks) and toks[i][0] and toks[i][0] in val["created_kinds"].values():
                kind = toks[i][0]                                       # $s1.created.circle[0]
                i += 1
                val = [k for k, v in val["created_kinds"].items() if v == kind]
                if not val:
                    raise _fail(ref, f"该步骤没有创建 {kind}")
                continue
            if isinstance(val, dict) and name in val:
                val = val[name]
            else:
                raise _fail(ref, f"没有字段 {name}")
        else:
            k = int(idx)
            if isinstance(val, dict) and "_recent" in val:
                if k >= len(val["_recent"]):
                    raise _fail(ref, f"最近图元不足 {k + 1} 个")
                val = val["_recent"][k]["entity_id"]
            elif isinstance(val, dict) and isinstance(val.get("_list"), list) and k < len(val["_list"]):
                val = val["_list"][k]                                   # $s2.points[0]：points 既能按类型名取，也能按序号取
            elif isinstance(val, (list, tuple)) and k < len(val):
                val = val[k]
            else:
                raise _fail(ref, f"下标 {k} 超出范围")
    if isinstance(val, dict) and "_recent" in val:
        return val["_recent"][0]["entity_id"]
    if val is None or val == {}:
        raise _fail(ref, "结果为空")
    return val


def ref_kind(ref: str) -> str:
    m = _REF.match(ref.strip())
    root = m["root"] if m else ""
    return "this" if root.startswith("this[") else "steps" if re.fullmatch(r"s\d+", root) else "session"


def resolve_refs(obj: Any, steps: dict[str, dict] | None = None, this: list[dict] | None = None, session=None, leave: tuple[str, ...] = ("this",)) -> Any:
    """递归替换 obj 里的引用。leave 里的种类原样保留：Executor 解析 $sN 和会话变量，把 $this 留给批量函数；批量函数里解析 $this 和会话变量。"""
    if isinstance(obj, str):
        if is_ref(obj) and ref_kind(obj) not in leave:
            return resolve_ref(obj, steps or {}, this, session)
        return obj
    if isinstance(obj, list):
        return [resolve_refs(x, steps, this, session, leave) for x in obj]
    if isinstance(obj, dict):
        return {k: resolve_refs(v, steps, this, session, leave) for k, v in obj.items()}
    return obj


def has_refs(obj: Any) -> bool:
    if isinstance(obj, str):
        return is_ref(obj)
    if isinstance(obj, list):
        return any(has_refs(x) for x in obj)
    if isinstance(obj, dict):
        return any(has_refs(v) for v in obj.values())
    return False
