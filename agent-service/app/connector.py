"""HTTP client for the read-only ai-agent-connector.

The agent only talks to the connector; it never holds Kubernetes credentials.
"""

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("k8spilot.connector")


class ConnectorError(Exception):
    """Connection-level failure: the connector is unreachable."""


class ToolError(ConnectorError):
    """The connector answered with an HTTP error (e.g. 404 not found)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConnectorClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/capabilities")

    def inspect(self, target: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/tools/inspect", {"target": target})

    def relations(self, target: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/tools/relations", {"target": target})

    def events(self, target: dict[str, Any], *, limit: Optional[int] = None,
               since_hours: Optional[int] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"target": target}
        if limit is not None:
            payload["limit"] = limit
        if since_hours is not None:
            payload["since_hours"] = since_hours
        return self._request("POST", "/tools/events", payload)

    def logs(self, target: dict[str, Any], *, container: Optional[str] = None,
             previous: bool = False, tail_lines: Optional[int] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"target": target, "previous": previous}
        if container:
            payload["container"] = container
        if tail_lines is not None:
            payload["tail_lines"] = tail_lines
        return self._request("POST", "/tools/logs", payload)

    def query_metrics(self, target: dict[str, Any], *, metric: Optional[str] = None,
                      range_minutes: Optional[int] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"target": target}
        if metric:
            payload["metric"] = metric
        if range_minutes is not None:
            payload["range_minutes"] = range_minutes
        return self._request("POST", "/tools/query_metrics", payload)

    def query_logs(self, target: dict[str, Any], *, range_minutes: Optional[int] = None,
                   filter: Optional[str] = None, max_lines: Optional[int] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"target": target}
        if range_minutes is not None:
            payload["range_minutes"] = range_minutes
        if filter:
            payload["filter"] = filter
        if max_lines is not None:
            payload["max_lines"] = max_lines
        return self._request("POST", "/tools/query_logs", payload)

    def _request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        url = self._base + path
        try:
            resp = httpx.request(
                method,
                url,
                json=payload,
                timeout=self._timeout,
                # The connector is an in-cluster service; its traffic must never
                # go through the internet HTTP(S)_PROXY (which yields 502 for
                # private pod IPs). The LLM client keeps its own proxy config.
                trust_env=False,
            )
            logger.debug("connector %s %s -> HTTP %d (%.200r)", method, url, resp.status_code, resp.text)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"connector unreachable at {self._base}: {exc}") from exc

        if resp.status_code >= 400:
            try:
                body = resp.json()
                code = body.get("code", "error")
                message = body.get("error", resp.text)
            except ValueError:
                code = "http_error"
                message = resp.text
            logger.error(
                "connector %s %s -> HTTP %d body=%r",
                method, url, resp.status_code, resp.text[:1000],
            )
            raise ToolError(code, message)
        return resp.json()
