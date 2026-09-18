package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"

	"k8spilot/connector/internal/config"
	"k8spilot/connector/internal/datasource"
	"k8spilot/connector/internal/tools"
)

func testServer(objects ...runtime.Object) *httptest.Server {
	cfg := &config.Config{EventLimit: 50, EventSince: time.Hour, LogTail: 300, LogLimitKB: 64}
	t := tools.New(fake.NewSimpleClientset(objects...), cfg)
	s := New(cfg, t)
	return httptest.NewServer(s.Handler())
}

func do(t *testing.T, s *httptest.Server, method, path string, body any) *http.Response {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req, err := http.NewRequest(method, s.URL+path, &buf)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func TestHealthzAndCapabilities(t *testing.T) {
	s := testServer()
	defer s.Close()

	resp := do(t, s, "GET", "/healthz", nil)
	if resp.StatusCode != 200 {
		t.Fatalf("healthz status = %d", resp.StatusCode)
	}
	resp = do(t, s, "GET", "/capabilities", nil)
	var caps map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&caps); err != nil {
		t.Fatal(err)
	}
	if caps["kubernetes.resources"] != true {
		t.Fatalf("expected kubernetes.resources=true, got %v", caps)
	}
}

func TestInspectUIDMismatchIsNotHTTPError(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "p", Namespace: "ns", UID: types.UID("current-uid")},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "c", Image: "img"}}},
	}
	s := testServer(pod)
	defer s.Close()

	resp := do(t, s, "POST", "/tools/inspect", map[string]any{
		"target": map[string]any{"kind": "Pod", "namespace": "ns", "name": "p", "uid": "stale-uid"},
	})
	if resp.StatusCode != 200 {
		t.Fatalf("uid mismatch must not be an HTTP error, got %d", resp.StatusCode)
	}
	var out struct {
		UIDMismatch bool `json:"uid_mismatch"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if !out.UIDMismatch {
		t.Fatal("expected uid_mismatch=true")
	}
}

func TestInspectNotFoundReturns404(t *testing.T) {
	s := testServer()
	defer s.Close()

	resp := do(t, s, "POST", "/tools/inspect", map[string]any{
		"target": map[string]any{"kind": "Pod", "namespace": "ns", "name": "missing"},
	})
	if resp.StatusCode != 404 {
		t.Fatalf("expected 404 for missing resource, got %d", resp.StatusCode)
	}
}

func TestUnsupportedKindReturns400(t *testing.T) {
	s := testServer()
	defer s.Close()

	resp := do(t, s, "POST", "/tools/inspect", map[string]any{
		"target": map[string]any{"kind": "ClusterRole", "name": "x"},
	})
	if resp.StatusCode != 400 {
		t.Fatalf("expected 400 for unsupported kind, got %d", resp.StatusCode)
	}
	var e toolError
	if err := json.NewDecoder(resp.Body).Decode(&e); err != nil {
		t.Fatal(err)
	}
	if e.Code != "not_supported" {
		t.Fatalf("expected code not_supported, got %q", e.Code)
	}
}

// ensure context import stays used across builds
var _ = context.Background

func testServerWithDataSources(promURL, lokiURL string) *httptest.Server {
	cfg := &config.Config{EventLimit: 50, EventSince: time.Hour, LogTail: 300, LogLimitKB: 64,
		DataSourceTimeout: 5 * time.Second, MetricsMaxSeries: 5}
	t := tools.New(fake.NewSimpleClientset(), cfg)
	if promURL != "" {
		t.Prom = &datasource.PrometheusClient{BaseURL: promURL}
	}
	if lokiURL != "" {
		t.Loki = &datasource.LokiClient{BaseURL: lokiURL}
	}
	return httptest.NewServer(New(cfg, t).Handler())
}

func TestQueryMetricsHandlerForwardsAlertTimeToPrometheus(t *testing.T) {
	var starts, ends []string
	prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		starts = append(starts, r.URL.Query().Get("start"))
		ends = append(ends, r.URL.Query().Get("end"))
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"matrix","result":[
			{"metric":{"namespace":"ns","pod":"p"},"values":[[1700000000,"1.0"]]}]}}`))
	}))
	defer prom.Close()
	s := testServerWithDataSources(prom.URL, "")
	defer s.Close()

	anchor := time.Now().UTC().Add(-3 * time.Minute).Truncate(time.Second)
	resp := do(t, s, "POST", "/tools/query_metrics", map[string]any{
		"target":        map[string]any{"kind": "Pod", "namespace": "ns", "name": "p"},
		"metric":        "memory",
		"range_minutes": 15,
		"alert_time":    anchor.Format(time.RFC3339),
	})
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var out struct {
		Available    bool   `json:"available"`
		RangeMinutes int    `json:"range_minutes"`
		WindowAnchor string `json:"window_anchor"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if !out.Available || out.WindowAnchor != anchor.Format(time.RFC3339) {
		t.Fatalf("unexpected response: %+v", out)
	}
	if len(starts) != 1 || len(ends) != 1 {
		t.Fatalf("expected one prometheus call, got start=%v end=%v", starts, ends)
	}
	end, _ := strconv.ParseInt(ends[0], 10, 64)
	start, _ := strconv.ParseInt(starts[0], 10, 64)
	if end != anchor.Unix() {
		t.Fatalf("prometheus end = %d, want anchor %d", end, anchor.Unix())
	}
	if end-start != 15*60 {
		t.Fatalf("prometheus span = %ds, want 15m", end-start)
	}
}

func TestQueryMetricsHandlerRejectsBadAlertTimeWithoutCallingPrometheus(t *testing.T) {
	calls := 0
	prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"matrix","result":[]}}`))
	}))
	defer prom.Close()
	s := testServerWithDataSources(prom.URL, "")
	defer s.Close()

	resp := do(t, s, "POST", "/tools/query_metrics", map[string]any{
		"target":     map[string]any{"kind": "Pod", "namespace": "ns", "name": "p"},
		"metric":     "memory",
		"alert_time": "2026-09-18T12:00:00", // no timezone offset
	})
	if resp.StatusCode != 200 {
		t.Fatalf("a bad anchor must degrade, not error; status = %d", resp.StatusCode)
	}
	var out struct {
		Available      bool   `json:"available"`
		DegradedReason string `json:"degraded_reason"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.Available || out.DegradedReason == "" {
		t.Fatalf("expected explicit degradation, got %+v", out)
	}
	if calls != 0 {
		t.Fatalf("must not query prometheus with an unverifiable anchor, calls=%d", calls)
	}
}

func TestQueryMetricsHandlerRejectsAlertRunWithoutAnchor(t *testing.T) {
	calls := 0
	prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"matrix","result":[]}}`))
	}))
	defer prom.Close()
	s := testServerWithDataSources(prom.URL, "")
	defer s.Close()

	// alert_expected without alert_time: must degrade, never query now-relative.
	resp := do(t, s, "POST", "/tools/query_metrics", map[string]any{
		"target":         map[string]any{"kind": "Pod", "namespace": "ns", "name": "p"},
		"metric":         "memory",
		"alert_expected": true,
	})
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var out struct {
		Available      bool   `json:"available"`
		DegradedReason string `json:"degraded_reason"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.Available || out.DegradedReason == "" {
		t.Fatalf("expected explicit degradation, got %+v", out)
	}
	if calls != 0 {
		t.Fatalf("must not query prometheus, calls=%d", calls)
	}
}

func TestQueryMetricsHandlerRejectsOutOfReachAlertWindow(t *testing.T) {
	calls := 0
	prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"matrix","result":[]}}`))
	}))
	defer prom.Close()
	s := testServerWithDataSources(prom.URL, "")
	defer s.Close()

	// A 29m-old anchor with the 30m default window reaches 59m back.
	anchor := time.Now().UTC().Add(-29 * time.Minute).Truncate(time.Second)
	resp := do(t, s, "POST", "/tools/query_metrics", map[string]any{
		"target":         map[string]any{"kind": "Pod", "namespace": "ns", "name": "p"},
		"metric":         "memory",
		"range_minutes":  30,
		"alert_time":     anchor.Format(time.RFC3339),
		"alert_expected": true,
	})
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var out struct {
		Available      bool   `json:"available"`
		DegradedReason string `json:"degraded_reason"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.Available || out.DegradedReason == "" {
		t.Fatalf("expected explicit degradation, got %+v", out)
	}
	if calls != 0 {
		t.Fatalf("must not query prometheus, calls=%d", calls)
	}
}

func TestQueryMetricsHandlerAcceptsInRangeAlertWindow(t *testing.T) {
	var ends []string
	prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ends = append(ends, r.URL.Query().Get("end"))
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"matrix","result":[
			{"metric":{"namespace":"ns","pod":"p"},"values":[[1700000000,"1.0"]]}]}}`))
	}))
	defer prom.Close()
	s := testServerWithDataSources(prom.URL, "")
	defer s.Close()

	anchor := time.Now().UTC().Add(-2 * time.Minute).Truncate(time.Second)
	resp := do(t, s, "POST", "/tools/query_metrics", map[string]any{
		"target":         map[string]any{"kind": "Pod", "namespace": "ns", "name": "p"},
		"metric":         "memory",
		"range_minutes":  10,
		"alert_time":     anchor.Format(time.RFC3339),
		"alert_expected": true,
	})
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var out struct {
		Available bool `json:"available"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if !out.Available {
		t.Fatal("an in-range alert window must succeed")
	}
	if len(ends) != 1 || ends[0] != strconv.FormatInt(anchor.Unix(), 10) {
		t.Fatalf("unexpected prometheus end: %v (anchor %d)", ends, anchor.Unix())
	}
}
