"""Multi-model benchmark orchestration (handoff §6/§7/§8).

Runs one suite against multiple server-side model profiles serially, records
each Case × Model × repetition attempt, and writes an honest comparison report
(model-benchmark.json / .md). It reuses the existing suite/case/inject/cleanup/
score code and never auto-reruns a failed diagnosis.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .cases import Case, load_case, load_suite
from .reporter import build_report, write_json, write_jsonl, _pct
from .runner import REPO_ROOT, Runner, file_hash
from .scorer import SCORER_VERSION


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_plan(cases: list[Case], models: list[str], runs_per_case: int, seed: int) -> list[dict[str, Any]]:
    """Deterministic attempt plan: model order is shuffled within each
    Case/repetition block with a fixed seed (handoff §6)."""
    rng = random.Random(seed)
    plan: list[dict[str, Any]] = []
    order = 0
    for case in cases:
        for rep in range(runs_per_case):
            block = list(models)
            rng.shuffle(block)
            for model in block:
                order += 1
                plan.append({
                    "attempt_id": f"{case.id}@{case.case_version}__{model}__r{rep}",
                    "case_id": case.id,
                    "case_version": case.case_version,
                    "model_profile": model,
                    "repetition": rep,
                    "execution_order": order,
                })
    return plan


def _extract_trace_usage(trace: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Extract usage/model identity from a trace. Unknown stays null/unknown."""
    out: dict[str, Any] = {
        "response_model_id": None,
        "input_tokens": None,
        "output_tokens": None,
        "cached_tokens": None,
        "reasoning_tokens": None,
        "llm_request_attempts": 0,
        "llm_duration_ms": None,
        "tool_duration_ms": None,
        "usage_complete": False,
    }
    if not trace:
        return out
    spans = trace.get("spans", [])
    if spans:
        # Model identity is carried on every span via the diagnosis eval ctx.
        root = spans[0]
        out["requested_model_id"] = root.get("requested_model_id")
        out["resolved_profile"] = root.get("resolved_profile")
        out["provider"] = root.get("provider")
        out["protocol"] = root.get("protocol")
        out["config_fingerprint"] = root.get("config_fingerprint")
        out["effective_parameters"] = root.get("effective_parameters")
    llm = [s for s in spans if s.get("kind") == "llm_call"]
    tools = [s for s in spans if s.get("kind") == "tool_call"]
    out["llm_request_attempts"] = sum(
        int(s.get("attributes", {}).get("attempts", 1) or 1) for s in llm
    )
    prompt = [s.get("attributes", {}).get("prompt_tokens") for s in llm]
    completion = [s.get("attributes", {}).get("completion_tokens") for s in llm]
    known_prompt = [t for t in prompt if t is not None]
    known_completion = [t for t in completion if t is not None]
    if known_prompt:
        out["input_tokens"] = sum(known_prompt)
    if known_completion:
        out["output_tokens"] = sum(known_completion)
    out["usage_complete"] = (
        len(known_prompt) == len(llm) and len(known_completion) == len(llm) and len(llm) > 0
    )
    llm_durations = [s.get("attributes", {}).get("duration_ms") for s in llm]
    tool_durations = [s.get("attributes", {}).get("duration_ms") for s in tools]
    if llm_durations:
        out["llm_duration_ms"] = round(sum(d for d in llm_durations if d is not None), 1)
    if tool_durations:
        out["tool_duration_ms"] = round(sum(d for d in tool_durations if d is not None), 1)
    response_models = [s.get("attributes", {}).get("response_model_id") for s in llm]
    out["response_model_id"] = next((m for m in reversed(response_models) if m), None)
    return out


