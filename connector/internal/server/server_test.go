package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"

	"k8spilot/connector/internal/config"
	"k8spilot/connector/internal/tools"
)

func testServer(objects ...runtime.Object) *httptest.Server {
	cfg := &config.Config{EventLimit: 50, EventSince: time.Hour, LogTail: 300, LogLimitKB: 64}
	t := tools.New(fake.NewSimpleClientset(objects...), cfg)
	s := New(cfg, t)
	return httptest.NewServer(s.Handler())
}

func do(t *testing.T, s *httptest.Server, method, path string, body any) *http.Response {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			t.Fatal(err)
		}
	}
	req, err := http.NewRequest(method, s.URL+path, &buf)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func TestHealthzAndCapabilities(t *testing.T) {
	s := testServer()
	defer s.Close()

	resp := do(t, s, "GET", "/healthz", nil)
	if resp.StatusCode != 200 {
		t.Fatalf("healthz status = %d", resp.StatusCode)
	}
	resp = do(t, s, "GET", "/capabilities", nil)
	var caps map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&caps); err != nil {
		t.Fatal(err)
	}
	if caps["kubernetes.resources"] != true {
		t.Fatalf("expected kubernetes.resources=true, got %v", caps)
	}
}

func TestInspectUIDMismatchIsNotHTTPError(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "p", Namespace: "ns", UID: types.UID("current-uid")},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "c", Image: "img"}}},
	}
	s := testServer(pod)
	defer s.Close()

	resp := do(t, s, "POST", "/tools/inspect", map[string]any{
		"target": map[string]any{"kind": "Pod", "namespace": "ns", "name": "p", "uid": "stale-uid"},
	})
	if resp.StatusCode != 200 {
		t.Fatalf("uid mismatch must not be an HTTP error, got %d", resp.StatusCode)
	}
	var out struct {
		UIDMismatch bool `json:"uid_mismatch"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if !out.UIDMismatch {
		t.Fatal("expected uid_mismatch=true")
	}
}

func TestInspectNotFoundReturns404(t *testing.T) {
	s := testServer()
	defer s.Close()

	resp := do(t, s, "POST", "/tools/inspect", map[string]any{
		"target": map[string]any{"kind": "Pod", "namespace": "ns", "name": "missing"},
	})
	if resp.StatusCode != 404 {
		t.Fatalf("expected 404 for missing resource, got %d", resp.StatusCode)
	}
}

func TestUnsupportedKindReturns400(t *testing.T) {
	s := testServer()
	defer s.Close()

	resp := do(t, s, "POST", "/tools/inspect", map[string]any{
		"target": map[string]any{"kind": "ClusterRole", "name": "x"},
	})
	if resp.StatusCode != 400 {
		t.Fatalf("expected 400 for unsupported kind, got %d", resp.StatusCode)
	}
	var e toolError
	if err := json.NewDecoder(resp.Body).Decode(&e); err != nil {
		t.Fatal(err)
	}
	if e.Code != "not_supported" {
		t.Fatalf("expected code not_supported, got %q", e.Code)
	}
}

// ensure context import stays used across builds
var _ = context.Background
