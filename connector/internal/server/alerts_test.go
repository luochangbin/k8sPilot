package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"k8spilot/connector/internal/config"
)

func alertsTestServer(t *testing.T, agentStatus int) (*httptest.Server, string) {
	t.Helper()
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(agentStatus)
		if agentStatus < 300 {
			_, _ = w.Write([]byte(`{"diagnosis_id":"diag_x","status":"queued"}`))
		} else {
			_, _ = w.Write([]byte(`{"detail":"nope"}`))
		}
	}))
	t.Cleanup(agent.Close)
	cfg := &config.Config{
		AgentURL: agent.URL, AlertForwardTimeout: 5 * time.Second,
		AlertSnapshotEventLimit: 5, AlertMaxBatch: 2, AlertWebhookToken: "secret",
	}
	srv := httptest.NewServer(New(cfg, nil).Handler())
	t.Cleanup(srv.Close)
	return srv, agent.URL
}

func body(alerts int) string {
	items := make([]string, 0, alerts)
	for i := 0; i < alerts; i++ {
		items = append(items, `{"fingerprint":"fp-`+string(rune('a'+i))+`","labels":{"foo":"bar"}}`)
	}
	return `{"status":"firing","alerts":[` + strings.Join(items, ",") + `]}`
}

func postAlerts(t *testing.T, base, auth, payload string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPost, base+"/alerts", strings.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	if auth != "" {
		req.Header.Set("Authorization", auth)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

func TestAlertsRequiresTokenWhenConfigured(t *testing.T) {
	srv, _ := alertsTestServer(t, 201)
	resp := postAlerts(t, srv.URL, "", body(1))
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestAlertsReturns502WhenAgentFails(t *testing.T) {
	srv, _ := alertsTestServer(t, 500)
	resp := postAlerts(t, srv.URL, "Bearer secret", body(1))
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestAlertsPassesThrough429(t *testing.T) {
	srv, _ := alertsTestServer(t, 429)
	resp := postAlerts(t, srv.URL, "Bearer secret", body(1))
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestAlertsRejectsOversizedBatch(t *testing.T) {
	srv, _ := alertsTestServer(t, 201)
	resp := postAlerts(t, srv.URL, "Bearer secret", body(3))
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("status=%d", resp.StatusCode)
	}
}

func TestAlertsAcceptsWhenAllForwarded(t *testing.T) {
	srv, _ := alertsTestServer(t, 201)
	resp := postAlerts(t, srv.URL, "Bearer secret", body(1))
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("status=%d", resp.StatusCode)
	}
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if out["accepted"].(float64) != 1 {
		t.Fatalf("summary=%v", out)
	}
}
