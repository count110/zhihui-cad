"""计划 JSON 协议（5.1）：Planner 的输出，pydantic 严格校验。"""
from __future__ import annotations

import json
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from zhihui.tools.general.registry import CATEGORIES, REGISTRY

PERCEPTION = {t.name for t in REGISTRY.values() if t.category == "perception"}
AGENTS = ("entity_recognition_agent", "tool_execution_agent")


_CATEGORY_HINTS: list[tuple[str, str]] = [
    ("vessel", "chem_equipment"), ("equip", "chem_equipment"), ("chem", "chem_equipment"), ("tank", "chem_equipment"),      # 设备图的词放最前：chemical_equipment_layout 不能被 layout 抢走
    ("layout", "layout_plotting"), ("plot", "layout_plotting"), ("print", "layout_plotting"), ("viewport", "layout_plotting"),
    ("array", "2d_editing"), ("replicat", "2d_editing"), ("edit", "2d_editing"), ("modif", "2d_editing"), ("transform", "2d_editing"), ("offset", "2d_editing"), ("fillet", "2d_editing"),
    ("dim", "annotation_dimension"), ("annot", "annotation_dimension"), ("text", "annotation_dimension"),
    ("toler", "engineering_specialty"), ("gdt", "engineering_specialty"), ("rough", "engineering_specialty"), ("special", "engineering_specialty"), ("symbol", "engineering_specialty"), ("toolset", "engineering_specialty"),
    ("block", "block_xref_constraint"), ("xref", "block_xref_constraint"), ("constraint", "block_xref_constraint"),
    ("3d", "3d_modeling"), ("solid", "3d_modeling"), ("extru", "3d_modeling"), ("revol", "3d_modeling"),
    ("snap", "precision_snap"), ("coord", "precision_snap"), ("recogn", "view_select"), ("select", "view_select"), ("view", "view_select"), ("zoom", "view_select"),
    ("env", "environment_setup"), ("setup", "environment_setup"), ("layer", "environment_setup"), ("setting", "environment_setup"),
    ("draw", "2d_drafting"), ("draft", "2d_drafting"), ("create", "2d_drafting"),
    ("undo", "system"), ("redo", "system"), ("batch", "system"), ("system", "system"),
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClarItem(Contract):
    """一次反问里的一个待问项（设备图：path 是参数路径，如 shell.inner_diameter_mm；图种用 drawing_type）。"""
    path: str = ""
    label: str = ""
    options: list[str] = Field(default_factory=list)


class Clarification(Contract):
    question: str = ""
    options: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    items: list[ClarItem] = Field(default_factory=list)


class Step(Contract):
    step_id: str = Field(pattern=r"^s\d+$")
    target_agent: Literal["entity_recognition_agent", "tool_execution_agent"]
    depends_on: list[str] = Field(default_factory=list)
    action_summary: str = ""
    payload: dict[str, Any]


class Plan(Contract):
    thought: str = ""
    intent_categories: list[str] = Field(default_factory=list)
    normalized_utterance: str = ""
    workflow_steps: list[Step] = Field(default_factory=list)
    replan_after: Optional[str] = None
    clarification_needed: bool = False
    clarification: Clarification = Field(default_factory=Clarification)
    user_response_preview: str = ""

    @field_validator("intent_categories", mode="before")
    @classmethod
    def _cats(cls, v):
        """意图类别只是路由提示，不影响执行：模型自造的类别名（replication_array、layout_and_print…）按关键词归到最接近的类别，归不了就丢掉，
        不能因为一个标签让整份计划作废（真实模型下这是计划失败的头号原因）。"""
        if not isinstance(v, list):
            return []
        out: list[str] = []
        for c in v:
            s = str(c).strip()
            if s in CATEGORIES or s == "pfd":
                m = s
            else:
                low = s.lower()
                m = next((cat for kw, cat in _CATEGORY_HINTS if kw in low), None)
            if m and m not in out:
                out.append(m)
        return out


class PlanError(ValueError):
    def __init__(self, message: str, issues: list[str] | None = None):
        super().__init__(message)
        self.issues = issues or [message]


def strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    if not t.startswith("{"):                                  # 模型在 JSON 前后加了话：取第一个 { 到最后一个 }
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            t = t[a:b + 1]
    return t


def validate_plan(plan: Plan, allowed_tools: set[str] | None = None) -> list[str]:
    """语义校验：步骤编号唯一、依赖指向已有的前序步骤、payload 形状、函数名存在且在允许的子集里。"""
    issues: list[str] = []
    seen: list[str] = []
    for st in plan.workflow_steps:
        if st.step_id in seen:
            issues.append(f"步骤编号 {st.step_id} 重复")
        for dep in st.depends_on:
            if dep not in seen:
                issues.append(f"{st.step_id} 依赖的 {dep} 不是前面的步骤")
        seen.append(st.step_id)
        p = st.payload
        if st.target_agent == "tool_execution_agent":
            tool = p.get("tool")
            if not isinstance(tool, str) or tool not in REGISTRY:
                issues.append(f"{st.step_id}: 没有函数 {tool!r}")
                continue
            if allowed_tools is not None and tool not in allowed_tools and tool not in ("cad_batch_execute", "history_undo_redo"):
                issues.append(f"{st.step_id}: 函数 {tool} 不在当前意图类别的工具子集里")
            if not isinstance(p.get("args", {}), dict):
                issues.append(f"{st.step_id}: args 必须是对象")
            if tool == "cad_batch_execute":
                for i, sub in enumerate(p.get("args", {}).get("steps", [])):
                    t2 = sub.get("tool") if isinstance(sub, dict) else None
                    if t2 not in REGISTRY:
                        issues.append(f"{st.step_id}: 批量第 {i + 1} 步没有函数 {t2!r}")
                    elif allowed_tools is not None and t2 not in allowed_tools:
                        issues.append(f"{st.step_id}: 批量第 {i + 1} 步的函数 {t2} 不在当前工具子集里")
        else:
            if "query" not in p and "tool" not in p:
                issues.append(f"{st.step_id}: 识别步骤需要 query 或 tool")
            elif "tool" in p and p["tool"] not in PERCEPTION:
                issues.append(f"{st.step_id}: 识别步骤只能调用感知函数 {sorted(PERCEPTION)}，不能是 {p['tool']}")
    if plan.replan_after and plan.replan_after not in seen:
        issues.append(f"replan_after 指向不存在的步骤 {plan.replan_after}")
    if plan.clarification_needed and not plan.clarification.question and not plan.clarification.items:
        issues.append("需要反问但没有写问题")
    if plan.clarification.items:
        from zhihui.cad.equipment.spec_ops import path_allowed
        bad = [it.path for it in plan.clarification.items if it.path and not path_allowed(it.path)]
        if bad:
            issues.append(f"反问的待问项路径不在可用路径里：{bad}（设备参数路径如 shell.inner_diameter_mm、shell.length、supports.kind、nozzles；图种用 drawing_type）")
    if not plan.clarification_needed and not plan.workflow_steps:
        issues.append("既没有步骤也没有反问")
    return issues


ENGINE_OWNED = ("turn_id", "confirmed")        # vessel_define 的这两个字段不归模型写：turn_id 由引擎注入；confirmed 只能在用户确认之后由引擎置 true


def strip_engine_fields(plan: Plan) -> list[str]:
    """丢掉模型自己写的 turn_id / confirmed（模型写 confirmed=true 就绕过了“替换现有设备参数 / 画在有手工图元的图上”的确认）。返回被丢掉的字段名。"""
    gone: list[str] = []

    def one(call: dict) -> None:
        a = call.get("args")
        if call.get("tool") == "vessel_define" and isinstance(a, dict):
            for k in ENGINE_OWNED:
                if k in a:
                    a.pop(k)
                    gone.append(k)

    for st in plan.workflow_steps:
        if st.target_agent != "tool_execution_agent":
            continue
        one(st.payload)
        if st.payload.get("tool") == "cad_batch_execute" and isinstance(st.payload.get("args"), dict):
            for sub in st.payload["args"].get("steps", []):
                if isinstance(sub, dict):
                    one(sub)
    return gone


def parse_plan(text: str, allowed_tools: set[str] | None = None) -> Plan:
    raw = strip_fence(text)
    try:
        plan = Plan.model_validate_json(raw)
    except ValidationError as ex:
        errs = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in ex.errors()[:6]]
        raise PlanError("计划 JSON 不符合格式：" + "；".join(errs), errs)
    except ValueError as ex:
        raise PlanError(f"计划不是合法的 JSON：{ex}")
    issues = validate_plan(plan, allowed_tools)
    if issues:
        raise PlanError("计划有问题：" + "；".join(issues[:5]), issues)
    strip_engine_fields(plan)
    return plan


def plan_dict(plan: Plan) -> dict:
    return json.loads(plan.model_dump_json())
