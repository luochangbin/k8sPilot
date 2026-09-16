package alerts

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"k8spilot/connector/internal/tools"
)

type stubInspector struct{ uid string }

func (s stubInspector) Inspect(_ context.Context, t tools.Target) (*tools.InspectResponse, error) {
	t.UID = s.uid
	return &tools.InspectResponse{
		Target: t, Exists: true,
		ActualState: map[string]any{"phase": "Running"},
		Anomalies:   []string{"container app is waiting with reason=CrashLoopBackOff"},
		Conditions:  []tools.Condition{{Type: "Ready", Status: "False"}},
	}, nil
}

func (s stubInspector) Events(_ context.Context, t tools.Target, _ tools.EventsParams) (*tools.EventsResponse, error) {
	return &tools.EventsResponse{Target: t, Count: 1, Events: []tools.Event{{Reason: "BackOff"}}}, nil
}

func TestResolveTarget(t *testing.T) {
	cases := []struct {
		name   string
		labels map[string]string
		wantOK bool
		want   tools.Target
	}{
		{"pod", map[string]string{"pod": "payment-api", "namespace": "payment"},
			true, tools.Target{Kind: "Pod", Namespace: "payment", Name: "payment-api"}},
		{"deployment", map[string]string{"deployment": "payment-api", "namespace": "payment"},
			true, tools.Target{Kind: "Deployment", Namespace: "payment", Name: "payment-api"}},
		{"node", map[string]string{"node": "worker-1"},
			true, tools.Target{Kind: "Node", Name: "worker-1"}},
		{"pvc", map[string]string{"persistentvolumeclaim": "data", "namespace": "payment"},
			true, tools.Target{Kind: "PersistentVolumeClaim", Namespace: "payment", Name: "data"}},
		{"explicit kind", map[string]string{"kind": "Pod", "pod": "p", "namespace": "ns"},
			true, tools.Target{Kind: "Pod", Namespace: "ns", Name: "p"}},
		{"missing namespace", map[string]string{"pod": "p"}, false, tools.Target{}},
		{"unsupported kind", map[string]string{"service": "svc", "namespace": "ns"}, false, tools.Target{}},
		{"unknown labels", map[string]string{"foo": "bar"}, false, tools.Target{}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := ResolveTarget(tc.labels)
			if ok != tc.wantOK {
				t.Fatalf("ok=%v want %v (target=%+v)", ok, tc.wantOK, got)
			}
			if ok && got != tc.want {
				t.Fatalf("target=%+v want %+v", got, tc.want)
			}
		})
	}
}

func TestHandleForwardsPodAlertWithSnapshot(t *testing.T) {
	var mu sync.Mutex
	var received map[string]any
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&received)
		mu.Lock()
		defer mu.Unlock()
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"diagnosis_id":"diag_1","status":"queued","deduped":false}`))
	}))
	defer agent.Close()

	fwd := NewForwarder(agent.URL, 5*time.Second, stubInspector{uid: "uid-9"}, 5)
	summary := fwd.Handle(context.Background(), Webhook{
		Status: "firing",
		Alerts: []Alert{{
			Status: "firing", Fingerprint: "fp-1",
			Labels: map[string]string{"alertname": "PodHighMemory", "pod": "payment-api", "namespace": "payment"},
		}},
	})

	if summary.Accepted != 1 || summary.Unresolved != 0 || summary.Failed != 0 {
		t.Fatalf("summary=%+v", summary)
	}
	mu.Lock()
	defer mu.Unlock()
	if received["trigger"] != "alert" {
		t.Fatalf("trigger=%v", received["trigger"])
	}
	res, _ := received["resource"].(map[string]any)
	if res["uid"] != "uid-9" || res["kind"] != "Pod" || res["name"] != "payment-api" {
		t.Fatalf("resource=%+v", res)
	}
	alert, _ := received["alert"].(map[string]any)
	if alert["fingerprint"] != "fp-1" || alert["alertname"] != "PodHighMemory" {
		t.Fatalf("alert=%+v", alert)
	}
	if _, ok := alert["snapshot"].(map[string]any); !ok {
		t.Fatalf("missing snapshot: %+v", alert)
	}
	if summary.Results[0].DiagnosisID != "diag_1" {
		t.Fatalf("result=%+v", summary.Results)
	}
}

func TestHandleUnresolvedTargetHasNoResource(t *testing.T) {
	var received map[string]any
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&received)
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"status":"unresolved_target","fingerprint":"fp-x"}`))
	}))
	defer agent.Close()

	fwd := NewForwarder(agent.URL, 5*time.Second, stubInspector{}, 5)
	summary := fwd.Handle(context.Background(), Webhook{
		Status: "firing",
		Alerts: []Alert{{Status: "firing", Fingerprint: "fp-x",
			Labels: map[string]string{"alertname": "Mystery", "foo": "bar"}}},
	})
	if summary.Unresolved != 1 || summary.Accepted != 1 {
		t.Fatalf("summary=%+v", summary)
	}
	if _, ok := received["resource"]; ok {
		t.Fatalf("unresolved alert must not carry a resource: %+v", received)
	}
	alert, _ := received["alert"].(map[string]any)
	if alert["unresolved_target"] != true {
		t.Fatalf("alert=%+v", alert)
	}
}

func TestHandleContinuesAfterAgentFailure(t *testing.T) {
	var calls int
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"diagnosis_id":"diag_2","status":"queued"}`))
	}))
	defer agent.Close()

	fwd := NewForwarder(agent.URL, 5*time.Second, stubInspector{}, 5)
	summary := fwd.Handle(context.Background(), Webhook{Alerts: []Alert{
		{Fingerprint: "a", Labels: map[string]string{"pod": "p1", "namespace": "ns"}},
		{Fingerprint: "b", Labels: map[string]string{"pod": "p2", "namespace": "ns"}},
	}})
	if summary.Failed != 1 || summary.Accepted != 1 || len(summary.Results) != 2 {
		t.Fatalf("summary=%+v", summary)
	}
}

func TestRetryableStatus(t *testing.T) {
	if got := (Summary{}).RetryableStatus(); got != 202 {
		t.Fatalf("no failure -> %d", got)
	}
	if got := (Summary{Failed: 1, RateLimited: 1}).RetryableStatus(); got != 429 {
		t.Fatalf("all rate limited -> %d", got)
	}
	if got := (Summary{Failed: 1}).RetryableStatus(); got != 502 {
		t.Fatalf("generic failure -> %d", got)
	}
	if got := (Summary{Failed: 2, RateLimited: 1}).RetryableStatus(); got != 502 {
		t.Fatalf("mixed failure -> %d", got)
	}
}

func TestHandleRecordsHTTPStatus(t *testing.T) {
	agent := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "too many", http.StatusTooManyRequests)
	}))
	defer agent.Close()
	fwd := NewForwarder(agent.URL, 5*time.Second, stubInspector{}, 5)
	summary := fwd.Handle(context.Background(), Webhook{Alerts: []Alert{
		{Fingerprint: "a", Labels: map[string]string{"foo": "bar"}},
	}})
	if summary.Failed != 1 || summary.RateLimited != 1 {
		t.Fatalf("summary=%+v", summary)
	}
	if summary.Results[0].HTTPStatus != 429 {
		t.Fatalf("http status=%d", summary.Results[0].HTTPStatus)
	}
}
