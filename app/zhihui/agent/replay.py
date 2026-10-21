"""Read-only SQLite trace export and executable offline fault replay.

Exported cases keep the original event payloads separately from normalized script
arguments. No inferred tool result is presented as an observed database result.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASE_DIR = ROOT / "06_测试与基准用例" / "cases"
SCENARIOS = {
    "S001": ("修改设计单位时盲搜标题栏", ("设计单位",)),
    "S002": ("填充失败后重复拾取", ("填充",)),
    "S003": ("PFD-01 几何振荡及设备自环", ("PFD-01",)),
    "S004": ("未确认的菜市场污水工艺", ("菜市场", "污水")),
    "S005": ("修复问题时缺少标注操作及明确目标", ("修复问题",)),
}
ACCEPTED = ["READY", "SUCCEEDED", "READY_WITH_WARNINGS", "WAITING_CONFIRMATION", "ASK_USER"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_cases(database: Path, output: Path, task_ids: list[str] | None = None) -> list[Path]:
    database = database.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=True)
    db_hash = sha256(database)
    # mode=ro prevents SQLite from silently creating a missing/empty database.
    conn = sqlite3.connect(database.as_uri()+"?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    paths = []
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY created_at, id")]
        for index, (sid, (title, keywords)) in enumerate(SCENARIOS.items()):
            if task_ids:
                candidates = [r for r in rows if r["id"] == task_ids[index]] if index < len(task_ids) else []
            else:
                candidates = [r for r in rows if r["status"] in {"FAILED", "WAITING_CONFIRMATION"} and all(k in r["prompt"] for k in keywords)]
            if not candidates:
                continue
            task = candidates[0]
            events = [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], "payload": json.loads(r["payload_json"])}
                for r in conn.execute("SELECT id,ts,kind,payload_json FROM events WHERE task_id=? ORDER BY id", (task["id"],))]
            calls = [e for e in events if e["kind"] == "tool_call"]
            inputs: dict[str, str] = {}
            input_records: list[dict] = []

            def normalize(v, key=""):
                if key == "drawing_id" and isinstance(v, str):
                    return "$drawing_id"
                if key == "file_ref" and isinstance(v, str):
                    if v not in inputs:
                        source = (database.parent/v).resolve()
                        if not source.is_relative_to(database.parent):
                            raise ValueError("Trace input escapes data directory")
                        token = "$file_ref" if not inputs else f"$file_ref_{len(inputs)+1}"
                        inputs[v] = token
                        target = output / "replay_inputs" / f"{sid}_{len(inputs)}.dxf"
                        record = {"token": token, "original_file_ref": v, "available": source.is_file()}
                        if source.is_file():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(source, target)
                            record.update(path=target.relative_to(output).as_posix(), sha256=sha256(target))
                        input_records.append(record)
                    return inputs[v]
                if isinstance(v, dict):
                    return {k: normalize(x, k) for k, x in v.items()}
                if isinstance(v, list):
                    return [normalize(x) for x in v]
                return v

            if task.get("file_ref"):
                normalize(task["file_ref"], "file_ref")
            steps = [[{"name": e["payload"]["name"], "arguments": normalize(e["payload"].get("arguments", {}))}] for e in calls]
            result = json.loads(task.get("result_json") or "{}")
            if result.get("question"):
                q = result["question"]
                steps.append({"ask": q.get("question", "请确认方案"), "options": q.get("options", [])})
            else:
                steps.append({"say": "回放的历史工具序列已结束，请程序审图并交付。"})
            case = {"id": sid, "category": "S", "name": title, "prompt": task["prompt"],
                "source": {"type": "sqlite_events", "task_id": task["id"], "database_sha256": db_hash,
                    "original_status": task["status"], "observed_tool_calls": len(calls),
                    "tool_counts": dict(Counter(e["payload"]["name"] for e in calls)),
                    "reported_usage": result.get("usage", {}),
                    "evidence_limits": "原库事件保存参数和摘要；缺失完整返回值时不补造。事件可能跨多次用户续接；reported_usage 为最后一次执行的统计。"},
                "historical_events": events, "historical_result": result,
                "inputs": input_records, "steps": steps,
                "expected": {"outcome": ACCEPTED, "max_tool_calls": 40, "max_seconds": 180}}
            target = output/f"{sid}.json"
            target.write_text(json.dumps(case, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            paths.append(target)
    finally:
        conn.close()
    return paths


async def run_case(case: dict, data: Path, case_dir: Path = CASE_DIR) -> dict:
    from .llm import ScriptedLLM
    from .orchestrator import Orchestrator
    data.mkdir(parents=True, exist_ok=True)
    replacements = {}
    for item in case.get("inputs", []):
        if not item["available"]:
            raise FileNotFoundError("Replay needs original input: "+item["original_file_ref"])
        src = (case_dir/item["path"]).resolve()
        if not src.is_relative_to(case_dir.resolve()) or sha256(src) != item["sha256"]:
            raise ValueError("Replay input path or checksum is invalid")
        dest = data/"uploads"/case["id"]/src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        replacements[item["token"]] = dest.relative_to(data).as_posix()

    def sub(v):
        if isinstance(v, str):
            return replacements.get(v, v)
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v

    old_data, old_mcp = os.environ.get("ZHIHUI_DATA_DIR"), os.environ.get("ZHIHUI_MCP")
    os.environ["ZHIHUI_DATA_DIR"] = str(data.resolve())
    os.environ["ZHIHUI_MCP"] = "inproc"
    events = []
    started = time.monotonic()
    try:
        orch = Orchestrator(ScriptedLLM(sub(case["steps"])), lambda k, p: events.append({"kind": k, **p}))
        outcome = await orch.run(case["prompt"], replacements.get("$file_ref"))
        elapsed = time.monotonic()-started
        expected = case["expected"]
        passed = (outcome.status in expected["outcome"] and outcome.usage["tool_calls"] <= expected["max_tool_calls"]
                  and elapsed <= expected["max_seconds"])
        if outcome.status == "WAITING_CONFIRMATION":
            passed = passed and bool(outcome.question and 2 <= len(outcome.question.get("options", [])) <= 4)
        if outcome.status in {"READY", "READY_WITH_WARNINGS"}:
            passed = passed and any(a["format"] == "dxf" and (data/a["file_ref"]).is_file() for a in outcome.artifacts)
        return {"id": case["id"], "category": "S", "passed": bool(passed), "reason": outcome.message,
            "status": outcome.status, "seconds": round(elapsed, 3), "usage": outcome.usage,
            "historical_tool_counts": case["source"]["tool_counts"],
            "executed_tool_counts": dict(Counter(e["name"] for e in events if e["kind"] == "tool_call")),
            "question": outcome.question, "attempt_log": orch.control.attempt_log,
            "events": events, "source_task_id": case["source"]["task_id"]}
    finally:
        for name, old in (("ZHIHUI_DATA_DIR", old_data), ("ZHIHUI_MCP", old_mcp)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export", help="只读导出真实任务及输入图纸")
    export.add_argument("--db", type=Path, required=True)
    export.add_argument("--output", type=Path, default=CASE_DIR)
    export.add_argument("--task-ids", help="按 S001~S005 顺序指定逗号分隔的任务 ID")
    run = subparsers.add_parser("run", help="离线 Scripted Mock 故障回放，不需要 API Key")
    run.add_argument("--mock", action="store_true", help="显式声明离线模式（回放始终使用本地脚本）")
    run.add_argument("--case-dir", type=Path, default=CASE_DIR)
    run.add_argument("--only", default="S001,S002,S003,S004,S005")
    run.add_argument("--output", type=Path)
    run.add_argument("--data-dir", type=Path, help="保留回放图纸和导出物的目录")
    args = parser.parse_args()
    if args.command == "export":
        try:
            paths = export_cases(args.db, args.output, args.task_ids.split(",") if args.task_ids else None)
        except (OSError, sqlite3.Error, ValueError) as ex:
            parser.exit(2, f"不能导出真实记录：{ex}\n")
        for path in paths:
            print(path)
        return 0 if len(paths) == 5 else 1
    async def run_all(data):
        results = []
        for sid in args.only.split(","):
            if not re_full_case_id(sid):
                raise ValueError("Invalid case ID: "+sid)
            case = json.loads((args.case_dir/f"{sid}.json").read_text(encoding="utf-8"))
            result = await run_case(case, data/sid, args.case_dir)
            results.append(result)
            print(f"{'PASS' if result['passed'] else 'FAIL'} {sid} {result['status']} calls={result['usage']['tool_calls']} seconds={result['seconds']}")
        return results
    if args.data_dir:
        results = asyncio.run(run_all(args.data_dir))
    else:
        with tempfile.TemporaryDirectory(prefix="zhihui_replay_") as tmp:
            results = asyncio.run(run_all(Path(tmp)))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"mode": "single", "llm": "scripted_mock", "results": results}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return 0 if all(r["passed"] for r in results) else 1


def re_full_case_id(value: str) -> bool:
    import re
    return re.fullmatch(r"S\d{3}", value) is not None


if __name__ == "__main__":
    raise SystemExit(main())
