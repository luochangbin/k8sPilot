package tools

import (
	"strings"
	"testing"
	"time"
)

func TestResolveWindowManualIsNowRelative(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)
	w, degraded := resolveWindow("", 15, now, false)
	if degraded != "" {
		t.Fatalf("manual window must not degrade, got %q", degraded)
	}
	if !w.End.Equal(now) {
		t.Fatalf("manual end = %v, want now %v", w.End, now)
	}
	if w.End.Sub(w.Start) != 15*time.Minute {
		t.Fatalf("manual span = %v, want 15m", w.End.Sub(w.Start))
	}
	if w.Minutes != 15 || w.Anchor != "" {
		t.Fatalf("unexpected window: %+v", w)
	}
}

func TestResolveWindowAlertAnchoredAndCapped(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)
	// Fresh anchor: start == now - 30m is exactly the lookback boundary.
	anchor := now

	// Requested window far beyond the existing cap is clamped, never widened.
	w, degraded := resolveWindow(anchor.Format(time.RFC3339), 9999, now, true)
	if degraded != "" {
		t.Fatalf("unexpected degrade: %q", degraded)
	}
	if w.Minutes != MaxRangeMinutes || w.Seconds != MaxRangeMinutes*60 {
		t.Fatalf("window = %ds/%dm, want %dm", w.Seconds, w.Minutes, MaxRangeMinutes)
	}
	if !w.End.Equal(anchor) {
		t.Fatalf("alert end = %v, want anchor %v", w.End, anchor)
	}
	if w.End.Sub(w.Start) != time.Duration(MaxRangeMinutes)*time.Minute {
		t.Fatalf("alert span = %v", w.End.Sub(w.Start))
	}
	if w.Anchor != anchor.Format(time.RFC3339) {
		t.Fatalf("anchor not echoed: %q", w.Anchor)
	}

	// Zero/negative requests fall back to the default.
	w, _ = resolveWindow(anchor.Format(time.RFC3339), 0, now, true)
	if w.Minutes != DefaultRangeMinutes {
		t.Fatalf("minutes = %d, want default %d", w.Minutes, DefaultRangeMinutes)
	}
}

func TestResolveWindowParsesTimezoneOffsets(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)
	// 20:00+08:00 == 12:00Z (the anchor itself); both spellings must resolve to
	// the same instant and sit exactly on the lookback boundary with a 30m window.
	local, d1 := resolveWindow("2026-09-18T20:00:00+08:00", 30, now, true)
	utc, d2 := resolveWindow("2026-09-18T12:00:00Z", 30, now, true)
	if d1 != "" || d2 != "" {
		t.Fatalf("unexpected degrade: %q / %q", d1, d2)
	}
	if !local.End.Equal(utc.End) || !local.Start.Equal(utc.Start) {
		t.Fatalf("timezone conversion mismatch: local=%+v utc=%+v", local, utc)
	}
	if local.End.Unix() != time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC).Unix() {
		t.Fatalf("end unix = %d", local.End.Unix())
	}
}

func TestResolveWindowRejectsInvalidTimestamps(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)
	for _, bad := range []string{
		"2026-09-18T12:00:00", // no timezone offset
		"2026-09-18",          // date only
		"not-a-time",
		"1758000000", // unix seconds are not accepted
	} {
		if _, degraded := resolveWindow(bad, 30, now, true); degraded == "" {
			t.Fatalf("expected degradation for %q", bad)
		} else if !strings.Contains(degraded, "invalid alert_time") {
			t.Fatalf("expected invalid alert_time, got %q", degraded)
		}
	}
}

func TestResolveWindowRejectsFutureAnchor(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)
	future := now.Add(10 * time.Minute).Format(time.RFC3339)
	_, degraded := resolveWindow(future, 30, now, true)
	if !strings.Contains(degraded, "future") {
		t.Fatalf("expected future rejection, got %q", degraded)
	}
	// Small clock skew between Alertmanager and the connector is tolerated.
	skewed := now.Add(30 * time.Second).Format(time.RFC3339)
	if _, d := resolveWindow(skewed, 30, now, true); d != "" {
		t.Fatalf("small skew must be accepted, got %q", d)
	}
}

