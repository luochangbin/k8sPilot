"""Reporter: run metadata, case results, aggregate report (design §23.7).

Reproducibility: `build_report` is computed from a list of case-result rows
only, so the same `case-results.jsonl` always yields the same `report.json`.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional

from .scorer import VERDICT_CORRECT, VERDICT_FIXTURE_FAILED, VERDICT_INCORRECT, VERDICT_SCHEMA_FAILED, VERDICT_SYSTEM_FAILED


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _valid(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["verdict"] not in (VERDICT_FIXTURE_FAILED, VERDICT_SYSTEM_FAILED, VERDICT_SCHEMA_FAILED)]


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


def build_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    valid = _valid(rows)
    counts = Counter(r["verdict"] for r in rows)

    root_cause_correct = sum(1 for r in valid if r["root_cause_correct"])
    root_cause_accuracy = round(root_cause_correct / len(valid), 3) if valid else None
    wrong_root_cause = sum(1 for r in valid if r["wrong_root_cause"])
    wrong_root_cause_rate = round(wrong_root_cause / len(valid), 3) if valid else None

    abstention_cases = [r for r in rows if r["abstention_correct"] is not None]
    abstention_accuracy = round(
        sum(1 for r in abstention_cases if r["abstention_correct"]) / len(abstention_cases), 3
    ) if abstention_cases else None

    schema_valid = sum(1 for r in rows if r["verdict"] in (VERDICT_CORRECT, VERDICT_INCORRECT))
    schema_valid_rate = round(schema_valid / total, 3) if total else None

    durations = [r["duration_ms"] for r in valid if r["duration_ms"] is not None]
    tool_calls = [r["tool_calls"] for r in valid]
    llm_calls = [r["llm_calls"] for r in valid]
    tokens = [r["token_usage"] for r in valid]

    return {
        "total_runs": total,
        "valid_runs": len(valid),
        "verdict_counts": dict(counts),
        "root_cause_accuracy": root_cause_accuracy,
        "wrong_root_cause_rate": wrong_root_cause_rate,
        "abstention_accuracy": abstention_accuracy,
        "schema_valid_rate": schema_valid_rate,
        "evidence_recall_avg": _mean([r["evidence_recall"] for r in valid]),
        "evidence_precision_avg": _mean([r["evidence_precision"] for r in valid]),
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
        "duplicate_tool_calls_total": sum(r["duplicate_tool_calls"] for r in valid),
        "truncated_logs_runs": sum(1 for r in valid if r["truncated_logs"]),
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
    lines.append("| metric | value |")
    lines.append("|---|---|")
    for key in ("root_cause_accuracy", "wrong_root_cause_rate", "abstention_accuracy",
                "schema_valid_rate", "evidence_recall_avg", "evidence_precision_avg"):
        lines.append(f"| {key} | {report.get(key)} |")
    lines.append(f"| verdict_counts | {report.get('verdict_counts')} |")
    lines.append(f"| duration_ms p50/p95 | {report['diagnosis_duration_ms'].get('p50')} / {report['diagnosis_duration_ms'].get('p95')} |")
    lines.append(f"| tool_calls p50/p95 | {report['tool_calls'].get('p50')} / {report['tool_calls'].get('p95')} |")
    lines.append(f"| token_usage p50/p95 | {report['token_usage'].get('p50')} / {report['token_usage'].get('p95')} |")
    lines.append("")

    lines.append("## Per case")
    lines.append("")
    for case_id in sorted(by_case):
        c = by_case[case_id]
        lines.append(f"### {case_id} (n={c['n']})")
        lines.append("")
        lines.append(f"- verdict_counts: {c['verdict_counts']}")
        lines.append(f"- root_cause_accuracy: {c['root_cause_accuracy']}")
        lines.append(f"- evidence_recall_avg: {c['evidence_recall_avg']}")
        lines.append(f"- wrong_root_cause_rate: {c['wrong_root_cause_rate']}")
        lines.append("")
    return "\n".join(lines)
