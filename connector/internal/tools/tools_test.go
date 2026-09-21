package tools

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes/fake"

	"k8spilot/connector/internal/config"
	"k8spilot/connector/internal/datasource"
)

func newTestTools(objects ...runtime.Object) *Tools {
	cfg := &config.Config{EventLimit: 50, EventSince: time.Hour, LogTail: 300, LogLimitKB: 64}
	return New(fake.NewSimpleClientset(objects...), cfg)
}

func testPod(name string, uid string, owner *metav1.OwnerReference) *corev1.Pod {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: "payment", UID: types.UID(uid),
			Labels: map[string]string{"app": "payment-api"},
		},
		Spec: corev1.PodSpec{
			NodeName:           "node-17",
			ServiceAccountName: "payment-api",
			RestartPolicy:      corev1.RestartPolicyAlways,
			Containers:         []corev1.Container{{Name: "payment-api", Image: "registry/payment-api:v1"}},
		},
		Status: corev1.PodStatus{Phase: corev1.PodRunning},
	}
	if owner != nil {
		pod.OwnerReferences = []metav1.OwnerReference{*owner}
	}
	return pod
}

func TestInspectPodNormalizesState(t *testing.T) {
	pod := testPod("payment-api-7b8c9", "uid-1", nil)
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{
		{
			Name: "payment-api", Ready: false, RestartCount: 37,
			State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{Reason: "CrashLoopBackOff"}},
			LastTerminationState: corev1.ContainerState{
				Terminated: &corev1.ContainerStateTerminated{Reason: "OOMKilled", ExitCode: 137},
			},
		},
	}
	tools := newTestTools(pod)

	resp, err := tools.Inspect(context.Background(), Target{Kind: "Pod", Namespace: "payment", Name: "payment-api-7b8c9", UID: "uid-1"})
	if err != nil {
		t.Fatalf("inspect: %v", err)
	}
	if !resp.Exists || resp.UIDMismatch {
		t.Fatalf("expected exists and no uid mismatch, got %+v", resp)
	}
	if len(resp.Anomalies) == 0 {
		t.Fatal("expected anomalies for CrashLoopBackOff pod")
	}
	if resp.ActualState["restart_count"] != int32(37) {
		t.Fatalf("expected restart_count=37, got %v", resp.ActualState["restart_count"])
	}
	states := resp.ActualState["container_states"].([]map[string]any)
	state := states[0]
	if state["reason"] != "CrashLoopBackOff" {
		t.Fatalf("expected reason CrashLoopBackOff, got %v", state["reason"])
	}
	lt := state["last_termination"].(map[string]any)
	if lt["reason"] != "OOMKilled" {
		t.Fatalf("expected last termination OOMKilled, got %v", lt["reason"])
	}
}

func TestInspectPodUIDMismatch(t *testing.T) {
	pod := testPod("payment-api-7b8c9", "uid-1", nil)
	tools := newTestTools(pod)

	resp, err := tools.Inspect(context.Background(), Target{Kind: "Pod", Namespace: "payment", Name: "payment-api-7b8c9", UID: "uid-old"})
	if err != nil {
		t.Fatalf("inspect: %v", err)
	}
	if !resp.UIDMismatch {
		t.Fatal("expected uid_mismatch=true when requested UID differs from current")
	}
}

func TestInspectPodNotFound(t *testing.T) {
	tools := newTestTools()
	if _, err := tools.Inspect(context.Background(), Target{Kind: "Pod", Namespace: "payment", Name: "nope"}); err == nil {
		t.Fatal("expected error for missing pod")
	}
}

func TestInspectDeploymentReplicaMismatch(t *testing.T) {
	replicas := int32(5)
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "payment-api", Namespace: "payment", UID: "dep-1", Generation: 2},
		Spec:       appsv1.DeploymentSpec{Replicas: &replicas, Selector: &metav1.LabelSelector{MatchLabels: map[string]string{"app": "payment-api"}}},
		Status: appsv1.DeploymentStatus{
			Replicas: 5, ReadyReplicas: 2, AvailableReplicas: 2, UpdatedReplicas: 2, ObservedGeneration: 2,
		},
	}
	tools := newTestTools(dep)

	resp, err := tools.Inspect(context.Background(), Target{Kind: "Deployment", Namespace: "payment", Name: "payment-api"})
	if err != nil {
		t.Fatalf("inspect: %v", err)
	}
	if resp.DesiredState["replicas"] != int32(5) {
		t.Fatalf("expected desired replicas=5, got %v", resp.DesiredState["replicas"])
	}
	if resp.ActualState["ready_replicas"] != int32(2) {
		t.Fatalf("expected ready_replicas=2, got %v", resp.ActualState["ready_replicas"])
	}
	if len(resp.Anomalies) == 0 {
		t.Fatal("expected replicas mismatch anomaly")
	}
}

