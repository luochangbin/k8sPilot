package tools

import (
	"fmt"
	"time"
)

// The single, deterministic time window used by BOTH query_metrics (Prometheus)
// and query_logs (Loki). Kept here so the two data sources can never drift.
//
// Definition (also documented in docs/diagnosis-center.md "告警时间窗"):
//
//	W = range_minutes, clamped to [1, MaxRangeMinutes]; the default is
//	    DefaultRangeMinutes. No arbitrary start/end is accepted and the window
//	    length is never increased beyond the connector's existing cap.
//	manual run (no alert anchor): start = now - W, end = now
//	alert run  (alert anchor):    start = anchor - W, end = anchor
//
//	The alert anchor is the Alertmanager starts_at injected by the Agent from the
//	trusted alert context (never from the model). It must be an RFC3339
//	timestamp with an explicit offset, must not be in the future, and the
//	effective window is narrowed to the part that fits the connector's reach:
//
//		effective_start = max(anchor - requested_span, now - MaxAlertLookback)
//		effective_end   = anchor
//
//	MaxAlertLookback (30m) is never exceeded and never extended. A 10s dispatch
//	delay therefore still yields a queryable window (~29m), an anchor 29m old
//	yields a 1m window, and an anchor at/beyond the 30m boundary (or an empty
//	interval) fails closed.
const (
	// DefaultRangeMinutes is the existing default window length.
	DefaultRangeMinutes = 30
	// MaxRangeMinutes is the existing effective window cap; never raise it
	// (doing so would widen what the connector is allowed to query).
	MaxRangeMinutes = 30
	// MaxAlertLookback bounds how far in the past the *query start* may be.
	// It is a hard bound: the effective window is narrowed to fit it, never
	// extended. The connector cannot read Prometheus/Loki retention, so we
	// deliberately reuse the existing 30-minute window cap instead of inventing
	// a retention number; anything older is reported as explicitly unverifiable,
	// never silently clamped to "now" and never queried as an unbounded range.
	MaxAlertLookback = 30 * time.Minute
	// maxClockSkew tolerates small clock differences between Alertmanager and
	// the connector when rejecting "future" anchors.
	maxClockSkew = time.Minute
)

// TimeWindow is a resolved, bounded query range.
type TimeWindow struct {
	Start time.Time
	End   time.Time
	// Minutes is the effective window length floored to whole minutes (kept for
	// the legacy `range_minutes` field); Seconds is the exact length. When the
	// alert anchor is close to the 30m boundary the effective window can be
	// shorter than a minute (Minutes == 0) while still being queryable.
	Minutes int
	Seconds int
	// Anchor is the alert timestamp (RFC3339) when the window is alert-anchored.
	Anchor string
}

// resolveWindow resolves the window for a request. A non-empty degraded string
// means the request must NOT be queried: the caller reports it as an explicit
// "unverifiable" degradation instead of falling back to now.
//
// alertExpected is true for alert-triggered runs. Such a run MUST carry a usable
// anchor: a missing/empty starts_at is explicitly unverifiable and is never
// reinterpreted as a manual (now-relative) query.
func resolveWindow(alertTime string, rangeMinutes int, now time.Time,
	alertExpected bool) (TimeWindow, string) {
	minutes := rangeMinutes
	if minutes <= 0 {
		minutes = DefaultRangeMinutes
	}
	if minutes > MaxRangeMinutes {
		minutes = MaxRangeMinutes
	}
	span := time.Duration(minutes) * time.Minute

	// Second granularity: both data sources are queried with second-level
	// instants (Prometheus seconds, Loki nanoseconds of the same instant), so the
	// reported window_seconds matches the queried bounds exactly.
	now = now.UTC().Truncate(time.Second)
	if alertTime == "" {
		if alertExpected {
			// Alert run without a usable starts_at: fail closed. Falling back to
			// now-relative would silently answer a different question.
			return TimeWindow{}, "unverifiable alert window: alert-triggered run has no starts_at " +
				"(the alert context must carry a timestamp); refusing to query a now-relative window"
		}
		// Manual diagnosis: unchanged now-relative semantics.
		return TimeWindow{Start: now.Add(-span), End: now, Minutes: minutes,
			Seconds: int(span / time.Second)}, ""
	}

	anchor, err := time.Parse(time.RFC3339, alertTime)
	if err != nil {
		return TimeWindow{}, fmt.Sprintf(
			"invalid alert_time %q: expected an RFC3339 timestamp with a timezone offset", alertTime)
	}
	anchor = anchor.UTC().Truncate(time.Second)
	if anchor.After(now.Add(maxClockSkew)) {
		return TimeWindow{}, fmt.Sprintf(
			"invalid alert_time %q: timestamp is in the future", alertTime)
	}
	// Clamp the window start to the hard bound instead of extending it:
	//   start = max(anchor - requested_span, now - MaxAlertLookback)
	// The boundary is exact (no rounding), so the lookback is never exceeded.
	boundary := now.Add(-MaxAlertLookback)
	start := anchor.Add(-span)
	if start.Before(boundary) {
		start = boundary
	}
	effective := anchor.Sub(start)
	if effective <= 0 {
		return TimeWindow{}, fmt.Sprintf(
			"unverifiable alert window: starts_at %s leaves no usable interval inside the "+
				"connector's %s maximum lookback (no data-source retention is configured on the connector)",
			anchor.Format(time.RFC3339), MaxAlertLookback)
	}
	return TimeWindow{Start: start, End: anchor,
		Minutes: int(effective / time.Minute), Seconds: int(effective / time.Second),
		Anchor: alertTime}, ""
}
