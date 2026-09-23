"""Multi-model benchmark tests (offline: plan, budget, identity, failure)."""

import json
from pathlib import Path

from eval.benchmark import build_plan, run_benchmark
from eval.cases import Budgets, Case, GroundTruth, ReadyWhen, Target

SUITE = Path(__file__).resolve().parents[1] / "suites" / "phase1.yaml"


def _case(cid: str) -> Case:
    return Case(
        schema_version="eval.k8spilot.io/v1alpha1", id=cid, case_version="1", suite="test",
        description="", target=Target(apiVersion="v1", kind="Pod", namespace="ns", name=cid),
        ready_when=ReadyWhen(type="jsonpath_equals", path="status.phase", value="Running"),
        ground_truth=GroundTruth(), budgets=Budgets(),
    )


def _row(case_id, *, verdict="diagnosis_correct", status="completed"):
    return {
        "case_id": case_id, "case_version": "1", "verdict": verdict,
        "fixture_ready": True, "abstention_expected": False, "explicit_root_cause": True,
        "valid_abstention": False, "conflicting_abstention": False, "invalid_output": False,
        "root_cause_correct": verdict == "diagnosis_correct", "wrong_root_cause": False,
        "abstention_correct": None, "evidence_recall": 1.0,
        "required_evidence_match_ratio": 1.0, "evidence_returned": 1, "required_evidence": 1,
        "tool_calls": 1, "duplicate_tool_calls": 0, "llm_calls": 1, "token_usage": 10,
        "token_usage_complete": True, "trace_present": True,
        "duration_ms": 1.0, "trace_failure_layer": None, "truncated_logs": False,
        "diagnosis_id": None, "status": status, "error": None,
    }


def _write_trace(trace_dir: Path, diagnosis_id: str, model: str,
                 include_response: bool = True) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    llm_attrs = {"prompt_tokens": 10, "completion_tokens": 5, "attempts": 2}
    if include_response:
        llm_attrs["response_model_id"] = model
    spans = [
        {"kind": "diagnosis_root", "name": "diagnosis", "resolved_profile": model,
         "requested_model_id": model, "provider": "commandcode",
         "protocol": "openai_chat_completions", "config_fingerprint": "fp123",
         "effective_parameters": {"max_tokens": 2048}},
        {"kind": "llm_call", "name": "llm.call", "attributes": llm_attrs},
    ]
    (trace_dir / f"{diagnosis_id}.jsonl").write_text(
        "\n".join(json.dumps(s) for s in spans) + "\n", encoding="utf-8")


class FakeRunner:
    def __init__(self, *, cleanup_fail_at=None, verdict="diagnosis_correct",
                 trace_dir=None, include_response=True):
        self.calls = []
        self.cleanup_fail_at = cleanup_fail_at
        self.verdict = verdict
        self.trace_dir = Path(trace_dir) if trace_dir else None
        self.include_response = include_response
        self.infra_retries = []

    def _k8s_version(self):
        return "fake"

    def run_case_attempt(self, case, run_id, attempt_index, *, enable_knowledge=None,
                         enable_incidents=None, model_profile=None, infra_retries=0):
        self.infra_retries.append(infra_retries)
        self.calls.append((case.id, model_profile))
        row = _row(case.id, verdict=self.verdict)
        row["diagnosis_id"] = f"diag_{len(self.calls)}"
        # Effective budgets come from the trace root span in real runs; the fake
        # must provide them or the budget-signature check reports unknown.
        row.update({"effective_max_tool_calls": 12, "effective_max_agent_rounds": 12,
                    "max_finalization_attempts": 1})
        if self.trace_dir and model_profile:
            _write_trace(self.trace_dir, row["diagnosis_id"], model_profile,
                         include_response=self.include_response)
        if self.cleanup_fail_at == len(self.calls):
            row["cleanup_failed"] = "boom"
        return row


def test_build_plan_2x2x2_is_eight_unique_and_seed_reproducible():
    cases = [_case("a"), _case("b")]
    plan = build_plan(cases, ["m1", "m2"], 2, seed=42)
    assert len(plan) == 8
    assert len({p["attempt_id"] for p in plan}) == 8
    assert [p["execution_order"] for p in plan] == list(range(1, 9))
    assert build_plan(cases, ["m1", "m2"], 2, seed=42) == plan
    assert build_plan(cases, ["m1", "m2"], 2, seed=7) != plan


