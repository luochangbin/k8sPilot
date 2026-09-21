"""Unit tests for eval: loader, jsonpath, scorer, reporter reproducibility."""

import json
import shutil
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
    assert len(files) == 17, f"expected 17 cases, got {len(files)}"
    supported = {"Pod", "Deployment", "ReplicaSet", "StatefulSet", "DaemonSet",
                 "Service", "Node", "PersistentVolumeClaim", "Namespace"}
    for f in files:
        case = load_case(f)
        assert case.id == f.stem
        assert case.target.kind in supported, f"{case.id}: unsupported kind"
        if case.target.kind not in ("Node", "Namespace"):
            assert case.target.namespace, f"{case.id}: namespaced target needs a namespace"
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


def test_summarize_trace_reports_effective_budgets_and_policy_rejections():
    trace = {"spans": [
        {"name": "diagnosis", "attributes": {
            "status": "completed", "duration_ms": 10.0,
            "max_tool_calls": 3, "max_agent_rounds": 5,
            "rounds_used": 4, "tool_calls_used": 3, "multi_tool_rejected_rounds": 2}},
    ]}
    out = summarize_trace(trace)
    assert out["effective_max_tool_calls"] == 3
    assert out["effective_max_agent_rounds"] == 5
    assert out["rounds_used"] == 4
    assert out["multi_tool_rejected_rounds"] == 2


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
        if str(url).endswith("/cancel"):
            seen["cancel"] = True
            return _FakeResp({"diagnosis_id": "diag-1", "cancel_requested": True})
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


def test_report_separates_budget_exhaustion_from_abstention():
    """Budget exhaustion is a planning failure: it must not be counted as a
    valid abstention, and it is reported on its own."""
    case = _case(ground_truth=_abstention_ground_truth())
    exhausted = {
        **_row(case, verdict=VERDICT_SYSTEM_FAILED),
        "abstention_expected": False,
        "budget_exhausted": True,
        "failure_reason": "budget_exhausted",
        "multi_tool_rejected_rounds": 2,
    }
    report = build_report([exhausted])
    assert report["budget_exhausted_count"] == 1
    assert report["budget_exhausted_rate"] == 1.0
    assert report["multi_tool_rejected_rounds_total"] == 2
    assert report["abstention_recall"] is None  # no abstention case was scored


def test_run_diagnosis_forwards_case_budgets(monkeypatch, tmp_path):
    from eval.cases import Budgets
    case = _case(budgets=Budgets(max_tool_calls=3, max_agent_rounds=4))
    seen = {}
    _install_fake_agent(monkeypatch, seen)
    base = {"eval_run_id": "r3", "case_id": case.id, "case_version": case.case_version,
            "attempt_index": 0}
    runner = _make_runner(tmp_path)

    diagnosis, error = runner._run_diagnosis(case, uid="u-3", base=base, timeout=10)
    assert error is None and diagnosis is not None
    assert seen["payload"]["eval_max_tool_calls"] == 3
    assert seen["payload"]["eval_max_agent_rounds"] == 4


def _abstention_ground_truth():
    from eval.cases import GroundTruth
    return GroundTruth(accepted_root_cause_codes=[], abstention_expected=True)


def test_timeout_cancels_the_agent_and_reports_the_terminal_state(monkeypatch, tmp_path):
    case = _case()
    seen = {}
    _install_fake_agent(monkeypatch, seen)
    base = {"eval_run_id": "r4", "case_id": case.id, "case_version": case.case_version,
            "attempt_index": 0}
    runner = _make_runner(tmp_path)

    diagnosis, error = runner._run_diagnosis(case, uid="u-4", base=base, timeout=0)

    assert seen.get("cancel") is True, "the runner must cancel on timeout"
    assert error is not None and "timed out" in error
    assert diagnosis is not None and diagnosis["status"] == "completed"


def test_required_evidence_contains_operator_matches_event_messages():
    """Event/log evidence is not an exact string: ground truth may use
    `operator: contains`, and evidence may omit its own operator."""
    from eval.scorer import SCORER_VERSION, _matches_required
    from eval.cases import EvidenceRequirement

    req = EvidenceRequirement(source="kubernetes.events", path="", operator="contains",
                              value="didn't match")
    assert SCORER_VERSION == "5"
    assert _matches_required(
        {"source": "kubernetes.events",
         "value": "0/2 nodes are available: 2 node(s) didn't match Pod's node affinity/selector."},
        req)
    assert not _matches_required({"source": "kubernetes.events", "value": "Insufficient cpu"}, req)

    # Exact equality remains the default and stays strict: the evidence must
    # declare the same operator (a looser claim must not pass as an exact fact).
    exact = EvidenceRequirement(source="kubernetes.status", path="actual_state.phase",
                                operator="equals", value="Pending")
    assert _matches_required({"source": "kubernetes.status", "path": "actual_state.phase",
                              "operator": "equals", "value": "Pending"}, exact)
    assert not _matches_required({"source": "kubernetes.status", "path": "actual_state.phase",
                                  "value": "Pending"}, exact)
    assert not _matches_required({"source": "kubernetes.status", "path": "actual_state.phase",
                                  "operator": "contains", "value": "Pending"}, exact)

    # contains is symmetric with equals: a conflicting declared operator fails.
    assert _matches_required({"source": "kubernetes.events", "operator": "contains",
                              "value": "didn't match"}, req)
    assert not _matches_required({"source": "kubernetes.events", "operator": "equals",
                                  "value": "didn't match"}, req)


def test_case_suite_is_well_formed_and_cause_level():
    """The suite must express causes, not Kubernetes failure modes, and cover
    Pod / Deployment / Node / PVC targets."""
    import app.root_causes as rc
    from eval.cases import load_case

    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    # Cases that can only inject a coarse failure in this environment, with the
    # reason recorded in their description.
    coarse_allowed = {"pod-imagepullauth-001", "pod-imagepullbackoff-001"}

    ids, kinds = set(), set()
    cause_level = 0
    for path in sorted(cases_dir.glob("*.yaml")):
        found = load_case(path)
        assert found.id not in ids, f"duplicate case id {found.id}"
        ids.add(found.id)
        kinds.add(found.target.kind)
        for manifest in found.setup_manifests:
            assert manifest.exists(), f"{found.id}: missing manifest {manifest}"
        accepted = found.ground_truth.accepted_root_cause_codes
        assert accepted or found.ground_truth.abstention_expected, \
            f"{found.id}: no accepted code and not an abstention case"
        for code in accepted:
            assert code in rc.ROOT_CAUSE_CODES, f"{found.id}: unknown code {code}"
            if code in rc.FAILURE_MODE_CODES:
                assert found.id in coarse_allowed, \
                    f"{found.id} accepts a failure-mode code {code}"
        if not (set(accepted) & rc.FAILURE_MODE_CODES):
            cause_level += 1

    assert {"Pod", "Deployment", "Node", "PersistentVolumeClaim"} <= kinds
    # Same surface symptom, different causes: the two FailedScheduling cases must
    # not collapse into one code.
    scheduling = [set(load_case(p).ground_truth.accepted_root_cause_codes)
                  for p in cases_dir.glob("pod-failedscheduling-*.yaml")]
    assert {"NODE_SELECTOR_MISMATCH"} in scheduling
    assert {"INSUFFICIENT_NODE_RESOURCES"} in scheduling
    # The suite is dominated by cause-level ground truth.
    assert cause_level >= 12


