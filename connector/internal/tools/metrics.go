package tools

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"k8spilot/connector/internal/datasource"
)

// MetricsParams controls the query_metrics tool.
type MetricsParams struct {
	Kind         string // Pod | Node
	Namespace    string
	Name         string
	Metric       string // memory | cpu
	RangeMinutes int
	// AlertTime is the trusted Alertmanager starts_at (RFC3339); empty for
	// manual runs (now-relative window).
	AlertTime string
	// AlertExpected is true for alert-triggered runs: an empty/invalid
	// AlertTime must then degrade explicitly instead of becoming now-relative.
	AlertExpected bool
}

// MetricsResponse is the normalized output of query_metrics.
type MetricsResponse struct {
	Target         Target                  `json:"target"`
	Capability     string                  `json:"capability"`
	Available      bool                    `json:"available"`
	DegradedReason string                  `json:"degraded_reason,omitempty"`
	Metric         string                  `json:"metric"`
	RangeMinutes   int                     `json:"range_minutes"`
	WindowStart    string                  `json:"window_start,omitempty"`
	WindowSeconds  int                     `json:"window_seconds"`
	WindowEnd      string                  `json:"window_end,omitempty"`
	WindowAnchor   string                  `json:"window_anchor,omitempty"`
	Summary        map[string]any          `json:"summary"`
	Series         []datasource.TimeSeries `json:"series"`
	Truncated      bool                    `json:"truncated"`
}

// QueryMetrics queries Prometheus for a fixed, safe set of metrics. It never
// accepts free-form PromQL. Data-source absence/errors degrade explicitly.
func (t *Tools) QueryMetrics(ctx context.Context, target Target, params MetricsParams) (*MetricsResponse, error) {
	resp := &MetricsResponse{
		Target:       target,
		Capability:   "prometheus.metrics",
		Metric:       params.Metric,
		Summary:      map[string]any{},
		RangeMinutes: params.RangeMinutes,
	}
	if params.RangeMinutes <= 0 {
		params.RangeMinutes = DefaultRangeMinutes
	}
	resp.RangeMinutes = params.RangeMinutes

	if t.Prom == nil {
		resp.DegradedReason = "prometheus not configured on connector"
		return resp, nil
	}

	window, degraded := resolveWindow(params.AlertTime, params.RangeMinutes, time.Now(),
		params.AlertExpected)
	if degraded != "" {
		// Missing/invalid/future/out-of-reach anchor: fail closed. Never silently query "now".
		resp.DegradedReason = degraded
		return resp, nil
	}
	resp.RangeMinutes = window.Minutes
	resp.WindowSeconds = window.Seconds
	resp.WindowStart = window.Start.Format(time.RFC3339)
	resp.WindowEnd = window.End.Format(time.RFC3339)
	resp.WindowAnchor = window.Anchor

	nodeIP := ""
	if target.Kind == "Node" {
		ip, err := t.nodeInternalIP(ctx, target.Name)
		if err != nil {
			resp.DegradedReason = fmt.Sprintf("cannot resolve node ip: %v", err)
			return resp, nil
		}
		nodeIP = ip
	}
	query, err := buildMetricPromQL(target, params.Metric, nodeIP)
	if err != nil {
		resp.DegradedReason = err.Error()
		return resp, nil
	}

	start, end := window.Start, window.End
	step := end.Sub(start) / 60
	if step < 15*time.Second {
		step = 15 * time.Second
	}

	ctx, cancel := context.WithTimeout(ctx, t.Cfg.DataSourceTimeout)
	defer cancel()
	series, err := t.Prom.RangeQuery(ctx, query, start, end, step, t.Cfg.MetricsMaxSeries)
	if err != nil {
		resp.DegradedReason = fmt.Sprintf("prometheus query failed: %v", err)
		return resp, nil
	}
	if len(series) == 0 {
		resp.DegradedReason = "no metric series matched the target in the window"
		return resp, nil
	}

	resp.Available = true
	resp.Series = series
	resp.Summary = summarizeSeries(series)
	resp.Truncated = len(series) >= t.Cfg.MetricsMaxSeries
	return resp, nil
}

func (t *Tools) nodeInternalIP(ctx context.Context, name string) (string, error) {
	node, err := t.Kube.CoreV1().Nodes().Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return "", err
	}
	for _, addr := range node.Status.Addresses {
		if addr.Type == corev1.NodeInternalIP {
			return addr.Address, nil
		}
	}
	return "", fmt.Errorf("node %q has no InternalIP", name)
}

func buildMetricPromQL(target Target, metric, nodeIP string) (string, error) {
	switch target.Kind {
	case "Pod":
		switch metric {
		case "memory":
			return fmt.Sprintf(`container_memory_working_set_bytes{namespace=%q, pod=%q, container!=""}`, target.Namespace, target.Name), nil
		case "cpu":
			return fmt.Sprintf(`sum by (pod) (rate(container_cpu_usage_seconds_total{namespace=%q, pod=%q}[5m]))`, target.Namespace, target.Name), nil
		}
	case "Node":
		inst := fmt.Sprintf(`instance=~%q`, nodeIP+":.*")
		switch metric {
		case "memory":
			return fmt.Sprintf(`(node_memory_MemTotal_bytes{%s} - node_memory_MemAvailable_bytes{%s})`, inst, inst), nil
		case "cpu":
			return fmt.Sprintf(`100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle", %s}[5m])) * 100`, inst), nil
		}
	}
	return "", fmt.Errorf("metric %q not supported for kind %q", metric, target.Kind)
}

func summarizeSeries(series []datasource.TimeSeries) map[string]any {
	var sum, max float64
	var count int
	var latest float64
	for _, s := range series {
		for _, p := range s.Points {
			if p.Value > max {
				max = p.Value
			}
			sum += p.Value
			count++
			latest = p.Value
		}
	}
	avg := 0.0
	if count > 0 {
		avg = sum / float64(count)
	}
	return map[string]any{
		"latest": round2(latest),
		"max":    round2(max),
		"avg":    round2(avg),
	}
}

func round2(v float64) float64 {
	return float64(int(v*100)) / 100
}