def _run(tmp_path, *, runner, trace_dir=None, **kw):
    return run_benchmark(
        suite_path=SUITE, case_ids=["pod-healthy-001", "pod-oomkilled-001"],
        models=["m1", "m2"], runs_per_case=2, seed=1, agent_url="http://x",
        trace_dir=trace_dir, reports_dir=str(tmp_path), runner=runner, **kw)


def test_benchmark_records_all_attempts_and_null_usage(tmp_path):
    runner = FakeRunner()
    benchmark_id, run_dir = _run(tmp_path, runner=runner)
    assert len(runner.calls) == 8
    report = json.loads((run_dir / "model-benchmark.json").read_text(encoding="utf-8"))
    assert report["attempts"] == 8
    assert report["stop_reason"] is None
    assert report["per_model"]["m1"]["attempt_count"] == 4
    assert report["per_model"]["m2"]["attempt_count"] == 4
    assert len((run_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()) == 8
    first = json.loads((run_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["input_tokens"] is None
    assert first["output_tokens"] is None
    assert first["cost_estimate"] is None
    assert first["usage_complete"] is False
    # No Trace -> no execution identity evidence -> must not claim comparability.
    assert report["comparable"] is False
    assert "identity" in report["incomparable_reason"]


def test_benchmark_comparable_only_with_verified_identity(tmp_path):
    trace_dir = tmp_path / "trace"
    runner = FakeRunner(trace_dir=trace_dir)
    benchmark_id, run_dir = _run(tmp_path, runner=runner, trace_dir=str(trace_dir))
    report = json.loads((run_dir / "model-benchmark.json").read_text(encoding="utf-8"))
    assert report["comparable"] is True
    assert report["incomparable_reason"] is None
    assert report["per_model"]["m1"]["identity_verified_attempts"] == 4
    assert report["per_model"]["m1"]["resolved_profiles"] == ["m1"]
    lines = [json.loads(l) for l in
             (run_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(a["identity_ok"] is True for a in lines)
    assert all(a["llm_request_attempts"] == 2 for a in lines)


def test_benchmark_missing_response_identity_is_incomparable(tmp_path):
    trace_dir = tmp_path / "trace"
    runner = FakeRunner(trace_dir=trace_dir, include_response=False)
    benchmark_id, run_dir = _run(tmp_path, runner=runner, trace_dir=str(trace_dir))
    report = json.loads((run_dir / "model-benchmark.json").read_text(encoding="utf-8"))
    assert report["comparable"] is False
    assert "missing_response_identity" in report["incomparable_reason"]
    lines = [json.loads(l) for l in
             (run_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(a["identity_ok"] is False for a in lines)


def test_benchmark_max_diagnoses_stops(tmp_path):
    runner = FakeRunner()
    benchmark_id, run_dir = _run(tmp_path, runner=runner, max_diagnoses=3)
    assert len(runner.calls) == 3
    report = json.loads((run_dir / "model-benchmark.json").read_text(encoding="utf-8"))
    assert report["attempts"] == 3
    assert report["stop_reason"] == "max_diagnoses_reached"


def test_benchmark_cleanup_failure_stops(tmp_path):
    runner = FakeRunner(cleanup_fail_at=2)
    benchmark_id, run_dir = _run(tmp_path, runner=runner)
    assert len(runner.calls) == 2
    report = json.loads((run_dir / "model-benchmark.json").read_text(encoding="utf-8"))
    assert report["stop_reason"] == "cleanup_failed"


def test_benchmark_records_failed_attempts(tmp_path):
    runner = FakeRunner(verdict="system_failed")
    benchmark_id, run_dir = _run(tmp_path, runner=runner)
    report = json.loads((run_dir / "model-benchmark.json").read_text(encoding="utf-8"))
    assert report["attempts"] == 8
    lines = (run_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(l)["failure_category"] == "system_failed" for l in lines)


# ---- comparability evidence (review round 2) ----

def _bench_row(case_id, model, *, identity_ok=True, fingerprint="fp1", params=None,
               verdict="diagnosis_correct", attempts=1, error=None, **kw):
    row = {
        "case_id": case_id, "case_version": "1", "model_profile": model,
        "attempt_id": f"{case_id}__{model}", "verdict": verdict,
        "fixture_ready": True, "abstention_expected": False, "explicit_root_cause": True,
        "valid_abstention": False, "conflicting_abstention": False, "invalid_output": False,
        "root_cause_correct": verdict == "diagnosis_correct", "wrong_root_cause": False,
        "abstention_correct": None, "evidence_recall": 1.0,
        "required_evidence_match_ratio": 1.0, "evidence_returned": 1, "required_evidence": 1,
        "tool_calls": 1, "duplicate_tool_calls": 0, "llm_calls": 1, "token_usage": 10,
        "duration_ms": 1.0, "trace_failure_layer": None, "truncated_logs": False,
        "identity_ok": identity_ok, "identity_error": error,
        "config_fingerprint": fingerprint, "effective_parameters": params or {"max_tokens": 2048},
        "llm_request_attempts": attempts, "provider": "commandcode",
        "protocol": "openai_chat_completions", "resolved_profile": model,
        "effective_knowledge_flags": {"knowledge": None, "incidents": None},
        "trace_present": True,
    }
    row.update(kw)
    return row


def _meta(models):
    return {"models": models, "runs_per_case": 1, "seed": 1, "suite": "t",
            "scorer_version": "3", "planned_total": 4, "effective_total": 4,
            "declared_model_label": None}


def _build(rows, models):
    from eval.benchmark import build_benchmark_report
    return build_benchmark_report("b1", _meta(models), rows, None)


def test_partial_unknown_identity_is_incomparable():
    rows = [_bench_row("c1", "m1"), _bench_row("c1", "m2"),
            _bench_row("c2", "m1", identity_ok=None, attempts=0, fingerprint=None,
                       params=None, error=None)]
    rep = _build(rows, ["m1", "m2"])
    assert rep["comparable"] is False
    assert "c2__m1" in rep["incomparable_reason"]


def test_different_case_sets_are_incomparable_and_only_common_compared():
    rows = [_bench_row("c1", "m1"), _bench_row("c2", "m1"), _bench_row("c1", "m2")]
    rep = _build(rows, ["m1", "m2"])
    assert rep["comparable"] is False
    assert "case_sets_differ" in rep["incomparable_reason"]
    assert rep["common_case_count"] == 1
    assert rep["per_model"]["m1"]["compared_attempt_count"] == 1
    assert rep["per_model"]["m1"]["attempt_count"] == 2


def test_no_common_cases_are_incomparable():
    rows = [_bench_row("c1", "m1"), _bench_row("c2", "m2")]
    rep = _build(rows, ["m1", "m2"])
    assert rep["comparable"] is False
    assert "no_common_cases" in rep["incomparable_reason"]
    assert rep["common_case_count"] == 0
    assert rep["per_model"]["m1"]["report"] is None


def test_config_drift_within_profile_is_incomparable():
    rows = [_bench_row("c1", "m1", fingerprint="fpA"),
            _bench_row("c1", "m1", fingerprint="fpB"), _bench_row("c1", "m2")]
    rep = _build(rows, ["m1", "m2"])
    assert rep["comparable"] is False
    assert "m1:config_fingerprint_varies" in rep["incomparable_reason"]


def test_missing_model_marks_batch_incomplete():
    rows = [_bench_row("c1", "m1"), _bench_row("c1", "m2")]
    rep = _build(rows, ["m1", "m2", "m3"])
    assert rep["comparable"] is False
    assert "model_missing_results:m3" in rep["incomparable_reason"]
    assert rep["models_missing"] == ["m3"]
    assert rep["models_compared"] == ["m1", "m2"]
    assert rep["per_model"]["m3"]["report"] is None


def test_failed_attempt_config_drift_is_detected():
    rows = [
        _bench_row("c1", "m1", fingerprint="fpA"),
        _bench_row("c1", "m1", fingerprint="fpB", verdict="system_failed",
                   identity_ok=True, attempts=1),
        _bench_row("c1", "m2", fingerprint="fpA"),
    ]
    rep = _build(rows, ["m1", "m2"])
    assert rep["comparable"] is False
    assert "m1:config_fingerprint_varies" in rep["incomparable_reason"]


class _RecordingTime:
    """Delegates to the real time module but records sleeps."""

    def __init__(self, real):
        self._real = real
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def __getattr__(self, item):
        return getattr(self._real, item)


def test_benchmark_paces_between_attempts(monkeypatch, tmp_path):
    import time as real_time

    import eval.benchmark as benchmark_module

    clock = _RecordingTime(real_time)
    monkeypatch.setattr(benchmark_module, "time", clock)

    runner = FakeRunner()
    _run(tmp_path, runner=runner, pace_seconds=30)
    # 8 attempts -> one sleep between each consecutive pair, never before the first.
    assert clock.sleeps == [30] * (len(runner.calls) - 1)


def test_single_profile_run_paces_between_attempts(monkeypatch, tmp_path):
    import time as real_time

    import eval.runner as runner_module
    from eval.cases import load_case_entry, load_suite, resolve_case_entry

    clock = _RecordingTime(real_time)
    monkeypatch.setattr(runner_module, "time", clock)
    # The test's subject is pacing, not report rendering.
    monkeypatch.setattr(runner_module, "build_report", lambda rows: {"scorer_version": "5"})
    monkeypatch.setattr(runner_module, "report_by_case", lambda rows: {})
    monkeypatch.setattr(runner_module, "render_markdown", lambda *a, **k: "# test")

    root = Path(__file__).resolve().parents[1]
    suite_path = root / "suites" / "cause-level-v1.yaml"
    suite = load_suite(suite_path)
    case_ids = suite["cases"][:2]
    runner = runner_module.Runner(agent_url="http://x", trace_dir=None, kubeconfig=None,
                                 reports_dir=str(tmp_path))
    runner._load_case = lambda cid: load_case_entry(root / "cases", cid)
    runner._run_case = lambda case, run_id, attempt, **kw: {
        "case_id": case.id, "case_version": case.case_version, "verdict": "diagnosis_correct",
        "fixture_ready": True, "abstention_expected": False, "duration_ms": 1.0,
        "tool_calls": 1, "llm_calls": 1, "token_usage": 10,
        "effective_max_tool_calls": 12, "effective_max_agent_rounds": 12,
        "max_finalization_attempts": 1,
    }
    runner.run(suite_path, case_ids, 2, "paced", pace_seconds=15)
    # 2 cases x 2 runs = 4 attempts -> 3 sleeps.
    assert clock.sleeps == [15, 15, 15]


def test_untraced_attempt_does_not_block_budget_comparability():
    """A no-trace attempt (fixture never ran, or the runner cancelled it on the
    case timeout) carries no evidence of its effective budget; it must not make
    the batch incomparable, while still counting as an attempt."""
    budgets = {"effective_max_tool_calls": 12, "effective_max_agent_rounds": 12,
               "max_finalization_attempts": 2}
    rows = [
        _bench_row("c1", "m1", **budgets), _bench_row("c1", "m2", **budgets),
        _bench_row("c2", "m2", **budgets),
        # cancelled attempt: no trace, no budget, no identity
        _bench_row("c2", "m1", verdict="system_failed", identity_ok=None, attempts=0,
                   fingerprint=None, error="diagnosis timed out after 180s",
                   trace_present=False, effective_max_tool_calls=None,
                   effective_max_agent_rounds=None, max_finalization_attempts=None),
    ]
    rep = _build(rows, ["m1", "m2"])
    assert "effective_budget_unknown" not in (rep["incomparable_reason"] or "")
    assert rep["comparable"] is True, rep["incomparable_reason"]
    assert rep["per_model"]["m1"]["attempt_count"] == 2


def test_traced_row_with_missing_budget_stays_incomparable():
    """Unknown != same still holds for a row that WAS traced: if its budget
    fields are missing, comparability is refused."""
    rows = [
        _bench_row("c1", "m1", effective_max_tool_calls=12, effective_max_agent_rounds=12,
                   max_finalization_attempts=2),
        _bench_row("c1", "m2"),  # traced, but no budget recorded
    ]
    rep = _build(rows, ["m1", "m2"])
    assert rep["comparable"] is False
    assert "effective_budget_unknown" in rep["incomparable_reason"]


def test_benchmark_forwards_and_records_infra_retries(tmp_path):
    """`--infra-retries` reaches the attempt seam and is frozen in the batch
    metadata, so a retry policy cannot silently differ between runs."""
    runner = FakeRunner()
    benchmark_id, run_dir = _run(tmp_path, runner=runner, infra_retries=2)
    assert runner.infra_retries == [2] * 8
    meta = json.loads((run_dir / "model-benchmark.plan.json").read_text(encoding="utf-8"))
    assert meta["benchmark_id"] == benchmark_id
    report = json.loads((run_dir / "model-benchmark.json").read_text(encoding="utf-8"))
    assert report["meta"]["infra_retries"] == 2
