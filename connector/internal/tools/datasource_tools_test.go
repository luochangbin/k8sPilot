package tools

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"k8spilot/connector/internal/config"
	"k8spilot/connector/internal/datasource"
)

// windowCapture records the time bounds a data source actually received.
type windowCapture struct {
	startsNano []string
	endsNano   []string
	calls      int
}

func captureServer(t *testing.T, body string, cap *windowCapture) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cap.calls++
		q := r.URL.Query()
		cap.startsNano = append(cap.startsNano, q.Get("start"))
		cap.endsNano = append(cap.endsNano, q.Get("end"))
		w.WriteHeader(200)
		_, _ = w.Write([]byte(body))
	}))
}

func mustUnix(t *testing.T, s string) int64 {
	t.Helper()
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		t.Fatalf("bad unix %q: %v", s, err)
	}
	return n
}

func metricsTools(prom *datasource.PrometheusClient, loki *datasource.LokiClient) *Tools {
	cfg := &config.Config{EventLimit: 50, EventSince: time.Hour, LogTail: 300, LogLimitKB: 64,
		DataSourceTimeout: 5 * time.Second, MetricsMaxSeries: 5}
	t := New(nil, cfg)
	t.Prom = prom
	t.Loki = loki
	return t
}

func TestQueryMetricsDegradedWhenPrometheusMissing(t *testing.T) {
	tools := metricsTools(nil, nil)
	resp, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"}, MetricsParams{Metric: "memory", RangeMinutes: 30})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if resp.Available {
		t.Fatal("expected degraded when prometheus not configured")
	}
	if resp.DegradedReason == "" {
		t.Fatal("expected a degraded reason")
	}
}

func TestQueryMetricsSummarizesSeries(t *testing.T) {
	body := `{"status":"success","data":{"resultType":"matrix","result":[
		{"metric":{"namespace":"ns","pod":"p"},"values":[[1700000000,"1.0"],[1700000060,"3.0"]]}
	]}}`
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		_, _ = w.Write([]byte(body))
	}))
	defer s.Close()
	prom := &datasource.PrometheusClient{BaseURL: s.URL, HTTP: s.Client()}
	tools := metricsTools(prom, nil)

	resp, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"}, MetricsParams{Metric: "memory", RangeMinutes: 30})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !resp.Available {
		t.Fatalf("expected available, degraded: %s", resp.DegradedReason)
	}
	if resp.Summary["max"] != 3.0 {
		t.Fatalf("expected max 3.0, got %v", resp.Summary["max"])
	}
	if resp.Summary["avg"] != 2.0 {
		t.Fatalf("expected avg 2.0, got %v", resp.Summary["avg"])
	}
}

func TestQueryMetricsUnsupportedCombination(t *testing.T) {
	tools := metricsTools(&datasource.PrometheusClient{BaseURL: "http://x"}, nil)
	resp, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"}, MetricsParams{Metric: "diskio", RangeMinutes: 30})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if resp.Available {
		t.Fatal("expected degraded for unsupported metric")
	}
}

func TestQueryLogsDegradedWithoutLoki(t *testing.T) {
	tools := metricsTools(nil, nil)
	resp, err := tools.QueryLogs(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"}, LokiLogsParams{RangeMinutes: 30})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if resp.Available {
		t.Fatal("expected degraded when loki missing")
	}
}

func TestQueryLogsSummarizesAndPrioritizesErrors(t *testing.T) {
	body := `{"status":"success","data":{"resultType":"streams","result":[
		{"stream":{"namespace":"ns","pod":"p"},"values":[
			["1700000000000000001","ok line"],
			["1700000000000000002","ERROR: connection refused"],
			["1700000000000000003","java.lang.OutOfMemoryError"]
		]}
	]}}`
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		_, _ = w.Write([]byte(body))
	}))
	defer s.Close()
	loki := &datasource.LokiClient{BaseURL: s.URL, HTTP: s.Client()}
	tools := metricsTools(nil, loki)

	resp, err := tools.QueryLogs(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"}, LokiLogsParams{RangeMinutes: 30, MaxLines: 200})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !resp.Available {
		t.Fatalf("expected available, degraded: %s", resp.DegradedReason)
	}
	if resp.Summary["total_lines"] != 3 {
		t.Fatalf("expected 3 lines, got %v", resp.Summary["total_lines"])
	}
	// Error lines must lead the evidence list.
	if len(resp.Evidence) == 0 || resp.Evidence[0] != "ERROR: connection refused" {
		t.Fatalf("expected error-first evidence, got %v", resp.Evidence)
	}
}

