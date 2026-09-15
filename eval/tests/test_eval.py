"""Unit tests for eval: loader, jsonpath, scorer, reporter reproducibility."""

import json
from pathlib import Path

import pytest

from eval.cases import CaseError, load_case
from eval.kubectl import JsonPathError, json_path_get
from eval.reporter import build_report, write_jsonl
from eval.scorer import (
    VERDICT_CORRECT,
    VERDICT_FIXTURE_FAILED,
    VERDICT_INCORRECT,
    VERDICT_SCHEMA_FAILED,
    VERDICT_SYSTEM_FAILED,
    score_case,
    summarize_trace,
)


def _case(**overrides):
    from eval.cases import Budgets, Case, EvidenceRequirement, GroundTruth, ReadyWhen, Target
    base = dict(
        schema_version="eval.k8spilot.io/v1alpha1",
        id="t-001",
        case_version="1",
        suite="test",
        description="",
        target=Target(apiVersion="v1", kind="Pod", namespace="ns", name="p"),
        ready_when=ReadyWhen(type="jsonpath_equals", path="status.phase", value="Running"),
        ground_truth=GroundTruth(
            accepted_root_cause_codes=["CONTAINER_OOMKILLED"],
            required_evidence=[EvidenceRequirement(
                source="kubernetes.status",
                path="status.containerStatuses[0].lastState.terminated.reason",
                operator="equals", value="OOMKilled",
            )],
        ),
        budgets=Budgets(),
    )
    base.update(overrides)
    return Case(**base)


def _row(case, *, verdict=VERDICT_CORRECT, abstention_expected=False, **kw):
    row = {
        "case_id": case.id,
        "case_version": case.case_version,
        "verdict": verdict,
        "fixture_ready": True,
        "abstention_expected": abstention_expected,
        "explicit_root_cause": verdict == VERDICT_CORRECT and not abstention_expected,
        "valid_abstention": verdict == VERDICT_CORRECT and abstention_expected,
        "conflicting_abstention": False,
        "root_cause_correct": verdict == VERDICT_CORRECT and not abstention_expected,
        "wrong_root_cause": verdict == VERDICT_INCORRECT and not abstention_expected,
        "abstention_correct": (verdict == VERDICT_CORRECT) if abstention_expected else None,
        "evidence_recall": None,
        "required_evidence_match_ratio": None,
        "evidence_returned": 0,
        "required_evidence": 1,
        "tool_calls": 0,
        "duplicate_tool_calls": 0,
        "llm_calls": 0,
        "token_usage": 0,
        "duration_ms": None,
        "trace_failure_layer": None,
        "truncated_logs": False,
    }
    row.update(kw)
    return row


# ---- loader ----

def test_load_real_cases():
    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    files = sorted(cases_dir.glob("*.yaml"))
    assert len(files) == 12, f"expected 12 cases, got {len(files)}"
    for f in files:
        case = load_case(f)
        assert case.id == f.stem
        assert case.target.kind == "Pod"
        assert case.setup_manifests, f"{case.id}: no setup manifests"


def test_loader_rejects_missing_file():
    with pytest.raises(CaseError):
        load_case(Path("does-not-exist.yaml"))


# ---- jsonpath ----

def test_jsonpath_get():
    obj = {"status": {"containerStatuses": [{"restartCount": 3, "lastState": {"terminated": {"reason": "OOMKilled"}}}]}}
    assert json_path_get(obj, "status.containerStatuses[0].restartCount") == 3
    assert json_path_get(obj, "status.containerStatuses[0].lastState.terminated.reason") == "OOMKilled"
    with pytest.raises(JsonPathError):
        json_path_get(obj, "status.missing")


# ---- scorer ----

def test_score_oom_correct():
    case = _case()
    result = {"root_cause_code": "CONTAINER_OOMKILLED", "insufficient_evidence": False,
              "evidence": [{"source": "kubernetes.status", "path": "status.containerStatuses[0].lastState.terminated.reason",
                            "operator": "equals", "value": "OOMKilled"}]}
    row = score_case(case, fixture_ready=True, diagnosis_status="completed", result=result, error=None, trace=None)
    assert row["verdict"] == VERDICT_CORRECT
    assert row["root_cause_correct"] is True
    assert row["evidence_recall"] == 1.0


def test_score_wrong_root_cause():
    case = _case()
    result = {"root_cause_code": "IMAGE_PULL_FAILED", "insufficient_evidence": False, "evidence": []}
    row = score_case(case, fixture_ready=True, diagnosis_status="completed", result=result, error=None, trace=None)
    assert row["verdict"] == VERDICT_INCORRECT
    assert row["wrong_root_cause"] is True


