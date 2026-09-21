"""Fault Injector: deterministic apply / wait / cleanup (design §23.3)."""

import json
import time

import httpx
from typing import Optional

from .cases import Case
from .kubectl import KubectlError, get_object, json_path_get, run_kubectl


class InjectorError(Exception):
    pass


# Bounded but generous timeout for the foreground namespace delete during
# cleanup (see Injector.cleanup).
CLEANUP_TIMEOUT_SECONDS = 180


class Injector:
    def __init__(self, kubeconfig: Optional[str] = None) -> None:
        self._kubeconfig = kubeconfig

    def apply(self, case: Case) -> None:
        # Preflight strictly precedes injection: it creates no resources, so a
        # failing preflight leaves the cluster untouched (a fixture_failed case
        # must not leak its namespace into later cases).
        self._run_preflight(case)
        try:
            for manifest in case.setup_manifests:
                run_kubectl(["apply", "-f", str(manifest)], kubeconfig=self._kubeconfig)
        except KubectlError as exc:
            raise InjectorError(f"inject failed for case {case.id}: {exc}") from exc

    def _run_preflight(self, case: Case) -> None:
        """Checks that must pass before any resource exists (no side effects)."""
        for pre in case.preflight:
            pre_type = pre.get("type")
            if pre_type == "namespace_empty":
                out = run_kubectl(
                    ["get", "pods", "-n", case.target.namespace, "-o", "name"],
                    kubeconfig=self._kubeconfig,
                )
                if out.strip():
                    raise InjectorError(
                        f"namespace {case.target.namespace} not empty before case {case.id}: "
                        f"{out.strip()}"
                    )
            elif pre_type == "registry_tag_absent":
                # IMAGE_NOT_FOUND can only be claimed when the registry is
                # reachable AND the tag is genuinely absent; otherwise the case
                # would silently degrade into a network/DNS/TLS failure verdict.
                check_registry_tag_absent(str(pre.get("image", "")),
                                          timeout=int(pre.get("timeout_seconds", 15)))
            else:
                raise InjectorError(f"unknown preflight type {pre_type!r}")

    def wait_ready(self, case: Case) -> bool:
        """Poll the target until ready_when holds; returns success.

        `event_message_contains` is the cluster-side counterpart of an HTTP
        preflight: it only succeeds once the target's *Events* carry the expected
        failure semantics, so a fixture is not marked ready when the cluster
        failed for an unrelated reason (DNS, TLS, network timeout, ...).
        """
        deadline = time.monotonic() + case.ready_when.timeout_seconds
        while time.monotonic() < deadline:
            if case.ready_when.type == "all":
                if all(self._condition_holds(case, condition)
                       for condition in case.ready_when.conditions):
                    return True
                time.sleep(2)
                continue
            if case.ready_when.type == "event_message_contains":
                if self._events_contain(case):
                    return True
                time.sleep(2)
                continue
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

    def _condition_holds(self, case: Case, condition: dict) -> bool:
        """Evaluate one entry of a composite (`all`) ready_when."""
        kind = str(condition.get("type") or "")
        if kind == "event_message_contains":
            return self._events_contain_values(case, condition.get("path") or "",
                                               str(condition.get("value") or ""))
        try:
            obj = get_object(case.target.kind, case.target.namespace,
                             case.target.name, kubeconfig=self._kubeconfig)
            got = json_path_get(obj, condition.get("path") or "")
        except Exception:  # noqa: BLE001 - transient errors mean "not ready yet"
            return False
        return self._ready_holds(kind, got, _str_value(condition.get("value")))

    def _events_contain(self, case: Case) -> bool:
        """True when an Event for the target matches the case's ready filter."""
        return self._events_contain_values(case, case.ready_when.path or "",
                                           str(case.ready_when.value or ""))

    def _events_contain_values(self, case: Case, reason_filter: str,
                               needle: str) -> bool:
        """True when an Event for the target matches the reason/message filter.

        ready_when.path holds an optional event reason filter, ready_when.value
        the required message substring.
        """
        args = ["get", "events"]
        if case.target.namespace:
            args += ["-n", case.target.namespace]
        args += ["--field-selector", f"involvedObject.name={case.target.name}", "-o", "json"]
        try:
            payload = json.loads(run_kubectl(args, kubeconfig=self._kubeconfig) or "{}")
        except Exception:  # noqa: BLE001 - transient errors mean "not ready yet"
            return False
        reason = (reason_filter or "").strip()
        for item in payload.get("items") or []:
            if reason and str(item.get("reason", "")) != reason:
                continue
            if needle and needle in str(item.get("message", "")):
                return True
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
        """Idempotent cleanup; delete errors that are not 'not found' are fatal.

        Namespace deletion is a foreground wait and can legitimately take longer
        than the default kubectl timeout on a busy/slow API server, so cleanup
        uses a larger, still-bounded timeout (it must confirm deletion to keep
        the "cleanup failure stops further injection" guarantee)."""
        for manifest in case.setup_manifests:
            try:
                run_kubectl(["delete", "-f", str(manifest), "--ignore-not-found=true"],
                            kubeconfig=self._kubeconfig, timeout=CLEANUP_TIMEOUT_SECONDS)
            except KubectlError as exc:
                raise InjectorError(f"cleanup failed for {case.id}: {exc}") from exc

    def get_target_uid(self, case: Case) -> str:
        obj = get_object(case.target.kind, case.target.namespace,
                         case.target.name, kubeconfig=self._kubeconfig)
        return obj.get("metadata", {}).get("uid", "")


