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
    from eval.cases import load_case, load_suite

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
        found = load_case(cases_dir / f"{case_id}.yaml")
        assert found.id == case_id
        kinds.add(found.target.kind)
    assert {"Pod", "Deployment", "Node", "PersistentVolumeClaim"} <= kinds

    # The frozen baseline suite stays untouched and remains a Pod-only baseline.
    phase1 = load_suite(suites_dir / "phase1.yaml")
    phase1_kinds = {load_case(cases_dir / f"{cid}.yaml").target.kind
                    for cid in phase1["cases"]}
    assert phase1_kinds == {"Pod"}


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

    # 404 (tag absent) -> preflight passes.
    monkeypatch.setattr(injector.httpx, "get",
                        lambda *a, **k: Resp(404))
    injector.check_registry_tag_absent("docker.io/library/busybox:missing-tag-001")

    # Any other status -> the case must fail closed.
    monkeypatch.setattr(injector.httpx, "get", lambda *a, **k: Resp(500))
    with pytest.raises(injector.InjectorError):
        injector.check_registry_tag_absent("docker.io/library/busybox:missing-tag-001")

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

    def fake_get(url, headers=None, timeout=None, trust_env=False):
        calls.append(url)
        if url.startswith("https://auth.example"):
            return Resp(200, payload={"token": "tok"})
        if headers and headers.get("Authorization") == "Bearer tok":
            return Resp(404)
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
