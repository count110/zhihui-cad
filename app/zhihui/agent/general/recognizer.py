"""图元对象识别 Agent（6.2）：规则解析 → 感知函数 → 打分判定（resolved / ambiguous / not_found）→ 写回选择集。

规则解析覆盖大部分口语指代，离线可用；规则看不懂时才让大模型把短语翻译成感知函数的调用参数（模型只写参数，不下结论）。
判定：只有一个候选，或第一名领先第二名足够多，记 resolved；有多个且没有区分依据记 ambiguous（返回候选摘要，前端高亮，用户可以点图作答）；没有候选记 not_found 并说明原因。
"""
from __future__ import annotations

import math
from typing import Any, Awaitable, Callable

from .backend import Backend
from .executor import normalize_perception
from .recognizer_rules import RecSpec, parse_phrase

KIND_CN = {"line": "直线", "circle": "圆", "arc": "圆弧", "ellipse": "椭圆", "polyline": "多段线", "text": "文字", "mtext": "多行文字", "dimension": "标注", "hatch": "填充", "insert": "块",
           "solid3d": "实体", "spline": "样条", "point": "点", "mesh": "网格"}
LLMPlanFn = Callable[[str, dict], Awaitable[dict | None]]


def bbox_c(c: dict) -> tuple[float, float]:
    b = c.get("bbox") or [0, 0, 0, 0]
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def metric(c: dict, name: str) -> float:
    kp = c.get("key_params", {})
    b = c.get("bbox") or [0, 0, 0, 0]
    if name == "x":
        return bbox_c(c)[0]
    if name == "y":
        return bbox_c(c)[1]
    if name == "area":
        a = kp.get("area")
        return float(a) if a is not None else (b[2] - b[0]) * (b[3] - b[1])
    if name == "length":
        return float(kp.get("length", math.hypot(b[2] - b[0], b[3] - b[1])))
    if name == "origin":
        x, y = bbox_c(c)
        return math.hypot(x, y)
    return 0.0


def orientation(c: dict) -> str | None:
    kp = c.get("key_params", {})
    if c.get("type") == "line" and "p1" in kp and "p2" in kp:
        dx, dy = abs(kp["p2"][0] - kp["p1"][0]), abs(kp["p2"][1] - kp["p1"][1])
        L = math.hypot(dx, dy) or 1.0
        if dx / L < 1e-4:
            return "vertical"
        if dy / L < 1e-4:
            return "horizontal"
        return "slanted"
    return None


def brief(c: dict) -> str:
    kp, t = c.get("key_params", {}), c.get("type")
    if t == "line":
        o = orientation(c)
        name = {"vertical": "竖线", "horizontal": "横线"}.get(o, "斜线")
        x, y = bbox_c(c)
        pos = f"x={x:g}" if o == "vertical" else f"y={y:g}" if o == "horizontal" else f"中点({x:g},{y:g})"
        return f"{c['entity_id']}：{name}，{pos}，长 {kp.get('length', 0):g}"
    if t == "circle":
        return f"{c['entity_id']}：圆，圆心({kp['center'][0]:g},{kp['center'][1]:g})，⌀{kp['diameter']:g}"
    if t == "arc":
        return f"{c['entity_id']}：圆弧，圆心({kp['center'][0]:g},{kp['center'][1]:g})，R{kp['radius']:g}"
    b = c.get("bbox")
    tag = "矩形" if "rectangle" in c.get("tags", []) else "多边形" if "polygon" in c.get("tags", []) else KIND_CN.get(t, t)
    return f"{c['entity_id']}：{tag}" + (f" ({b[0]:g},{b[1]:g})-({b[2]:g},{b[3]:g})" if b else "")


def reasons(spec: RecSpec) -> str:
    r = []
    if spec.types:
        r.append("类型" + "/".join(KIND_CN.get(x, x) for x in spec.types))
    if spec.region:
        r.append({"left": "位置偏左", "right": "位置偏右", "top": "位置偏上", "bottom": "位置偏下", "center": "位置居中"}.get(spec.region, spec.region))
    if spec.superlative:
        r.append({"x": {"min": "最左", "max": "最右"}, "y": {"min": "最下", "max": "最上"}, "area": {"min": "最小", "max": "最大"}, "length": {"min": "最短", "max": "最长"}, "origin": {"min": "最靠近原点"}}[spec.superlative[0]][spec.superlative[1]])
    if spec.size_class:
        r.append("较大" if spec.size_class == "大" else "较小")
    if spec.where:
        r.append("、".join(f"{w['field']}{w['op']}{w['value']:g}" for w in spec.where))
    if spec.orient:
        r.append({"vertical": "竖直", "horizontal": "水平", "slanted": "倾斜"}[spec.orient])
    return "、".join(r) or "符合描述"