func TestResolveWindowNarrowsToTheHardLookback(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)
	if MaxAlertLookback != 30*time.Minute {
		t.Fatalf("MaxAlertLookback = %v, want exactly 30m", MaxAlertLookback)
	}

	// A fresh anchor with the default 30m window: exactly the boundary.
	w, d := resolveWindow(now.Format(time.RFC3339), 30, now, true)
	if d != "" || w.Seconds != 1800 || w.Minutes != 30 {
		t.Fatalf("boundary window = %+v (%q)", w, d)
	}
	if now.Sub(w.Start) != MaxAlertLookback {
		t.Fatalf("start must sit on the boundary, got %v back", now.Sub(w.Start))
	}

	// A 10s dispatch delay keeps the window queryable (start clamped to now-30m).
	lagged := now.Add(-10 * time.Second)
	w, d = resolveWindow(lagged.Format(time.RFC3339), 30, now, true)
	if d != "" {
		t.Fatalf("a 10s-old anchor must stay queryable, got %q", d)
	}
	if w.Seconds != 1790 || w.Minutes != 29 {
		t.Fatalf("effective window = %ds/%dm, want 1790s/29m", w.Seconds, w.Minutes)
	}
	if now.Sub(w.Start) != MaxAlertLookback || !w.End.Equal(lagged) {
		t.Fatalf("unexpected bounds: [%v,%v]", w.Start, w.End)
	}

	// A 29m-old anchor narrows to a 1m window instead of being rejected.
	aged := now.Add(-29 * time.Minute)
	if w, d := resolveWindow(aged.Format(time.RFC3339), 30, now, true); d != "" {
		t.Fatalf("29m-old anchor must narrow, not fail: %q", d)
	} else if w.Seconds != 60 || w.Minutes != 1 || now.Sub(w.Start) != MaxAlertLookback {
		t.Fatalf("expected a 1m narrowed window on the boundary, got %+v", w)
	}

	// Sub-minute remainder stays usable and is reported exactly.
	almost := now.Add(-29*time.Minute - 30*time.Second)
	if w, d := resolveWindow(almost.Format(time.RFC3339), 30, now, true); d != "" {
		t.Fatalf("30s remainder must stay queryable, got %q", d)
	} else if w.Seconds != 30 || w.Minutes != 0 || now.Sub(w.Start) != MaxAlertLookback {
		t.Fatalf("expected a 30s window, got %+v", w)
	}

	// No usable interval remains -> fail closed.
	for _, age := range []time.Duration{MaxAlertLookback, 31 * time.Minute} {
		stamp := now.Add(-age).Format(time.RFC3339)
		if _, d := resolveWindow(stamp, 30, now, true); !strings.Contains(d, "unverifiable") {
			t.Fatalf("age %v must fail closed, got %q", age, d)
		}
	}
}

func TestResolveWindowAlertRunWithoutAnchorIsUnverifiable(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)

	// Alert-triggered run with no usable starts_at: fail closed, never now-relative.
	_, degraded := resolveWindow("", 30, now, true)
	if !strings.Contains(degraded, "unverifiable") {
		t.Fatalf("expected unverifiable, got %q", degraded)
	}
	if strings.Contains(degraded, "invalid alert_time") {
		t.Fatalf("a missing anchor is not a parse error: %q", degraded)
	}

	// Manual run without alert context keeps now-relative semantics.
	w, d := resolveWindow("", 30, now, false)
	if d != "" {
		t.Fatalf("manual window must not degrade, got %q", d)
	}
	if !w.End.Equal(now) || w.End.Sub(w.Start) != 30*time.Minute {
		t.Fatalf("unexpected manual window: %+v", w)
	}
}