func TestQueryLogsRejectsNonPodTarget(t *testing.T) {
	tools := metricsTools(nil, nil)
	resp, err := tools.QueryLogs(context.Background(),
		Target{Kind: "Deployment", Namespace: "ns", Name: "d"}, LokiLogsParams{})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if resp.Available {
		t.Fatal("expected degraded for non-pod logs target")
	}
}

// ---- alert-time anchor: the window must really reach Prometheus/Loki ----

const matrixBody = `{"status":"success","data":{"resultType":"matrix","result":[
	{"metric":{"namespace":"ns","pod":"p"},"values":[[1700000000,"1.0"]]}
]}}`

const streamsBody = `{"status":"success","data":{"resultType":"streams","result":[
	{"stream":{"namespace":"ns","pod":"p"},"values":[["1700000000000000001","line"]]}
]}}`

func TestQueryMetricsUsesAlertAnchorWindow(t *testing.T) {
	cap := &windowCapture{}
	s := captureServer(t, matrixBody, cap)
	defer s.Close()
	prom := &datasource.PrometheusClient{BaseURL: s.URL, HTTP: s.Client()}
	tools := metricsTools(prom, nil)

	anchor := time.Now().UTC().Add(-5 * time.Minute).Truncate(time.Second)
	resp, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		MetricsParams{Metric: "memory", RangeMinutes: 10, AlertTime: anchor.Format(time.RFC3339)})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !resp.Available {
		t.Fatalf("expected available, degraded: %s", resp.DegradedReason)
	}
	if cap.calls != 1 {
		t.Fatalf("expected exactly one prometheus call, got %d", cap.calls)
	}
	gotStart := time.Unix(mustUnix(t, cap.startsNano[0]), 0)
	gotEnd := time.Unix(mustUnix(t, cap.endsNano[0]), 0)
	if !gotEnd.Equal(anchor) {
		t.Fatalf("prometheus end = %v, want anchor %v", gotEnd, anchor)
	}
	if gotEnd.Sub(gotStart) != 10*time.Minute {
		t.Fatalf("prometheus span = %v, want 10m", gotEnd.Sub(gotStart))
	}
	if resp.RangeMinutes != 10 || resp.WindowAnchor != anchor.Format(time.RFC3339) {
		t.Fatalf("unexpected response window: %+v", resp)
	}
}

func TestQueryMetricsClampsRangeMinutesBeyondCap(t *testing.T) {
	cap := &windowCapture{}
	s := captureServer(t, matrixBody, cap)
	defer s.Close()
	tools := metricsTools(&datasource.PrometheusClient{BaseURL: s.URL, HTTP: s.Client()}, nil)

	resp, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		MetricsParams{Metric: "memory", RangeMinutes: 9999})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if resp.RangeMinutes != MaxRangeMinutes {
		t.Fatalf("range_minutes = %d, want clamped %d", resp.RangeMinutes, MaxRangeMinutes)
	}
	span := mustUnix(t, cap.endsNano[0]) - mustUnix(t, cap.startsNano[0])
	if span != int64(MaxRangeMinutes*60) {
		t.Fatalf("prometheus span = %ds, want %dm", span, MaxRangeMinutes)
	}
}

