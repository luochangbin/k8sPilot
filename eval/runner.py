"""Eval Runner (design §23.5).

Per case attempt: inject -> wait ready -> create diagnosis -> poll to terminal
-> collect trace -> deterministic score -> cleanup -> write immutable case row.
The runner never selects prompts/models or auto-retries failed cases.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .cases import Case, CaseError, load_case, load_suite, resolve_case_entry
from .injector import Injector, InjectorError
from .reporter import build_report, render_markdown, report_by_case, write_json, write_jsonl
from .scorer import ROOT_CAUSE_VOCABULARY_VERSION, score_case, summarize_trace

REPO_ROOT = Path(__file__).resolve().parents[1]


class RunnerError(Exception):
    pass


class Runner:
    def __init__(self, *, agent_url: str, trace_dir: Optional[str], kubeconfig: Optional[str],
                 reports_dir: str, model: str = "") -> None:
        self._agent_url = agent_url.rstrip("/")
        self._trace_dir = Path(trace_dir) if trace_dir else None
        self._injector = Injector(kubeconfig)
        self._reports_dir = Path(reports_dir)
        self._model = model

    # ---- public ----

    def run(self, suite_path: Path, case_ids: list[str], runs_per_case: int,
            profile: str, *, enable_knowledge: Optional[bool] = None,
            enable_incidents: Optional[bool] = None,
            model_profile: Optional[str] = None) -> tuple[str, Path]:
        suite_raw = load_suite(suite_path)
        cases = [self._load_case(cid) for cid in case_ids]

        run_id = f"{profile}-{time.strftime('%Y%m%dT%H%M%S')}"
        run_dir = self._reports_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "run_id": run_id,
            "profile": profile,
            "suite": suite_raw.get("id", suite_path.stem),
            "model": self._model or self._agent_model_hint(),
            "prompt_hash": file_hash(REPO_ROOT / "agent-service/app/prompts.py"),
            "tool_schema_hash": file_hash(REPO_ROOT / "agent-service/app/tools.py"),
            "k8s_version": self._k8s_version(),
            "agent_url": self._agent_url,
            "runs_per_case": runs_per_case,
            "enable_knowledge": enable_knowledge,
            "enable_incidents": enable_incidents,
            "model_profile": model_profile,
            "cases": [c.key() for c in cases],
            "root_cause_vocabulary_version": ROOT_CAUSE_VOCABULARY_VERSION,
        }
        write_json(run_dir / "run.json", meta)

        rows: list[dict[str, Any]] = []
        for case in cases:
            for attempt in range(runs_per_case):
                row = self._run_case(case, run_id, attempt, enable_knowledge=enable_knowledge,
                                     enable_incidents=enable_incidents,
                                     model_profile=model_profile)
                rows.append(row)
                write_jsonl(run_dir / "case-results.jsonl", [row])

        report = build_report(rows)
        by_case = report_by_case(rows)
        write_json(run_dir / "report.json", {"run": meta, "report": report, "by_case": by_case})
        (run_dir / "report.md").write_text(render_markdown(meta, report, by_case), encoding="utf-8")
        return run_id, run_dir

    # ---- helpers ----

    def run_case_attempt(self, case: Case, run_id: str, attempt_index: int, *,
                         enable_knowledge: Optional[bool] = None,
                         enable_incidents: Optional[bool] = None,
                         model_profile: Optional[str] = None) -> dict[str, Any]:
        """Run one inject→diagnose→collect→cleanup attempt (public seam for the
        multi-model benchmark orchestration)."""
        return self._run_case(case, run_id, attempt_index,
                              enable_knowledge=enable_knowledge,
                              enable_incidents=enable_incidents,
                              model_profile=model_profile)

    def _load_case(self, case_id: str) -> Case:
        # `case-id` or `case-id@version` (pinned historical definition).
        return load_case(resolve_case_entry(self._cases_dir(), case_id))

    def _cases_dir(self) -> Path:
        return Path(__file__).resolve().parent / "cases"

    def _run_case(self, case: Case, run_id: str, attempt_index: int,
                  *, enable_knowledge: Optional[bool] = None,
                  enable_incidents: Optional[bool] = None,
                  model_profile: Optional[str] = None) -> dict[str, Any]:
        base = {
            "eval_run_id": run_id,
            "case_id": case.id,
            "case_version": case.case_version,
            "attempt_index": attempt_index,
            "diagnosis_id": None,
            "fixture_ready": False,
            "error": None,
        }

        def _complete_row(fixture_ready: bool, diagnosis: Optional[dict[str, Any]],
                          error: Optional[str], trace: Optional[dict[str, Any]],
                          cleanup_failed: Optional[str] = None) -> dict[str, Any]:
            # Surface the agent's own failure reason when it finished with
            # status=failed (so system_failed rows are diagnosable).
            if error is None and diagnosis and diagnosis.get("status") == "failed":
                error = diagnosis.get("error") or "diagnosis failed (agent)"
            row = {**base, "fixture_ready": fixture_ready, "error": error,
                   "failure_reason": (diagnosis or {}).get("failure_reason")}
            if diagnosis:
                row["diagnosis_id"] = diagnosis.get("diagnosis_id")
            status = (diagnosis or {}).get("status", "failed")
            result = (diagnosis or {}).get("result")
            row.update(score_case(case, fixture_ready=fixture_ready, diagnosis_status=status,
                                  result=result, error=error, trace=trace))
            row.update(summarize_trace(trace))
            # A budget-exhausted session is a planning failure, never an abstention.
            row["budget_exhausted"] = row.get("failure_reason") == "budget_exhausted"
            if cleanup_failed:
                row["cleanup_failed"] = cleanup_failed
            return row

        try:
            self._injector.apply(case)
        except InjectorError as exc:
            # apply() may fail after a partial kubectl apply: always try to clean
            # up so a fixture_failed case cannot pollute later cases.
            return _complete_row(False, None, f"inject failed: {exc}", None,
                                 cleanup_failed=self._best_effort_cleanup(case))

        fixture_ready = self._injector.wait_ready(case)
        diagnosis: Optional[dict[str, Any]] = None
        error: Optional[str] = None
        trace: Optional[dict[str, Any]] = None
        if fixture_ready:
            uid = self._injector.get_target_uid(case)
            diagnosis, error = self._run_diagnosis(case, uid, base,
                                                   timeout=case.budgets.diagnosis_timeout_seconds,
                                                   enable_knowledge=enable_knowledge,
                                                   enable_incidents=enable_incidents,
                                                   model_profile=model_profile)
            trace = self._collect_trace(diagnosis.get("diagnosis_id") if diagnosis else None)

        cleanup_failed = None
        try:
            self._injector.cleanup(case)
        except InjectorError as exc:
            cleanup_failed = str(exc)
        return _complete_row(fixture_ready, diagnosis, error, trace, cleanup_failed)

    def _run_diagnosis(self, case: Case, uid: str, base: dict[str, Any],
                       timeout: int, *, enable_knowledge: Optional[bool] = None,
                       enable_incidents: Optional[bool] = None,
                       model_profile: Optional[str] = None) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        payload = {
            "trigger": "manual",
            "resource": {
                "apiVersion": case.target.apiVersion,
                "kind": case.target.kind,
                "namespace": case.target.namespace,
                "name": case.target.name,
                "uid": uid,
            },
            "eval_run_id": base["eval_run_id"],
            "case_id": base["case_id"],
            "case_version": base["case_version"],
            "attempt_index": base["attempt_index"],
            # Case budgets (eval-only on the agent side; frozen at session
            # creation). The trace reports the *effective* values actually used.
            "eval_max_tool_calls": case.budgets.max_tool_calls,
            "eval_max_agent_rounds": case.budgets.max_agent_rounds,
        }
        # Phase 4 four-group ablation: retrieval gates. Omitted (None) keeps the
        # agent default (enabled when the knowledge module is configured).
        if enable_knowledge is not None:
            payload["enable_knowledge"] = enable_knowledge
        if enable_incidents is not None:
            payload["enable_incidents"] = enable_incidents
        # Model benchmark: explicit profile selection (agent must enable it).
        if model_profile is not None:
            payload["model_profile"] = model_profile
        try:
            resp = httpx.post(f"{self._agent_url}/api/v1/diagnoses", json=payload,
                              timeout=30, trust_env=False)
            resp.raise_for_status()
            diagnosis_id = resp.json()["diagnosis_id"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return None, f"create diagnosis failed: {exc}"

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{self._agent_url}/api/v1/diagnoses/{diagnosis_id}",
                                 timeout=15, trust_env=False)
                resp.raise_for_status()
                d = resp.json()
                if d["status"] in ("completed", "failed"):
                    return d, None
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                return None, f"poll diagnosis failed: {exc}"
            time.sleep(1)
        # Timeout: stop the worker instead of letting it keep burning tokens
        # against a fixture that is about to be deleted. The stop is
        # cooperative (checked before every LLM call / tool execution); we poll
        # briefly so the caller can report the terminal state.
        self._cancel(diagnosis_id)
        grace_deadline = time.monotonic() + 5
        while time.monotonic() < grace_deadline:
            try:
                resp = httpx.get(f"{self._agent_url}/api/v1/diagnoses/{diagnosis_id}",
                                 timeout=5, trust_env=False)
                resp.raise_for_status()
                d = resp.json()
                if d["status"] in ("completed", "failed"):
                    return d, f"diagnosis timed out after {timeout}s (cancelled)"
            except (httpx.HTTPError, KeyError, ValueError):
                break
            time.sleep(0.5)
        return None, f"diagnosis timed out after {timeout}s"

    def _cancel(self, diagnosis_id: str) -> None:
        """Best-effort cooperative cancel; failures must not mask the timeout."""
        try:
            httpx.post(f"{self._agent_url}/api/v1/diagnoses/{diagnosis_id}/cancel",
                       timeout=10, trust_env=False)
        except httpx.HTTPError:
            pass

    def _best_effort_cleanup(self, case: Case) -> Optional[str]:
        """Cleanup that must never mask the original failure."""
        try:
            self._injector.cleanup(case)
            return None
        except InjectorError as exc:
            return str(exc)

    def _collect_trace(self, diagnosis_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not self._trace_dir or not diagnosis_id:
            return None
        path = self._trace_dir / f"{diagnosis_id}.jsonl"
        if not path.is_file():
            return None
        spans = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return {"spans": spans}

    def _k8s_version(self) -> str:
        try:
            from .kubectl import run_kubectl
            out = run_kubectl(["version", "-o", "json"], timeout=30)
            import json as _json
            server = _json.loads(out).get("serverVersion", {})
            return server.get("gitVersion", "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"

    def _agent_model_hint(self) -> str:
        return "unknown"


def file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"