def test_score_fixture_not_ready():
    case = _case()
    row = score_case(case, fixture_ready=False, diagnosis_status="completed", result=None, error=None, trace=None)
    assert row["verdict"] == VERDICT_FIXTURE_FAILED


def test_score_system_failed():
    case = _case()
    row = score_case(case, fixture_ready=True, diagnosis_status="failed", result=None, error="boom", trace=None)
    assert row["verdict"] == VERDICT_SYSTEM_FAILED


def test_score_schema_failed():
    case = _case()
    result = {"root_cause_code": "NOT_A_REAL_CODE", "insufficient_evidence": False, "evidence": []}
    row = score_case(case, fixture_ready=True, diagnosis_status="completed", result=result, error=None, trace=None)
    assert row["verdict"] == VERDICT_SCHEMA_FAILED


def test_score_abstention():
    case = _case()
    case.ground_truth.abstention_expected = True
    good = score_case(case, fixture_ready=True, diagnosis_status="completed",
                      result={"root_cause_code": None, "insufficient_evidence": True, "evidence": []},
                      error=None, trace=None)
    assert good["verdict"] == VERDICT_CORRECT
    assert good["abstention_correct"] is True
    bad = score_case(case, fixture_ready=True, diagnosis_status="completed",
                     result={"root_cause_code": "CONFIG_ERROR", "insufficient_evidence": False, "evidence": []},
                     error=None, trace=None)
    assert bad["verdict"] == VERDICT_INCORRECT


def test_score_conflicting_abstention_is_not_valid_abstention():
    case = _case()
    case.ground_truth.abstention_expected = True
    result = {"root_cause_code": "CONFIG_ERROR", "insufficient_evidence": True, "evidence": []}
    row = score_case(case, fixture_ready=True, diagnosis_status="completed",
                     result=result, error=None, trace=None)
    assert row["valid_abstention"] is False
    assert row["conflicting_abstention"] is True
    assert row["wrong_root_cause"] is True
    assert row["abstention_correct"] is False
    assert row["verdict"] == VERDICT_INCORRECT


def test_score_extra_evidence_not_fabricated_but_lowers_match_ratio():
    case = _case()
    result = {
        "root_cause_code": "CONTAINER_OOMKILLED", "insufficient_evidence": False,
        "evidence": [
            {"source": "kubernetes.status",
             "path": "status.containerStatuses[0].lastState.terminated.reason",
             "operator": "equals", "value": "OOMKilled"},
            {"source": "kubernetes.logs", "path": "data", "operator": "contains", "value": "noise"},
        ],
    }
    row = score_case(case, fixture_ready=True, diagnosis_status="completed",
                     result=result, error=None, trace=None)
    assert row["evidence_recall"] == 1.0
    assert row["required_evidence_match_ratio"] == 0.5
    assert row["evidence_returned"] == 2


def test_score_duplicate_evidence_deduped_not_inflating():
    case = _case()
    req = {"source": "kubernetes.status",
           "path": "status.containerStatuses[0].lastState.terminated.reason",
           "operator": "equals", "value": "OOMKilled"}
    result = {"root_cause_code": "CONTAINER_OOMKILLED", "insufficient_evidence": False,
              "evidence": [dict(req), dict(req)]}
    row = score_case(case, fixture_ready=True, diagnosis_status="completed",
                     result=result, error=None, trace=None)
    assert row["evidence_returned"] == 1
    assert row["required_evidence_match_ratio"] == 1.0
    assert row["evidence_recall"] == 1.0


def test_report_zero_denominator_is_null():
    case = _case()
    rows = [_row(case, verdict=VERDICT_CORRECT, abstention_expected=True, abstention_correct=True)]
    rep = build_report(rows)
    assert rep["root_cause_accuracy"] is None
    assert rep["answerable_coverage"] is None
    assert rep["abstention_recall"] == 1.0


def test_report_no_fixture_ok_yields_null_rates():
    case = _case()
    rows = [_row(case, verdict=VERDICT_FIXTURE_FAILED, fixture_ready=False, root_cause_correct=None)]
    rep = build_report(rows)
    assert rep["end_to_end_correct_rate"] is None
    assert rep["wrong_root_cause_rate"] is None
    assert rep["fixture_failed_rate"] == 1.0


def test_score_conflicting_abstention_on_answerable_is_invalid_not_correct():
    """insufficient_evidence=true with an explicit root cause is contradictory
    output: even if the code matches ground truth it must not be scored correct,
    and it must not be silently dropped (explicit risk marker stays)."""
    case = _case()
    result = {
        "root_cause_code": "CONTAINER_OOMKILLED", "root_cause": "内存超限",
        "insufficient_evidence": True,
        "evidence": [{"source": "kubernetes.status",
                      "path": "status.containerStatuses[0].lastState.terminated.reason",
                      "operator": "equals", "value": "OOMKilled"}],
    }
    row = score_case(case, fixture_ready=True, diagnosis_status="completed",
                     result=result, error=None, trace=None)
    assert row["conflicting_abstention"] is True
    assert row["invalid_output"] is True
    assert row["explicit_root_cause"] is True
    assert row["root_cause_correct"] is False
    assert row["wrong_root_cause"] is False
    assert row["verdict"] == VERDICT_INCORRECT