def _read_trace(trace_dir: Optional[str], diagnosis_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not trace_dir or not diagnosis_id:
        return None
    path = Path(trace_dir) / f"{diagnosis_id}.jsonl"
    if not path.is_file():
        return None
    spans = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return {"spans": spans}


def run_benchmark(*, suite_path: Path, case_ids: list[str], models: list[str],
                  runs_per_case: int, seed: int, agent_url: str,
                  trace_dir: Optional[str], reports_dir: str,
                  kubeconfig: Optional[str] = None, max_diagnoses: Optional[int] = None,
                  time_limit_seconds: Optional[float] = None,
                  model_label: str = "", runner: Any = None,
                  enable_knowledge: Optional[bool] = None,
                  enable_incidents: Optional[bool] = None) -> tuple[str, Path]:
    suite_raw = load_suite(suite_path)
    cases = [load_case(Path(__file__).resolve().parent / "cases" / f"{cid}.yaml") for cid in case_ids]

    benchmark_id = f"benchmark-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run_dir = Path(reports_dir) / benchmark_id
    run_dir.mkdir(parents=True, exist_ok=True)

    plan = build_plan(cases, models, runs_per_case, seed)
    planned_total = len(plan)
    if max_diagnoses is not None and planned_total > max_diagnoses:
        plan = plan[:max_diagnoses]

    runner = runner or Runner(agent_url=agent_url, trace_dir=trace_dir,
                              kubeconfig=kubeconfig, reports_dir=reports_dir, model=model_label)
    cases_by_id = {c.id: c for c in cases}

    meta = {
        "benchmark_id": benchmark_id,
        "suite": suite_raw.get("id", suite_path.stem),
        "models": list(models),
        "runs_per_case": runs_per_case,
        "seed": seed,
        "agent_url": agent_url,
        "prompt_hash": file_hash(REPO_ROOT / "agent-service/app/prompts.py"),
        "tool_schema_hash": file_hash(REPO_ROOT / "agent-service/app/tools.py"),
        "k8s_version": runner._k8s_version(),  # reuse existing probe
        "scorer_version": SCORER_VERSION,
        "planned_total": planned_total,
        "effective_total": len(plan),
        "max_diagnoses": max_diagnoses,
        "time_limit_seconds": time_limit_seconds,
        "enable_knowledge": enable_knowledge,
        "enable_incidents": enable_incidents,
        "declared_model_label": model_label or None,
        "plan": plan,
    }
    write_json(run_dir / "model-benchmark.plan.json", {"benchmark_id": benchmark_id, "plan": plan})

    rows: list[dict[str, Any]] = []
    stop_reason: Optional[str] = None
    started = time.monotonic()

    for attempt in plan:
        if time_limit_seconds is not None and (time.monotonic() - started) >= time_limit_seconds:
            stop_reason = "time_limit_reached"
            break
        case = cases_by_id[attempt["case_id"]]
        t0 = _now_iso()
        row = runner.run_case_attempt(
            case, benchmark_id, attempt["execution_order"],
            enable_knowledge=enable_knowledge, enable_incidents=enable_incidents,
            model_profile=attempt["model_profile"],
        )
        t1 = _now_iso()
        trace = _read_trace(trace_dir, row.get("diagnosis_id"))
        usage = _extract_trace_usage(trace)
        status = row.get("status", "unknown")
        if row.get("cleanup_failed"):
            stop_reason = "cleanup_failed"
        # Execution identity evidence: the server-confirmed profile/model and
        # config fingerprint must exist and be consistent with what was asked.
        requested = usage.get("requested_model_id")
        response = usage.get("response_model_id")
        resolved = usage.get("resolved_profile")
        fingerprint = usage.get("config_fingerprint")
        identity_ok: Optional[bool] = None
        identity_error: Optional[str] = None
        if usage.get("llm_request_attempts", 0) > 0:
            if not requested or not resolved or not fingerprint:
                identity_ok, identity_error = False, "missing_identity"
            elif not response:
                # Requesting a model does not prove which model the provider
                # actually executed; no response identity means not comparable.
                identity_ok, identity_error = False, "missing_response_identity"
            elif resolved != attempt["model_profile"]:
                identity_ok = False
                identity_error = f"resolved_profile_mismatch:{resolved}!={attempt['model_profile']}"
            elif response != requested:
                identity_ok = False
                identity_error = f"model_id_mismatch:{requested}!={response}"
            else:
                identity_ok = True
        enriched = {
            **row,
            "benchmark_id": benchmark_id,
            "attempt_id": attempt["attempt_id"],
            "model_profile": attempt["model_profile"],
            "repetition": attempt["repetition"],
            "execution_order": attempt["execution_order"],
            "seed": seed,
            "started_at": t0,
            "finished_at": t1,
            "effective_knowledge_flags": {"knowledge": enable_knowledge, "incidents": enable_incidents},
            "knowledge_snapshot_hash": None,
            "agent_revision": None,
            "scorer_version": SCORER_VERSION,
            "status": status,
            "failure_category": row.get("verdict"),
            "identity_ok": identity_ok,
            "identity_error": identity_error,
            "cost_estimate": None,
            "pricing_source": "unknown",
            "pricing_timestamp": None,
            **usage,
        }
        rows.append(enriched)
        write_jsonl(run_dir / "attempts.jsonl", [enriched])
        if stop_reason:
            break

    if stop_reason is None and len(rows) < planned_total:
        stop_reason = "max_diagnoses_reached"

    report = build_benchmark_report(benchmark_id, meta, rows, stop_reason)
    write_json(run_dir / "model-benchmark.json", report)
    (run_dir / "model-benchmark.md").write_text(
        render_benchmark_markdown(report), encoding="utf-8")
    return benchmark_id, run_dir


def build_benchmark_report(benchmark_id: str, meta: dict[str, Any],
                           rows: list[dict[str, Any]], stop_reason: Optional[str]) -> dict[str, Any]:
    models = list(meta["models"])
    scored_verdicts = ("diagnosis_correct", "diagnosis_incorrect", "schema_failed")

    def model_rows_of(model: str) -> list[dict[str, Any]]:
        return [r for r in rows if r.get("model_profile") == model]

    def scored_rows_of(model: str) -> list[dict[str, Any]]:
        return [r for r in model_rows_of(model) if r.get("verdict") in scored_verdicts]

    # ---- comparability evidence (handoff §7) ----
    issues: list[str] = []
    # 1. Every attempt that actually called the provider must have verified
    #    identity; a single verified row does not vouch for the others.
    for r in rows:
        if r.get("identity_ok") is True:
            continue
        called = bool(int(r.get("llm_request_attempts") or 0) > 0
                      or r.get("verdict") in scored_verdicts)
        if r.get("identity_ok") is False or called:
            reason = r.get("identity_error") or "identity_unknown"
            issues.append(f"{r.get('attempt_id')}:{reason}")
    # 2. Only identical, non-empty case coverage can be compared. Coverage is
    #    the set of cases actually executed (a timeout/failure still ran it),
    #    not only the successfully scored ones.
    model_cases = {m: {r["case_id"] for r in model_rows_of(m)} for m in models}
    covered = {m: cs for m, cs in model_cases.items() if cs}
    missing_models = [m for m in models if not covered.get(m)]
    if missing_models:
        # A partly-empty batch is incomplete: record it instead of silently
        # comparing only the models that happened to produce results.
        issues.append("model_missing_results:" + ",".join(missing_models))
    if len(covered) < 2:
        issues.append("insufficient_models_with_results")
        common_cases: set[str] = set()
    else:
        common_cases = set.intersection(*covered.values())
        if not common_cases:
            issues.append("no_common_cases")
        elif any(cs != common_cases for cs in covered.values()):
            issues.append("case_sets_differ")
    # 3. Configuration drift within a profile. Failures with execution identity
    #    also enter the quality denominators, so they must be consistent too.
    for model in models:
        attempted = [r for r in model_rows_of(model)
                     if r.get("identity_ok") or int(r.get("llm_request_attempts") or 0) > 0]
        fingerprints = {r.get("config_fingerprint") for r in attempted
                        if r.get("config_fingerprint")}
        if len(fingerprints) > 1:
            issues.append(f"{model}:config_fingerprint_varies")
        params = {json.dumps(r.get("effective_parameters"), sort_keys=True) for r in attempted}
        if len(params) > 1:
            issues.append(f"{model}:parameters_vary")
    conditions = {(r.get("provider"), r.get("protocol")) for r in rows if r.get("identity_ok")}
    if len(conditions) > 1:
        issues.append("provider_protocol_varies")
    knowledge_flags = {json.dumps(r.get("effective_knowledge_flags"), sort_keys=True) for r in rows}
    if len(knowledge_flags) > 1:
        issues.append("knowledge_flags_vary_across_attempts")
    comparable = not issues

    # Aggregates are computed over the common case set only, so a model that
    # ran extra cases cannot pad or dilute the comparison.
    per_model: dict[str, Any] = {}
    for model in models:
        all_model_rows = model_rows_of(model)
        compared = [r for r in all_model_rows if r["case_id"] in common_cases]
        per_model[model] = {
            "report": build_report(compared) if compared else None,
            "attempt_count": len(all_model_rows),
            "compared_attempt_count": len(compared),
            "usage_complete_rate": _usage_complete_rate(compared),
            "resolved_profiles": sorted({r.get("resolved_profile") for r in all_model_rows
                                         if r.get("resolved_profile")}),
            "identity_verified_attempts": sum(1 for r in all_model_rows if r.get("identity_ok")),
            "identity_unknown_attempts": sum(1 for r in all_model_rows
                                             if r.get("identity_ok") is None),
        }
    per_case: dict[str, Any] = {}
    for case_id in sorted(common_cases):
        per_case[case_id] = {
            model: _case_summary([r for r in model_rows_of(model) if r["case_id"] == case_id])
            for model in models
        }

    return {
        "benchmark_id": benchmark_id,
        "meta": {k: v for k, v in meta.items() if k != "plan"},
        "stop_reason": stop_reason,
        "attempts": len(rows),
        "per_model": per_model,
        "per_case_common": per_case,
        "common_case_count": len(common_cases),
        "models_requested": models,
        "models_compared": [m for m in models if covered.get(m)],
        "models_missing": missing_models,
        "comparable": comparable,
        "incomparable_reason": None if comparable else "; ".join(issues),
        "caveats": [
            "同一 Case 的重复运行不等于增加独立场景；样本少时不要从差值断言稳定优胜。",
            "Token 用量不等于跨模型成本；缺少可靠费率时只报告用量。",
            "仅 OpenAI-compatible /chat/completions 路径；Claude 系模型不在本阶段范围。",
            "端到端耗时包含注入/清理，与 LLM/Tool 耗时不可相加对比。",
        ],
    }


def _usage_complete_rate(rows: list[dict[str, Any]]) -> Optional[float]:
    scored = [r for r in rows if r.get("usage_complete") is not None]
    if not scored:
        return None
    complete = sum(1 for r in scored if r.get("usage_complete"))
    return round(complete / len(scored), 3)


def _case_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0}
    rep = build_report(rows)
    return {
        "n": len(rows),
        "root_cause_accuracy": rep["root_cause_accuracy"],
        "wrong_root_cause_rate": rep["wrong_root_cause_rate"],
        "end_to_end_correct_rate": rep["end_to_end_correct_rate"],
    }