func TestRelationsPodOwnerNodeServicePVC(t *testing.T) {
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "payment-api", Namespace: "payment", UID: "dep-1"},
	}
	rs := &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Name: "payment-api-7b8c9-6d8f7", Namespace: "payment", UID: "rs-1",
			OwnerReferences: []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "Deployment", Name: "payment-api", UID: "dep-1"}},
		},
	}
	pod := testPod("payment-api-7b8c9", "uid-1", &metav1.OwnerReference{APIVersion: "apps/v1", Kind: "ReplicaSet", Name: "payment-api-7b8c9-6d8f7", UID: "rs-1"})
	pod.Spec.Volumes = []corev1.Volume{{Name: "data", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: "payment-data"}}}}
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: "payment-api", Namespace: "payment"},
		Spec:       corev1.ServiceSpec{Selector: map[string]string{"app": "payment-api"}},
	}
	pvc := &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Name: "payment-data", Namespace: "payment"},
	}
	tools := newTestTools(dep, rs, pod, svc, pvc)

	resp, err := tools.Relations(context.Background(), Target{Kind: "Pod", Namespace: "payment", Name: "payment-api-7b8c9"})
	if err != nil {
		t.Fatalf("relations: %v", err)
	}
	have := map[string]bool{}
	for _, r := range resp.Relations {
		have[r.Kind+"/"+r.Name] = true
	}
	for want := range map[string]bool{
		"ReplicaSet/payment-api-7b8c9-6d8f7": true,
		"Deployment/payment-api":             true,
		"Node/node-17":                       true,
		"Service/payment-api":                true,
		"PersistentVolumeClaim/payment-data": true,
	} {
		if !have[want] {
			t.Errorf("expected relation %q, got %v", want, resp.Relations)
		}
	}
	// ServiceAccount is intentionally not advertised: inspect() does not support
	// it, so following it would contradict the tool contract.
	if have["ServiceAccount/payment-api"] {
		t.Errorf("ServiceAccount must not be a relation, got %v", resp.Relations)
	}
}

func TestEventsLimitAndTimeFilter(t *testing.T) {
	now := time.Now().UTC()
	old := now.Add(-3 * time.Hour)
	events := []*corev1.Event{
		{
			ObjectMeta:     metav1.ObjectMeta{Name: "e-old", Namespace: "payment"},
			InvolvedObject: corev1.ObjectReference{Kind: "Pod", Namespace: "payment", Name: "payment-api"},
			Type:           "Warning", Reason: "BackOff", Message: "old event",
			LastTimestamp: metav1.NewTime(old), FirstTimestamp: metav1.NewTime(old), Count: 1,
		},
		{
			ObjectMeta:     metav1.ObjectMeta{Name: "e2", Namespace: "payment"},
			InvolvedObject: corev1.ObjectReference{Kind: "Pod", Namespace: "payment", Name: "payment-api"},
			Type:           "Warning", Reason: "BackOff", Message: "second",
			LastTimestamp: metav1.NewTime(now), FirstTimestamp: metav1.NewTime(now), Count: 1,
		},
		{
			ObjectMeta:     metav1.ObjectMeta{Name: "e1", Namespace: "payment"},
			InvolvedObject: corev1.ObjectReference{Kind: "Pod", Namespace: "payment", Name: "payment-api"},
			Type:           "Warning", Reason: "OOMKilling", Message: "first (newest)",
			LastTimestamp: metav1.NewTime(now.Add(time.Minute)), FirstTimestamp: metav1.NewTime(now.Add(time.Minute)), Count: 1,
		},
	}
	tools := newTestTools()
	// events must be added via the fake clientset tracker
	for _, e := range events {
		_, err := tools.Kube.CoreV1().Events("payment").Create(context.Background(), e, metav1.CreateOptions{})
		if err != nil {
			t.Fatalf("create event: %v", err)
		}
	}

	resp, err := tools.Events(context.Background(), Target{Kind: "Pod", Namespace: "payment", Name: "payment-api"}, EventsParams{Limit: 1, Since: time.Hour})
	if err != nil {
		t.Fatalf("events: %v", err)
	}
	if len(resp.Events) != 1 {
		t.Fatalf("expected 1 event (old one filtered, newest kept), got %d: %+v", len(resp.Events), resp.Events)
	}
	if resp.Events[0].Reason != "OOMKilling" {
		t.Fatalf("expected newest event OOMKilling, got %+v", resp.Events[0])
	}
	if !resp.Truncated {
		t.Fatal("expected truncated=true when limit applied")
	}
}

