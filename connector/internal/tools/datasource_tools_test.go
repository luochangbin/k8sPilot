package tools

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"k8spilot/connector/internal/config"
	"k8spilot/connector/internal/datasource"
)

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
