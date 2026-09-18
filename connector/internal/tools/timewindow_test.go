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
	if w.Minutes != MaxRangeMinutes {
		t.Fatalf("minutes = %d, want %d", w.Minutes, MaxRangeMinutes)
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

func TestResolveWindowStartBeyondLookbackIsExplicitlyUnverifiable(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)
	if MaxAlertLookback != 30*time.Minute {
		t.Fatalf("MaxAlertLookback = %v, want exactly 30m", MaxAlertLookback)
	}
	// A one-minute-old anchor with the default 30m window starts 31m back.
	old := now.Add(-time.Minute).Format(time.RFC3339)
	_, degraded := resolveWindow(old, 30, now, true)
	if !strings.Contains(degraded, "unverifiable") {
		t.Fatalf("expected unverifiable, got %q", degraded)
	}
	if strings.Contains(degraded, "invalid") {
		t.Fatalf("a too-old window is not a parse error: %q", degraded)
	}
	// Right at the boundary (start == now - 30m) it is accepted.
	edge := now.Format(time.RFC3339)
	w, d := resolveWindow(edge, MaxRangeMinutes, now, true)
	if d != "" {
		t.Fatalf("boundary window must be accepted, got %q", d)
	}
	if now.Sub(w.Start) != MaxAlertLookback {
		t.Fatalf("expected start at the boundary, got %v", now.Sub(w.Start))
	}
}

func TestResolveWindowChecksActualStartAgainstLookback(t *testing.T) {
	now := time.Date(2026, 9, 18, 12, 0, 0, 0, time.UTC)

	// Defect regression: a 29m-old anchor with a 30m window reaches 59m back,
	// far beyond MaxAlertLookback — it must be rejected even though the anchor
	// itself looks "recent".
	oldAnchor := now.Add(-29 * time.Minute)
	if _, degraded := resolveWindow(oldAnchor.Format(time.RFC3339), 30, now, true); degraded == "" {
		t.Fatal("expected rejection when the window start leaves the lookback")
	} else if !strings.Contains(degraded, "unverifiable") {
		t.Fatalf("expected unverifiable, got %q", degraded)
	}

	// Strict policy: there is no dispatch grace, so even a 10s-old anchor with
	// the default 30m window starts just past the 30m boundary and is rejected.
	lagged := now.Add(-10 * time.Second)
	if _, d := resolveWindow(lagged.Format(time.RFC3339), 30, now, true); d == "" {
		t.Fatal("expected rejection: 30m window + any lag leaves the 30m lookback")
	}
	// A boundary window (start == now-30m) passes.
	if w, d := resolveWindow(now.Format(time.RFC3339), 30, now, true); d != "" {
		t.Fatalf("boundary window must be valid, got %q", d)
	} else if w.End.Sub(w.Start) != 30*time.Minute {
		t.Fatalf("unexpected span %v", w.End.Sub(w.Start))
	}
	// An older anchor with a correspondingly smaller window also passes.
	older := now.Add(-29 * time.Minute)
	if _, d := resolveWindow(older.Format(time.RFC3339), 1, now, true); d != "" {
		t.Fatalf("older anchor with a 1m window must be valid, got %q", d)
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