func TestLogsRejectsNonPodTarget(t *testing.T) {
	tools := newTestTools()
	if _, err := tools.Logs(context.Background(), Target{Kind: "Deployment", Namespace: "payment", Name: "payment-api"}, LogsParams{}); err == nil {
		t.Fatal("expected error for non-Pod logs target")
	}
}

func TestTargetValidation(t *testing.T) {
	if err := (Target{Kind: "Pod", Namespace: "payment", Name: "x"}).Validate(); err != nil {
		t.Fatalf("valid target rejected: %v", err)
	}
	if err := (Target{Kind: "Pod", Name: "x"}).Validate(); err == nil {
		t.Fatal("expected error when namespace missing for Pod")
	}
	if err := (Target{Kind: "Node", Name: "x"}).Validate(); err != nil {
		t.Fatalf("cluster-scoped target rejected: %v", err)
	}
	if err := (Target{Kind: "ClusterRole", Name: "x"}).Validate(); err == nil {
		t.Fatal("expected error for unsupported kind")
	}
}

func TestRelationsDeploymentReplicaSetPods(t *testing.T) {
	deploy := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "payment-api", Namespace: "payment", UID: types.UID("d-1")},
	}
	rs := &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Name: "payment-api-abc", Namespace: "payment", UID: types.UID("rs-1"),
			OwnerReferences: []metav1.OwnerReference{
				{APIVersion: "apps/v1", Kind: "Deployment", Name: "payment-api", UID: types.UID("d-1")},
			},
		},
	}
	otherRS := &appsv1.ReplicaSet{
		ObjectMeta: metav1.ObjectMeta{
			Name: "other-abc", Namespace: "payment", UID: types.UID("rs-2"),
			OwnerReferences: []metav1.OwnerReference{
				{APIVersion: "apps/v1", Kind: "Deployment", Name: "other", UID: types.UID("d-2")},
			},
		},
	}
	pod1 := testPod("payment-api-abc-1", "p-1", &metav1.OwnerReference{
		APIVersion: "v1", Kind: "ReplicaSet", Name: "payment-api-abc", UID: types.UID("rs-1")})
	pod2 := testPod("payment-api-abc-2", "p-2", &metav1.OwnerReference{
		APIVersion: "v1", Kind: "ReplicaSet", Name: "payment-api-abc", UID: types.UID("rs-1")})
	stray := testPod("other-abc-1", "p-3", &metav1.OwnerReference{
		APIVersion: "v1", Kind: "ReplicaSet", Name: "other-abc", UID: types.UID("rs-2")})

	tools := newTestTools(deploy, rs, otherRS, pod1, pod2, stray)
	resp, err := tools.Relations(context.Background(),
		Target{Kind: "Deployment", Namespace: "payment", Name: "payment-api"})
	if err != nil {
		t.Fatalf("relations: %v", err)
	}
	got := map[string]ResourceRef{}
	for _, ref := range resp.Relations {
		got[ref.Kind+"/"+ref.Name] = ref
	}
	if ref, ok := got["ReplicaSet/payment-api-abc"]; !ok {
		t.Fatalf("expected the owned ReplicaSet, got %v", resp.Relations)
	} else if ref.APIVersion != "apps/v1" {
		t.Fatalf("ReplicaSet apiVersion = %q, want apps/v1", ref.APIVersion)
	}
	for _, name := range []string{"Pod/payment-api-abc-1", "Pod/payment-api-abc-2"} {
		if _, ok := got[name]; !ok {
			t.Fatalf("expected %s in relations, got %v", name, resp.Relations)
		}
	}
	if _, ok := got["Pod/other-abc-1"]; ok {
		t.Fatal("a Pod owned by another Deployment's ReplicaSet must not be returned")
	}
	if _, ok := got["ReplicaSet/other-abc"]; ok {
		t.Fatal("a ReplicaSet owned by another Deployment must not be returned")
	}
}

func TestRelationsPodDoesNotExposeServiceAccount(t *testing.T) {
	pod := testPod("payment-api-7b8c9", "uid-1", nil) // sets ServiceAccountName=payment-api
	tools := newTestTools(pod)

	resp, err := tools.Relations(context.Background(),
		Target{Kind: "Pod", Namespace: "payment", Name: "payment-api-7b8c9"})
	if err != nil {
		t.Fatalf("relations: %v", err)
	}
	// inspect() does not support ServiceAccount, so it must not be advertised as
	// a relation the agent could follow.
	for _, ref := range resp.Relations {
		if ref.Kind == "ServiceAccount" {
			t.Fatalf("ServiceAccount must not be exposed as a relation: %+v", ref)
		}
	}
}

