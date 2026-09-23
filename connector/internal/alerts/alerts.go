// Package alerts implements the Phase 5 Alertmanager webhook adapter.
//
// Alertmanager fans out to the connector (design §14/§18); the connector
// resolves the alert target, takes a lightweight fact snapshot (§15) and
// forwards a normalized alert-triggered diagnosis request to the Agent Service.
// It never crawls all metrics/logs and never writes to Kubernetes.
package alerts

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"

	"k8spilot/connector/internal/tools"
)

// Webhook is the Alertmanager webhook v4 payload (subset we consume).
type Webhook struct {
	Version           string            `json:"version"`
	GroupKey          string            `json:"groupKey"`
	Status            string            `json:"status"`
	CommonLabels      map[string]string `json:"commonLabels"`
	CommonAnnotations map[string]string `json:"commonAnnotations"`
	Alerts            []Alert           `json:"alerts"`
}

// Alert is one Alertmanager alert instance.
type Alert struct {
	Status      string            `json:"status"`
	Labels      map[string]string `json:"labels"`
	Annotations map[string]string `json:"annotations"`
	StartsAt    string            `json:"startsAt"`
	EndsAt      string            `json:"endsAt"`
	Fingerprint string            `json:"fingerprint"`
}

// Inspector is the read-only fact surface used for the lightweight snapshot.
type Inspector interface {
	Inspect(ctx context.Context, target tools.Target) (*tools.InspectResponse, error)
	Events(ctx context.Context, target tools.Target, params tools.EventsParams) (*tools.EventsResponse, error)
}

// Forwarder resolves, snapshots and forwards alerts to the Agent Service.
type Forwarder struct {
	AgentURL   string
	Client     *http.Client
	Inspector  Inspector
	EventLimit int
}

// Result is the per-alert outcome reported back to Alertmanager.
type Result struct {
	Fingerprint string `json:"fingerprint"`
	Status      string `json:"status,omitempty"`
	DiagnosisID string `json:"diagnosis_id,omitempty"`
	Deduped     bool   `json:"deduped,omitempty"`
	HTTPStatus  int    `json:"http_status,omitempty"`
	Error       string `json:"error,omitempty"`
}

// Summary is the aggregate outcome for one webhook delivery.
type Summary struct {
	Accepted    int      `json:"accepted"`
	Unresolved  int      `json:"unresolved"`
	Failed      int      `json:"failed"`
	RateLimited int      `json:"rate_limited,omitempty"`
	Results     []Result `json:"results"`
}

// RetryableStatus maps a summary to the HTTP status the webhook should return
// so Alertmanager keeps retrying. Any failure yields a non-2xx response: a pure
// Agent 429 is passed through, everything else is a 502.
func (s Summary) RetryableStatus() int {
	if s.Failed == 0 {
		return http.StatusAccepted
	}
	if s.RateLimited == s.Failed {
		return http.StatusTooManyRequests
	}
	return http.StatusBadGateway
}

// NewForwarder builds a Forwarder with a bounded HTTP client.
func NewForwarder(agentURL string, timeout time.Duration, inspector Inspector, eventLimit int) *Forwarder {
	if agentURL == "" {
		agentURL = "http://localhost:8001"
	}
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	return &Forwarder{
		AgentURL:   strings.TrimRight(agentURL, "/"),
		Client:     &http.Client{Timeout: timeout},
		Inspector:  inspector,
		EventLimit: eventLimit,
	}
}