def test_summarize_trace_unknown_tokens_are_none_not_zero():
    empty = summarize_trace(None)
    assert empty["token_usage"] is None
    assert empty["token_usage_complete"] is False
    no_usage = summarize_trace({"spans": [
        {"kind": "llm_call", "name": "llm.call", "attributes": {"duration_ms": 1.0}},
    ]})
    assert no_usage["token_usage"] is None
    assert no_usage["token_usage_complete"] is False


def test_required_evidence_missing_operator_does_not_match():
    from eval.cases import EvidenceRequirement
    case = _case()
    case.ground_truth.required_evidence = [EvidenceRequirement(
        source="kubernetes.status", path="p", operator="equals", value="OOMKilled")]
    result = {"root_cause_code": "CONTAINER_OOMKILLED", "insufficient_evidence": False,
              "evidence": [{"source": "kubernetes.status", "path": "p", "value": "OOMKilled"}]}
    row = score_case(case, fixture_ready=True, diagnosis_status="completed",
                     result=result, error=None, trace=None)
    assert row["evidence_recall"] == 0.0
    assert row["required_evidence_match_ratio"] == 0.0


def test_required_evidence_resource_uid_enforced():
    from eval.cases import EvidenceRequirement
    req = EvidenceRequirement(source="kubernetes.status", path="p", operator="equals",
                              value="v", resource_uid="uid-1")
    case = _case()
    case.ground_truth.required_evidence = [req]
    good = {"root_cause_code": "CONTAINER_OOMKILLED", "insufficient_evidence": False,
            "evidence": [{"source": "kubernetes.status", "path": "p", "operator": "equals",
                          "value": "v", "resource_uid": "uid-1"}]}
    bad = {"root_cause_code": "CONTAINER_OOMKILLED", "insufficient_evidence": False,
           "evidence": [{"source": "kubernetes.status", "path": "p", "operator": "equals",
                         "value": "v", "resource_uid": "uid-2"}]}
    assert score_case(case, fixture_ready=True, diagnosis_status="completed",
                      result=good, error=None, trace=None)["evidence_recall"] == 1.0
    assert score_case(case, fixture_ready=True, diagnosis_status="completed",
                      result=bad, error=None, trace=None)["evidence_recall"] == 0.0


def test_summarize_trace_counts():
    trace = {"spans": [
        {"kind": "llm_call", "attributes": {"prompt_tokens": 100, "completion_tokens": 50}},
        {"kind": "tool_call", "name": "tool.inspect", "attributes": {"args_summary": '{"kind":"Pod"}'}},
        {"kind": "tool_call", "name": "tool.inspect", "attributes": {"args_summary": '{"kind":"Pod"}'}},
        {"kind": "tool_call", "name": "tool.logs", "attributes": {"truncated": True}},
        {"name": "diagnosis", "attributes": {"duration_ms": 1234.0}},
    ]}
    out = summarize_trace(trace)
    assert out["llm_calls"] == 1
    assert out["tool_calls"] == 3
    assert out["duplicate_tool_calls"] == 1
    assert out["token_usage"] == 150
    assert out["duration_ms"] == 1234.0
    assert out["truncated_logs"] is True


# ---- reporter ----

def test_report_reproducible_from_jsonl(tmp_path):
    case = _case()
    rows = [_row(case) for _ in range(3)] + [_row(case, verdict=VERDICT_INCORRECT, wrong_root_cause=True)]
    write_jsonl(tmp_path / "case-results.jsonl", rows)

    reloaded = [json.loads(line) for line in (tmp_path / "case-results.jsonl").read_text().splitlines()]
    report1 = build_report(rows)
    report2 = build_report(reloaded)
    assert report1 == report2
    assert report1["total_runs"] == 4
    assert report1["valid_runs"] == 4
    assert report1["root_cause_accuracy"] == 0.75
    assert report1["wrong_root_cause_rate"] == 0.25


# ---- runner: Phase 4 retrieval gates ----

class _FakeResp:
    def __init__(self, body, ok=True):
        self._body = body
        self.ok = ok

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("boom")

    def json(self):
        return self._body


