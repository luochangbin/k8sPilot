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

from .cases import Case, CaseError, load_case, load_suite
from .injector import Injector, InjectorError
from .reporter import build_report, render_markdown, report_by_case, write_json, write_jsonl
from .scorer import score_case, summarize_trace

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
            enable_incidents: Optional[bool] = None) -> tuple[str, Path]:
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
            "cases": [c.key() for c in cases],
        }
        write_json(run_dir / "run.json", meta)

        rows: list[dict[str, Any]] = []
        for case in cases:
            for attempt in range(runs_per_case):
                row = self._run_case(case, run_id, attempt, enable_knowledge=enable_knowledge,
                                     enable_incidents=enable_incidents)
                rows.append(row)
                write_jsonl(run_dir / "case-results.jsonl", [row])

        report = build_report(rows)
        by_case = report_by_case(rows)
        write_json(run_dir / "report.json", {"run": meta, "report": report, "by_case": by_case})
        (run_dir / "report.md").write_text(render_markdown(meta, report, by_case), encoding="utf-8")
        return run_id, run_dir

    # ---- helpers ----

    def _load_case(self, case_id: str) -> Case:
        path = self._cases_dir() / f"{case_id}.yaml"
        return load_case(path)

    def _cases_dir(self) -> Path:
        return Path(__file__).resolve().parent / "cases"

    def _run_case(self, case: Case, run_id: str, attempt_index: int,
                  *, enable_knowledge: Optional[bool] = None,
                  enable_incidents: Optional[bool] = None) -> dict[str, Any]:
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
            row = {**base, "fixture_ready": fixture_ready, "error": error}
            if diagnosis:
                row["diagnosis_id"] = diagnosis.get("diagnosis_id")
            status = (diagnosis or {}).get("status", "failed")
            result = (diagnosis or {}).get("result")
            row.update(score_case(case, fixture_ready=fixture_ready, diagnosis_status=status,
                                  result=result, error=error, trace=trace))
            row.update(summarize_trace(trace))
            if cleanup_failed:
                row["cleanup_failed"] = cleanup_failed
            return row

        try:
            self._injector.apply(case)
        except InjectorError as exc:
            return _complete_row(False, None, f"inject failed: {exc}", None)

        fixture_ready = self._injector.wait_ready(case)
        diagnosis: Optional[dict[str, Any]] = None
        error: Optional[str] = None
        trace: Optional[dict[str, Any]] = None
        if fixture_ready:
            uid = self._injector.get_target_uid(case)
            diagnosis, error = self._run_diagnosis(case, uid, base,
                                                   timeout=case.budgets.diagnosis_timeout_seconds,
                                                   enable_knowledge=enable_knowledge,
                                                   enable_incidents=enable_incidents)
            trace = self._collect_trace(diagnosis.get("diagnosis_id") if diagnosis else None)

        cleanup_failed = None
        try:
            self._injector.cleanup(case)
        except InjectorError as exc:
            cleanup_failed = str(exc)
        return _complete_row(fixture_ready, diagnosis, error, trace, cleanup_failed)

    def _run_diagnosis(self, case: Case, uid: str, base: dict[str, Any],
                       timeout: int, *, enable_knowledge: Optional[bool] = None,
                       enable_incidents: Optional[bool] = None) -> tuple[Optional[dict[str, Any]], Optional[str]]:
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
        }
        # Phase 4 four-group ablation: retrieval gates. Omitted (None) keeps the
        # agent default (enabled when the knowledge module is configured).
        if enable_knowledge is not None:
            payload["enable_knowledge"] = enable_knowledge
        if enable_incidents is not None:
            payload["enable_incidents"] = enable_incidents
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
        return None, f"diagnosis timed out after {timeout}s"

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