def _str_value(value: Any) -> str:
    return "" if value is None else str(value)


def _split_image(image: str) -> tuple[str, str, str]:
    """Return (registry, repository, reference) for a Docker image reference."""
    ref = image.split("@", 1)[0]
    name, _, tag = ref.rpartition(":")
    if not name or "/" in tag:
        name, tag = ref, "latest"
    parts = name.split("/")
    if len(parts) == 1 or ("." not in parts[0] and ":" not in parts[0]
                           and parts[0] != "localhost"):
        registry = "registry-1.docker.io"
        repository = name if "/" in name else "library/" + name
    else:
        registry, repository = parts[0], "/".join(parts[1:])
    return registry, repository, tag


def _pull_token(registry: str, repository: str, challenge: str,
                timeout: int) -> str:
    """Fetch an anonymous pull token from the registry's Bearer challenge."""
    realm, params = "", {}
    for part in challenge.replace("Bearer ", "").split(","):
        key, _, value = part.strip().partition("=")
        value = value.strip('"')
        if key == "realm":
            realm = value
        else:
            params[key] = value
    if not realm:
        raise InjectorError("registry challenge without a realm")
    query = f"?service={params.get('service', '')}&scope={params.get('scope', '')}"
    resp = httpx.get(realm + query, timeout=timeout, trust_env=False)
    resp.raise_for_status()
    return str(resp.json().get("token") or resp.json().get("access_token") or "")


def check_registry_tag_absent(image: str, timeout: int = 15) -> None:
    """Preflight: the registry must be reachable and report the tag as absent."""
    if not image:
        raise InjectorError("registry_tag_absent preflight needs an 'image'")
    registry, repository, reference = _split_image(image)
    accept = (",".join([
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]))
    url = f"https://{registry}/v2/{repository}/manifests/{reference}"
    try:
        resp = httpx.get(url, headers={"Accept": accept}, timeout=timeout, trust_env=False)
        if resp.status_code == 401:
            token = _pull_token(registry, repository,
                                resp.headers.get("WWW-Authenticate", ""), timeout)
            resp = httpx.get(url, headers={"Accept": accept,
                                           "Authorization": f"Bearer {token}"},
                             timeout=timeout, trust_env=False)
    except httpx.HTTPError as exc:
        raise InjectorError(f"registry {registry} unreachable: {exc}") from exc
    if resp.status_code == 404:
        # A bare 404 is not enough: the registry API distinguishes NAME_UNKNOWN
        # (repository absent) from MANIFEST_UNKNOWN (tag absent). Only the latter
        # matches the IMAGE_NOT_FOUND ground truth.
        code = ""
        try:
            errors = (resp.json() or {}).get("errors") or []
            code = str((errors[0] or {}).get("code", "")) if errors else ""
        except (ValueError, AttributeError, TypeError):
            code = ""
        if code != "MANIFEST_UNKNOWN":
            raise InjectorError(
                f"registry {registry} answered 404/{code or 'no-error-code'} for "
                f"{repository}:{reference}; only an explicit MANIFEST_UNKNOWN can "
                "guarantee IMAGE_NOT_FOUND")
        return
    raise InjectorError(
        f"registry {registry} answered HTTP {resp.status_code} for "
        f"{repository}:{reference}; cannot guarantee IMAGE_NOT_FOUND")