def render_benchmark_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    lines = [f"# Model Benchmark — {report['benchmark_id']}", ""]
    lines.append(f"- suite: {meta.get('suite')}")
    lines.append(f"- models: {', '.join(meta.get('models', []))}")
    lines.append(f"- runs_per_case: {meta.get('runs_per_case')}")
    lines.append(f"- seed: {meta.get('seed')}")
    lines.append(f"- scorer_version: {meta.get('scorer_version')}")
    lines.append(f"- planned/effective attempts: {meta.get('planned_total')}/{meta.get('effective_total')}")
    lines.append(f"- stop_reason: {report.get('stop_reason')}")
    lines.append(f"- comparable: {report.get('comparable')}"
                 + (f" ({report['incomparable_reason']})" if report.get("incomparable_reason") else ""))
    lines.append(f"- declared_model_label (label only): {meta.get('declared_model_label')}")
    lines.append("")
    lines.append("## Per model")
    lines.append("")
    lines.append("| model | attempts | end_to_end_correct | root_cause_accuracy | wrong_root_cause_rate | usage_complete_rate |")
    lines.append("|---|---|---|---|---|---|")
    for model, entry in report["per_model"].items():
        rep = entry["report"] or {}
        lines.append(
            f"| {model} | {entry['attempt_count']} | {_pct(rep.get('end_to_end_correct_rate'))} | "
            f"{_pct(rep.get('root_cause_accuracy'))} | {_pct(rep.get('wrong_root_cause_rate'))} | "
            f"{_pct(entry['usage_complete_rate'])} |")
    lines.append("")
    lines.append("## Per case (common across all models)")
    lines.append("")
    header = "| case | " + " | ".join(report["meta"]["models"]) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(report["meta"]["models"]) + 1))
    for case_id, per_model in report["per_case_common"].items():
        cells = []
        for model in report["meta"]["models"]:
            c = per_model.get(model, {})
            cells.append(f"n={c.get('n', 0)} rca={_pct(c.get('root_cause_accuracy'))}")
        lines.append(f"| {case_id} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    for c in report.get("caveats", []):
        lines.append(f"- {c}")
    lines.append("")
    return "\n".join(lines)
