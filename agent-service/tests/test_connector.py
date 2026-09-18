"""ConnectorClient HTTP contract for the alert time anchor.

The agent must send the trusted `alert_time` (and the bounded window) on the
wire; manual requests must not carry an anchor at all.
"""

from app.connector import ConnectorClient

import app.connector as connector_module


class _FakeResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True}


def _capture(monkeypatch):
    calls = []

    def fake_request(method, url, json=None, **kwargs):
        calls.append({"method": method, "url": url, "body": json})
        return _FakeResponse()

    monkeypatch.setattr(connector_module.httpx, "request", fake_request)
    return calls


def test_query_metrics_sends_alert_time_and_bounded_range(monkeypatch):
    calls = _capture(monkeypatch)
    client = ConnectorClient("http://connector:8080")

    client.query_metrics({"kind": "Pod", "namespace": "ns", "name": "p"},
                         metric="memory", range_minutes=30,
                         alert_time="2026-09-15T00:00:00Z")

    body = calls[0]["body"]
    assert calls[0]["url"].endswith("/tools/query_metrics")
    assert body["alert_time"] == "2026-09-15T00:00:00Z"
    assert body["range_minutes"] == 30
    assert body["metric"] == "memory"


def test_query_logs_sends_alert_time(monkeypatch):
    calls = _capture(monkeypatch)
    client = ConnectorClient("http://connector:8080")

    client.query_logs({"kind": "Pod", "namespace": "ns", "name": "p"},
                      range_minutes=30, alert_time="2026-09-15T00:00:00Z")

    body = calls[0]["body"]
    assert calls[0]["url"].endswith("/tools/query_logs")
    assert body["alert_time"] == "2026-09-15T00:00:00Z"


def test_manual_requests_omit_the_anchor(monkeypatch):
    calls = _capture(monkeypatch)
    client = ConnectorClient("http://connector:8080")

    client.query_metrics({"kind": "Pod", "namespace": "ns", "name": "p"},
                         metric="memory", range_minutes=5)
    client.query_logs({"kind": "Pod", "namespace": "ns", "name": "p"},
                      range_minutes=5)

    assert "alert_time" not in calls[0]["body"]
    assert "alert_time" not in calls[1]["body"]

def test_alert_run_without_anchor_is_flagged_alert_expected(monkeypatch):
    calls = _capture(monkeypatch)
    client = ConnectorClient("http://connector:8080")

    client.query_metrics({"kind": "Pod", "namespace": "ns", "name": "p"},
                         metric="memory", range_minutes=30, alert_expected=True)
    client.query_logs({"kind": "Pod", "namespace": "ns", "name": "p"},
                      range_minutes=30, alert_expected=True)

    assert calls[0]["body"]["alert_expected"] is True
    assert "alert_time" not in calls[0]["body"]
    assert calls[1]["body"]["alert_expected"] is True


def test_alert_run_with_anchor_carries_both(monkeypatch):
    calls = _capture(monkeypatch)
    client = ConnectorClient("http://connector:8080")

    client.query_metrics({"kind": "Pod", "namespace": "ns", "name": "p"},
                         metric="memory", alert_time="2026-09-15T00:00:00Z",
                         alert_expected=True)

    assert calls[0]["body"]["alert_time"] == "2026-09-15T00:00:00Z"
    assert calls[0]["body"]["alert_expected"] is True


def test_manual_requests_do_not_mark_alert_expected(monkeypatch):
    calls = _capture(monkeypatch)
    client = ConnectorClient("http://connector:8080")

    client.query_metrics({"kind": "Pod", "namespace": "ns", "name": "p"},
                         metric="memory", range_minutes=5)

    assert "alert_expected" not in calls[0]["body"]