class Recognizer:
    def __init__(self, backend: Backend, llm_plan: LLMPlanFn | None = None):
        self.backend = backend
        self.llm_plan = llm_plan

    # ------------------------------------------------------------ 入口
    async def resolve(self, query: str, expect: dict | None, need: list[str] | None, drawing_id: str, resolved_ref=None) -> dict:
        calls = 0
        ctx = await self.backend.context(drawing_id)
        calls += 1
        spec = parse_phrase(query, ctx.get("layers", []))
        method = "rules"
        if spec.unparsed:
            if self.llm_plan is not None:
                out = await self._llm(query, ctx, drawing_id, expect, need)
                if out is not None:
                    return out
            return self._result("not_found", method, query, [], message=f"没看懂「{query}」指的是哪个图元", hint="换一种说法（如“左边那个圆”“最大的矩形”），或直接给出图元编号")
        cands, extra = await self._retrieve(spec, drawing_id)
        calls += extra
        if spec.orient:
            cands = [c for c in cands if orientation(c) == spec.orient or (c.get("type") not in ("line",))]
            cands = [c for c in cands if c.get("type") != "line" or orientation(c) == spec.orient]
        chosen, status, scores, why = self._judge(spec, cands)
        if status == "not_found":
            msg = await self._explain_missing(spec, drawing_id)
            return self._result("not_found", method, query, [], message=msg, calls=calls)
        res = self._result(status, method, query, chosen, spec=spec, scores=scores, why=why, cands=cands, calls=calls)
        # 写回选择集：resolved 选中结果；ambiguous 选中候选（前端高亮，用户可点图作答）
        ids = res["entity_ids"] if status == "resolved" else res["candidates"][:12]
        if ids:
            await self.backend.call("select_entities", {"selection_mode": "by_ids", "entity_ids": ids}, drawing_id)
            calls += 1
        if status == "resolved":
            wants = list(need or [])
            snaps = list(spec.features)
            for n in wants:
                if n.startswith("snap:") and n[5:] not in snaps:
                    snaps.append(n[5:])
            if snaps:
                for s in list(snaps):
                    if s == "center" and "geometric_center" not in snaps:
                        snaps.append("geometric_center")
                    if s == "geometric_center" and "center" not in snaps:
                        snaps.append("center")
                args: dict[str, Any] = {"entity_ids": res["entity_ids"][:1] if not spec.plural else res["entity_ids"], "snap_types": snaps}
                if spec.spatial_hint:
                    args["spatial_hint"] = spec.spatial_hint
                r = await self.backend.call("get_entity_snap_points", args, drawing_id)
                calls += 1
                if r.get("ok"):
                    norm = normalize_perception("get_entity_snap_points", r)
                    res["points"] = norm.get("points", {})
                    for s_ in snaps:                                           # primary：按短语要求的顺序，第一个求出来的特征点
                        if s_ in res["points"]:
                            res["points"]["primary"] = res["points"][s_]
                            break
                    if not res["points"].get("_list"):
                        res["log"] += "；没有求出捕捉点"
            if spec.boundary or "boundary" in wants:
                b, c2 = await self._boundary(res, cands, drawing_id)
                calls += c2
                if b:
                    res["boundary"] = b
        res["calls"] = calls
        return res

    async def from_ids(self, query: str, ids: list[str], need: list[str] | None, drawing_id: str) -> dict:
        """用户替识别步骤选定了图元：补齐捕捉点和边界，返回和 resolve 一样的结构。"""
        spec = parse_phrase(query, [])
        r = await self.backend.call("query_entities_by_filter", {"entity_ids": ids}, drawing_id)
        cands = (r.get("data") or {}).get("candidates", []) if r.get("ok") else []
        res = self._result("resolved", "user", query, cands, spec=spec, scores={c["entity_id"]: 1.0 for c in cands}, why="用户选择", cands=cands, calls=1)
        snaps = list(spec.features)
        for n in need or []:
            if n.startswith("snap:") and n[5:] not in snaps:
                snaps.append(n[5:])
        if snaps and ids:
            rr = await self.backend.call("get_entity_snap_points", {"entity_ids": ids[:1], "snap_types": snaps, **({"spatial_hint": spec.spatial_hint} if spec.spatial_hint else {})}, drawing_id)
            if rr.get("ok"):
                res["points"] = normalize_perception("get_entity_snap_points", rr).get("points", {})
                for s_ in snaps:
                    if s_ in res["points"]:
                        res["points"]["primary"] = res["points"][s_]
                        break
        if "boundary" in (need or []) or spec.boundary:
            b, _ = await self._boundary(res, cands, drawing_id)
            if b:
                res["boundary"] = b
        await self.backend.call("select_entities", {"selection_mode": "by_ids", "entity_ids": ids}, drawing_id)
        return res

    # ------------------------------------------------------------ 取候选
    async def _retrieve(self, spec: RecSpec, did: str) -> tuple[list[dict], int]:
        be = self.backend
        if spec.context or spec.nth_from_last is not None:
            tf = None
            if spec.types:
                tf = "rectangle" if "rectangle" in spec.tags else "polygon" if "polygon" in spec.tags else spec.types[0]
            refs = ["last_selected", "last_created"] if spec.context == "last_selected_or_created" else ["last_selected" if spec.context == "last_selected" else "last_created"]
            if spec.nth_from_last is not None:
                refs = ["nth_from_last"]
            n = 0
            for rt in refs:
                args: dict = {"reference_type": rt}
                if tf and rt != "last_selected":
                    args["entity_type_filter"] = tf
                if rt == "nth_from_last":
                    args["offset_index"] = spec.nth_from_last
                if spec.count and rt != "nth_from_last":
                    args["count"] = spec.count
                elif spec.plural and rt == "last_created":
                    args["count"] = 20
                elif rt == "last_selected" and spec.context in ("last_selected", "last_selected_or_created"):
                    args["count"] = 20                                   # 多取一些：选中了好几个而用户说“它”，要能发现
                r = await be.call("resolve_contextual_reference", args, did)
                n += 1
                if r.get("ok") and (r.get("data") or {}).get("entities"):
                    ents = r["data"]["entities"]
                    if spec.types and rt == "last_selected":
                        ents = [e for e in ents if e["type"] in spec.types] or ents
                    return ents, n
            return [], n
        args = {}
        if spec.ids:
            args["entity_ids"] = spec.ids
        if spec.types and not (spec.ids and not spec.types):
            args["entity_types"] = spec.types
        if spec.tags:
            args["tags"] = spec.tags
        if spec.layer:
            args["layer"] = spec.layer
        if spec.color is not None:
            args["color"] = spec.color
        if spec.where:
            args["where"] = spec.where
        if spec.region and not (spec.superlative and spec.superlative[0] in ("x", "y")):
            args["spatial_region"] = spec.region
        if spec.relation:
            args["relation"] = spec.relation
        r = await be.call("query_entities_by_filter", args, did)
        if not r.get("ok"):
            return [], 1
        out = (r.get("data") or {}).get("candidates", [])
        if not out and "rectangle" in args.get("tags", []):          # 带圆角的矩形已经不再带 rectangle 标签：放宽成闭合多段线
            args2 = {k: v for k, v in args.items() if k != "tags"}
            r2 = await be.call("query_entities_by_filter", args2, did)
            out = [c for c in (r2.get("data") or {}).get("candidates", []) if c.get("key_params", {}).get("closed")] if r2.get("ok") else []
            if len(out) > 1:                                                # “矩形”指图里最主要的那个：面积是第二名两倍以上时只取最大的
                out.sort(key=lambda c: -metric(c, "area"))
                if metric(out[0], "area") >= 2 * metric(out[1], "area"):
                    out = out[:1]
            return out, 2
        return out, 1

    # ------------------------------------------------------------ 判定
    def _judge(self, spec: RecSpec, cands: list[dict]) -> tuple[list[dict], str, dict[str, float], str]:
        if not cands:
            return [], "not_found", {}, ""
        why = reasons(spec)
        if spec.context or spec.nth_from_last is not None:
            if not spec.count and not spec.plural and spec.nth_from_last is None and len(cands) > 1 and spec.context in ("last_selected", "last_selected_or_created"):
                return cands, "ambiguous", {c["entity_id"]: round(1.0 / len(cands), 2) for c in cands}, f"当前选中了 {len(cands)} 个图元，说的是哪一个？"
            take = cands[: spec.count] if spec.count else (cands if spec.plural else cands[:1])
            return take, "resolved", {c["entity_id"]: 0.9 for c in take}, "指代上下文"
        if spec.ids and not (spec.types or spec.region or spec.superlative):
            return cands, "resolved", {c["entity_id"]: 1.0 for c in cands}, "显式编号"
        pool = list(cands)
        if spec.ordinal is not None:
            if spec.ordinal > len(pool):
                return [], "not_found", {}, ""
            c = pool[spec.ordinal - 1]
            return [c], "resolved", {c["entity_id"]: 0.9}, f"第 {spec.ordinal} 个"
        if spec.last:
            return [pool[-1]], "resolved", {pool[-1]["entity_id"]: 0.9}, "最后一个"
        if spec.size_class:
            vals = sorted({round(metric(c, "area"), 6) for c in pool})
            if len(vals) < 2:
                return pool, "resolved", {c["entity_id"]: 0.8 for c in pool}, "图中同类图元大小一样，全部选中"
            target = vals[-1] if spec.size_class == "大" else vals[0]
            klass = [c for c in pool if abs(round(metric(c, "area"), 6) - target) < 1e-9]
            gap = abs(vals[-1] - vals[-2]) / max(abs(vals[-1]), 1e-9) if spec.size_class == "大" else abs(vals[1] - vals[0]) / max(abs(vals[1]), 1e-9)
            sc = round(min(0.99, 0.6 + 0.4 * min(1.0, gap * 3)), 2)
            return klass, "resolved", {c["entity_id"]: sc for c in klass}, f"{'最大' if spec.size_class == '大' else '最小'}的一类"
        if spec.superlative:
            m, direction = spec.superlative
            vals = [(metric(c, m), c) for c in pool]
            vals.sort(key=lambda t: t[0], reverse=(direction == "max"))
            top = [c for v, c in vals if abs(v - vals[0][0]) <= 1e-6 * max(1.0, abs(vals[0][0]))]
            ext = (max(v for v, _ in vals) - min(v for v, _ in vals)) or 1.0
            gap = (abs(vals[0][0] - vals[len(top)][0]) / ext) if len(vals) > len(top) else 1.0
            if len(top) > 1 and not spec.plural:
                return top, "ambiguous", {c["entity_id"]: round(1.0 / len(top), 2) for c in top}, why
            sc = round(min(0.99, 0.6 + 0.4 * min(1.0, gap * 3)), 2) if len(vals) > 1 else 0.95
            if len(top) == 1 and len(vals) > 1 and gap < 0.05:
                return [c for _, c in vals[:2]], "ambiguous", {c["entity_id"]: 0.5 for _, c in vals[:2]}, why
            return top, "resolved", {c["entity_id"]: sc for c in top}, why
        if len(pool) == 1:
            return pool, "resolved", {pool[0]["entity_id"]: 0.95}, why
        if spec.plural:
            return pool, "resolved", {c["entity_id"]: 0.8 for c in pool}, why + "，全部"
        return pool, "ambiguous", {c["entity_id"]: round(1.0 / len(pool), 2) for c in pool}, why

    async def _explain_missing(self, spec: RecSpec, did: str) -> str:
        what = "/".join(KIND_CN.get(x, x) for x in spec.types) or "图元"
        if spec.tags and "rectangle" in spec.tags:
            what = "矩形"
        cond = reasons(spec)
        msg = f"图中没有符合「{cond}」的{what}"
        if spec.where and spec.types:
            r = await self.backend.call("query_entities_by_filter", {"entity_types": spec.types, **({"tags": spec.tags} if spec.tags else {})}, did)
            cs = (r.get("data") or {}).get("candidates", []) if r.get("ok") else []
            if cs:
                fld = spec.where[0]["field"]
                vals = sorted({round(float(c.get("key_params", {}).get(fld, metric(c, "length"))), 4) for c in cs if c.get("key_params", {}).get(fld) is not None})
                if vals:
                    msg += f"，现有{what}的{ {'radius': '半径', 'diameter': '直径', 'length': '长度', 'width': '宽度', 'height': '高度', 'area': '面积'}.get(fld, fld) }是 " + "、".join(f"{v:g}" for v in vals[:8])
            else:
                msg += f"（图中没有任何{what}）"
        elif spec.types:
            msg += "（图中没有这类图元，或它们在隐藏/冻结的图层上）"
        return msg

    # ------------------------------------------------------------ 边界
    async def _boundary(self, res: dict, cands: list[dict], did: str) -> tuple[dict | None, int]:
        ids = res["entity_ids"][:1]
        outer = next((c for c in res["matched_entities"] if c["entity_id"] == ids[0]), None) if ids else None
        if outer is None:
            return None, 0
        ob = outer.get("bbox") or [0, 0, 0, 0]
        r = await self.backend.call("query_entities_by_filter", {"entity_types": ["circle", "polyline", "arc", "ellipse"]}, did)
        pool = (r.get("data") or {}).get("candidates", []) if r.get("ok") else []
        inner = [c["entity_id"] for c in pool if c["entity_id"] != ids[0] and c.get("bbox") and c["bbox"][0] >= ob[0] - 1e-6 and c["bbox"][1] >= ob[1] - 1e-6 and c["bbox"][2] <= ob[2] + 1e-6 and c["bbox"][3] <= ob[3] + 1e-6]
        r2 = await self.backend.call("find_closed_boundary", {"candidate_entity_ids": ids + inner}, did)
        if not r2.get("ok"):
            return None, 2
        b = normalize_perception("find_closed_boundary", r2)["boundary"]
        return b, 2

    # ------------------------------------------------------------ 大模型兜底
    async def _llm(self, query: str, ctx: dict, did: str, expect, need) -> dict | None:
        plan = await self.llm_plan(query, ctx)
        if not plan or not plan.get("calls"):
            return None
        last = None
        n = 0
        for c in plan["calls"][:4]:
            if c.get("tool") not in ("query_entities_by_filter", "resolve_contextual_reference", "get_entity_snap_points", "find_closed_boundary", "get_entity_grips_and_topology", "inspect_drawing_context"):
                continue
            r = await self.backend.call(c["tool"], c.get("args", {}), did)
            n += 1
            if not r.get("ok"):
                return self._result("not_found", "llm", query, [], message=r.get("message", "识别失败"), calls=n)
            last = (c["tool"], r)
        if last is None:
            return None
        norm = normalize_perception(*last)
        cands = norm.get("matched_entities", [])
        status = "resolved" if len(cands) == 1 else "not_found" if not cands else "ambiguous"
        out = self._result(status, "llm", query, cands, scores={c["entity_id"]: round(1 / max(1, len(cands)), 2) for c in cands}, why=plan.get("note", ""), cands=cands, calls=n)
        for k in ("points", "boundary"):
            if k in norm:
                out[k] = norm[k]
        return out

    # ------------------------------------------------------------ 组装
    def _result(self, status: str, method: str, query: str, chosen: list[dict], spec: RecSpec | None = None, scores: dict | None = None, why: str = "", cands: list[dict] | None = None, message: str = "", hint: str = "", calls: int = 1) -> dict:
        ids = [c["entity_id"] for c in chosen]
        matched = [{"entity_id": c["entity_id"], "handle": c.get("handle"), "entity_type": c.get("type"), "tags": c.get("tags", []), "layer": c.get("layer"), "bbox": c.get("bbox"),
                    "geometry_summary": c.get("key_params", {}), "score": (scores or {}).get(c["entity_id"], 0.0)} for c in chosen]
        out = {"ok": True, "status": status, "method": method, "entity_ids": ids, "matched_entities": matched, "points": {}, "boundary": {"outer": [], "islands": []}, "ambiguity_message": None,
               "candidates": [c["entity_id"] for c in ((chosen if status == "ambiguous" else cands) or chosen)], "calls": calls, "created": [], "modified": [], "deleted": []}
        if status == "resolved":
            top = max((scores or {}).values(), default=0.0)
            out["log"] = f"识别：「{query}」→ {'、'.join(ids)}（{why}，得分 {top:.2f}）"
        elif status == "ambiguous":
            out["ambiguity_message"] = f"「{query}」有 {len(chosen)} 个候选，请选择：" + "；".join(brief(c) for c in chosen[:6])
            out["options"] = [brief(c) for c in chosen[:6]] + (["全部"] if len(chosen) > 1 else [])
            out["log"] = f"识别：「{query}」有 {len(chosen)} 个候选，需要确认"
        else:
            out["ambiguity_message"] = message
            out["hint"] = hint
            out["log"] = f"识别：「{query}」没有找到：{message}"
        return out
