"""程序级护栏：在工具真正执行前，拦下大模型「猜目标」和「编数值」。

提示词只能要求模型，护栏是强制的。被拦下时返回 ok=false + 修复提示，模型下一轮通常会改为 ask_user。
1. 改图目标必须明确：move_equipment / edit_equipment 的设备，要么用户原话里写了位号，
   要么是按用户原话 find_entities 查到的唯一匹配，要么是本任务新放的设备。
2. 数值不能编造：add_annotation 的文字、设备 params（含高程 elevations、特性参数 feature）、管道规格 spec、
   管道标注（管道号、外径、壁厚）、图纸接续的图号、工段号、标题栏 title_block、说明 notes 里的数字，必须在用户原话或用户回答里出现过；
   管道材料 / 等级 / 温度范围 / 隔热隔音 / 伴热这些字母代号，用户要么直接写了代号，要么说了它对应的中文（如「不锈钢」→ E）；
   标注里也不能用「？」「待定」之类的占位符糊弄过去。
3. 改图时整图重排（allow_full_relayout=true）必须有用户的明确同意。
"""

from __future__ import annotations

import json
import re

TAG_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}-\d{2,4}[A-Za-z]?)(?![A-Za-z0-9])")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
TARGETED = {"move_equipment", "edit_equipment"}
LIST_NO_RE = re.compile(r"^\s*\d{1,2}\s*[.、．)）]\s*")  # 说明条文的序号「1.」「2、」不算数值
PLACEHOLDER_RE = re.compile(r"[?？]|待定|待确认|未知|[Xx]{2,}|_{2,}")
ASK_NOW = "不得编造尺寸、规格、流量等数值，也不要用问号或占位符代替。立即调用 ask_user 向用户索取这些数值，拿到后再标注"


def user_text(messages: list[dict]) -> str:
    """用户真正说过的话：原始需求 + 对反问的回答（不含系统校验消息）。"""
    parts = []
    for m in messages:
        c = m.get("content") or ""
        if m.get("role") == "user" and not c.startswith("【系统校验"):
            parts.append(c)
        elif m.get("role") == "tool" and '"user_answer"' in c:
            try:
                parts.append(json.loads(c)["user_answer"])
            except (json.JSONDecodeError, KeyError):
                pass
    return "\n".join(parts)


CODE_TABLES = {"material": "pipe_material", "temp_code": "temp_range", "insulation_code": "insulation", "tracing": "heat_tracing"}


def _unsourced_codes(it: dict, said: str) -> list[str]:
    """字母代号只能来自用户：原话里出现了这个代号（独立的字母），或者出现了它在代号表里的中文名。"""
    from zhihui.cad.codes import lookup

    bad = []
    for k in ("material", "grade", "temp_code", "insulation_code", "tracing"):
        v = str(it.get(k) or "").strip().upper()
        if not v:
            continue
        if re.search(rf"(?<![A-Za-z]){re.escape(v)}(?![A-Za-z])", said.upper()):
            continue
        row = lookup(CODE_TABLES[k], v) if k in CODE_TABLES else None
        name = (row or {}).get("name", "")
        if name and (name in said or name.split("（")[0] in said):
            continue
        bad.append(f"{k}={v}")
    return bad


def _numbers(text: str) -> set[float]:
    return {float(n) for n in NUM_RE.findall(TAG_RE.sub(" ", text))}


STAGE_WORDS = (  # 用户原话 → 设计阶段（规范化要求 11.3）；先匹配更具体的说法
    ("construction", ("施工流程图", "管道及仪表流程图", "管道仪表流程图", "带控制点的工艺流程图", "带控制点流程图", "带控制点的流程图", "安装流程图", "P&ID", "PID", "PI&D")),
    ("material", ("物料流程图", "物料平衡图", "物料衡算")),
    ("scheme", ("方案流程图", "流程示意图")),
)


def detect_stage(text: str) -> str | None:
    """从用户原话里认出设计阶段；没提到返回 None。"""
    t = str(text or "")
    up = t.upper()
    for stage, words in STAGE_WORDS:
        for w in words:
            if re.search(rf"(?<![A-Z]){re.escape(w.upper())}(?![A-Z])", up) if w.isascii() else (w in t):
                return stage
    return None