def test_cause_level_suite_declares_multi_kind_coverage():
    """Benchmark coverage must come from the *suite*, not from whatever files
    happen to sit in eval/cases/."""
    from eval.cases import load_case, load_suite, resolve_case_entry

    suites_dir = Path(__file__).resolve().parents[1] / "suites"
    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    suite = load_suite(suites_dir / "cause-level.yaml")
    declared = suite["cases"]
    # The coarse image-pull compatibility cases stay in the frozen baseline.
    assert "pod-imagepullauth-001" not in declared
    assert "pod-imagepullbackoff-001" not in declared
    assert len(declared) >= 15

    kinds = set()
    for case_id in declared:
        found = load_case(resolve_case_entry(cases_dir, case_id))
        assert found.id == case_id.split("@")[0]
        kinds.add(found.target.kind)
    assert {"Pod", "Deployment", "Node", "PersistentVolumeClaim"} <= kinds

    # The frozen baseline suite stays untouched and remains a Pod-only baseline.
    phase1 = load_suite(suites_dir / "phase1.yaml")
    phase1_cases = [load_case(resolve_case_entry(cases_dir, cid)) for cid in phase1["cases"]]
    assert {c.target.kind for c in phase1_cases} == {"Pod"}
    # The frozen baseline pins the historical definitions...
    assert all(cid.endswith("@1") for cid in phase1["cases"])
    # ...and the pinned ground truth is the old failure-mode vocabulary, not the
    # current cause-level one, so re-running phase1 reproduces the old numbers.
    crashloop = next(c for c in phase1_cases if c.id == "pod-crashloop-001")
    assert crashloop.case_version == "1"
    assert crashloop.ground_truth.accepted_root_cause_codes == ["CRASH_LOOP_BACKOFF"]
    current = load_case(cases_dir / "pod-crashloop-001.yaml")
    assert current.ground_truth.accepted_root_cause_codes == ["APPLICATION_EXIT_NONZERO"]


def test_registry_preflight_fails_closed(monkeypatch):
    """IMAGE_NOT_FOUND cases are only allowed to run when the registry is
    reachable and the tag is truly absent."""
    import eval.injector as injector

    class Resp:
        def __init__(self, status, headers=None, payload=None):
            self.status_code = status
            self.headers = headers or {}
            self._payload = payload or {}

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise injector.httpx.HTTPStatusError("bad", request=None, response=None)

    # 404 with MANIFEST_UNKNOWN (tag absent) -> preflight passes.
    monkeypatch.setattr(injector.httpx, "get", lambda *a, **k: Resp(
        404, payload={"errors": [{"code": "MANIFEST_UNKNOWN"}]}))
    injector.check_registry_tag_absent("docker.io/library/busybox:missing-tag-001")

    # A bare 404 (no error code) is NOT proof of a missing manifest.
    monkeypatch.setattr(injector.httpx, "get", lambda *a, **k: Resp(404))
    with pytest.raises(injector.InjectorError):
        injector.check_registry_tag_absent("docker.io/library/busybox:missing-tag-001")

    # Any other status -> the case must fail closed.
    monkeypatch.setattr(injector.httpx, "get", lambda *a, **k: Resp(500))
    with pytest.raises(injector.InjectorError):
        injector.check_registry_tag_absent("docker.io/library/busybox:missing-tag-001")

    # A 302 that redirects to a non-JSON auth page must fail closed.
    class RedirectResp:
        status_code = 302
        headers = {"WWW-Authenticate":
                   'Bearer realm="https://auth.example/token",service="reg",scope="repository:x/y:pull"'}

        def json(self):
            raise ValueError("not json")

        def raise_for_status(self):
            return None

    monkeypatch.setattr(injector.httpx, "get", lambda *a, **k: RedirectResp())
    with pytest.raises(injector.InjectorError):
        injector.check_registry_tag_absent("registry.example/x/y:missing")

    # Unreachable registry -> fail closed.
    def _boom(*a, **k):
        raise injector.httpx.ConnectError("no route")

    monkeypatch.setattr(injector.httpx, "get", _boom)
    with pytest.raises(injector.InjectorError):
        injector.check_registry_tag_absent("docker.io/library/busybox:missing-tag-001")


def test_registry_preflight_handles_bearer_challenge(monkeypatch):
    import eval.injector as injector

    calls = []

    class Resp:
        def __init__(self, status, headers=None, payload=None):
            self.status_code = status
            self.headers = headers or {}
            self._payload = payload or {}

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError("unexpected raise_for_status")

    def fake_get(url, headers=None, timeout=None, trust_env=False, **kwargs):
        calls.append(url)
        if url.startswith("https://auth.example"):
            return Resp(200, payload={"token": "tok"})
        if headers and headers.get("Authorization") == "Bearer tok":
            return Resp(404, payload={"errors": [{"code": "MANIFEST_UNKNOWN"}]})
        return Resp(401, headers={"WWW-Authenticate":
                                  'Bearer realm="https://auth.example/token",service="reg",scope="repository:x/y:pull"'})

    monkeypatch.setattr(injector.httpx, "get", fake_get)
    injector.check_registry_tag_absent("registry.example/x/y:missing-tag", timeout=1)
    assert any(u.startswith("https://auth.example") for u in calls)


