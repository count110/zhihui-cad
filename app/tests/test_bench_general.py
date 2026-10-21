"""通用 CAD 基准用例（B/D/A）：用例集本身的完整性，以及 scripted / mock 两种离线模式全部通过。"""
import asyncio
import json
from pathlib import Path

import pytest

import zhihui.tools.general  # noqa: F401
from zhihui.bench.general_cases import expected_calls, run_general_case, steps_to_plan
from zhihui.bench.run import CASE_DIR, load_cases
from zhihui.agent.general.plan import validate_plan
from zhihui.tools.general.registry import REGISTRY

CASES = {k: v for k, v in load_cases().items() if v["category"] in ("B", "D", "A")}


def test_case_counts_and_ids():
    ids = sorted(CASES)
    assert [i for i in ids if i[0] == "B"] == [f"B{n:03d}" for n in range(1, 11)]
    assert [i for i in ids if i[0] == "D"][:17] == [f"D{n:03d}" for n in range(1, 18)]
    assert [i for i in ids if i[0] == "A"][:18] == [f"A{n:03d}" for n in range(1, 19)]


def test_every_function_is_covered_by_at_least_one_case():
    spec_functions = {t.name for t in REGISTRY.values() if t.category not in ("perception", "system", "chem_equipment")}      # 45 项规格函数 + 补充的 erase_entities（化工设备图函数由 E 类用例覆盖）
    perception = {t.name for t in REGISTRY.values() if t.category == "perception"}
    system = {t.name for t in REGISTRY.values() if t.category == "system"}
    covered = {f for c in CASES.values() for f in c["functions"]}
    assert len(spec_functions) == 46
    assert spec_functions <= covered, f"没有基准用例覆盖：{sorted(spec_functions - covered)}"
    assert (perception | system) <= covered, f"感知/系统函数没有用例：{sorted((perception | system) - covered)}"
    called = {t for c in CASES.values() for t in expected_calls(c)}
    assert spec_functions <= called                                     # 不光是声明，期望的函数调用序列里真的有


@pytest.mark.parametrize("cid", sorted(CASES))
def test_case_is_well_formed(cid):
    c = CASES[cid]
    assert c["prompt"] and c["prompt_asr"] and c["steps"] and c["assert"] and c["intents"] is not None
    assert c["prompt_asr"].strip() != "" and c["functions"]
    plan = steps_to_plan(c, {"$XREF_FILE": "x.dxf"})
    assert validate_plan(plan) == [], validate_plan(plan)               # 步骤里的函数名、依赖、识别步骤形状都合法
    for t in expected_calls(c):
        assert t in REGISTRY


@pytest.mark.parametrize("cid", sorted(CASES))
def test_scripted_mode_passes(cid, tmp_path):
    r = asyncio.run(run_general_case(CASES[cid], "scripted", tmp_path))
    assert r["passed"], f"{cid}: {r['reason']}"


MOCK = sorted(c for c in CASES if CASES[c].get("mock_ok"))


@pytest.mark.parametrize("cid", MOCK)
def test_mock_planner_passes_spoken_and_asr_variants(cid, tmp_path):
    """离线规则 Planner 从口令（以及带语音误识别的口令）生成计划，结果断言同样成立；全程不调用模型。"""
    r = asyncio.run(run_general_case(CASES[cid], "mock", tmp_path))
    assert r["passed"] and not r.get("skipped"), f"{cid}: {r['reason']}"
    assert (r.get("usage") or {}).get("prompt_tokens", 0) == 0


def test_mock_mode_reports_uncovered_cases_as_skipped_not_passed(tmp_path):
    unc = next(c for c in CASES.values() if not c.get("mock_ok"))
    r = asyncio.run(run_general_case(unc, "mock", tmp_path))
    assert r.get("skipped") and r["status"] == "SKIPPED"


def test_most_cases_are_covered_by_the_offline_planner():
    n = sum(1 for c in CASES.values() if c.get("mock_ok"))
    assert n >= 0.6 * len(CASES)                                        # 离线规则 Planner 覆盖大部分用例（其余只做 scripted，如实标注）


def test_bench_cli_runs_general_categories(tmp_path):
    import subprocess
    import sys
    out = tmp_path / "r.json"
    env = {**__import__("os").environ, "ZHIHUI_LLM": "mock", "PYTHONUTF8": "1"}
    p = subprocess.run([sys.executable, "-m", "zhihui.bench.run", "--llm", "scripted", "--only", "B001,D003,A016", "--output", str(out)], cwd=str(Path(__file__).resolve().parents[1]),
                       capture_output=True, text=True, env=env, timeout=300)
    assert p.returncode == 0, p.stdout + p.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["summary"]["B"]["passed"] == 1 and data["summary"]["D"]["passed"] == 1 and data["summary"]["A"]["passed"] == 1