def _install_fake_agent(monkeypatch, seen):
    def _post(url, json=None, timeout=None, trust_env=False):
        seen["payload"] = json
        return _FakeResp({"diagnosis_id": "diag-1"})

    def _get(url, timeout=None, trust_env=False):
        return _FakeResp({"diagnosis_id": "diag-1", "status": "completed", "result": None})

    monkeypatch.setattr("eval.runner.httpx.post", _post)
    monkeypatch.setattr("eval.runner.httpx.get", _get)


def _make_runner(tmp_path):
    from eval.runner import Runner
    return Runner(agent_url="http://agent.test", trace_dir=None, kubeconfig=None,
                  reports_dir=str(tmp_path))


def test_run_diagnosis_forwards_retrieval_gates(monkeypatch, tmp_path):
    case = _case()
    seen = {}
    _install_fake_agent(monkeypatch, seen)
    base = {"eval_run_id": "r1", "case_id": case.id, "case_version": case.case_version,
            "attempt_index": 0}
    runner = _make_runner(tmp_path)

    diagnosis, error = runner._run_diagnosis(case, uid="u-1", base=base, timeout=10,
                                             enable_knowledge=False, enable_incidents=True)
    assert error is None
    assert diagnosis is not None
    assert seen["payload"]["enable_knowledge"] is False
    assert seen["payload"]["enable_incidents"] is True


def test_run_diagnosis_omits_gates_when_auto(monkeypatch, tmp_path):
    case = _case()
    seen = {}
    _install_fake_agent(monkeypatch, seen)
    base = {"eval_run_id": "r2", "case_id": case.id, "case_version": case.case_version,
            "attempt_index": 0}
    runner = _make_runner(tmp_path)

    diagnosis, error = runner._run_diagnosis(case, uid="u-2", base=base, timeout=10)
    assert error is None
    assert diagnosis is not None
    assert "enable_knowledge" not in seen["payload"]
    assert "enable_incidents" not in seen["payload"]


def test_cli_tri_state_parsing():
    from eval.cli import _tri_state
    assert _tri_state("on") is True
    assert _tri_state("off") is False
    assert _tri_state("auto") is None

def test_compare_marks_mixed_scorer_versions_incomparable(tmp_path):
    from eval.compare import compare_runs

    def _mk(name, ver, verdict):
        d = tmp_path / name
        d.mkdir()
        rep = {"run": {"run_id": name}, "report": {
            "scorer_version": ver,
            "root_cause_accuracy": 0.5, "wrong_root_cause_rate": 0.0,
            "schema_valid_rate": 1.0, "evidence_recall_avg": 0.5,
            "diagnosis_duration_ms": {"p50": 1, "p95": 1},
            "token_usage": {"p50": 1, "p95": 1}}}
        (d / "report.json").write_text(json.dumps(rep), encoding="utf-8")
        (d / "case-results.jsonl").write_text(
            json.dumps({"case_id": "c", "verdict": verdict}) + "\n", encoding="utf-8")
        return d

    base = _mk("b", "1", "diagnosis_correct")
    cand = _mk("c", "2", "diagnosis_incorrect")
    out = compare_runs(base, cand)
    assert out["comparable"] is False
    assert "scorer_version" in out["incomparable_reason"]
    assert out["aggregate"]["root_cause_accuracy"]["delta"] is None

def test_score_evidence_extra_and_unsupported_counts():
    from eval.cases import EvidenceRequirement
    case = _case()
    case.ground_truth.required_evidence = [EvidenceRequirement(
        source="kubernetes.status", path="p", operator="equals", value="v")]
    result = {"root_cause_code": "CONTAINER_OOMKILLED", "insufficient_evidence": False,
              "evidence": [
                  {"source": "kubernetes.status", "path": "p", "operator": "equals", "value": "v"},
                  {"source": "kubernetes.events", "value": "x"},
                  {"summary": "no identifiers"},
              ]}
    row = score_case(case, fixture_ready=True, diagnosis_status="completed",
                     result=result, error=None, trace=None)
    assert row["evidence_total_entries"] == 3
    assert row["evidence_matched_entries"] == 1
    assert row["evidence_extra_entries"] == 1
    assert row["evidence_unsupported_entries"] == 1


def test_report_evidence_rates_and_llm_duration():
    case = _case()
    rows = [
        _row(case, evidence_total_entries=4, evidence_matched_entries=2,
             evidence_extra_entries=2, evidence_unsupported_entries=1, llm_duration_ms=100.0),
        _row(case, evidence_total_entries=0, evidence_matched_entries=0,
             evidence_extra_entries=0, evidence_unsupported_entries=0, llm_duration_ms=300.0),
    ]
    rep = build_report(rows)
    assert rep["evidence_extra_rate"] == 0.5
    assert rep["evidence_unsupported_rate"] == 0.25
    assert rep["llm_duration_ms"]["p50"] == 100.0
    assert rep["llm_duration_ms"]["mean"] == 200.0