def test_preflight_runs_before_manifests_and_creates_nothing(monkeypatch):
    """A failing preflight must leave the cluster untouched."""
    import eval.injector as injector
    from eval.cases import Budgets, Case, GroundTruth, ReadyWhen, Target

    calls = []
    monkeypatch.setattr(injector, "run_kubectl",
                        lambda args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(injector, "check_registry_tag_absent",
                        lambda image, timeout=15: (_ for _ in ()).throw(
                            injector.InjectorError("registry unreachable")))

    case = Case(schema_version="eval.k8spilot.io/v1alpha1", id="t-pre", case_version="1",
                suite="test", description="", target=Target("v1", "Pod", "ns", "p"),
                setup_manifests=[Path(__file__)], ready_when=ReadyWhen(type="jsonpath_exists",
                                                                      path="status", value=""),
                ground_truth=GroundTruth(), budgets=Budgets(),
                preflight=[{"type": "registry_tag_absent", "image": "x/y:z"}])
    with pytest.raises(injector.InjectorError):
        injector.Injector().apply(case)
    assert calls == [], "no kubectl call may happen before preflight passes"


def test_runner_cleans_up_when_injection_fails(monkeypatch, tmp_path):
    from eval.injector import InjectorError
    from eval.runner import Runner

    cleaned = []

    class FailingInjector:
        def apply(self, case):
            raise InjectorError("apply exploded")

        def cleanup(self, case):
            cleaned.append(case.id)

        def get_target_uid(self, case):
            return "uid-1"

        def wait_ready(self, case):
            return True

    runner = Runner(agent_url="http://agent.test", trace_dir=None, kubeconfig=None,
                    reports_dir=str(tmp_path))
    runner._injector = FailingInjector()
    row = runner._run_case(_case(), "run-1", 0)

    assert row["fixture_ready"] is False
    assert "apply exploded" in row["error"]
    assert cleaned == ["t-001"], "a failed injection must still be cleaned up"


def test_registry_preflight_requires_manifest_unknown(monkeypatch):
    import eval.injector as injector

    class Resp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self.headers = {}
            self._payload = payload or {}

        def json(self):
            return self._payload

    # 404 with NAME_UNKNOWN means the repository (not the tag) is unknown.
    monkeypatch.setattr(injector.httpx, "get", lambda *a, **k: Resp(
        404, {"errors": [{"code": "NAME_UNKNOWN"}]}))
    with pytest.raises(injector.InjectorError):
        injector.check_registry_tag_absent("registry.example/x/y:missing")

    # 404 with MANIFEST_UNKNOWN is the deterministic IMAGE_NOT_FOUND case.
    monkeypatch.setattr(injector.httpx, "get", lambda *a, **k: Resp(
        404, {"errors": [{"code": "MANIFEST_UNKNOWN"}]}))
    injector.check_registry_tag_absent("registry.example/x/y:missing")


def test_event_ready_condition_requires_the_cluster_side_semantics(monkeypatch):
    """`event_message_contains` only turns ready once the target's Events carry
    the expected failure semantics, so DNS/TLS/network failures cannot pass."""
    import eval.injector as injector
    from eval.cases import Budgets, Case, GroundTruth, ReadyWhen, Target

    events = {"items": [
        {"reason": "Failed", "message": 'Failed to pull image "x/y:z": network timeout'},
    ]}
    monkeypatch.setattr(injector, "run_kubectl", lambda args, **k: json.dumps(events))

    def make_case(value, reason="Failed", timeout=1):
        return Case(schema_version="eval.k8spilot.io/v1alpha1", id="t-ev", case_version="1",
                    suite="test", description="", target=Target("v1", "Pod", "ns", "p"),
                    ready_when=ReadyWhen(type="event_message_contains", path=reason,
                                         value=value, timeout_seconds=timeout),
                    ground_truth=GroundTruth(), budgets=Budgets())

    injector_instance = injector.Injector()
    assert injector_instance._events_contain(make_case("network timeout")) is True
    assert injector_instance._events_contain(make_case("manifest unknown")) is False
    assert injector_instance._events_contain(make_case("network timeout",
                                                       reason="BackOff")) is False
    # wait_ready polls until the deadline and reports False (fixture_failed).
    assert injector_instance.wait_ready(make_case("manifest unknown", timeout=1)) is False


def test_image_notfound_case_loads_top_level_preflight_and_composite_ready():
    """The preflight must actually load from the case YAML (it is a top-level
    field), and readiness must require both the event and the steady state."""
    from eval.cases import load_case

    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    case = load_case(cases_dir / "pod-image-notfound-001.yaml")

    assert case.preflight, "preflight must be loaded from the case file"
    assert case.preflight[0]["type"] == "registry_tag_absent"
    assert case.preflight[0]["image"].endswith("k8spilot-missing-tag-001")
    assert case.ready_when.type == "all"
    kinds = [c["type"] for c in case.ready_when.conditions]
    assert kinds == ["event_message_contains", "jsonpath_equals"]
    assert case.ready_when.conditions[1]["value"] == "ImagePullBackOff"


def test_composite_ready_needs_both_event_and_status(monkeypatch):
    import eval.injector as injector
    from eval.cases import Budgets, Case, GroundTruth, ReadyWhen, Target

    events = {"items": [{"reason": "Failed",
                         "message": 'Failed to pull image "x/y:z": manifest unknown'}]}
    monkeypatch.setattr(injector, "run_kubectl", lambda args, **k: json.dumps(events))

    statuses = {"reason": "ErrImagePull"}
    monkeypatch.setattr(injector, "get_object",
                        lambda kind, ns, name, **k: {"status": {"containerStatuses": [
                            {"state": {"waiting": {"reason": statuses["reason"]}}}]}})

    case = Case(schema_version="eval.k8spilot.io/v1alpha1", id="t-all", case_version="1",
                suite="test", description="", target=Target("v1", "Pod", "ns", "p"),
                ready_when=ReadyWhen(type="all", timeout_seconds=1, conditions=[
                    {"type": "event_message_contains", "path": "Failed",
                     "value": "manifest unknown"},
                    {"type": "jsonpath_equals",
                     "path": "status.containerStatuses[0].state.waiting.reason",
                     "value": "ImagePullBackOff"}]),
                ground_truth=GroundTruth(), budgets=Budgets())
    injector_instance = injector.Injector()

    # Event matches but the container is still in ErrImagePull: not ready yet.
    assert injector_instance.wait_ready(case) is False

    statuses["reason"] = "ImagePullBackOff"
    assert injector_instance.wait_ready(case) is True


def test_compare_refuses_case_set_and_vocabulary_drift(tmp_path):
    """A re-pointed suite or a changed vocabulary is not a comparable delta."""
    from eval.compare import compare_runs
    from eval.reporter import write_json, write_jsonl

    def make_run(name, cases, vocab, scorer="5"):
        run_dir = tmp_path / name
        run_dir.mkdir()
        row = {"case_id": cases[0].split("@")[0], "case_version": cases[0].split("@")[1],
               "verdict": "diagnosis_correct", "fixture_ready": True,
               "abstention_expected": False, "root_cause_correct": True,
               "effective_max_tool_calls": 12, "effective_max_agent_rounds": 12,
               "max_finalization_attempts": 1}
        write_jsonl(run_dir / "case-results.jsonl", [row])
        write_json(run_dir / "report.json", {
            "run": {"run_id": name, "cases": cases, "scorer_version": scorer,
                    "root_cause_vocabulary_version": vocab},
            "report": {"scorer_version": scorer, "root_cause_accuracy": 1.0,
                       "wrong_root_cause_rate": 0.0, "schema_valid_rate": 1.0,
                       "evidence_recall_avg": None, "diagnosis_duration_ms": {"p50": None},
                       "token_usage": {"p50": None}},
            "by_case": {},
        })
        return run_dir

    same = compare_runs(make_run("b1", ["pod-crashloop-001@1"], "v2"),
                        make_run("c1", ["pod-crashloop-001@1"], "v2"))
    assert same["comparable"] is True

    drift = compare_runs(make_run("b2", ["pod-crashloop-001@1"], "v2"),
                         make_run("c2", ["pod-crashloop-001@2"], "v2"))
    assert drift["comparable"] is False
    assert "case_set mismatch" in drift["incomparable_reason"]
    assert all(metric["delta"] is None for metric in drift["aggregate"].values())

    vocab = compare_runs(make_run("b3", ["pod-crashloop-001@1"], "v1"),
                         make_run("c3", ["pod-crashloop-001@1"], "v2"))
    assert vocab["comparable"] is False
    assert "root_cause_vocabulary mismatch" in vocab["incomparable_reason"]


def test_reports_keep_case_versions_separate():
    """id@version must not be merged: v1 and v2 are different measurements."""
    from eval.reporter import report_by_case

    base = _row(_case(), verdict="diagnosis_correct")
    rows = [
        {**base, "case_id": "c-1", "case_version": "1", "model_profile": "m1"},
        {**base, "case_id": "c-1", "case_version": "2", "model_profile": "m1",
         "verdict": "diagnosis_incorrect"},
        {**base, "case_id": "c-1", "case_version": "1", "model_profile": "m2"},
        {**base, "case_id": "c-1", "case_version": "2", "model_profile": "m2",
         "verdict": "diagnosis_incorrect"},
    ]
    by_case = report_by_case(rows)
    assert set(by_case) == {"c-1@1", "c-1@2"}
    assert by_case["c-1@1"]["n"] == 2
    assert by_case["c-1@2"]["n"] == 2

    from eval.benchmark import build_benchmark_report
    report = build_benchmark_report("bench-1",
                                    {"models": ["m1", "m2"], "case_budgets": {}}, rows, None)
    assert set(report["per_case_common"]) == {"c-1@1", "c-1@2"}


def test_build_plan_and_budgets_use_case_version_keys():
    from eval.benchmark import build_plan
    from eval.cases import load_case_entry

    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    cases = [load_case_entry(cases_dir, "pod-crashloop-001@1"),
             load_case_entry(cases_dir, "pod-crashloop-001@2")]
    plan = build_plan(cases, ["m"], 1, seed=42)
    assert {p["case_version"] for p in plan} == {"1", "2"}
    assert {p["attempt_id"].split("__")[0] for p in plan} == {
        "pod-crashloop-001@1", "pod-crashloop-001@2"}


def test_pinned_cases_use_frozen_manifests():
    """A pinned case must keep its own fixture: editing the current manifest
    cannot change what phase1 injects."""
    from eval.cases import load_case_entry

    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    pinned = load_case_entry(cases_dir, "pod-crashloop-001@1")
    current = load_case_entry(cases_dir, "pod-crashloop-001")

    pinned_manifest = pinned.setup_manifests[0]
    assert pinned_manifest.parent.name == "manifests"
    assert pinned_manifest.parent.parent.name == "versions"
    assert pinned_manifest != current.setup_manifests[0]
    assert pinned_manifest.is_file()
    # Same failure mechanism, frozen copy: the exit-1 command is in the pinned file.
    assert "exit 1" in pinned_manifest.read_text(encoding="utf-8")


def test_load_case_entry_rejects_a_mislabelled_pinned_file(tmp_path):
    """A file whose name says v1 but whose content says v2 must not load."""
    from eval.cases import CaseError, load_case_entry
    from eval.cases import load_case

    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    source = (cases_dir / "pod-crashloop-001.yaml").read_text(encoding="utf-8")
    bogus_dir = tmp_path / "cases" / "versions"
    bogus_dir.mkdir(parents=True)
    (bogus_dir / "pod-crashloop-001.v1.yaml").write_text(source, encoding="utf-8")
    # Provide the manifest so the *only* problem is the mislabelled version.
    manifest_dir = tmp_path / "cases" / "manifests"
    manifest_dir.mkdir(parents=True)
    shutil.copy(cases_dir / "manifests" / "pod-crashloop-001.yaml",
                manifest_dir / "pod-crashloop-001.yaml")

    with pytest.raises(CaseError):
        load_case_entry(tmp_path / "cases", "pod-crashloop-001@1")
    # The file itself is fine; only the suite entry was mislabelled.
    assert load_case(bogus_dir / "pod-crashloop-001.v1.yaml").case_version == "2"


def test_compare_requires_vocabulary_version_on_both_sides(tmp_path):
    from eval.compare import compare_runs
    from eval.reporter import write_json, write_jsonl

    def make_run(name, vocab):
        run_dir = tmp_path / name
        run_dir.mkdir()
        write_jsonl(run_dir / "case-results.jsonl", [
            {"case_id": "pod-crashloop-001", "case_version": "1",
             "verdict": "diagnosis_correct", "fixture_ready": True,
             "abstention_expected": False, "effective_max_tool_calls": 12,
             "effective_max_agent_rounds": 12, "max_finalization_attempts": 1}])
        write_json(run_dir / "report.json", {
            "run": {"run_id": name, "cases": ["pod-crashloop-001@1"],
                    "scorer_version": "5", "root_cause_vocabulary_version": vocab},
            "report": {"scorer_version": "5", "root_cause_accuracy": 1.0,
                       "wrong_root_cause_rate": 0.0, "schema_valid_rate": 1.0,
                       "evidence_recall_avg": None, "diagnosis_duration_ms": {"p50": None},
                       "token_usage": {"p50": None}},
            "by_case": {},
        })
        return run_dir

    missing = compare_runs(make_run("b-missing", None), make_run("c-known", "v2"))
    assert missing["comparable"] is False
    assert "root_cause_vocabulary_version missing" in missing["incomparable_reason"]


def test_cli_accepts_a_bare_case_id_when_the_suite_has_one_version(tmp_path):
    from eval.cli import _load_case_ids

    suite = tmp_path / "suite.yaml"
    suite.write_text("id: t\ncases:\n  - pod-crashloop-001@1\n  - pod-oomkilled-001@1\n",
                     encoding="utf-8")
    assert _load_case_ids(suite, ["pod-crashloop-001"]) == ["pod-crashloop-001@1"]
    assert _load_case_ids(suite, ["pod-crashloop-001@1"]) == ["pod-crashloop-001@1"]

    # Two versions of the same id require an explicit version.
    suite2 = tmp_path / "suite2.yaml"
    suite2.write_text("id: t2\ncases:\n  - pod-crashloop-001@1\n  - pod-crashloop-001@2\n",
                      encoding="utf-8")
    with pytest.raises(Exception):
        _load_case_ids(suite2, ["pod-crashloop-001"])
    assert _load_case_ids(suite2, ["pod-crashloop-001@2"]) == ["pod-crashloop-001@2"]


def test_benchmark_report_flags_uneven_attempt_counts():
    """Equal case sets with unequal repetitions are not comparable."""
    from eval.benchmark import build_benchmark_report

    base = {**_row(_case(), verdict="diagnosis_correct"),
            # Identity must be verified, otherwise the benchmark reports
            # identity_unknown independently of the attempt-count question, and
            # effective budgets must be known for the budget signature check.
            "identity_ok": True, "llm_request_attempts": 1, "provider": "test",
            "protocol": "openai_chat_completions", "config_fingerprint": "fp-1",
            "effective_parameters": {}, "effective_max_tool_calls": 12,
            "effective_max_agent_rounds": 12, "max_finalization_attempts": 1}
    rows = []
    for model, reps in (("m1", 3), ("m2", 1)):
        for _ in range(reps):
            rows.append({**base, "case_id": "c-1", "case_version": "1",
                         "model_profile": model})
    report = build_benchmark_report("b", {"models": ["m1", "m2"], "case_budgets": {}}, rows, None)
    assert report["comparable"] is False
    assert "attempt_counts_differ" in report["incomparable_reason"]

    only_m1 = [r for r in rows if r["model_profile"] == "m1"]
    even = only_m1 + [{**r, "model_profile": "m2"} for r in only_m1]
    report = build_benchmark_report("b2", {"models": ["m1", "m2"], "case_budgets": {}}, even, None)
    assert report["comparable"] is True


def test_compare_requires_equal_attempt_counts_and_effective_budgets(tmp_path):
    from eval.compare import compare_runs
    from eval.reporter import write_json, write_jsonl

    def make_run(name, rows, budgets=(12, 12, 1)):
        run_dir = tmp_path / name
        run_dir.mkdir()
        if budgets is None:
            payload = list(rows)
        else:
            payload = [{**r, "effective_max_tool_calls": budgets[0],
                        "effective_max_agent_rounds": budgets[1],
                        "max_finalization_attempts": budgets[2]} for r in rows]
        write_jsonl(run_dir / "case-results.jsonl", payload)
        write_json(run_dir / "report.json", {
            "run": {"run_id": name, "cases": ["pod-crashloop-001@1"],
                    "scorer_version": "5", "root_cause_vocabulary_version": "v2"},
            "report": {"scorer_version": "5", "root_cause_accuracy": 1.0,
                       "wrong_root_cause_rate": 0.0, "schema_valid_rate": 1.0,
                       "evidence_recall_avg": None, "diagnosis_duration_ms": {"p50": None},
                       "token_usage": {"p50": None}},
            "by_case": {},
        })
        return run_dir

    row = {"case_id": "pod-crashloop-001", "case_version": "1",
           "verdict": "diagnosis_correct", "fixture_ready": True,
           "abstention_expected": False}

    counts = compare_runs(make_run("b-count", [row, row]),
                          make_run("c-count", [row]))
    assert counts["comparable"] is False
    assert "case_attempt_count_mismatch" in counts["incomparable_reason"]

    budgets = compare_runs(make_run("b-budget", [row], budgets=(12, 12, 1)),
                           make_run("c-budget", [row], budgets=(6, 12, 1)))
    assert budgets["comparable"] is False
    assert "effective_budget_mismatch" in budgets["incomparable_reason"]

    unknown = compare_runs(make_run("b-unknown", [row], budgets=None),
                           make_run("c-known", [row]))
    assert unknown["comparable"] is False
    assert "effective_budget_unknown" in unknown["incomparable_reason"]

    same = compare_runs(make_run("b-same", [row]), make_run("c-same", [row]))
    assert same["comparable"] is True


def test_compare_detects_case_content_drift(tmp_path):
    from eval.compare import compare_runs
    from eval.reporter import write_json, write_jsonl

    def make_run(name, hashes):
        run_dir = tmp_path / name
        run_dir.mkdir()
        write_jsonl(run_dir / "case-results.jsonl", [
            {"case_id": "pod-crashloop-001", "case_version": "1",
             "verdict": "diagnosis_correct", "fixture_ready": True,
             "abstention_expected": False, "effective_max_tool_calls": 12,
             "effective_max_agent_rounds": 12, "max_finalization_attempts": 1}])
        write_json(run_dir / "report.json", {
            "run": {"run_id": name, "cases": ["pod-crashloop-001@1"],
                    "scorer_version": "5", "root_cause_vocabulary_version": "v2",
                    "case_hashes": hashes},
            "report": {"scorer_version": "5", "root_cause_accuracy": 1.0,
                       "wrong_root_cause_rate": 0.0, "schema_valid_rate": 1.0,
                       "evidence_recall_avg": None, "diagnosis_duration_ms": {"p50": None},
                       "token_usage": {"p50": None}},
            "by_case": {},
        })
        return run_dir

    drift = compare_runs(make_run("b-hash", {"pod-crashloop-001@1": {"definition": "aaa"}}),
                         make_run("c-hash", {"pod-crashloop-001@1": {"definition": "bbb"}}))
    assert drift["comparable"] is False
    assert "case_content_mismatch" in drift["incomparable_reason"]


def test_pathless_evidence_only_matches_business_fields():
    """Metadata (namespace/pod/container/metric names) is not evidence."""
    from app.validation import UNVERIFIABLE, VERIFIED, validate_submission

    def submission(source, value, operator="equals"):
        return {"root_cause_code": "CRASH_LOOP_BACKOFF", "root_cause": "rc",
                "insufficient_evidence": False,
                "evidence": [{"source": source, "operator": operator, "value": value}]}

    logs = [{"tool": "logs", "tool_call_id": "c1", "kind": "realtime",
             "args": {"namespace": "payment", "name": "p", "uid": "u1"},
             "output": json.dumps({"namespace": "payment", "pod": "api-x", "container": "app",
                                   "data": "real log line"})}]
    ok, _, report = validate_submission(submission("kubernetes.logs", "payment"), logs)
    assert not ok and report[0]["status"] == UNVERIFIABLE
    ok, _, report = validate_submission(submission("kubernetes.logs", "real log line",
                                                   operator="contains"), logs)
    assert ok and report[0]["status"] == VERIFIED

    metrics = [{"tool": "query_metrics", "tool_call_id": "c2", "kind": "realtime",
                "args": {"namespace": "payment", "name": "p", "uid": "u1"},
                "output": json.dumps({"metric": "memory", "summary": {"max": 3.0},
                                      "series": []})}]
    ok, _, report = validate_submission(submission("prometheus.metrics", "memory"), metrics)
    assert not ok and report[0]["status"] == UNVERIFIABLE
    ok, _, report = validate_submission(submission("prometheus.metrics", "3.0"), metrics)
    assert ok and report[0]["status"] == VERIFIED

    events = [{"tool": "events", "tool_call_id": "c3", "kind": "realtime",
               "args": {"kind": "Pod", "namespace": "payment", "name": "p", "uid": "u1"},
               "output": json.dumps({"events": [{"type": "Warning", "reason": "BackOff",
                                                 "message": "Back-off restarting"}]})}]
    ok, _, report = validate_submission(submission("kubernetes.events", "BackOff"), events)
    assert ok and report[0]["status"] == VERIFIED
    # Status facts must be addressed by path.
    status = [{"tool": "inspect", "tool_call_id": "c4", "kind": "realtime",
               "args": {"kind": "Pod", "namespace": "payment", "name": "p", "uid": "u1"},
               "output": json.dumps({"actual_state": {"phase": "Pending"}})}]
    ok, _, report = validate_submission(submission("kubernetes.status", "Pending"), status)
    assert not ok and report[0]["status"] == UNVERIFIABLE


def test_cause_level_v1_suite_is_fully_pinned():
    """The future baseline suite must resolve entirely to frozen definitions."""
    from eval.cases import load_case_entry, load_suite

    root = Path(__file__).resolve().parents[1]
    suite = load_suite(root / "suites" / "cause-level-v1.yaml")
    assert suite["cases"], "cause-level-v1 must declare cases"
    for entry in suite["cases"]:
        assert "@" in entry, f"{entry} must be pinned to a version"
        case = load_case_entry(root / "cases", entry)
        for manifest in case.setup_manifests:
            assert "versions" in str(manifest), f"{entry}: manifest not frozen"


def test_compare_treats_unknown_finalization_budget_as_unknown(tmp_path):
    """Unknown != same: a missing finalization budget is not 'equal'."""
    from eval.compare import compare_runs
    from eval.reporter import write_json, write_jsonl

    def make_run(name, finalization):
        run_dir = tmp_path / name
        run_dir.mkdir()
        write_jsonl(run_dir / "case-results.jsonl", [
            {"case_id": "pod-crashloop-001", "case_version": "1",
             "verdict": "diagnosis_correct", "fixture_ready": True,
             "abstention_expected": False, "effective_max_tool_calls": 12,
             "effective_max_agent_rounds": 12, "max_finalization_attempts": finalization}])
        write_json(run_dir / "report.json", {
            "run": {"run_id": name, "cases": ["pod-crashloop-001@1"],
                    "scorer_version": "5", "root_cause_vocabulary_version": "v2"},
            "report": {"scorer_version": "5", "root_cause_accuracy": 1.0,
                       "wrong_root_cause_rate": 0.0, "schema_valid_rate": 1.0,
                       "evidence_recall_avg": None, "diagnosis_duration_ms": {"p50": None},
                       "token_usage": {"p50": None}},
            "by_case": {},
        })
        return run_dir

    result = compare_runs(make_run("b-fin-none", None), make_run("c-fin-none", None))
    assert result["comparable"] is False
    assert "effective_budget_unknown" in result["incomparable_reason"]


def test_incomparable_runs_have_no_per_case_deltas(tmp_path):
    from eval.compare import compare_runs
    from eval.reporter import write_json, write_jsonl

    def make_run(name, scorer):
        run_dir = tmp_path / name
        run_dir.mkdir()
        write_jsonl(run_dir / "case-results.jsonl", [
            {"case_id": "pod-crashloop-001", "case_version": "1",
             "verdict": "diagnosis_correct", "fixture_ready": True,
             "abstention_expected": False, "effective_max_tool_calls": 12,
             "effective_max_agent_rounds": 12, "max_finalization_attempts": 1}])
        write_json(run_dir / "report.json", {
            "run": {"run_id": name, "cases": ["pod-crashloop-001@1"],
                    "scorer_version": scorer, "root_cause_vocabulary_version": "v2"},
            "report": {"scorer_version": scorer, "root_cause_accuracy": 1.0,
                       "wrong_root_cause_rate": 0.0, "schema_valid_rate": 1.0,
                       "evidence_recall_avg": None, "diagnosis_duration_ms": {"p50": None},
                       "token_usage": {"p50": None}},
            "by_case": {},
        })
        return run_dir

    result = compare_runs(make_run("b-v3", "3"), make_run("c-v5", "5"))
    assert result["comparable"] is False
    assert all(metric["delta"] is None for metric in result["aggregate"].values())
    for case in result["per_case"].values():
        assert case["root_cause_accuracy"]["delta"] is None


def test_benchmark_flags_budget_variation_across_models():
    from eval.benchmark import build_benchmark_report

    base = {**_row(_case(), verdict="diagnosis_correct"),
            "identity_ok": True, "llm_request_attempts": 1, "provider": "test",
            "protocol": "openai_chat_completions", "config_fingerprint": "fp-1",
            "effective_parameters": {}, "case_id": "c-1", "case_version": "1"}

    def run(models_budgets):
        rows = []
        for model, budget in models_budgets.items():
            rows.append({**base, "model_profile": model,
                         "effective_max_tool_calls": budget,
                         "effective_max_agent_rounds": 12,
                         "max_finalization_attempts": 1})
        return build_benchmark_report("b", {"models": list(models_budgets), "case_budgets": {}},
                                      rows, None)

    varies = run({"m1": 12, "m2": 6})
    assert varies["comparable"] is False
    assert "effective_budget_varies_across_models" in varies["incomparable_reason"]

    same = run({"m1": 12, "m2": 12})
    assert same["comparable"] is True

    unknown = run({"m1": 12, "m2": None})
    assert unknown["comparable"] is False
    assert "effective_budget_unknown" in unknown["incomparable_reason"]


def test_path_evidence_must_stay_inside_the_source_allowlist():
    """A path may not smuggle metadata (namespace/pod/container/metric) as evidence."""
    from app.validation import UNVERIFIABLE, VERIFIED, validate_submission

    def submission(source, path, value):
        return {"root_cause_code": "CRASH_LOOP_BACKOFF", "root_cause": "rc",
                "insufficient_evidence": False,
                "evidence": [{"source": source, "path": path, "operator": "equals",
                              "value": value}]}

    logs = [{"tool": "logs", "tool_call_id": "c1", "kind": "realtime",
             "args": {"namespace": "payment", "name": "p", "uid": "u1"},
             "output": json.dumps({"namespace": "payment", "pod": "api-x",
                                   "container": "app", "data": "real log line"})}]
    ok, _, report = validate_submission(submission("kubernetes.logs", "namespace", "payment"),
                                        logs)
    assert not ok and report[0]["status"] == UNVERIFIABLE
    ok, _, report = validate_submission(submission("kubernetes.logs", "data", "real log line"),
                                        logs)
    assert ok and report[0]["status"] == VERIFIED

    metrics = [{"tool": "query_metrics", "tool_call_id": "c2", "kind": "realtime",
                "args": {"namespace": "payment", "name": "p", "uid": "u1"},
                "output": json.dumps({"metric": "memory", "capability": "prometheus.metrics",
                                      "summary": {"max": 3.0}, "series": []})}]
    for bad_path in ("metric", "capability", "target.namespace"):
        ok, _, report = validate_submission(
            submission("prometheus.metrics", bad_path, "memory"), metrics)
        assert not ok and report[0]["status"] == UNVERIFIABLE, bad_path
    ok, _, report = validate_submission(submission("prometheus.metrics", "summary.max", "3.0"),
                                        metrics)
    assert ok and report[0]["status"] == VERIFIED

    events = [{"tool": "events", "tool_call_id": "c3", "kind": "realtime",
               "args": {"kind": "Pod", "namespace": "payment", "name": "p", "uid": "u1"},
               "output": json.dumps({"count": 1, "events": [
                   {"type": "Warning", "reason": "BackOff", "message": "Back-off"}]})}]
    ok, _, report = validate_submission(
        submission("kubernetes.events", "events[0].count", "1"), events)
    assert not ok and report[0]["status"] == UNVERIFIABLE
    ok, _, report = validate_submission(
        submission("kubernetes.events", "events[0].reason", "BackOff"), events)
    assert ok and report[0]["status"] == VERIFIED

    status = [{"tool": "inspect", "tool_call_id": "c4", "kind": "realtime",
               "args": {"kind": "Pod", "namespace": "payment", "name": "p", "uid": "u1"},
               "output": json.dumps({"target": {"name": "p"},
                                     "actual_state": {"phase": "Pending"}})}]
    ok, _, report = validate_submission(
        submission("kubernetes.status", "target.name", "p"), status)
    assert not ok and report[0]["status"] == UNVERIFIABLE
    ok, _, report = validate_submission(
        submission("kubernetes.status", "actual_state.phase", "Pending"), status)
    assert ok and report[0]["status"] == VERIFIED


def test_case_entry_resolution_is_shared_between_loading_and_hashing(tmp_path):
    """load_case_entry and the hash path must use the same resolution rules."""
    from eval.cases import load_case_entry, resolve_and_load_case_entry

    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    case, path = resolve_and_load_case_entry(cases_dir, "pod-crashloop-001@1")
    assert case.case_version == "1"
    assert "versions" in str(path)
    assert load_case_entry(cases_dir, "pod-crashloop-001@1").id == case.id
    # A pinned copy wins for @2 as well, and the returned path is the one used
    # for hashing (no second, stricter lookup).
    current, current_path = resolve_and_load_case_entry(cases_dir, "pod-crashloop-001@2")
    assert current.case_version == "2"
    assert "versions" in str(current_path)
    assert current_path.is_file()
    # When there is no pinned copy, @current falls back to the current file and
    # still reports that path.
    only_current = tmp_path / "cases"
    (only_current / "versions").mkdir(parents=True)
    (only_current / "manifests").mkdir(parents=True)
    shutil.copy(cases_dir / "pod-crashloop-001.yaml", only_current / "pod-crashloop-001.yaml")
    shutil.copy(cases_dir / "manifests" / "pod-crashloop-001.yaml",
                only_current / "manifests" / "pod-crashloop-001.yaml")
    fallback, fallback_path = resolve_and_load_case_entry(only_current, "pod-crashloop-001@2")
    assert fallback.case_version == "2"
    assert fallback_path == only_current / "pod-crashloop-001.yaml"


def test_benchmark_meta_records_case_hashes():
    from eval.benchmark import run_benchmark
    from eval.cases import case_hashes, resolve_and_load_case_entry

    cases_dir = Path(__file__).resolve().parents[1] / "cases"
    case, path = resolve_and_load_case_entry(cases_dir, "pod-crashloop-001@1")
    hashes = case_hashes([case], [path])
    assert set(hashes) == {"pod-crashloop-001@1"}
    entry = hashes["pod-crashloop-001@1"]
    assert len(entry["definition"]) == 64
    assert entry["manifests"], "fixture hashes must be recorded"


def test_benchmark_detects_budget_drift_within_one_model():
    """Same model + same case with different budgets across repetitions must not
    be hidden by a last-value-wins dict."""
    from eval.benchmark import build_benchmark_report

    base = {**_row(_case(), verdict="diagnosis_correct"),
            "identity_ok": True, "llm_request_attempts": 1, "provider": "test",
            "protocol": "openai_chat_completions", "config_fingerprint": "fp-1",
            "effective_parameters": {}, "case_id": "c-1", "case_version": "1",
            "effective_max_agent_rounds": 12, "max_finalization_attempts": 1}

    def rows_for(budgets_by_model):
        out = []
        for model, budgets in budgets_by_model.items():
            for budget in budgets:
                out.append({**base, "model_profile": model,
                            "effective_max_tool_calls": budget})
        return out

    drifted = build_benchmark_report(
        "b", {"models": ["m1", "m2"], "case_budgets": {}},
        rows_for({"m1": [12, 6, 12], "m2": [12, 12, 12]}), None)
    assert drifted["comparable"] is False
    assert "effective_budget_varies_within_model" in drifted["incomparable_reason"]

    clean = build_benchmark_report(
        "b2", {"models": ["m1", "m2"], "case_budgets": {}},
        rows_for({"m1": [12, 12, 12], "m2": [12, 12, 12]}), None)
    assert clean["comparable"] is True


def test_path_patterns_block_nested_metadata_and_whole_objects():
    """Path evidence must match a fact pattern, not a metadata sub-path."""
    from app.validation import UNVERIFIABLE, VERIFIED, validate_submission

    def submission(source, path, value, operator="equals"):
        return {"root_cause_code": "CRASH_LOOP_BACKOFF", "root_cause": "rc",
                "insufficient_evidence": False,
                "evidence": [{"source": source, "path": path, "operator": operator,
                              "value": value}]}

    metrics = [{"tool": "query_metrics", "tool_call_id": "c1", "kind": "realtime",
                "args": {"namespace": "payment", "name": "p", "uid": "u1"},
                "output": json.dumps({
                    "metric": "memory", "summary": {"latest": 1.0, "max": 3.0, "avg": 2.0},
                    "series": [{"labels": {"container": "app", "pod": "p"},
                                "points": [{"timestamp": 1700000000, "value": 3.0}]}]})}]
    for bad_path in ("series[0].labels.container", "series[0].points[0].timestamp",
                     "series[0].labels", "metric"):
        ok, _, report = validate_submission(
            submission("prometheus.metrics", bad_path, "app"), metrics)
        assert not ok and report[0]["status"] == UNVERIFIABLE, bad_path
    for good_path in ("summary.max", "series[0].points[0].value"):
        ok, _, report = validate_submission(
            submission("prometheus.metrics", good_path, "3.0"), metrics)
        assert ok and report[0]["status"] == VERIFIED, good_path

    events = [{"tool": "events", "tool_call_id": "c2", "kind": "realtime",
               "args": {"kind": "Pod", "namespace": "payment", "name": "p", "uid": "u1"},
               "output": json.dumps({"count": 2, "events": [
                   {"type": "Warning", "reason": "BackOff", "message": "Back-off"}]})}]
    ok, _, report = validate_submission(
        submission("kubernetes.events", "events[0]", "BackOff", operator="contains"), events)
    assert not ok and report[0]["status"] == UNVERIFIABLE
    ok, _, report = validate_submission(
        submission("kubernetes.events", "events[0].message", "Back-off", operator="contains"),
        events)
    assert ok and report[0]["status"] == VERIFIED

    status = [{"tool": "inspect", "tool_call_id": "c3", "kind": "realtime",
               "args": {"kind": "Pod", "namespace": "payment", "name": "p", "uid": "u1"},
               "output": json.dumps({"target": {"name": "p"},
                                     "desired_state": {"containers": [{"image": "img:v1"}]},
                                     "actual_state": {"phase": "Pending"},
                                     "conditions": [{"reason": "PodScheduled", "status": "False"}],
                                     "anomalies": ["pod is Pending"]})}]
    for good_path, value in (("actual_state.phase", "Pending"),
                             ("desired_state.containers[0].image", "img:v1"),
                             ("conditions[0].reason", "PodScheduled"),
                             ("anomalies[0]", "pod is Pending")):
        ok, _, report = validate_submission(
            submission("kubernetes.status", good_path, value), status)
        assert ok and report[0]["status"] == VERIFIED, good_path
    for bad_path in ("actual_state", "desired_state", "target.name", "conditions"):
        ok, _, report = validate_submission(
            submission("kubernetes.status", bad_path, "Pending"), status)
        assert not ok and report[0]["status"] == UNVERIFIABLE, bad_path


def test_regression_gates_cover_quality_coverage_and_cost():
    from eval.compare import _evaluate_gates

    def metric(baseline, candidate):
        return {"baseline": baseline, "candidate": candidate,
                "delta": None if baseline is None or candidate is None
                else round(candidate - baseline, 3)}

    agg = {
        "end_to_end_correct_rate": metric(0.9, 0.6),
        "wrong_root_cause_rate": metric(0.0, 0.0),
        "answerable_coverage": metric(0.9, 0.5),
        "abstention_recall": metric(1.0, 0.4),
        "system_failed_rate": metric(0.0, 0.2),
        "budget_exhausted_rate": metric(0.0, 0.25),
        "schema_valid_rate": metric(1.0, 1.0),
        "root_cause_accuracy": metric(0.9, 0.6),
    }
    gates = {g["name"]: g["pass"] for g in _evaluate_gates(agg, {}, [])}
    # The old gate set would have passed this candidate; the new one must not.
    assert gates["wrong_root_cause_rate_not_up"] is True
    assert gates["schema_valid_rate_not_down"] is True
    assert gates["end_to_end_correct_rate_not_down"] is False
    assert gates["answerable_coverage_not_down"] is False
    assert gates["abstention_recall_not_down"] is False
    assert gates["system_failed_rate_not_up"] is False
    assert gates["budget_exhausted_rate_not_up"] is False

    per_case = {"pod-crashloop-001@2": {"root_cause_accuracy": metric(1.0, 0.0)}}
    gates = {g["name"]: (g["pass"], g["detail"])
             for g in _evaluate_gates(agg, per_case, ["pod-crashloop-001@2"])}
    assert gates["critical_cases_no_regression"][0] is False
    assert "pod-crashloop-001@2" in gates["critical_cases_no_regression"][1]


def test_reporter_operational_cost_charges_failed_runs():
    """Cost must include failures, and per-correct cost must absorb them."""
    from eval.reporter import build_report

    base = _row(_case(), verdict="diagnosis_correct")
    rows = [
        {**base, "token_usage": 1000, "tool_calls": 2, "llm_calls": 3, "duration_ms": 10.0},
        {**base, "verdict": "system_failed", "token_usage": 900, "tool_calls": 1,
         "llm_calls": 2, "duration_ms": 5.0},
        {**base, "verdict": "system_failed", "token_usage": 900, "tool_calls": 1,
         "llm_calls": 2, "duration_ms": 5.0},
    ]
    report = build_report(rows)

    assert report["operational_cost"]["rows"] == 3
    assert report["operational_cost"]["token_usage_total"] == 2800
    # One correct diagnosis carries the whole operational spend.
    assert report["cost_per_correct_diagnosis"]["correct_count"] == 1
    assert report["cost_per_correct_diagnosis"]["token_usage"] == 2800
    assert report["cost_per_correct_diagnosis"]["tool_calls"] == 4
    # Abstention-expected correct rows are excluded from the answerable variant.
    abstained = {**base, "abstention_expected": True, "abstention_correct": True}
    report2 = build_report([abstained])
    assert report2["cost_per_correct_diagnosis"]["correct_count"] == 1
    assert report2["cost_per_correct_answerable_diagnosis"]["correct_count"] == 0
    assert report2["cost_per_correct_answerable_diagnosis"]["token_usage"] is None


def test_case_ground_truth_paths_are_allowed_by_the_runtime_allowlist():
    """Ground-truth evidence paths must be citable under the runtime rules,
    otherwise a case would be unwinnable."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-service"))
    from app.validation import _path_allowed, _SOURCE_TOOL

    root = Path(__file__).resolve().parents[1]
    checked = 0
    for case_file in sorted((root / "cases").glob("*.yaml")) + \
            sorted((root / "cases" / "versions").glob("*.yaml")):
        try:
            case = load_case(case_file)
        except Exception:
            continue
        for req in case.ground_truth.required_evidence:
            assert req.source in _SOURCE_TOOL, f"{case_file.name}: {req.source}"
            if req.path:
                assert _path_allowed(req.source, req.path), \
                    f"{case_file.name}: GT path not citable: {req.source} {req.path}"
                checked += 1
    assert checked >= 5, "expected several path-based ground-truth facts"


def test_compare_ignores_fixture_failed_rows_for_budget_consistency(tmp_path):
    """A case that cannot run here must not make every comparison incomparable:
    fixture failures have no effective budget, only their count matters."""
    from eval.compare import compare_runs
    from eval.reporter import write_json, write_jsonl

    def make_run(name):
        run_dir = tmp_path / name
        run_dir.mkdir()
        write_jsonl(run_dir / "case-results.jsonl", [
            {"case_id": "pod-image-notfound-001", "case_version": "1",
             "verdict": "fixture_failed", "fixture_ready": False,
             "abstention_expected": False},
            {"case_id": "pod-crashloop-001", "case_version": "1",
             "verdict": "diagnosis_correct", "fixture_ready": True,
             "abstention_expected": False, "effective_max_tool_calls": 12,
             "effective_max_agent_rounds": 12, "max_finalization_attempts": 1},
        ])
        write_json(run_dir / "report.json", {
            "run": {"run_id": name,
                    "cases": ["pod-image-notfound-001@1", "pod-crashloop-001@1"],
                    "scorer_version": "5", "root_cause_vocabulary_version": "v2"},
            "report": {"scorer_version": "5", "root_cause_accuracy": 1.0,
                       "wrong_root_cause_rate": 0.0, "schema_valid_rate": 1.0,
                       "evidence_recall_avg": None, "diagnosis_duration_ms": {"p50": None},
                       "token_usage": {"p50": None}},
            "by_case": {},
        })
        return run_dir

    result = compare_runs(make_run("b-fx"), make_run("c-fx"))
    assert result["comparable"] is True, result["incomparable_reason"]
