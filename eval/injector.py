"""Fault Injector: deterministic apply / wait / cleanup (design §23.3)."""

import time
from typing import Optional

from .cases import Case
from .kubectl import KubectlError, get_object, json_path_get, run_kubectl


class InjectorError(Exception):
    pass


class Injector:
    def __init__(self, kubeconfig: Optional[str] = None) -> None:
        self._kubeconfig = kubeconfig

    def apply(self, case: Case) -> None:
        try:
            for manifest in case.setup_manifests:
                run_kubectl(["apply", "-f", str(manifest)], kubeconfig=self._kubeconfig)
        except KubectlError as exc:
            raise InjectorError(f"inject failed for {case.id}: {exc}") from exc
        for pre in case.preflight:
            pre_type = pre.get("type")
            if pre_type == "namespace_empty":
                out = run_kubectl(
                    ["get", "pods", "-n", case.target.namespace, "-o", "name"],
                    kubeconfig=self._kubeconfig,
                )
                if out.strip():
                    raise InjectorError(
                        f"namespace {case.target.namespace} not empty before case {case.id}: {out.strip()}"
                    )
            else:
                raise InjectorError(f"unknown preflight type {pre_type!r}")

    def wait_ready(self, case: Case) -> bool:
        """Poll the target until ready_when holds; returns success."""
        deadline = time.monotonic() + case.ready_when.timeout_seconds
        while time.monotonic() < deadline:
            try:
                obj = get_object(case.target.kind, case.target.namespace,
                                 case.target.name, kubeconfig=self._kubeconfig)
                got = json_path_get(obj, case.ready_when.path)
            except Exception:  # noqa: BLE001 - transient errors mean "not ready yet"
                got = None
            if self._ready_holds(case.ready_when.type, got, case.ready_when.value):
                return True
            time.sleep(2)
        return False

    @staticmethod
    def _ready_holds(when_type: str, got: Any, expected: str) -> bool:
        if when_type == "jsonpath_exists":
            return got is not None
        if when_type == "jsonpath_gte":
            try:
                return float(got) >= float(expected)
            except (TypeError, ValueError):
                return False
        return got == expected

    def cleanup(self, case: Case) -> None:
        """Idempotent cleanup; delete errors that are not 'not found' are fatal."""
        for manifest in case.setup_manifests:
            try:
                run_kubectl(["delete", "-f", str(manifest), "--ignore-not-found=true"],
                            kubeconfig=self._kubeconfig)
            except KubectlError as exc:
                raise InjectorError(f"cleanup failed for {case.id}: {exc}") from exc

    def get_target_uid(self, case: Case) -> str:
        obj = get_object(case.target.kind, case.target.namespace,
                         case.target.name, kubeconfig=self._kubeconfig)
        return obj.get("metadata", {}).get("uid", "")