func TestAPIVersionForKind(t *testing.T) {
	cases := map[string]string{
		"Deployment":            "apps/v1",
		"ReplicaSet":            "apps/v1",
		"StatefulSet":           "apps/v1",
		"DaemonSet":             "apps/v1",
		"Job":                   "batch/v1",
		"Pod":                   "v1",
		"Node":                  "v1",
		"PersistentVolumeClaim": "v1",
		"Namespace":             "v1",
		"SomethingNew":          "v1",
	}
	for kind, want := range cases {
		if got := APIVersionForKind(kind); got != want {
			t.Fatalf("APIVersionForKind(%q) = %q, want %q", kind, got, want)
		}
	}
}

func TestEventsIgnoreEventsFromARecreatedObject(t *testing.T) {
	now := time.Now().UTC()
	newEvent := func(name, uid string) *corev1.Event {
		return &corev1.Event{
			ObjectMeta:     metav1.ObjectMeta{Name: name, Namespace: "payment"},
			InvolvedObject: corev1.ObjectReference{Kind: "Pod", Namespace: "payment", Name: "p", UID: types.UID(uid)},
			Type:           "Warning", Reason: "BackOff", Message: name,
			LastTimestamp: metav1.NewTime(now), FirstTimestamp: metav1.NewTime(now), Count: 1,
		}
	}
	tools := newTestTools(newEvent("e-old", "uid-old"), newEvent("e-new", "uid-new"))

	resp, err := tools.Events(context.Background(),
		Target{Kind: "Pod", Namespace: "payment", Name: "p", UID: "uid-new"},
		EventsParams{Limit: 50, Since: time.Hour})
	if err != nil {
		t.Fatalf("events: %v", err)
	}
	if resp.Count != 1 || resp.Events[0].Message != "e-new" {
		t.Fatalf("expected only the new object's event, got %+v", resp.Events)
	}

	// Without a UID (relations-discovered resource) both are returned.
	resp, err = tools.Events(context.Background(),
		Target{Kind: "Pod", Namespace: "payment", Name: "p"},
		EventsParams{Limit: 50, Since: time.Hour})
	if err != nil {
		t.Fatalf("events: %v", err)
	}
	if resp.Count != 2 {
		t.Fatalf("expected both events without a uid, got %+v", resp.Events)
	}
}

func TestLogsRejectARecreatedPod(t *testing.T) {
	pod := testPod("payment-api-7b8c9", "uid-new", nil)
	tools := newTestTools(pod)

	_, err := tools.Logs(context.Background(),
		Target{Kind: "Pod", Namespace: "payment", Name: "payment-api-7b8c9", UID: "uid-old"},
		LogsParams{})
	var recreated ErrTargetRecreated
	if !errors.As(err, &recreated) {
		t.Fatalf("expected ErrTargetRecreated, got %v", err)
	}
}

func TestQueryMetricsRejectsARecreatedPodWithoutQuerying(t *testing.T) {
	pod := testPod("p", "uid-new", nil)
	cap := &windowCapture{}
	promSrv := captureServer(t, matrixBody, cap)
	defer promSrv.Close()
	tools := metricsTools(&datasource.PrometheusClient{BaseURL: promSrv.URL, HTTP: promSrv.Client()}, nil)
	tools.Kube = fake.NewSimpleClientset(pod)

	resp, err := tools.QueryMetrics(context.Background(),
		Target{Kind: "Pod", Namespace: "payment", Name: "p", UID: "uid-old"},
		MetricsParams{Metric: "memory", RangeMinutes: 30})
	if err != nil {
		t.Fatalf("metrics: %v", err)
	}
	if resp.Available || !strings.Contains(resp.DegradedReason, "recreated") {
		t.Fatalf("expected a recreation degradation, got %+v", resp)
	}
	if cap.calls != 0 {
		t.Fatalf("must not query prometheus for a recreated target, calls=%d", cap.calls)
	}
}

func TestQueryLogsRejectsARecreatedPodWithoutQuerying(t *testing.T) {
	pod := testPod("p", "uid-new", nil)
	lokiCap := &windowCapture{}
	lokiSrv := captureServer(t, streamsBody, lokiCap)
	defer lokiSrv.Close()
	tools := metricsTools(nil, &datasource.LokiClient{BaseURL: lokiSrv.URL, HTTP: lokiSrv.Client()})
	tools.Kube = fake.NewSimpleClientset(pod)

	resp, err := tools.QueryLogs(context.Background(),
		Target{Kind: "Pod", Namespace: "payment", Name: "p", UID: "uid-old"},
		LokiLogsParams{RangeMinutes: 30})
	if err != nil {
		t.Fatalf("logs: %v", err)
	}
	if resp.Available || !strings.Contains(resp.DegradedReason, "recreated") {
		t.Fatalf("expected a recreation degradation, got %+v", resp)
	}
	if lokiCap.calls != 0 {
		t.Fatalf("must not query loki for a recreated target, calls=%d", lokiCap.calls)
	}
}