// Handle processes every alert in the webhook; one failure never aborts the rest.
func (f *Forwarder) Handle(ctx context.Context, wh Webhook) Summary {
	summary := Summary{Results: []Result{}}
	for _, a := range wh.Alerts {
		fp := a.Fingerprint
		if fp == "" {
			fp = fingerprintOf(a.Labels)
		}
		status := a.Status
		if status == "" {
			status = wh.Status
		}
		if status == "" {
			status = "firing"
		}
		result := Result{Fingerprint: fp, Status: status}

		alertName := firstLabel(a.Labels, "alertname")
		if alertName == "" {
			alertName = firstLabel(wh.CommonLabels, "alertname")
		}
		alert := map[string]any{
			"fingerprint": fp,
			"status":      status,
			"labels":      a.Labels,
			"annotations": a.Annotations,
		}
		if alertName != "" {
			alert["alertname"] = alertName
		}
		if a.StartsAt != "" {
			alert["starts_at"] = a.StartsAt
		}
		if wh.GroupKey != "" {
			alert["group_key"] = wh.GroupKey
		}
		payload := map[string]any{"trigger": "alert", "alert": alert}

		if target, ok := ResolveTarget(a.Labels); ok {
			snapshot := f.snapshot(ctx, target)
			if t, ok := snapshot["target"].(tools.Target); ok && t.UID != "" {
				target.UID = t.UID
			}
			payload["resource"] = map[string]any{
				"apiVersion": tools.APIVersionForKind(target.Kind), "kind": target.Kind,
				"namespace": target.Namespace, "name": target.Name, "uid": target.UID,
			}
			alert["snapshot"] = snapshot
		} else {
			// Never guess a target (§26.2).
			alert["unresolved_target"] = true
			summary.Unresolved++
		}

		body, err := json.Marshal(payload)
		if err != nil {
			result.Error = err.Error()
			summary.Failed++
			summary.Results = append(summary.Results, result)
			continue
		}
		resp, httpStatus, err := f.post(ctx, body)
		result.HTTPStatus = httpStatus
		if err != nil {
			result.Error = err.Error()
			summary.Failed++
			if httpStatus == http.StatusTooManyRequests {
				summary.RateLimited++
			}
			summary.Results = append(summary.Results, result)
			continue
		}
		if s, ok := resp["status"].(string); ok {
			result.Status = s
		}
		if id, ok := resp["diagnosis_id"].(string); ok {
			result.DiagnosisID = id
		}
		if deduped, ok := resp["deduped"].(bool); ok {
			result.Deduped = deduped
		}
		summary.Accepted++
		summary.Results = append(summary.Results, result)
	}
	return summary
}

// snapshot takes a bounded lightweight snapshot (design §15).
func (f *Forwarder) snapshot(ctx context.Context, target tools.Target) map[string]any {
	out := map[string]any{}
	if f.Inspector == nil {
		return out
	}
	if insp, err := f.Inspector.Inspect(ctx, target); err == nil && insp != nil {
		out["target"] = insp.Target
		out["exists"] = insp.Exists
		out["anomalies"] = insp.Anomalies
		out["conditions"] = insp.Conditions
		out["actual_state"] = insp.ActualState
	} else if err != nil {
		out["inspect_error"] = err.Error()
	}
	limit := f.EventLimit
	if limit <= 0 {
		limit = 10
	}
	if events, err := f.Inspector.Events(ctx, target, tools.EventsParams{Limit: limit}); err == nil && events != nil {
		out["events"] = events.Events
		out["event_count"] = events.Count
		out["events_truncated"] = events.Truncated
	}
	return out
}

func (f *Forwarder) post(ctx context.Context, body []byte) (map[string]any, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		f.AgentURL+"/api/v1/diagnoses", bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := f.Client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, resp.StatusCode,
			fmt.Errorf("agent returned %d: %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, resp.StatusCode, fmt.Errorf("invalid agent response: %w", err)
	}
	return out, resp.StatusCode, nil
}

// ResolveTarget maps alert labels to a diagnosable resource. Only the
// connector-supported, agent-diagnosable kinds are accepted; anything else is
// unresolved (the caller must not guess).
func ResolveTarget(labels map[string]string) (tools.Target, bool) {
	kind := firstLabel(labels, "kind", "resource", "resource_kind")
	if kind == "" {
		switch {
		case hasAny(labels, "pod", "pod_name"):
			kind = "Pod"
		case hasAny(labels, "deployment", "deployment_name"):
			kind = "Deployment"
		case hasAny(labels, "node", "node_name"):
			kind = "Node"
		case hasAny(labels, "persistentvolumeclaim", "pvc"):
			kind = "PersistentVolumeClaim"
		}
	}
	var name string
	switch kind {
	case "Pod":
		name = firstLabel(labels, "pod", "pod_name")
	case "Deployment":
		name = firstLabel(labels, "deployment", "deployment_name")
	case "Node":
		name = firstLabel(labels, "node", "node_name")
	case "PersistentVolumeClaim":
		name = firstLabel(labels, "persistentvolumeclaim", "pvc")
	default:
		return tools.Target{}, false
	}
	target := tools.Target{
		Kind:      kind,
		Namespace: firstLabel(labels, "namespace", "namespace_name"),
		Name:      name,
	}
	if err := target.Validate(); err != nil {
		return tools.Target{}, false
	}
	return target, true
}

func firstLabel(labels map[string]string, keys ...string) string {
	for _, k := range keys {
		if v, ok := labels[k]; ok && strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

func hasAny(labels map[string]string, keys ...string) bool {
	return firstLabel(labels, keys...) != ""
}

// fingerprintOf derives a stable fingerprint when Alertmanager omits one.
func fingerprintOf(labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		b.WriteString(k)
		b.WriteByte('=')
		b.WriteString(labels[k])
		b.WriteByte('\n')
	}
	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:16])
}
