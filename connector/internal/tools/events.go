package tools

import (
	"context"
	"fmt"
	"sort"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Event is a normalized Kubernetes event.
type Event struct {
	Type           string `json:"type"`
	Reason         string `json:"reason"`
	Message        string `json:"message"`
	Source         string `json:"source,omitempty"`
	Count          int32  `json:"count"`
	FirstTimestamp string `json:"first_timestamp,omitempty"`
	LastTimestamp  string `json:"last_timestamp,omitempty"`
}

// EventsResponse is returned by the events tool.
type EventsResponse struct {
	Target    Target  `json:"target"`
	Count     int     `json:"count"`
	Events    []Event `json:"events"`
	Truncated bool    `json:"truncated"`
}

// EventsParams controls the events tool query window and cardinality.
type EventsParams struct {
	Limit int
	Since time.Duration
}

// Events returns recent events for the target resource, newest first, bounded
// by a time window and count limit.
func (t *Tools) Events(ctx context.Context, target Target, params EventsParams) (*EventsResponse, error) {
	if params.Limit <= 0 {
		params.Limit = t.Cfg.EventLimit
	}
	if params.Since <= 0 {
		params.Since = t.Cfg.EventSince
	}

	ns := target.Namespace
	if IsClusterScoped(target.Kind) {
		ns = ""
	}
	fieldSelector := fmt.Sprintf("involvedObject.kind=%s", target.Kind)
	if target.Name != "" {
		fieldSelector += fmt.Sprintf(",involvedObject.name=%s", target.Name)
	}

	list, err := t.Kube.CoreV1().Events(ns).List(ctx, metav1.ListOptions{FieldSelector: fieldSelector})
	if err != nil {
		return nil, err
	}

	resp := &EventsResponse{Target: target, Events: []Event{}}
	since := time.Now().UTC().Add(-params.Since)
	for i := range list.Items {
		ev := &list.Items[i]
		last := ev.LastTimestamp.Time
		if last.IsZero() {
			last = ev.EventTime.Time
		}
		if last.IsZero() || last.Before(since) {
			continue
		}
		first := ev.FirstTimestamp.Time
		resp.Events = append(resp.Events, Event{
			Type:           ev.Type,
			Reason:         ev.Reason,
			Message:        ev.Message,
			Source:         ev.Source.Component,
			Count:          ev.Count,
			FirstTimestamp: formatTime(first),
			LastTimestamp:  formatTime(last),
		})
	}

	sort.SliceStable(resp.Events, func(i, j int) bool {
		return resp.Events[i].LastTimestamp > resp.Events[j].LastTimestamp
	})
	if len(resp.Events) > params.Limit {
		resp.Truncated = true
		resp.Events = resp.Events[:params.Limit]
	}
	resp.Count = len(resp.Events)
	return resp, nil
}

func formatTime(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	return t.UTC().Format(time.RFC3339)
}
