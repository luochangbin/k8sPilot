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
//	timestamp with an explicit offset, must not be in the future, and the whole
//	requested window must fit inside the connector's reach:
//
//		start >= now - MaxAlertLookback          (MaxAlertLookback == 30m)
//
//	The check is on the *actual start* (anchor - W), not just the anchor age:
//	an anchor 29m old with a 30m window reaches 59m back and is rejected; a fresh
//	anchor with the default 30m window sits exactly on the boundary and passes.
const (
	// DefaultRangeMinutes is the existing default window length.
	DefaultRangeMinutes = 30
	// MaxRangeMinutes is the existing effective window cap; never raise it
	// (doing so would widen what the connector is allowed to query).
	MaxRangeMinutes = 30
	// MaxAlertLookback bounds how far in the past the *query start* may be.
	// There is no extra grace for dispatch lag: the whole requested window must
	// fit inside this bound. The connector cannot read Prometheus/Loki retention,
	// so we deliberately reuse the existing 30-minute window cap instead of
	// inventing a retention number; anything older is reported as explicitly
	// unverifiable, never silently clamped to "now" and never queried as an
	// unbounded range.
	MaxAlertLookback = 30 * time.Minute
	// maxClockSkew tolerates small clock differences between Alertmanager and
	// the connector when rejecting "future" anchors.
	maxClockSkew = time.Minute
)

// TimeWindow is a resolved, bounded query range.
type TimeWindow struct {
	Start   time.Time
	End     time.Time
	Minutes int
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

	now = now.UTC()
	if alertTime == "" {
		if alertExpected {
			// Alert run without a usable starts_at: fail closed. Falling back to
			// now-relative would silently answer a different question.
			return TimeWindow{}, "unverifiable alert window: alert-triggered run has no starts_at " +
				"(the alert context must carry a timestamp); refusing to query a now-relative window"
		}
		// Manual diagnosis: unchanged now-relative semantics.
		return TimeWindow{Start: now.Add(-span), End: now, Minutes: minutes}, ""
	}

	anchor, err := time.Parse(time.RFC3339, alertTime)
	if err != nil {
		return TimeWindow{}, fmt.Sprintf(
			"invalid alert_time %q: expected an RFC3339 timestamp with a timezone offset", alertTime)
	}
	anchor = anchor.UTC()
	if anchor.After(now.Add(maxClockSkew)) {
		return TimeWindow{}, fmt.Sprintf(
			"invalid alert_time %q: timestamp is in the future", alertTime)
	}
	start := anchor.Add(-span)
	if now.Sub(start) > MaxAlertLookback {
		return TimeWindow{}, fmt.Sprintf(
			"unverifiable alert window: start %s is %s before now, outside the connector's "+
				"%s maximum lookback (no data-source retention is configured on the connector)",
			start.Format(time.RFC3339), now.Sub(start).Round(time.Second), MaxAlertLookback)
	}
	return TimeWindow{Start: start, End: anchor, Minutes: minutes, Anchor: alertTime}, ""
}