func TestManualQueriesStayNowRelative(t *testing.T) {
	promCap, lokiCap := &windowCapture{}, &windowCapture{}
	promSrv := captureServer(t, matrixBody, promCap)
	defer promSrv.Close()
	lokiSrv := captureServer(t, streamsBody, lokiCap)
	defer lokiSrv.Close()
	tools := metricsTools(
		&datasource.PrometheusClient{BaseURL: promSrv.URL, HTTP: promSrv.Client()},
		&datasource.LokiClient{BaseURL: lokiSrv.URL, HTTP: lokiSrv.Client()})

	before := time.Now().UTC().Add(-5 * time.Minute).Truncate(time.Second)
	if _, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		MetricsParams{Metric: "memory", RangeMinutes: 5}); err != nil {
		t.Fatalf("metrics err: %v", err)
	}
	if _, err := tools.QueryLogs(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		LokiLogsParams{RangeMinutes: 5}); err != nil {
		t.Fatalf("logs err: %v", err)
	}

	for name, cap := range map[string]*windowCapture{"prometheus": promCap, "loki": lokiCap} {
		var start, end time.Time
		if name == "prometheus" {
			start = time.Unix(mustUnix(t, cap.startsNano[0]), 0) // seconds
			end = time.Unix(mustUnix(t, cap.endsNano[0]), 0)
		} else {
			start = time.Unix(0, mustUnix(t, cap.startsNano[0])) // nanoseconds
			end = time.Unix(0, mustUnix(t, cap.endsNano[0]))
		}
		if end.Sub(start) != 5*time.Minute {
			t.Fatalf("%s span = %v, want 5m", name, end.Sub(start))
		}
		// Manual runs must not be anchored: the window ends at "now".
		if end.Before(before) {
			t.Fatalf("%s end %v is not now-relative", name, end)
		}
	}
}

func TestMetricsAndLogsShareTheSameAlertWindow(t *testing.T) {
	promCap, lokiCap := &windowCapture{}, &windowCapture{}
	promSrv := captureServer(t, matrixBody, promCap)
	defer promSrv.Close()
	lokiSrv := captureServer(t, streamsBody, lokiCap)
	defer lokiSrv.Close()
	tools := metricsTools(
		&datasource.PrometheusClient{BaseURL: promSrv.URL, HTTP: promSrv.Client()},
		&datasource.LokiClient{BaseURL: lokiSrv.URL, HTTP: lokiSrv.Client()})

	anchor := time.Now().UTC().Add(-2 * time.Minute).Truncate(time.Second)
	stamp := anchor.Format(time.RFC3339)
	if _, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		MetricsParams{Metric: "memory", RangeMinutes: 10, AlertTime: stamp}); err != nil {
		t.Fatalf("metrics err: %v", err)
	}
	if _, err := tools.QueryLogs(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		LokiLogsParams{RangeMinutes: 10, AlertTime: stamp}); err != nil {
		t.Fatalf("logs err: %v", err)
	}

	// Prometheus uses seconds, Loki nanoseconds: compare the instants.
	promStart := time.Unix(mustUnix(t, promCap.startsNano[0]), 0)
	promEnd := time.Unix(mustUnix(t, promCap.endsNano[0]), 0)
	lokiStart := time.Unix(0, mustUnix(t, lokiCap.startsNano[0]))
	lokiEnd := time.Unix(0, mustUnix(t, lokiCap.endsNano[0]))
	if !promStart.Equal(lokiStart) || !promEnd.Equal(lokiEnd) {
		t.Fatalf("window drift: prom=[%v,%v] loki=[%v,%v]", promStart, promEnd, lokiStart, lokiEnd)
	}
	if !promEnd.Equal(anchor) || promEnd.Sub(promStart) != 10*time.Minute {
		t.Fatalf("unexpected shared window: [%v,%v]", promStart, promEnd)
	}
}

