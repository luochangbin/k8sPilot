"""Thin kubectl wrapper and a minimal jsonpath evaluator.

The jsonpath evaluator supports the subset used by case ready_when:
paths like `status.containerStatuses[0].lastState.terminated.reason`.
"""

import json
import os
import re
import subprocess
from typing import Any, Optional


class KubectlError(Exception):
    pass


class JsonPathError(Exception):
    pass


def run_kubectl(args: list[str], *, kubeconfig: Optional[str] = None,
                timeout: int = 60) -> str:
    cmd = ["kubectl", *args]
    env = None
    if kubeconfig:
        env = {**os.environ, "KUBECONFIG": kubeconfig}
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             env=env, encoding="utf-8")
    except FileNotFoundError as exc:
        raise KubectlError("kubectl not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise KubectlError(f"kubectl timed out: {' '.join(cmd)}") from exc
    if res.returncode != 0:
        raise KubectlError(res.stderr.strip() or f"kubectl {' '.join(cmd)} failed")
    return res.stdout


def get_object(kind: str, namespace: str, name: str, *, kubeconfig: Optional[str] = None) -> dict[str, Any]:
    args = ["get", kind.lower(), "-n", namespace, name, "-o", "json"]
    out = run_kubectl(args, kubeconfig=kubeconfig)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise KubectlError(f"cannot parse kubectl get {kind}/{name}: {exc}") from exc


def json_path_get(obj: Any, path: str) -> Any:
    """Evaluate `a.b[0].c` style paths against a JSON object."""
    if not path:
        raise JsonPathError("empty jsonpath")
    cur = obj
    for part in path.split("."):
        m = re.match(r"^([^[]*)(.*)$", part)
        key = m.group(1)
        idx_part = m.group(2)  # e.g. "[0][2]"
        if key:
            if not isinstance(cur, dict) or key not in cur:
                raise JsonPathError(f"missing key '{key}' in path '{path}'")
            cur = cur[key]
        for im in re.finditer(r"\[(\d+)\]", idx_part):
            if not isinstance(cur, list):
                raise JsonPathError(f"cannot index non-list at '{part}' in '{path}'")
            idx = int(im.group(1))
            if idx >= len(cur):
                raise JsonPathError(f"index {idx} out of range at '{part}' in '{path}'")
            cur = cur[idx]
    return cur