class Guard:
    def __init__(self) -> None:
        self.confirmed: set[str] = set()

    def normalize(self, name: str, args: dict, messages: list[dict]) -> dict:
        """调用前补全参数：create_drawing 没写 stage 时，按用户原话里的「方案 / 物料 / 施工流程图、PID」选（规范化要求 11.3）。原地修改并返回 args。"""
        if name == "create_drawing" and not args.get("stage") and args.get("template_id", "pfd_default") != "water_elevation":
            st = detect_stage(user_text(messages))
            if st:
                args["stage"] = st
        return args

    def check(self, name: str, args: dict, messages: list[dict]) -> dict | None:
        said = user_text(messages)
        if name in TARGETED:
            ref = str(args.get("equipment_ref", "")).strip().upper()
            mentioned = {t.upper() for t in TAG_RE.findall(said)}
            if ref and ref not in mentioned and ref not in self.confirmed:
                return {
                    "ok": False,
                    "error_code": "E_TARGET_UNCONFIRMED",
                    "message": f"用户没有明确指出要改的是 {ref}",
                    "hint": "先用 find_entities 按用户原话里的描述查找：只有唯一匹配才能直接修改；有多个候选或找不到时，必须调用 ask_user 让用户选择（options 填候选位号）",
                }
        if name == "auto_layout" and args.get("allow_full_relayout"):
            if not re.search(r"重排|重新布局|重新排|整体调整|全部重新|同意|可以|好的|确认", said):
                return {
                    "ok": False,
                    "error_code": "E_RELAYOUT_UNCONFIRMED",
                    "message": "整图重排会挪动所有设备，用户还没有同意",
                    "hint": "先 ask_user 说明整图重排的影响并征得同意",
                }
        texts = []
        if name == "add_annotation":
            text = str(args.get("text", ""))
            texts.append(LIST_NO_RE.sub("", text) if args.get("anchor_ref") == "notes" else text)
        if name == "create_drawing":
            texts += [LIST_NO_RE.sub("", str(n)) for n in args.get("notes") or []]
            tb = args.get("title_block") or {}
            if isinstance(tb, dict):
                texts += [json.dumps(v, ensure_ascii=False) for v in tb.values()]
        if name == "add_dimension":  # 尺寸数值只能来自用户
            texts.append(str(args.get("text") or ""))
        if name == "connect_ports":
            for it in args.get("items") or []:
                if not isinstance(it, dict):
                    continue
                texts += [str(it.get(k) or "") for k in ("spec", "pipe_no", "od", "wall")]
                texts += [str(f.get("size_class") or "") for f in it.get("fittings") or [] if isinstance(f, dict)]
                if isinstance(it.get("params"), dict):
                    texts += [str(v) for v in it["params"].values()]
                for side in ("from_sheet", "to_sheet"):
                    if isinstance(it.get(side), dict):
                        texts += [str(it[side].get("sheet") or ""), str(it[side].get("ref") or "")]
                bad = _unsourced_codes(it, said)
                if bad:
                    return {"ok": False, "error_code": "E_UNSOURCED_VALUE", "message": f"代号 {bad} 不是用户给出的", "hint": ASK_NOW}
        if name == "create_drawing" and args.get("section_no"):
            texts.append(str(args["section_no"]))
        if name == "repeat_system":
            texts.append(str(args.get("numbers") or ""))
        if name == "add_stream_table":  # 物料组成表的流量、摩尔分率只能来自用户
            texts += [str(r.get(k) or "") for r in args.get("rows") or [] if isinstance(r, dict) for k in ("flow_kg_h", "mole_pct")]
        if name == "add_instrument":  # 仪表的工段序号只能来自用户
            texts.append(str(args.get("loop_no") or ""))
        if name in ("place_equipment",):
            texts += [json.dumps(it.get("params") or {}, ensure_ascii=False) for it in args.get("items", []) if isinstance(it, dict)]
            for it in args.get("items", []):
                pr = it.get("params") if isinstance(it, dict) else None
                if isinstance(pr, dict):
                    bad = _unsourced_codes({"insulation_code": pr.get("insulation"), "tracing": pr.get("tracing")}, said)
                    if bad:
                        return {"ok": False, "error_code": "E_UNSOURCED_VALUE", "message": f"设备参数里的代号 {bad} 不是用户给出的", "hint": ASK_NOW}
        if name == "edit_equipment" and args.get("params"):
            texts.append(json.dumps(args["params"], ensure_ascii=False))
        holders = [t for t in texts if PLACEHOLDER_RE.search(t)] if name == "add_annotation" else []
        if holders:
            return {"ok": False, "error_code": "E_PLACEHOLDER_VALUE", "message": f"标注「{holders[0]}」里是占位符，缺少用户提供的数值", "hint": ASK_NOW}
        given = _numbers(said)
        invented = sorted({n for t in texts for n in _numbers(t)} - given)
        if invented:
            shown = [f"{n:g}" for n in invented]
            return {
                "ok": False,
                "error_code": "E_UNSOURCED_VALUE",
                "message": f"数值 {shown} 不是用户给出的",
                "hint": ASK_NOW,
            }
        return None

    def observe(self, name: str, args: dict, result: dict, messages: list[dict]) -> None:
        if not result.get("ok"):
            return
        if name == "place_equipment":
            for e in result.get("equipment", []):
                self.confirmed.update({str(e.get("ref", "")).upper(), str(e.get("id", "")).upper()})
        elif name == "edit_equipment" and args.get("new_tag"):
            self.confirmed.add(str(args["new_tag"]).upper())
        elif name == "find_entities":
            said = user_text(messages).lower()
            keys = [str(args.get(k) or "") for k in ("query", "tag", "text")]
            # 只有用用户原话里的词去查才算数，防止模型先猜位号再自己「确认」
            if not any(k and k.lower() in said for k in keys):
                return
            c = result.get("candidates", [])
            if c and not result.get("ambiguous") and (len(c) == 1 or c[0]["score"] > c[1]["score"]):
                self.confirmed.update({str(c[0].get("ref", "")).upper(), str(c[0].get("id", "")).upper()})