func TestBadAlertAnchorsAreRejectedWithoutQuerying(t *testing.T) {
	now := time.Now().UTC()
	cases := map[string]string{
		"invalid": "2026-09-18T12:00:00", // no offset
		"future":  now.Add(10 * time.Minute).Format(time.RFC3339),
		"too-old": now.Add(-MaxAlertLookback - time.Hour).Format(time.RFC3339),
		"garbage": "yesterday",
	}
	for name, stamp := range cases {
		promCap, lokiCap := &windowCapture{}, &windowCapture{}
		promSrv := captureServer(t, matrixBody, promCap)
		lokiSrv := captureServer(t, streamsBody, lokiCap)
		tools := metricsTools(
			&datasource.PrometheusClient{BaseURL: promSrv.URL, HTTP: promSrv.Client()},
			&datasource.LokiClient{BaseURL: lokiSrv.URL, HTTP: lokiSrv.Client()})

		mResp, err := tools.QueryMetrics(context.Background(),
			Target{Kind: "Pod", Namespace: "ns", Name: "p"},
			MetricsParams{Metric: "memory", RangeMinutes: 30, AlertTime: stamp, AlertExpected: true})
		if err != nil {
			t.Fatalf("%s metrics err: %v", name, err)
		}
		lResp, err := tools.QueryLogs(context.Background(),
			Target{Kind: "Pod", Namespace: "ns", Name: "p"},
			LokiLogsParams{RangeMinutes: 30, AlertTime: stamp, AlertExpected: true})
		if err != nil {
			t.Fatalf("%s logs err: %v", name, err)
		}
		promSrv.Close()
		lokiSrv.Close()

		for source, reason := range map[string]string{
			"metrics": mResp.DegradedReason,
			"logs":    lResp.DegradedReason,
		} {
			if !strings.Contains(reason, "alert_time") && !strings.Contains(reason, "alert window") {
				t.Fatalf("%s/%s: expected an explicit anchor degradation, got %q",
					name, source, reason)
			}
		}
		// Fail closed: no data-source request at all (never a silent "now" query).
		if promCap.calls != 0 || lokiCap.calls != 0 {
			t.Fatalf("%s: expected no data-source calls, prom=%d loki=%d",
				name, promCap.calls, lokiCap.calls)
		}
	}
}

func TestAgedAlertWindowIsNarrowedToTheLookback(t *testing.T) {
	// A 29m30s-old anchor with a requested 30m window narrows to the ~30s that
	// fits the hard 30m lookback, and that narrowed interval is what the
	// datasource actually receives (bounded by now-30m..anchor).
	anchor := time.Now().UTC().Add(-29*time.Minute - 30*time.Second).Truncate(time.Second)
	stamp := anchor.Format(time.RFC3339)

	promCap, lokiCap := &windowCapture{}, &windowCapture{}
	promSrv := captureServer(t, matrixBody, promCap)
	defer promSrv.Close()
	lokiSrv := captureServer(t, streamsBody, lokiCap)
	defer lokiSrv.Close()
	tools := metricsTools(
		&datasource.PrometheusClient{BaseURL: promSrv.URL, HTTP: promSrv.Client()},
		&datasource.LokiClient{BaseURL: lokiSrv.URL, HTTP: lokiSrv.Client()})

	mResp, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		MetricsParams{Metric: "memory", RangeMinutes: 30, AlertTime: stamp, AlertExpected: true})
	if err != nil {
		t.Fatalf("metrics err: %v", err)
	}
	lResp, err := tools.QueryLogs(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		LokiLogsParams{RangeMinutes: 30, AlertTime: stamp, AlertExpected: true})
	if err != nil {
		t.Fatalf("logs err: %v", err)
	}
	if !mResp.Available || !lResp.Available {
		t.Fatalf("narrowed windows must be queryable: %s / %s",
			mResp.DegradedReason, lResp.DegradedReason)
	}
	promStart := mustUnix(t, promCap.startsNano[0])
	promEnd := mustUnix(t, promCap.endsNano[0])
	if promEnd != anchor.Unix() {
		t.Fatalf("prometheus end = %d, want anchor %d", promEnd, anchor.Unix())
	}
	if span := promEnd - promStart; span <= 0 || span > int64(MaxRangeMinutes*60) {
		t.Fatalf("prometheus span = %ds, want a positive window within 30m", span)
	}
	if mResp.WindowStart == "" || mResp.WindowSeconds != int(promEnd-promStart) {
		t.Fatalf("reported window %ds does not match the queried span %ds",
			mResp.WindowSeconds, promEnd-promStart)
	}
	if lResp.WindowSeconds != mResp.WindowSeconds {
		t.Fatalf("metrics/logs window drift: %d vs %d", mResp.WindowSeconds, lResp.WindowSeconds)
	}
}

