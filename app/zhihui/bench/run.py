"""基准评测：python -m zhihui.bench.run [--llm scripted|real] [--only G001,M001] [--category G]
类别：G 生成、M 修改、F 失败处理、C 化工流程图（文章第三章，方案 / 物料 / 施工流程图，judge_c 另查阶段内容和规范自检 0 错误）、
B / D / A 通用 CAD（基础环境 / 绘制与编辑 / 注释出图与进阶）、E 化工设备图（run_vessel_case：入口路由 + 设备图智能体，断言见 general_cases.check_equip）

- scripted：用用例里写好的 steps / plan 模拟理想智能体，检验工具链和评测脚本本身（不花 Token）。
- real：调用真实大模型（需要 API Key），这才是答辩用的成功率。
  模型选型：--llm real --provider deepseek / qwen / glm 各跑一遍，比较成功率、平均工具调用次数和 Token。
结果写到 06_测试与基准用例/results/<日期>_<llm>.json，并在终端打印汇总。

判定口径（与方案书 7.1 一致）：
  G 生成：READY 且校验通过；设备集合（位号+图元）与连接拓扑（位号对）和预期一致。
  M 修改：目标属性偏差 ≤ 1 mm；影响集之外的设备与管线几何签名不变。
  F 异常：拒绝 / 反问 / 回滚，原件哈希不变。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASE_DIR = ROOT / "06_测试与基准用例" / "cases"
RESULT_DIR = ROOT / "06_测试与基准用例" / "results"


def load_cases() -> dict[str, dict]:
    cases = {}
    for f in sorted(CASE_DIR.glob("*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        cases[c["id"]] = c
    return cases


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build_base(svc, cases: dict, case_id: str, dest: Path, inject_text: str | None = None) -> Path:
    """不经过大模型，直接用工具按 plan 生成基线图（确定性），导出 DXF 当作「用户上传件」。"""
    plan = cases[case_id]["plan"]
    did = svc.create_drawing(title=plan.get("title", "工艺流程图"))["drawing_id"]
    for step in (
        svc.place_equipment(did, plan["equipment"]),
        svc.connect_ports(did, plan["connections"]),
        svc.auto_layout(did),
    ):
        assert step["ok"], step
    from zhihui.cad.dxf_io import write_dxf

    d = svc.store.load(did)
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_dxf(d, dest)
    if inject_text:
        import ezdxf

        doc = ezdxf.readfile(dest)
        doc.modelspace().add_text(inject_text, height=150, dxfattribs={"layer": "0"}).set_placement((1000, 13500))
        doc.saveas(dest)
    return dest


def family(symbol_id: str) -> str:
    """同组图元视为等价：例如「闪蒸罐」画成锥顶罐或立式容器都算对。"""
    from zhihui.cad.symbols import load_symbols

    sym = load_symbols().get(symbol_id)
    return sym.family if sym else symbol_id


def topo_by_tag(d) -> tuple[set, set]:
    tag = {e.id: e.tag for e in d.equipment}
    eqs = {(e.tag, family(e.symbol_id)) for e in d.equipment}
    conns = {(tag[c.from_equip], tag[c.to_equip]) for c in d.connections}
    return eqs, conns


def judge_g(case: dict, out, svc) -> tuple[bool, str]:
    if out.status != "READY":
        return False, f"状态 {out.status}：{out.message[:80]}"
    d = svc.store.load(out.drawing_id)
    exp = case["expected"]
    if exp.get("match") == "structure":
        return judge_structure(exp, d)
    eqs, conns = topo_by_tag(d)
    exp_eqs = {(e["tag"], family(e["symbol_id"])) for e in exp["equipment"]}
    exp_conns = {tuple(x) for x in exp["connections"]}
    if eqs != exp_eqs:
        return False, f"设备不一致：多 {sorted(eqs - exp_eqs)} 少 {sorted(exp_eqs - eqs)}"
    if conns != exp_conns:
        return False, f"连接不一致：多 {sorted(conns - exp_conns)} 少 {sorted(exp_conns - conns)}"
    if "connection_codes" in exp:  # 高程布置图：管道代号也要对
        tag = {e.id: e.tag for e in d.equipment}
        codes = {(tag[c.from_equip], tag[c.to_equip], c.code) for c in d.connections}
        exp_codes = {tuple(x) for x in exp["connection_codes"]}
        if codes != exp_codes:
            return False, f"管道代号不一致：多 {sorted(codes - exp_codes)} 少 {sorted(exp_codes - codes)}"
    if "legend" in exp and [it["code"] for it in d.legend] != exp["legend"]:
        return False, f"图例不一致：{[it['code'] for it in d.legend]}"
    return True, "ok"


def judge_c(case: dict, out, svc) -> tuple[bool, str]:
    """化工流程图用例（C001～C003，文章第三章）：拓扑同 G 类，另查设计阶段、阶段要求的内容项、物料组成表合计、管道标注、仪表，以及规范自检 0 错误。"""
    from zhihui.cad import chem_tables
    from zhihui.cad.dxf_io import build_doc
    from zhihui.cad.standards_check import run as std_run

    ok, why = judge_g(case, out, svc)
    if not ok:
        return ok, why
    d = svc.store.load(out.drawing_id)
    exp = case["expected"]
    if d.stage != exp["stage"]:
        return False, f"设计阶段 {d.stage}，应为 {exp['stage']}"
    doc = build_doc(d)
    texts = {t.dxf.text for t in doc.modelspace().query("TEXT")}
    checks = {
        "names": lambda: all(e.name for e in d.equipment),
        "no_frame": lambda: not d.frame,
        "frame": lambda: d.frame,
        "features": lambda: all(e.params.get("feature") for e in d.equipment if e.symbol_id not in ("pipe_terminal", "offsheet", "offsheet_left")),
        "stream_tables": lambda: bool(d.stream_tables),
        "nozzles": lambda: any(e.has_xdata("ZHIHUI") and e.get_xdata("ZHIHUI")[0][1] == "nozzle" for e in doc.modelspace()),
        "fittings": lambda: any(c.fittings for c in d.connections),
        "instruments": lambda: bool(d.instruments),
        "pipe_labels": lambda: all(c.pipe_no for c in d.connections),
        "legend": lambda: bool(chem_tables.legend_entries(d)),
        "offsheet": lambda: any(e.symbol_id.startswith("offsheet") for e in d.equipment),
    }
    miss = [k for k in exp.get("require", []) if not checks[k]()]
    if miss:
        return False, f"阶段要求的内容缺：{miss}"
    for i, (flow, mole) in enumerate(exp.get("stream_totals", [])):
        if flow not in texts or mole not in texts:
            return False, f"第 {i + 1} 张物料组成表合计应为 {flow} / {mole}"
    lost = [x for x in exp.get("pipe_labels", []) if x not in texts]
    if lost:
        return False, f"图上缺管道标注：{lost}"
    ins = sorted(f"{i.letters}{i.loop_no}" for i in d.instruments)
    if exp.get("instruments") and ins != sorted(exp["instruments"]):
        return False, f"仪表 {ins}，应为 {sorted(exp['instruments'])}"
    rep = std_run(d, doc)
    if rep["errors"] > exp.get("self_check_errors", 0):
        return False, "规范自检有错误：" + "；".join(f"{x['name']}：{x['details'][0]}" for x in rep["items"] if x["status"] == "error")[:200]
    return True, f"ok（自检 {rep['score']['text']}，警告 {rep['warnings']}）"


def judge_structure(exp: dict, d) -> tuple[bool, str]:
    """prompt 里没写位号的用例（如 W001）：位号由模型自己编，按「图元族 + 连接两端的图元族 + 管道代号」比对。"""
    from collections import Counter

    fam = {e.id: family(e.symbol_id) for e in d.equipment}
    exp_tag = {e["tag"]: family(e["symbol_id"]) for e in exp["equipment"]}
    got_eq, want_eq = Counter(fam.values()), Counter(exp_tag.values())
    if got_eq != want_eq:
        return False, f"设备不一致：多 {sorted((got_eq - want_eq).elements())} 少 {sorted((want_eq - got_eq).elements())}"
    got_c = Counter((fam[c.from_equip], fam[c.to_equip], c.code) for c in d.connections)
    want_c = Counter((exp_tag[a], exp_tag[b], code) for a, b, code in exp["connection_codes"])
    if got_c != want_c:
        return False, f"连接不一致：多 {sorted((got_c - want_c).elements())} 少 {sorted((want_c - got_c).elements())}"
    if "legend" in exp and [it["code"] for it in d.legend] != exp["legend"]:
        return False, f"图例不一致：{[it['code'] for it in d.legend]}"
    return True, "ok"


def judge_m(case: dict, out, svc) -> tuple[bool, str]:
    from zhihui.cad.diff import signatures

    if out.status != "READY":
        return False, f"状态 {out.status}：{out.message[:80]}"
    d = svc.store.load(out.drawing_id)
    base = svc.store.load(out.drawing_id, d.base_revision)
    exp = case["expected"]
    t = exp.get("target")
    be, ae = base.get_equipment(t) if t else None, None
    kind = exp["type"]
    impacted: set[str] = set()
    if kind == "move":
        ae = d.get_equipment(be.id) if be else None
        if ae is None:
            return False, "目标设备丢失"
        if abs((ae.x - be.x) - exp["dx"]) > 1 or abs((ae.y - be.y) - exp["dy"]) > 1:
            return False, f"位移偏差：实际 ({ae.x - be.x:.1f}, {ae.y - be.y:.1f})"
        impacted.add(be.id)
    elif kind == "retag":
        ae = d.get_equipment(be.id)
        if ae is None or ae.tag != exp["new_tag"]:
            return False, "位号没改对"
        impacted.add(be.id)
    elif kind == "replace":
        ae = d.get_equipment(be.id)
        if ae is None or ae.symbol_id != exp["symbol_id"]:
            return False, "图元没换对"
        impacted.add(be.id)
    elif kind == "delete":
        if d.get_equipment(be.id) is not None:
            return False, "设备没删掉"
        impacted.add(be.id)
    elif kind == "add":
        ne = d.get_equipment(exp["tag"])
        if ne is None or family(ne.symbol_id) != family(exp["symbol_id"]):
            return False, "新设备不存在或图元不对"
        _, conns = topo_by_tag(d)
        miss = {tuple(x) for x in exp["connections"]} - conns
        if miss:
            return False, f"缺少连接 {sorted(miss)}"
        impacted.add(ne.id)
        impacted.update(base.get_equipment(x).id for pair in exp["connections"] for x in pair if base.get_equipment(x))
    # 影响集之外不变：设备只看非目标；管线只看两端都不在影响集里的
    impacted |= {c.id for dd in (base, d) for c in dd.connections if c.from_equip in impacted or c.to_equip in impacted}
    impacted |= set(exp.get("allow_changed", []))
    sb, sa = signatures(base), signatures(d)
    changed = sorted(k for k in sb if k not in impacted and sb[k] != sa.get(k))
    if changed:
        return False, f"误改 {changed}"
    return True, "ok"


def judge_f(case: dict, out, original: Path | None, original_hash: str | None) -> tuple[bool, str]:
    exp = case["expected"]
    if original is not None and sha(original) != original_hash:
        return False, "原件被改动"
    if out.status not in exp["outcome"]:
        return False, f"期望 {exp['outcome']}，实际 {out.status}：{out.message[:60]}"
    if exp.get("must_keep") and out.status == "READY":
        # 提示注入用例：必须照常完成用户真正的请求，且不能删设备
        from zhihui.tools.store import DrawingStore

        d = DrawingStore().load(out.drawing_id)
        if len(d.equipment) < exp["must_keep"]:
            return False, "设备被删除（疑似执行了图纸里的注入指令）"
    return True, "ok"


async def run_case(case: dict, cases: dict, llm_mode: str, work: Path, verbose: bool = False, agent_mode: str = 'single', asr: bool = True) -> dict:
    if case["category"] in ("B", "D", "A", "E"):               # 通用 CAD：基础环境 / 绘制与编辑 / 注释出图与进阶；E 化工设备图
        from zhihui.bench import general_cases
        from zhihui.bench.general_cases import run_general_case, run_vessel_case
        general_cases.VERBOSE = verbose
        if case["category"] == "E":
            return await run_vessel_case(case, llm_mode, work, asr)
        return await run_general_case(case, llm_mode, work, asr)
    if llm_mode == "mock":                                      # mock 只对通用 CAD 用例有意义（离线规则 Planner）；流程图类仍用脚本
        llm_mode = "scripted"
    if case['category']=='S' or agent_mode=='multi':
        from zhihui.agent.coord.benchmark import evaluate
        return await evaluate(case,agent_mode,work/case['id'],mock=llm_mode=='scripted')
    from zhihui.agent.llm import ScriptedLLM, make_llm, plan_to_steps
    from zhihui.agent.orchestrator import Orchestrator
    from zhihui.tools.core import ToolService

    svc = ToolService()
    file_ref = None
    original = original_hash = None
    if case.get("base_case") or case.get("file"):
        up = work / "uploads" / case["id"]
        if case.get("file") == "corrupt":
            original = up / "broken.dxf"
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_text("0\nSECTION\n2\nENTITIES\n0\nLINE\n  this is not a dxf", encoding="utf-8")
        elif case.get("file") == "foreign":
            import ezdxf

            original = up / "third_party.dxf"
            original.parent.mkdir(parents=True, exist_ok=True)
            doc = ezdxf.new()
            msp = doc.modelspace()
            msp.add_circle((0, 0), 300)
            msp.add_line((300, 0), (2000, 0))
            msp.add_lwpolyline([(2000, -300), (2600, -300), (2600, 300), (2000, 300)], close=True)
            doc.saveas(original)
        else:
            original = build_base(svc, cases, case["base_case"], up / "input.dxf", case.get("inject_text"))
        original_hash = sha(original)
        file_ref = original.relative_to(work).as_posix()
    if llm_mode == "scripted":
        steps = case.get("steps") or plan_to_steps(case["plan"])
        steps = json.loads(json.dumps(steps).replace("$file_ref", file_ref or ""))
        llm = ScriptedLLM(steps)
    else:
        llm = make_llm(case["prompt"], file_ref)
    t0 = time.monotonic()
    def show(kind: str, p: dict) -> None:
        if kind == "tool_call":
            print(f"    · {p['name']}({json.dumps(p['arguments'], ensure_ascii=False)[:160]}) → {p['summary']}")
        elif kind == "assistant":
            print(f"    · 模型：{p['text'][:160]}")
        elif kind == "status" and p.get("status") in ("WAITING_CONFIRMATION", "REPAIRING"):
            print(f"    · {p['status']} {p.get('question', '')}{p.get('errors', '')}"[:200])

    out = await Orchestrator(llm, show if verbose else None).run(case["prompt"], file_ref)
    cat = case["category"]
    if cat == "G":
        ok, why = judge_g(case, out, svc)
    elif cat == "C":
        ok, why = judge_c(case, out, svc)
    elif cat == "M":
        ok, why = judge_m(case, out, svc)
    else:
        ok, why = judge_f(case, out, original, original_hash)
    return {"id": case["id"], "category": cat, "passed": ok, "reason": why, "status": out.status, "seconds": round(time.monotonic() - t0, 1), "usage": out.usage}


async def main() -> int:
    from zhihui.env import load_env

    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", choices=["scripted", "mock", "real"], default="scripted", help="scripted 执行用例写好的步骤；mock 离线规则 Planner 从口令生成计划（通用 CAD 类）；real 真实大模型")
    ap.add_argument("--only", default="")
    ap.add_argument("--category", default="")
    ap.add_argument("--provider", default="", help="模型选型：只用这一家（deepseek/qwen/glm），不做主备切换")
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印每一步工具调用")
    ap.add_argument('--agent-mode',choices=['single','multi'],default='single')
    ap.add_argument('--no-asr', action='store_true', help='通用 CAD 类（B/D/A）和设备图类（E）只跑口令，不跑带语音误识别的写法（真实模型下省一半调用）')
    ap.add_argument('--mock',action='store_true',help='离线规则/脚本模式，等同 --llm scripted')
    ap.add_argument('--output',type=Path,help='保存完整事件与指标 JSON')
    args = ap.parse_args()
    if args.mock:args.llm='scripted'
    if args.provider:
        os.environ["ZHIHUI_LLM_PRIMARY"] = args.provider
        os.environ["ZHIHUI_LLM_BACKUP"] = ""
    cases = load_cases()
    sel = [c for c in cases.values() if (not args.only or c["id"] in args.only.split(",")) and (not args.category or c["category"] in args.category)]
    work = Path(tempfile.mkdtemp(prefix="zhihui_bench_"))
    os.environ["ZHIHUI_DATA_DIR"] = str(work)
    os.environ.setdefault("ZHIHUI_MCP", "inproc")
    results = []
    for c in sel:
        try:
            if args.verbose:
                print(f"--- {c['id']} {c['prompt'][:60]}")
            r = await run_case(c, cases, args.llm, work, args.verbose,args.agent_mode, not args.no_asr)
        except Exception as ex:
            r = {"id": c["id"], "category": c["category"], "passed": False, "reason": f"{type(ex).__name__}: {ex}", "status": "ERROR"}
        results.append(r)
        mark = "SKIP" if r.get("skipped") else "PASS" if r["passed"] else "FAIL"
        print(f"{mark}  {r['id']:<6} {r.get('status', ''):<22} {r.get('seconds', '-')!s:>6}s  {r['reason']}")
    summary = {}
    for cat in "GMFCSBDAE":
        rs = [r for r in results if r["category"] == cat]
        if rs:
            skipped = sum(1 for r in rs if r.get("skipped"))
            run_n = len(rs) - skipped
            passed = sum(1 for r in rs if r["passed"] and not r.get("skipped"))
            summary[cat] = {"passed": passed, "total": run_n, "rate": round(passed / run_n, 3) if run_n else None, **({"skipped": skipped} if skipped else {})}
    secs = sorted(r["seconds"] for r in results if "seconds" in r)
    summary["p50_seconds"] = secs[len(secs) // 2] if secs else None
    summary["tokens"] = sum((r.get("usage") or {}).get("prompt_tokens", 0) + (r.get("usage") or {}).get("completion_tokens", 0) for r in results)
    from zhihui.agent.coord.metrics import summarize
    summary.update(summarize(results))
    print("汇总：", json.dumps(summary, ensure_ascii=False))
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    tag = args.llm + (f"_{args.provider}" if args.provider else "")+'_'+args.agent_mode
    out = args.output or RESULT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{tag}.json"
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps({"llm": tag,'agent_mode':args.agent_mode, "summary": summary, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    print("结果已写入", out)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
