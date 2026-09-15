"""Reporter: run metadata, case results, aggregate report (design §23.7).

Reproducibility: `build_report` is computed from a list of case-result rows
only, so the same `case-results.jsonl` always yields the same `report.json`.

Scoring semantics v2 (handoff §3): every rate exposes its numerator and
denominator; fixture failures are excluded from diagnosis-quality denominators
but reported separately; zero denominators yield null.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from .scorer import (
    SCORER_VERSION,
    VERDICT_CORRECT,
    VERDICT_FIXTURE_FAILED,
    VERDICT_INCORRECT,
    VERDICT_SCHEMA_FAILED,
    VERDICT_SYSTEM_FAILED,
)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 3) if den else None


def _mean(values: list[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 3)


def _percentile(values: list[float], p: float) -> Optional[float]:
    vals = sorted(values)
    if not vals:
        return None
    idx = max(0, min(len(vals) - 1, int(p / 100.0 * (len(vals) - 1))))
    return round(vals[idx], 3)


def _pct(value: Optional[float]) -> str:
    """Render a 0..1 ratio as a percentage (JSON keeps the raw ratio)."""
    return "null" if value is None else f"{round(value * 100, 1)}%"


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    counts = Counter(r["verdict"] for r in rows)

    # fixture_ready False => fixture failure; excluded from quality denominators.
    fixture_ok = [r for r in rows if r.get("fixture_ready")]
    fixture_failed = [r for r in rows if not r.get("fixture_ready")]
    answerable = [r for r in fixture_ok if not r.get("abstention_expected")]
    abstention = [r for r in fixture_ok if r.get("abstention_expected")]

    # Latency/token metrics only over rows with a scored outcome.
    scored = [r for r in fixture_ok if r["verdict"] in (VERDICT_CORRECT, VERDICT_INCORRECT)]

    correct = sum(1 for r in fixture_ok if r["verdict"] == VERDICT_CORRECT)
    root_cause_correct = sum(1 for r in answerable if r.get("root_cause_correct"))
    wrong_root_cause = sum(1 for r in fixture_ok if r.get("wrong_root_cause"))
    explicit = sum(1 for r in fixture_ok if r.get("explicit_root_cause"))
    abstention_correct = sum(1 for r in abstention if r.get("abstention_correct"))
    answerable_covered = sum(1 for r in answerable if r.get("explicit_root_cause"))

    durations = [r["duration_ms"] for r in scored if r["duration_ms"] is not None]
    tool_calls = [r["tool_calls"] for r in scored]
    llm_calls = [r["llm_calls"] for r in scored]
    # Token usage is only aggregated over rows that actually reported it; a
    # missing trace must not be presented as zero consumption.
    tokens = [r["token_usage"] for r in scored if r.get("token_usage") is not None]
    llm_durations = [r["llm_duration_ms"] for r in scored if r.get("llm_duration_ms") is not None]
    tool_durations = [r["tool_duration_ms"] for r in scored if r.get("tool_duration_ms") is not None]
    ev_total = sum(int(r.get("evidence_total_entries") or 0) for r in scored)
    ev_extra = sum(int(r.get("evidence_extra_entries") or 0) for r in scored)
    ev_unsupported = sum(int(r.get("evidence_unsupported_entries") or 0) for r in scored)

    return {
        "scorer_version": SCORER_VERSION,
        "total_runs": total,
        "valid_runs": len(scored),
        "verdict_counts": dict(counts),
        # --- denominators (handoff §3.1) ---
        "fixture_failed_count": len(fixture_failed),
        "fixture_failed_rate": _rate(len(fixture_failed), total),
        "fixture_ok_count": len(fixture_ok),
        "system_failed_rate": _rate(counts.get(VERDICT_SYSTEM_FAILED, 0), len(fixture_ok)),
        "schema_failed_rate": _rate(counts.get(VERDICT_SCHEMA_FAILED, 0), len(fixture_ok)),
        "schema_failed_with_root_cause_count": sum(
            1 for r in rows if r["verdict"] == VERDICT_SCHEMA_FAILED and r.get("explicit_root_cause")
        ),
        "conflicting_abstention_count": sum(1 for r in rows if r.get("conflicting_abstention")),
        # --- quality metrics, each with numerator / denominator ---
        "end_to_end_correct_rate": _rate(correct, len(fixture_ok)),
        "end_to_end_correct": {"numerator": correct, "denominator": len(fixture_ok)},
        "root_cause_accuracy": _rate(root_cause_correct, len(answerable)),
        "root_cause_accuracy_counts": {"numerator": root_cause_correct, "denominator": len(answerable)},
        "wrong_root_cause_rate": _rate(wrong_root_cause, len(fixture_ok)),
        "wrong_root_cause_counts": {"numerator": wrong_root_cause, "denominator": len(fixture_ok)},
        "conditional_wrong_root_cause_rate": _rate(wrong_root_cause, explicit),
        "conditional_wrong_root_cause_counts": {"numerator": wrong_root_cause, "denominator": explicit},
        "abstention_recall": _rate(abstention_correct, len(abstention)),
        "abstention_recall_counts": {"numerator": abstention_correct, "denominator": len(abstention)},
        "answerable_coverage": _rate(answerable_covered, len(answerable)),
        "answerable_coverage_counts": {"numerator": answerable_covered, "denominator": len(answerable)},
        # --- compatibility keys (same definition as before) ---
        "abstention_accuracy": _rate(abstention_correct, len(abstention)),
        "schema_valid_rate": _rate(len(scored), total),
        # --- evidence ---
        "evidence_recall_avg": _mean([r.get("evidence_recall") for r in scored]),
        "required_evidence_match_ratio_avg": _mean(
            [r.get("required_evidence_match_ratio") for r in scored]
        ),
        "evidence_extra_rate": _rate(ev_extra, ev_total),
        "evidence_extra_counts": {"numerator": ev_extra, "denominator": ev_total},
        "evidence_unsupported_rate": _rate(ev_unsupported, ev_total),
        "evidence_unsupported_counts": {"numerator": ev_unsupported, "denominator": ev_total},
        # --- cost / effort ---
        "diagnosis_duration_ms": {
            "mean": _mean(durations),
            "p50": _percentile(durations, 50),
            "p95": _percentile(durations, 95),
        },
        "tool_calls": {
            "mean": _mean(tool_calls),
            "p50": _percentile(tool_calls, 50),
            "p95": _percentile(tool_calls, 95),
        },
        "llm_calls": {
            "mean": _mean(llm_calls),
            "p50": _percentile(llm_calls, 50),
            "p95": _percentile(llm_calls, 95),
        },
        "token_usage": {
            "mean": _mean(tokens),
            "p50": _percentile(tokens, 50),
            "p95": _percentile(tokens, 95),
        },
        "token_usage_known_runs": len(tokens),
        "token_usage_missing_runs": len(scored) - len(tokens),
        "llm_duration_ms": {
            "mean": _mean(llm_durations),
            "p50": _percentile(llm_durations, 50),
            "p95": _percentile(llm_durations, 95),
        },
        "tool_duration_ms": {
            "mean": _mean(tool_durations),
            "p50": _percentile(tool_durations, 50),
            "p95": _percentile(tool_durations, 95),
        },
        "duplicate_tool_calls_total": sum(r["duplicate_tool_calls"] for r in scored),
        "truncated_logs_runs": sum(1 for r in scored if r["truncated_logs"]),
    }


def report_by_case(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_case.setdefault(r["case_id"], []).append(r)
    out: dict[str, Any] = {}
    for case_id, case_rows in by_case.items():
        out[case_id] = build_report(case_rows)
        out[case_id]["n"] = len(case_rows)
    return out


def render_markdown(run_meta: dict[str, Any], report: dict[str, Any],
                    by_case: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Eval Report — {run_meta.get('run_id', 'unknown')}")
    lines.append("")
    lines.append(f"- scorer_version: {report.get('scorer_version')}")
    lines.append(f"- profile: {run_meta.get('profile')}")
    lines.append(f"- suite: {run_meta.get('suite')}")
    lines.append(f"- model: {run_meta.get('model')}")
    lines.append(f"- prompt_hash: {run_meta.get('prompt_hash')}")
    lines.append(f"- tool_schema_hash: {run_meta.get('tool_schema_hash')}")
    lines.append(f"- k8s_version: {run_meta.get('k8s_version')}")
    lines.append(f"- runs_per_case: {run_meta.get('runs_per_case')}")
    lines.append("")

    lines.append("## Aggregate")
    lines.append("")
    lines.append("| metric | value | numerator/denominator |")
    lines.append("|---|---|---|")
    for key in ("end_to_end_correct_rate", "root_cause_accuracy", "wrong_root_cause_rate",
                "conditional_wrong_root_cause_rate", "abstention_recall", "answerable_coverage",
                "evidence_recall_avg", "required_evidence_match_ratio_avg",
                "evidence_extra_rate", "evidence_unsupported_rate",
                "schema_valid_rate", "fixture_failed_rate", "system_failed_rate",
                "schema_failed_rate"):
        counts_key = f"{key}_counts"
        denom = report.get(counts_key)
        lines.append(f"| {key} | {_pct(report.get(key))} | {denom if denom else ''} |")
    lines.append(f"| verdict_counts | {report.get('verdict_counts')} | |")
    lines.append(f"| schema_failed_with_root_cause_count | {report.get('schema_failed_with_root_cause_count')} | |")
    lines.append(f"| duration_ms p50/p95 | {report['diagnosis_duration_ms'].get('p50')} / {report['diagnosis_duration_ms'].get('p95')} | |")
    lines.append(f"| tool_calls p50/p95 | {report['tool_calls'].get('p50')} / {report['tool_calls'].get('p95')} | |")
    lines.append(f"| llm_duration_ms p50/p95 | {report['llm_duration_ms'].get('p50')} / {report['llm_duration_ms'].get('p95')} | |")
    lines.append(f"| token_usage p50/p95 | {report['token_usage'].get('p50')} / {report['token_usage'].get('p95')} | |")
    lines.append("")

    lines.append("## Per case")
    lines.append("")
    for case_id in sorted(by_case):
        c = by_case[case_id]
        lines.append(f"### {case_id} (n={c['n']})")
        lines.append("")
        lines.append(f"- verdict_counts: {c['verdict_counts']}")
        lines.append(f"- root_cause_accuracy: {_pct(c['root_cause_accuracy'])} {c['root_cause_accuracy_counts']}")
        lines.append(f"- evidence_recall_avg: {_pct(c['evidence_recall_avg'])}")
        lines.append(f"- wrong_root_cause_rate: {_pct(c['wrong_root_cause_rate'])} {c['wrong_root_cause_counts']}")
        lines.append("")
    return "\n".join(lines)
