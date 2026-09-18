package tools

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"
)

// LokiLogsParams controls the query_logs tool (Phase 3 capability loki.logs).
type LokiLogsParams struct {
	Namespace    string
	Pod          string
	RangeMinutes int
	MaxLines     int
	Filter       string // optional substring (e.g. "error")
	// AlertTime is the trusted Alertmanager starts_at (RFC3339); empty for
	// manual runs (now-relative window). Shares resolveWindow with metrics.
	AlertTime string
	// AlertExpected is true for alert-triggered runs: an empty/invalid
	// AlertTime must then degrade explicitly instead of becoming now-relative.
	AlertExpected bool
}

// LokiLogsResponse is a bounded, summarized log view (design §12).
type LokiLogsResponse struct {
	Target         Target         `json:"target"`
	Capability     string         `json:"capability"`
	Available      bool           `json:"available"`
	DegradedReason string         `json:"degraded_reason,omitempty"`
	RangeMinutes   int            `json:"range_minutes"`
	WindowStart    string         `json:"window_start,omitempty"`
	WindowEnd      string         `json:"window_end,omitempty"`
	WindowAnchor   string         `json:"window_anchor,omitempty"`
	Summary        map[string]any `json:"summary"`
	Evidence       []string       `json:"evidence"`
	Truncated      bool           `json:"truncated"`
}

// QueryLogs fetches pod logs from Loki with cardinality limits and error-line
// prioritization; the connector never hands raw bulk logs to the LLM.
func (t *Tools) QueryLogs(ctx context.Context, target Target, params LokiLogsParams) (*LokiLogsResponse, error) {
	resp := &LokiLogsResponse{
		Target:       target,
		Capability:   "loki.logs",
		RangeMinutes: params.RangeMinutes,
		Summary:      map[string]any{},
		Evidence:     []string{},
	}
	if params.RangeMinutes <= 0 {
		params.RangeMinutes = DefaultRangeMinutes
	}
	resp.RangeMinutes = params.RangeMinutes
	if params.MaxLines <= 0 {
		params.MaxLines = 200
	}
	if target.Kind != "Pod" {
		resp.DegradedReason = fmt.Sprintf("query_logs only supports Pod targets, got %q", target.Kind)
		return resp, nil
	}
	if t.Loki == nil {
		resp.DegradedReason = "loki not configured on connector"
		return resp, nil
	}

	// Same window definition as query_metrics (see timewindow.go).
	window, degraded := resolveWindow(params.AlertTime, params.RangeMinutes, time.Now(),
		params.AlertExpected)
	if degraded != "" {
		// Missing/invalid/future/out-of-reach anchor: fail closed. Never silently query "now".
		resp.DegradedReason = degraded
		return resp, nil
	}
	resp.RangeMinutes = window.Minutes
	resp.WindowStart = window.Start.Format(time.RFC3339)
	resp.WindowEnd = window.End.Format(time.RFC3339)
	resp.WindowAnchor = window.Anchor

	query := fmt.Sprintf(`{namespace=%q, pod=%q}`, target.Namespace, target.Name)
	if f := strings.TrimSpace(params.Filter); f != "" {
		query += fmt.Sprintf(` |= %q`, f)
	}

	start, end := window.Start, window.End
	ctx, cancel := context.WithTimeout(ctx, t.Cfg.DataSourceTimeout)
	defer cancel()
	entries, err := t.Loki.QueryRange(ctx, query, start, end, params.MaxLines)
	if err != nil {
		resp.DegradedReason = fmt.Sprintf("loki query failed: %v", err)
		return resp, nil
	}
	if len(entries) == 0 {
		resp.DegradedReason = "no log entries matched in the window"
		return resp, nil
	}

	resp.Available = true
	// Newest first.
	sort.SliceStable(entries, func(i, j int) bool { return entries[i].Timestamp > entries[j].Timestamp })

	lines := make([]string, 0, len(entries))
	errorLike := 0
	patterns := map[string]int{}
	for _, e := range entries {
		line := e.Line
		lines = append(lines, line)
		low := strings.ToLower(line)
		if isErrorLike(low) {
			errorLike++
		}
		patterns[patternKey(line)]++
	}

	// Evidence: newest error-ish lines first, bounded.
	evidence := prioritizeEvidence(lines, 20)
	resp.Truncated = len(entries) >= params.MaxLines
	resp.Summary = map[string]any{
		"total_lines":  len(entries),
		"error_lines":  errorLike,
		"top_patterns": topPatterns(patterns, 5),
	}
	resp.Evidence = evidence
	return resp, nil
}

func isErrorLike(low string) bool {
	for _, kw := range []string{"error", "exception", "fatal", "panic", "fail", "outofmemory", "refused"} {
		if strings.Contains(low, kw) {
			return true
		}
	}
	return false
}

// patternKey collapses numbers/hashes so repeated stack traces group.
func patternKey(line string) string {
	if len(line) > 120 {
		line = line[:120]
	}
	// naive collapse of hex/numbers
	var b strings.Builder
	for _, r := range line {
		if (r >= '0' && r <= '9') || r == 'x' {
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

func topPatterns(patterns map[string]int, n int) []string {
	type kv struct {
		k string
		v int
	}
	var arr []kv
	for k, v := range patterns {
		arr = append(arr, kv{k, v})
	}
	sort.Slice(arr, func(i, j int) bool { return arr[i].v > arr[j].v })
	out := make([]string, 0, n)
	for i := 0; i < len(arr) && i < n; i++ {
		out = append(out, fmt.Sprintf("%d x %s", arr[i].v, truncateStr(arr[i].k, 80)))
	}
	return out
}

func prioritizeEvidence(lines []string, n int) []string {
	var errs, others []string
	for _, l := range lines {
		if isErrorLike(strings.ToLower(l)) {
			errs = append(errs, l)
		} else {
			others = append(others, l)
		}
	}
	var out []string
	seen := map[string]bool{}
	for _, l := range append(errs, others...) {
		if len(out) >= n {
			break
		}
		key := truncateStr(l, 200)
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, truncateStr(l, 500))
	}
	return out
}

func truncateStr(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
