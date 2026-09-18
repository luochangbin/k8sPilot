package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
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

func TestQueryMetricsHandlerNarrowsAgedAlertWindow(t *testing.T) {
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

	// 25m-old anchor with the requested 30m window: narrowed to ~5m, still queried.
	anchor := time.Now().UTC().Add(-25 * time.Minute).Truncate(time.Second)
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
		Available    bool `json:"available"`
		RangeMinutes int  `json:"range_minutes"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if !out.Available || out.RangeMinutes < 1 || out.RangeMinutes > tools.MaxRangeMinutes {
		t.Fatalf("expected a narrowed, positive window, got %+v", out)
	}
	if len(starts) != 1 || len(ends) != 1 {
		t.Fatalf("expected one prometheus call, got start=%v end=%v", starts, ends)
	}
	end, _ := strconv.ParseInt(ends[0], 10, 64)
	start, _ := strconv.ParseInt(starts[0], 10, 64)
	if end != anchor.Unix() {
		t.Fatalf("prometheus end = %d, want anchor %d", end, anchor.Unix())
	}
	if span := end - start; span <= 0 || span > int64(tools.MaxRangeMinutes*60) {
		t.Fatalf("prometheus span = %ds, want a positive window within 30m", span)
	}
}

func TestQueryMetricsHandlerRejectsBeyondLookbackAnchor(t *testing.T) {
	calls := 0
	prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"matrix","result":[]}}`))
	}))
	defer prom.Close()
	s := testServerWithDataSources(prom.URL, "")
	defer s.Close()

	anchor := time.Now().UTC().Add(-31 * time.Minute).Truncate(time.Second)
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
	if out.Available || !strings.Contains(out.DegradedReason, "unverifiable") {
		t.Fatalf("expected unverifiable, got %+v", out)
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

func assertSnakeCaseWindowSeconds(t *testing.T, body map[string]any) {
	t.Helper()
	raw, ok := body["window_seconds"]
	if !ok {
		t.Fatalf("response is missing window_seconds: %v", body)
	}
	if _, camel := body["WindowSeconds"]; camel {
		t.Fatalf("response must not emit Go-style WindowSeconds: %v", body)
	}
	seconds, ok := raw.(float64)
	if !ok {
		t.Fatalf("window_seconds is not a number: %T", raw)
	}
	start, err := time.Parse(time.RFC3339, body["window_start"].(string))
	if err != nil {
		t.Fatalf("bad window_start: %v", err)
	}
	end, err := time.Parse(time.RFC3339, body["window_end"].(string))
	if err != nil {
		t.Fatalf("bad window_end: %v", err)
	}
	// The reported value is the actual effective interval.
	if int(seconds) != int(end.Sub(start)/time.Second) || int(seconds) <= 0 {
		t.Fatalf("window_seconds=%v does not match [%v,%v]", seconds, start, end)
	}
}

func TestQueryMetricsResponseSerializesSnakeCaseWindowSeconds(t *testing.T) {
	prom := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"matrix","result":[
			{"metric":{"namespace":"ns","pod":"p"},"values":[[1700000000,"1.0"]]}]}}`))
	}))
	defer prom.Close()
	s := testServerWithDataSources(prom.URL, "")
	defer s.Close()

	anchor := time.Now().UTC().Add(-10 * time.Second).Truncate(time.Second)
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
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	assertSnakeCaseWindowSeconds(t, body)
}

func TestQueryLogsResponseSerializesSnakeCaseWindowSeconds(t *testing.T) {
	loki := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[
			{"stream":{"namespace":"ns","pod":"p"},"values":[["1700000000000000001","line"]]}]}}`))
	}))
	defer loki.Close()
	s := testServerWithDataSources("", loki.URL)
	defer s.Close()

	anchor := time.Now().UTC().Add(-10 * time.Second).Truncate(time.Second)
	resp := do(t, s, "POST", "/tools/query_logs", map[string]any{
		"target":         map[string]any{"kind": "Pod", "namespace": "ns", "name": "p"},
		"range_minutes":  30,
		"alert_time":     anchor.Format(time.RFC3339),
		"alert_expected": true,
	})
	if resp.StatusCode != 200 {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	assertSnakeCaseWindowSeconds(t, body)
}
