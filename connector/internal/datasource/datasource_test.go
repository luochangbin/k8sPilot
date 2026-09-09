package datasource

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func testPromServer(t *testing.T, body string, status int) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(s.Close)
	return s
}

func TestPrometheusRangeQueryParsesMatrix(t *testing.T) {
	body := `{"status":"success","data":{"resultType":"matrix","result":[
		{"metric":{"namespace":"ns","pod":"p"},"values":[[1700000000,"1.5"],[1700000060,"2.5"]]}
	]}}`
	s := testPromServer(t, body, 200)
	c := &PrometheusClient{BaseURL: s.URL, HTTP: s.Client()}

	series, err := c.RangeQuery(context.Background(), "up", time.Now().Add(-time.Minute), time.Now(), 15*time.Second, 5)
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	if len(series) != 1 {
		t.Fatalf("expected 1 series, got %d", len(series))
	}
	if len(series[0].Points) != 2 {
		t.Fatalf("expected 2 points, got %d", len(series[0].Points))
	}
	if series[0].Points[1].Value != 2.5 {
		t.Fatalf("expected value 2.5, got %v", series[0].Points[1].Value)
	}
	if series[0].Labels["pod"] != "p" {
		t.Fatalf("labels not parsed: %v", series[0].Labels)
	}
}

func TestPrometheusHTTPErrorSurfaces(t *testing.T) {
	s := testPromServer(t, "upstream error", 503)
	c := &PrometheusClient{BaseURL: s.URL, HTTP: s.Client()}
	if _, err := c.RangeQuery(context.Background(), "up", time.Now().Add(-time.Minute), time.Now(), 15*time.Second, 5); err == nil {
		t.Fatal("expected error on HTTP 503")
	}
}

func testLokiServer(t *testing.T, body string, status int) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(s.Close)
	return s
}

func TestLokiQueryRangeParsesStreams(t *testing.T) {
	body := `{"status":"success","data":{"resultType":"streams","result":[
		{"stream":{"namespace":"ns","pod":"p"},"values":[["1700000000000000000","java.lang.OutOfMemoryError"]]}
	]}}`
	s := testLokiServer(t, body, 200)
	c := &LokiClient{BaseURL: s.URL, HTTP: s.Client()}

	entries, err := c.QueryRange(context.Background(), `{namespace="ns",pod="p"}`, time.Now().Add(-time.Minute), time.Now(), 200)
	if err != nil {
		t.Fatalf("query: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("expected 1 entry, got %d", len(entries))
	}
	if entries[0].Line != "java.lang.OutOfMemoryError" {
		t.Fatalf("unexpected line: %q", entries[0].Line)
	}
}
