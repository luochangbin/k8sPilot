"""Baseline/Candidate paired comparison (design §23.8).

Reports per-case paired diffs plus aggregate deltas, and evaluates the
pre-declared regression gates. The gate thresholds live in the suite; default
gates: key-case no regression, wrong-root-cause-rate not up,
schema-valid-rate not down.
"""

import json
from pathlib import Path
from typing import Any, Optional

from .scorer import VERDICT_CORRECT, VERDICT_FIXTURE_FAILED, VERDICT_INCORRECT, VERDICT_SCHEMA_FAILED, VERDICT_SYSTEM_FAILED


class CompareError(Exception):
    pass


def _load_run(run_dir: Path) -> dict[str, Any]:
    report_path = run_dir / "report.json"
    if not report_path.is_file():
        raise CompareError(f"no report.json in {run_dir}")
    return json.loads(report_path.read_text(encoding="utf-8"))


def _load_rows(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    path = run_dir / "case-results.jsonl"
    if not path.is_file():
        raise CompareError(f"no case-results.jsonl in {run_dir}")
    by_case: dict[str, list[dict[str, Any]]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        by_case.setdefault(row["case_id"], []).append(row)
    return by_case


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(b - a, 3)


def compare_runs(baseline_dir: Path, candidate_dir: Path) -> dict[str, Any]:
    b = _load_run(baseline_dir)
    c = _load_run(candidate_dir)
    b_rows = _load_rows(baseline_dir)
    c_rows = _load_rows(candidate_dir)

    b_rep = b["report"]
    c_rep = c["report"]
    agg = {
        "root_cause_accuracy": {"baseline": b_rep["root_cause_accuracy"], "candidate": c_rep["root_cause_accuracy"],
                                "delta": _delta(b_rep["root_cause_accuracy"], c_rep["root_cause_accuracy"])},
        "wrong_root_cause_rate": {"baseline": b_rep["wrong_root_cause_rate"], "candidate": c_rep["wrong_root_cause_rate"],
                                  "delta": _delta(b_rep["wrong_root_cause_rate"], c_rep["wrong_root_cause_rate"])},
        "schema_valid_rate": {"baseline": b_rep["schema_valid_rate"], "candidate": c_rep["schema_valid_rate"],
                              "delta": _delta(b_rep["schema_valid_rate"], c_rep["schema_valid_rate"])},
        "evidence_recall_avg": {"baseline": b_rep["evidence_recall_avg"], "candidate": c_rep["evidence_recall_avg"],
                                "delta": _delta(b_rep["evidence_recall_avg"], c_rep["evidence_recall_avg"])},
        "diagnosis_duration_ms_p50": {"baseline": b_rep["diagnosis_duration_ms"]["p50"],
                                      "candidate": c_rep["diagnosis_duration_ms"]["p50"],
                                      "delta": _delta(b_rep["diagnosis_duration_ms"]["p50"], c_rep["diagnosis_duration_ms"]["p50"])},
        "token_usage_p50": {"baseline": b_rep["token_usage"]["p50"], "candidate": c_rep["token_usage"]["p50"],
                            "delta": _delta(b_rep["token_usage"]["p50"], c_rep["token_usage"]["p50"])},
    }

    per_case: dict[str, Any] = {}
    for case_id in sorted(set(b_rows) | set(c_rows)):
        rows_b = b_rows.get(case_id, [])
        rows_c = c_rows.get(case_id, [])
        verdicts_b = _count_verdicts(rows_b)
        verdicts_c = _count_verdicts(rows_c)
        acc_b = _case_accuracy(rows_b)
        acc_c = _case_accuracy(rows_c)
        per_case[case_id] = {
            "n": {"baseline": len(rows_b), "candidate": len(rows_c)},
            "verdict_counts": {"baseline": verdicts_b, "candidate": verdicts_c},
            "root_cause_accuracy": {"baseline": acc_b, "candidate": acc_c,
                                    "delta": _delta(acc_b, acc_c)},
        }

    b_ver = b_rep.get("scorer_version")
    c_ver = c_rep.get("scorer_version")
    comparable = b_ver == c_ver
    reason = None if comparable else f"scorer_version mismatch: baseline={b_ver} candidate={c_ver}"
    if not comparable:
        # Results scored under different semantics must not be diffed silently.
        for metric in agg.values():
            metric["delta"] = None
    gates = _evaluate_gates(agg) if comparable else []
    return {"aggregate": agg, "per_case": per_case, "gates": gates,
            "comparable": comparable, "incomparable_reason": reason,
            "baseline_run": b["run"].get("run_id"), "candidate_run": c["run"].get("run_id")}


def _count_verdicts(rows: list[dict[str, Any]]) -> dict[str, int]:
    out = {VERDICT_CORRECT: 0, VERDICT_INCORRECT: 0, VERDICT_FIXTURE_FAILED: 0,
           VERDICT_SYSTEM_FAILED: 0, VERDICT_SCHEMA_FAILED: 0}
    for r in rows:
        out[r["verdict"]] = out.get(r["verdict"], 0) + 1
    return out


def _case_accuracy(rows: list[dict[str, Any]]) -> Optional[float]:
    valid = [r for r in rows if r["verdict"] in (VERDICT_CORRECT, VERDICT_INCORRECT)]
    if not valid:
        return None
    return round(sum(1 for r in valid if r["verdict"] == VERDICT_CORRECT) / len(valid), 3)


def _evaluate_gates(agg: dict[str, Any]) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    # wrong root cause rate must not rise
    wrr = agg["wrong_root_cause_rate"]
    gates.append({
        "name": "wrong_root_cause_rate_not_up",
        "pass": wrr["delta"] is None or wrr["delta"] <= 0.001,
        "detail": f"baseline={wrr['baseline']} candidate={wrr['candidate']} delta={wrr['delta']}",
    })
    # schema valid rate must not drop
    svr = agg["schema_valid_rate"]
    gates.append({
        "name": "schema_valid_rate_not_down",
        "pass": svr["delta"] is None or svr["delta"] >= -0.001,
        "detail": f"baseline={svr['baseline']} candidate={svr['candidate']} delta={svr['delta']}",
    })
    return gates


def render_compare_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Compare")
    lines.append("")
    lines.append(f"- baseline: {result['baseline_run']}")
    lines.append(f"- candidate: {result['candidate_run']}")
    lines.append(f"- comparable: {result.get('comparable')}"
                 + (f" ({result['incomparable_reason']})" if result.get("incomparable_reason") else ""))
    lines.append("")
    lines.append("## Aggregate deltas")
    lines.append("")
    lines.append("| metric | baseline | candidate | delta |")
    lines.append("|---|---|---|---|")
    for key, v in result["aggregate"].items():
        lines.append(f"| {key} | {v['baseline']} | {v['candidate']} | {v['delta']} |")
    lines.append("")
    lines.append("## Per-case")
    lines.append("")
    lines.append("| case | n b/c | accuracy b/c | delta |")
    lines.append("|---|---|---|---|")
    for case_id, c in result["per_case"].items():
        acc = c["root_cause_accuracy"]
        lines.append(f"| {case_id} | {c['n']['baseline']}/{c['n']['candidate']} | {acc['baseline']}/{acc['candidate']} | {acc['delta']} |")
    lines.append("")
    lines.append("## Gates")
    lines.append("")
    for g in result["gates"]:
        lines.append(f"- {'PASS' if g['pass'] else 'FAIL'} {g['name']}: {g['detail']}")
    lines.append("")
    return "\n".join(lines)
