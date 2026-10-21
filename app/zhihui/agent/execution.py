"""Model-independent execution policy shared by single and coordinated agents.

This module never calls a model or a CAD tool. The caller executes its decisions,
so the same policy works with in-process, stdio and remote MCP transports.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLAYBOOK = Path(__file__).resolve().parents[1] / "kb" / "error_playbook.json"
MUTATIONS = frozenset({"create_drawing", "place_equipment", "connect_ports", "auto_layout",
    "move_equipment", "edit_equipment", "add_annotation", "add_hatch", "edit_hatch",
    "add_dimension", "add_detail_view", "add_schedule", "repeat_system", "add_instrument",
    "migrate_valves", "add_stream_table", "edit_title_block", "edit_annotation", "delete_annotation"})
FATAL_CODES = frozenset({"E_EMPTY", "E_UNPLACED", "E_SELF_LOOP", "E_PORT_INVALID", "E_UNEXPECTED_CHANGE",
    "E_DANGLING", "E_TAG_DUP", "E_SYMBOL_UNKNOWN", "E_UNROUTED", "E_PIPE_DETACHED", "E_PIPE_NOT_ORTHOGONAL"})


def canonical_json(value: Any) -> str:
    """Stable JSON, with equivalent integer/float coordinates rounded to 0.1 mm."""
    def normalize(v):
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            if not math.isfinite(v):
                raise ValueError("Non-finite tool argument")
            n = round(v, 1)
            return int(n) if n == int(n) else n
        if isinstance(v, dict):
            return {k: normalize(v[k]) for k in sorted(v)}
        if isinstance(v, (tuple, list)):
            return [normalize(x) for x in v]
        return v
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def signature(name: str, args: dict) -> str:
    return name + ":" + canonical_json(args)


def issue_score(validation: dict) -> int:
    return 10 * len(validation.get("errors", [])) + len(validation.get("warnings", []))


def usable(validation: dict | None) -> bool:
    return bool(validation and validation.get("ok") and validation.get("stats", {}).get("equipment", 0)
                and not any(e.get("code") in FATAL_CODES for e in validation.get("errors", [])))


@dataclass
class ExecutionConfig:
    max_calls: int = 40
    max_seconds: float = 180
    stall_calls: int = field(default_factory=lambda: max(2, int(os.getenv("ZHIHUI_STALL_CALLS", "6"))))
    finish_reserve: int = 4
    finish_seconds: float = 12
    stage_calls: dict[str, int] = field(default_factory=lambda: {"planning": 6, "building": 15, "repairing": 12})


@dataclass
class Issue:
    id: str
    code: str
    target_id: str
    message: str
    status: str = "open"
    attempts: int = 0
    blocked: int = 0
    source: str = "tool"


class ExecutionControl:
    def __init__(self, config: ExecutionConfig | None = None, clock=time.monotonic):
        self.config = config or ExecutionConfig()
        self.clock = clock
        self.started = clock()
        self.calls = 0
        self.stage = "building"
        self.stage_used: dict[str, int] = {}
        self.failed: dict[str, dict] = {}
        self.issues: dict[str, Issue] = {}
        self.attempt_log: list[dict] = []
        self.revisions: dict[str, int] = {}
        self.validations: dict[tuple[str, int], dict] = {}
        self.best: dict[str, dict] = {}
        self.positions: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self.frozen: set[tuple[str, str]] = set()
        self.no_revision = 0
        self.no_improvement = 0
        self.previous_score: dict[str, int] = {}
        self.duplicates = 0
        self.oscillations = 0
        self.stalls = 0
        self.rollbacks = 0
        self.playbook = json.loads(PLAYBOOK.read_text(encoding="utf-8"))
        self.available_tools: set[str] = set()

    @property
    def remaining_seconds(self) -> float:
        return max(0., self.config.max_seconds - (self.clock() - self.started))

    def budget_reason(self, *, finishing: bool = False) -> str | None:
        reserve = 0 if finishing else self.config.finish_reserve
        seconds = 0 if finishing else self.config.finish_seconds
        if self.calls >= self.config.max_calls - reserve:
            return "工具预算已用完，转入交付"
        if self.remaining_seconds <= seconds:
            return "执行时间预算已用完，转入交付"
        return None

    def consume(self) -> None:
        if self.budget_reason(finishing=True):
            raise TimeoutError("Execution budget exhausted")
        self.calls += 1
        self.stage_used[self.stage] = self.stage_used.get(self.stage, 0) + 1

    def rule(self, code: str) -> dict:
        if code.startswith("E_STD_TEXT"):
            code = "E_TEXT_OVERLAP"
        return self.playbook.get(code, self.playbook["default"])

    def issue_key(self, name: str, args: dict) -> str:
        target = args.get("equipment_ref") or args.get("target") or args.get("anchor_ref") or args.get("hatch_ref")
        if not target and isinstance(args.get("pick"), dict):
            target = args["pick"].get("anchor")
        if not target and args.get("select"):
            target = ",".join(sorted(args["select"]))
        return f"{args.get('drawing_id', '')}:{name}:{target or ''}"

    def action(self, issue: Issue) -> dict:
        rule = self.rule(issue.code)
        actions = rule["actions"]
        index = min(max(0, issue.attempts + issue.blocked - 1), len(actions) - 1)
        return {"action": actions[index], "escalate": rule["escalate"], "issue_id": issue.id}

    def reject(self, name: str, args: dict, code: str, message: str, **extra) -> dict:
        return {"ok": False, "error_code": code, "message": message, **extra}

    def before(self, name: str, args: dict) -> dict | None:
        if name in {"validate_drawing", "export_artifacts"} or "restore_revision" in args or "include_state" in args:
            return self.reject(name, args, "E_SCHEDULER_ONLY", "校验、导出和回滚仅由程序调度器执行")
        if name == "find_entities" and args.get('kind') != 'title_block_field' and re.search(r"TITLE_BLOCK|设计单位|标题栏", str(args), re.I):
            return self.reject(name, args, "E_CAPABILITY_MISSING", "当前工具目录不能修改标题栏，请确认降级方案", escalate="ask_user")
        try:
            sig = signature(name, args)
        except (ValueError, TypeError):
            return self.reject(name, args, "E_BAD_JSON", "参数必须是有限数值组成的 JSON 对象")
        key = self.issue_key(name, args)
        issue = self.issues.get(key)
        old = self.failed.get(sig)
        if old is not None:
            self.duplicates += 1
            if issue:
                issue.status = "open"
                issue.code = old["code"]
                issue.blocked += 1
            return self.reject(name, args, "E_REPEATED_CALL", "相同失败调用已被阻止，工具未执行",
                original_code=old["code"], **(self.action(issue) if issue else {}))
        if issue and issue.status != "resolved" and issue.code in {"E_HATCH_NO_BOUNDARY", "E_HATCH_ON_BOUNDARY", "E_HATCH_NOT_CLOSED"} and args.get("pick"):
            self.duplicates += 1
            issue.blocked += 1
            return self.reject(name, args, "E_REPEATED_CALL", "填充边界失败后禁止继续猜拾取点，必须使用已定义命名区域或跳过",
                original_code=issue.code, **self.action(issue))
        if issue and issue.status != "resolved" and issue.attempts >= self.rule(issue.code)["max_attempts"]:
            issue.blocked += 1
            return self.reject(name, args, "E_ISSUE_EXHAUSTED", "该问题的修复尝试次数已用完", **self.action(issue))
        target = str(args.get("equipment_ref", ""))
        if (str(args.get("drawing_id", "")), target) in self.frozen and name in {"move_equipment", "edit_equipment"}:
            return self.reject(name, args, "E_OSCILLATION", "对象已冻结，交由确定性布局程序处理", escalate="layout_fixer")
        if name == "connect_ports":
            for it in args.get("items") or []:
                a, b = it.get("from_port", ""), it.get("to_port", "")
                if a and b and a.split(".")[0] == b.split(".")[0]:
                    return self.reject(name, args, "E_SELF_LOOP", "边界物料必须通过 terminal 表达，不能让设备连接自己", escalate="ask_user")
        return None

    def observe(self, name: str, args: dict, result: dict, ms: int = 0, *, executed: bool = True) -> str | None:
        did = str(result.get("drawing_id") or args.get("drawing_id") or "")
        previous = self.revisions.get(did)
        revision = result.get("revision", previous)
        advanced = revision is not None and previous != revision
        if revision is not None:
            self.revisions[did] = revision
        self.no_revision = 0 if advanced else self.no_revision + 1
        code = result.get("error_code", "")
        empty = name == "find_entities" and result.get("ok") and not result.get("candidates")
        effective_failure = not result.get("ok") or empty
        if effective_failure:
            code = code or "E_NOT_FOUND"
            key = self.issue_key(name, args)
            issue = self.issues.get(key)
            if issue is None or issue.status == "resolved":
                issue = Issue(key, code, str(args.get("equipment_ref") or args.get("target") or ""), result.get("message") or result.get("hint", "没有匹配对象"))
                self.issues[key] = issue
            if executed or code not in {"E_REPEATED_CALL", "E_ISSUE_EXHAUSTED"}:
                issue.attempts += 1
            if code not in {"E_REPEATED_CALL", "E_ISSUE_EXHAUSTED"}:
                issue.code = code
                try:
                    self.failed[signature(name, args)] = {"code": code, "issue_id": key}
                except (ValueError, TypeError):
                    issue.code = "E_BAD_JSON"
        else:
            issue = self.issues.get(self.issue_key(name, args))
            if issue:
                issue.status = "resolved"
        try:
            call_signature = signature(name, args)
        except (ValueError, TypeError):
            call_signature = name+":invalid_json"
        self.attempt_log.append({"signature": call_signature, "arguments": args,
            "tool": name, "ok": bool(result.get("ok")), "error_code": code or None,
            "executed": executed, "revision": revision, "ms": ms, "stage": self.stage})
        if name == "move_equipment" and result.get("ok"):
            target = str(args.get("equipment_ref", ""))
            key = (did, target)
            hist = self.positions.setdefault(key, [])
            old, new = result.get("old_position"), result.get("new_position")
            if not hist and old:
                hist.append(tuple(round(v, 1) for v in old))
            if new:
                hist.append(tuple(round(v, 1) for v in new))
            if len(hist) >= 3 and hist[-1] == hist[-3] and hist[-2] != hist[-1]:
                self.frozen.add(key)
                self.oscillations += 1
                return "oscillation"
        if self.no_revision >= self.config.stall_calls:
            self.stalls += 1
            return "stall"
        issue = self.issues.get(self.issue_key(name, args))
        if issue and issue.blocked >= 2:
            return "escalate"
        return None

    def record_validation(self, did: str, revision: int, validation: dict, *, compare: bool = False) -> str | None:
        self.validations[did, revision] = validation
        score = issue_score(validation)
        best = self.best.get(did)
        if usable(validation) and (best is None or score < best["score"]):
            self.best[did] = {"revision": revision, "score": score}
        if not compare:
            return None
        old = self.previous_score.get(did)
        self.previous_score[did] = score
        if old is not None and score >= old:
            self.no_improvement += 1
        else:
            self.no_improvement = 0
        if best is not None and score > best["score"]:
            return "rollback"
        if self.no_improvement >= 3:
            self.oscillations += 1
            return "layout_fixer"
        return None

    def current_validation(self, did: str) -> dict | None:
        return self.validations.get((did, self.revisions.get(did, -1)))

    def needs_rollback(self, did: str) -> bool:
        v, best = self.current_validation(did), self.best.get(did)
        return bool(best and v and issue_score(v) > best["score"])

    def metrics(self) -> dict:
        return {"repeated_calls_blocked": self.duplicates, "oscillations": self.oscillations,
            "stalls": self.stalls, "rollbacks": self.rollbacks, "budget_remaining": max(0, self.config.max_calls - self.calls)}


def compliant_paper(drawing, need_mm: list[float] | None = None) -> str | None:
    """Calculate a supported format on a copy using existing footprint/layout rules."""
    from zhihui.cad.layout import LayoutError, auto_layout
    from zhihui.cad.standards import for_drawing
    from zhihui.cad.symbols import usable_area
    from zhihui.ir import PAPER_SIZES
    candidates = set(PAPER_SIZES)
    limit = int(for_drawing(drawing).raw.get("paper", {}).get("extend_quarters_max", 4))
    for base in ("A3", "A2", "A1", "A0"):
        candidates.update(f"{base}+{n}/4" for n in range(1, limit + 1))
    minimum = "A1" if for_drawing(drawing).id == "hg_pfd" else drawing.paper_size
    min_area = math.prod(PAPER_SIZES[minimum])
    for paper in sorted(candidates, key=lambda p: (math.prod(PAPER_SIZES[p]), PAPER_SIZES[p][0], p)):
        if math.prod(PAPER_SIZES[paper]) < min_area:
            continue
        trial = drawing.model_copy(deep=True)
        trial.paper_size = paper
        box = usable_area(trial)
        if need_mm and ((box[2]-box[0])/trial.unit < need_mm[0] or (box[3]-box[1])/trial.unit < need_mm[1]):
            continue
        try:
            auto_layout(trial, spacing="compact", mode="full")
            return trial.paper_size
        except LayoutError:
            continue
    return None