func TestAlertAnchorBeyondTheLookbackFailsClosed(t *testing.T) {
	anchor := time.Now().UTC().Add(-31 * time.Minute).Truncate(time.Second)
	stamp := anchor.Format(time.RFC3339)

	promCap, lokiCap := &windowCapture{}, &windowCapture{}
	promSrv := captureServer(t, matrixBody, promCap)
	defer promSrv.Close()
	lokiSrv := captureServer(t, streamsBody, lokiCap)
	defer lokiSrv.Close()
	tools := metricsTools(
		&datasource.PrometheusClient{BaseURL: promSrv.URL, HTTP: promSrv.Client()},
		&datasource.LokiClient{BaseURL: lokiSrv.URL, HTTP: lokiSrv.Client()})

	mResp, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		MetricsParams{Metric: "memory", RangeMinutes: 30, AlertTime: stamp, AlertExpected: true})
	if err != nil {
		t.Fatalf("metrics err: %v", err)
	}
	lResp, err := tools.QueryLogs(context.Background(),
		Target{Kind: "Pod", Namespace: "ns", Name: "p"},
		LokiLogsParams{RangeMinutes: 30, AlertTime: stamp, AlertExpected: true})
	if err != nil {
		t.Fatalf("logs err: %v", err)
	}
	if mResp.Available || lResp.Available {
		t.Fatal("an anchor beyond the 30m lookback must not be available")
	}
	if !strings.Contains(mResp.DegradedReason, "unverifiable") ||
		!strings.Contains(lResp.DegradedReason, "unverifiable") {
		t.Fatalf("expected unverifiable: %q / %q", mResp.DegradedReason, lResp.DegradedReason)
	}
	if promCap.calls != 0 || lokiCap.calls != 0 {
		t.Fatalf("expected zero datasource calls, prom=%d loki=%d", promCap.calls, lokiCap.calls)
	}
}

func TestAlertRunWithoutAnchorIsRejectedWithoutQuerying(t *testing.T) {
	// Defect 2 regression: an alert-triggered run must never become now-relative.
	for _, tc := range []struct {
		name string
		call func(*Tools) (bool, string)
	}{
		{"metrics", func(tt *Tools) (bool, string) {
			resp, err := tt.QueryMetrics(context.Background(),
				Target{Kind: "Pod", Namespace: "ns", Name: "p"},
				MetricsParams{Metric: "memory", RangeMinutes: 30, AlertExpected: true})
			if err != nil {
				t.Fatalf("metrics err: %v", err)
			}
			return resp.Available, resp.DegradedReason
		}},
		{"logs", func(tt *Tools) (bool, string) {
			resp, err := tt.QueryLogs(context.Background(),
				Target{Kind: "Pod", Namespace: "ns", Name: "p"},
				LokiLogsParams{RangeMinutes: 30, AlertExpected: true})
			if err != nil {
				t.Fatalf("logs err: %v", err)
			}
			return resp.Available, resp.DegradedReason
		}},
	} {
		promCap, lokiCap := &windowCapture{}, &windowCapture{}
		promSrv := captureServer(t, matrixBody, promCap)
		lokiSrv := captureServer(t, streamsBody, lokiCap)
		tools := metricsTools(
			&datasource.PrometheusClient{BaseURL: promSrv.URL, HTTP: promSrv.Client()},
			&datasource.LokiClient{BaseURL: lokiSrv.URL, HTTP: lokiSrv.Client()})

		available, reason := tc.call(tools)
		promSrv.Close()
		lokiSrv.Close()

		if available {
			t.Fatalf("%s: alert run without anchor must not be available", tc.name)
		}
		if !strings.Contains(reason, "unverifiable") || !strings.Contains(reason, "starts_at") {
			t.Fatalf("%s: expected unverifiable starts_at reason, got %q", tc.name, reason)
		}
		if promCap.calls != 0 || lokiCap.calls != 0 {
			t.Fatalf("%s: expected zero datasource calls, prom=%d loki=%d",
				tc.name, promCap.calls, lokiCap.calls)
		}
	}
}
