"""Baseline/Candidate paired comparison (design §23.8).

Reports per-case paired diffs plus aggregate deltas, and evaluates the
pre-declared regression gates. The gate thresholds live in the suite; default
gates: key-case no regression, wrong-root-cause-rate not up,
schema-valid-rate not down.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from .cases import case_key
from .scorer import (budget_signature, budget_signature_unknown,
                     VERDICT_CORRECT, VERDICT_FIXTURE_FAILED, VERDICT_INCORRECT,
                     VERDICT_SCHEMA_FAILED, VERDICT_SYSTEM_FAILED)


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
        by_case.setdefault(case_key(row), []).append(row)
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
    def _pair(key: str, *, b_rep=b_rep, c_rep=c_rep) -> dict[str, Any]:
        return {"baseline": b_rep.get(key), "candidate": c_rep.get(key),
                "delta": _delta(b_rep.get(key), c_rep.get(key))}

    agg = {
        "end_to_end_correct_rate": _pair("end_to_end_correct_rate"),
        "answerable_coverage": _pair("answerable_coverage"),
        "abstention_recall": _pair("abstention_recall"),
        "system_failed_rate": _pair("system_failed_rate"),
        "budget_exhausted_rate": _pair("budget_exhausted_rate"),
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
    b_set = _case_set(b["run"], _flat_rows(b_rows))
    c_set = _case_set(c["run"], _flat_rows(c_rows))
    b_vocab = b["run"].get("root_cause_vocabulary_version")
    c_vocab = c["run"].get("root_cause_vocabulary_version")

    reasons: list[str] = []
    if b_ver != c_ver:
        reasons.append(f"scorer_version mismatch: baseline={b_ver} candidate={c_ver}")
    # Same scorer is not enough: a suite re-pointed at new case definitions (or a
    # changed root-cause vocabulary) measures something different.
    if b_set != c_set:
        reasons.append(
            "case_set mismatch: only differing ids are listed "
            f"baseline_only={sorted(b_set - c_set)} candidate_only={sorted(c_set - b_set)}")
    # Paired comparison needs the same number of attempts per case on both
    # sides; 12x5 vs 12x1 is not a paired delta.
    b_counts = Counter(case_key(r) for r in _flat_rows(b_rows))
    c_counts = Counter(case_key(r) for r in _flat_rows(c_rows))
    if b_counts != c_counts:
        reasons.append("case_attempt_count_mismatch: " + json.dumps(
            {"baseline": dict(sorted(b_counts.items())),
             "candidate": dict(sorted(c_counts.items()))}, sort_keys=True))
    # Effective budgets (min(case, service)) change coverage/abstention/cost, so
    # they must match per case; unknown must not be treated as same.
    budget_reason = _budget_mismatch(b_rows, c_rows)
    if budget_reason:
        reasons.append(budget_reason)
    # Case/fixture content hashes are audit-only, but when both sides record
    # them and they differ, the "same case version" claim is not substantiated.
    hash_reason = _hash_mismatch(b["run"], c["run"])
    if hash_reason:
        reasons.append(hash_reason)

    if not b_vocab or not c_vocab:
        # "Unknown" must never be treated as "same": without a recorded
        # vocabulary version the comparison cannot be proven valid.
        reasons.append(
            f"root_cause_vocabulary_version missing: baseline={b_vocab} candidate={c_vocab}")
    elif b_vocab != c_vocab:
        reasons.append(
            f"root_cause_vocabulary mismatch: baseline={b_vocab} candidate={c_vocab}")
    comparable = not reasons
    reason = None if comparable else "; ".join(reasons)
    if not comparable:
        # Results scored under different semantics must not be diffed silently:
        # this includes the per-case deltas, which are computed earlier.
        for metric in agg.values():
            metric["delta"] = None
        for case in per_case.values():
            case["root_cause_accuracy"]["delta"] = None
    critical = sorted(set(b["run"].get("critical_cases") or [])
                      | set(c["run"].get("critical_cases") or []))
    gates = _evaluate_gates(agg, per_case, critical) if comparable else []
    return {"aggregate": agg, "per_case": per_case, "gates": gates,
            "comparable": comparable, "incomparable_reason": reason,
            "baseline_run": b["run"].get("run_id"), "candidate_run": c["run"].get("run_id")}


def _budget_mismatch(b_rows: dict[str, list[dict[str, Any]]],
                     c_rows: dict[str, list[dict[str, Any]]]) -> Optional[str]:
    """Per-case effective budgets must be identical (and known) on both sides."""
    def budgets(rows_by_case: dict[str, list[dict[str, Any]]]) -> dict[str, set]:
        # A fixture failure means no investigation ran, so it has no effective
        # budget to compare (the case is reported separately as fixture_failed).
        return {
            key: {budget_signature(r) for r in rows if r.get("fixture_ready")}
            for key, rows in rows_by_case.items()
        }

    b_budgets = budgets(b_rows)
    c_budgets = budgets(c_rows)
    diffs = {}
    for key in sorted(set(b_budgets) & set(c_budgets)):
        if b_budgets[key] != c_budgets[key]:
            diffs[key] = {"baseline": sorted(map(str, b_budgets[key])),
                          "candidate": sorted(map(str, c_budgets[key]))}
    # Check BOTH sides (merging would hide one side) and every element of the
    # signature, including the finalization budget.
    unknown = sorted(
        key for key in (set(b_budgets) | set(c_budgets)) if b_budgets.get(key) or c_budgets.get(key)
        if any(budget_signature_unknown(signature)
               for signature in (b_budgets.get(key, set()) | c_budgets.get(key, set())))
    )
    if unknown:
        return "effective_budget_unknown: " + ",".join(unknown)
    if diffs:
        return "effective_budget_mismatch: " + json.dumps(diffs, sort_keys=True)
    return None


def _hash_mismatch(b_meta: dict[str, Any], c_meta: dict[str, Any]) -> Optional[str]:
    b_hashes = b_meta.get("case_hashes")
    c_hashes = c_meta.get("case_hashes")
    if not b_hashes or not c_hashes:
        return None
    if b_hashes != c_hashes:
        keys = sorted(set(b_hashes) | set(c_hashes))
        diff = [k for k in keys if b_hashes.get(k) != c_hashes.get(k)]
        return "case_content_mismatch: " + ",".join(diff[:10])
    return None


def _flat_rows(rows_by_case: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [row for rows in rows_by_case.values() for row in rows]


def _case_set(run_meta: dict[str, Any], rows: list[dict[str, Any]]) -> set[str]:
    """The measured case set as `id@version` (declared by run.json, or derived
    from the scored rows for runs written before that field existed)."""
    declared = run_meta.get("cases")
    if declared:
        return {str(item) for item in declared}
    return {f"{r.get('case_id')}@{r.get('case_version')}" for r in rows}


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


def _gate(name: str, metric: dict[str, Any], *, direction: str) -> dict[str, Any]:
    """A directional regression gate: not_down or not_up (small tolerance)."""
    delta = metric.get("delta")
    if delta is None:
        passed = True  # nothing to compare (missing metric on one side)
    elif direction == "not_down":
        passed = delta >= -0.001
    else:
        passed = delta <= 0.001
    return {"name": name, "pass": passed,
            "detail": (f"baseline={metric.get('baseline')} candidate={metric.get('candidate')} "
                       f"delta={delta}")}


def _evaluate_gates(agg: dict[str, Any], per_case: dict[str, Any],
                    critical_cases: list[str]) -> list[dict[str, Any]]:
    gates = [
        _gate("end_to_end_correct_rate_not_down", agg["end_to_end_correct_rate"],
              direction="not_down"),
        _gate("wrong_root_cause_rate_not_up", agg["wrong_root_cause_rate"],
              direction="not_up"),
        _gate("answerable_coverage_not_down", agg["answerable_coverage"],
              direction="not_down"),
        _gate("abstention_recall_not_down", agg["abstention_recall"], direction="not_down"),
        _gate("system_failed_rate_not_up", agg["system_failed_rate"], direction="not_up"),
        _gate("budget_exhausted_rate_not_up", agg["budget_exhausted_rate"],
              direction="not_up"),
        _gate("schema_valid_rate_not_down", agg["schema_valid_rate"], direction="not_down"),
    ]
    # Declared critical cases must not regress (no weighted score, just a gate).
    regressed = []
    for key in critical_cases:
        case = per_case.get(key)
        if not case:
            regressed.append(f"{key}:missing")
            continue
        delta = case["root_cause_accuracy"]["delta"]
        if delta is not None and delta < -0.001:
            regressed.append(f"{key}:{delta}")
    gates.append({
        "name": "critical_cases_no_regression",
        "pass": not regressed,
        "detail": ("no critical cases declared" if not critical_cases
                   else ("ok: " + ",".join(critical_cases) if not regressed
                         else "regressed: " + ", ".join(regressed))),
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
