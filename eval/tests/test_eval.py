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


def _row(case, *, verdict=VERDICT_CORRECT, **kw):
    row = {
        "case_id": case.id,
        "case_version": case.case_version,
        "verdict": verdict,
        "root_cause_correct": verdict == VERDICT_CORRECT and not case.ground_truth.abstention_expected,
        "abstention_correct": verdict == VERDICT_CORRECT if case.ground_truth.abstention_expected else None,
        "wrong_root_cause": False,
        "evidence_recall": None,
        "evidence_precision": None,
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
