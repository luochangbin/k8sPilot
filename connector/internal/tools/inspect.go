package tools

import (
	"context"
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

// InspectResponse is the normalized view returned by the inspect tool.
type InspectResponse struct {
	Target       Target         `json:"target"`
	Exists       bool           `json:"exists"`
	UIDMismatch  bool           `json:"uid_mismatch"`
	DesiredState map[string]any `json:"desired_state"`
	ActualState  map[string]any `json:"actual_state"`
	Conditions   []Condition    `json:"conditions"`
	Anomalies    []string       `json:"anomalies"`
}

// Inspect fetches the target resource, verifies its UID (when supplied), and
// returns a normalized desired/actual state, conditions and anomalies.
func (t *Tools) Inspect(ctx context.Context, target Target) (*InspectResponse, error) {
	switch target.Kind {
	case "Pod":
		return t.inspectPod(ctx, target)
	case "Deployment":
		return t.inspectDeployment(ctx, target)
	case "ReplicaSet":
		return t.inspectReplicaSet(ctx, target)
	case "StatefulSet":
		return t.inspectStatefulSet(ctx, target)
	case "DaemonSet":
		return t.inspectDaemonSet(ctx, target)
	case "Node":
		return t.inspectNode(ctx, target)
	case "Service":
		return t.inspectService(ctx, target)
	case "PersistentVolumeClaim":
		return t.inspectPVC(ctx, target)
	case "Namespace":
		return t.inspectNamespace(ctx, target)
	default:
		return nil, ErrNotSupported{Kind: target.Kind}
	}
}

func (t *Tools) uidMismatch(target Target, currentUID types.UID) bool {
	return target.UID != "" && string(currentUID) != target.UID
}

func (t *Tools) inspectPod(ctx context.Context, target Target) (*InspectResponse, error) {
	pod, err := t.Kube.CoreV1().Pods(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, pod.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	for _, c := range pod.Status.Conditions {
		resp.Conditions = append(resp.Conditions, podCondition(c))
	}

	containers := make([]map[string]any, 0, len(pod.Spec.Containers))
	for _, c := range pod.Spec.Containers {
		containers = append(containers, map[string]any{
			"name":              c.Name,
			"image":             c.Image,
			"image_pull_policy": string(c.ImagePullPolicy),
			"resources": map[string]any{
				"limits":   resourceList(c.Resources.Limits),
				"requests": resourceList(c.Resources.Requests),
			},
		})
	}
	resp.DesiredState = map[string]any{
		"restart_policy":   string(pod.Spec.RestartPolicy),
		"service_account":  pod.Spec.ServiceAccountName,
		"qos_class":        string(pod.Status.QOSClass),
		"node_selector":    pod.Spec.NodeSelector,
		"containers":       containers,
		"owner_references": ownerRefs(pod),
	}

	totalRestarts := int32(0)
	containerStates := make([]map[string]any, 0, len(pod.Status.ContainerStatuses))
	for _, cs := range pod.Status.ContainerStatuses {
		totalRestarts += cs.RestartCount
		state := containerStateSummary(cs)
		containerStates = append(containerStates, state)
	}
	resp.ActualState = map[string]any{
		"phase":              string(pod.Status.Phase),
		"message":            pod.Status.Message,
		"reason":             pod.Status.Reason,
		"node":               pod.Spec.NodeName,
		"pod_ip":             pod.Status.PodIP,
		"host_ip":            pod.Status.HostIP,
		"start_time":         timeString(pod.Status.StartTime),
		"deletion_timestamp": timeString(pod.DeletionTimestamp),
		"restart_count":      totalRestarts,
		"container_states":   containerStates,
	}

	resp.Anomalies = podAnomalies(pod, containerStates)
	return resp, nil
}

func containerStateSummary(cs corev1.ContainerStatus) map[string]any {
	state := map[string]any{
		"name":          cs.Name,
		"ready":         cs.Ready,
		"restart_count": cs.RestartCount,
		"image":         cs.Image,
	}
	if cs.State.Waiting != nil {
		state["state"] = "waiting"
		state["reason"] = cs.State.Waiting.Reason
		state["message"] = cs.State.Waiting.Message
	}
	if cs.State.Running != nil {
		state["state"] = "running"
		state["started_at"] = cs.State.Running.StartedAt.String()
	}
	if cs.State.Terminated != nil {
		state["state"] = "terminated"
		state["reason"] = cs.State.Terminated.Reason
		state["exit_code"] = cs.State.Terminated.ExitCode
		state["finished_at"] = cs.State.Terminated.FinishedAt.String()
	}
	if lt := cs.LastTerminationState.Terminated; lt != nil {
		state["last_termination"] = map[string]any{
			"reason":      lt.Reason,
			"exit_code":   lt.ExitCode,
			"finished_at": lt.FinishedAt.String(),
		}
	}
	return state
}

func podAnomalies(pod *corev1.Pod, states []map[string]any) []string {
	var out []string
	switch pod.Status.Phase {
	case corev1.PodPending:
		out = append(out, fmt.Sprintf("pod is Pending (message: %q)", pod.Status.Message))
	case corev1.PodFailed:
		out = append(out, fmt.Sprintf("pod is Failed (reason: %s)", pod.Status.Reason))
	case corev1.PodUnknown:
		out = append(out, "pod status is Unknown")
	}
	for _, cond := range pod.Status.Conditions {
		if cond.Type == corev1.PodReady && cond.Status == corev1.ConditionFalse {
			out = append(out, fmt.Sprintf("pod condition Ready=False: reason=%s message=%q", cond.Reason, cond.Message))
		}
		if cond.Type == corev1.PodScheduled && cond.Status == corev1.ConditionFalse {
			out = append(out, fmt.Sprintf("pod condition Scheduled=False: reason=%s message=%q", cond.Reason, cond.Message))
		}
	}
	for _, s := range states {
		name, _ := s["name"].(string)
		state, _ := s["state"].(string)
		reason, _ := s["reason"].(string)
		if state == "waiting" && (reason == "CrashLoopBackOff" || reason == "ErrImagePull" || reason == "ImagePullBackOff" || reason == "CreateContainerConfigError") {
			out = append(out, fmt.Sprintf("container %s is waiting with reason=%s", name, reason))
		}
		if lt, ok := s["last_termination"].(map[string]any); ok {
			lr, _ := lt["reason"].(string)
			if lr != "" && lr != "Completed" {
				out = append(out, fmt.Sprintf("container %s last terminated reason=%s exit_code=%v", name, lr, lt["exit_code"]))
			}
		}
	}
	return out
}

func resourceList(l corev1.ResourceList) map[string]string {
	out := map[string]string{}
	for k, v := range l {
		out[string(k)] = v.String()
	}
	return out
}

func timeString(t *metav1.Time) string {
	if t == nil {
		return ""
	}
	return t.UTC().Format("2006-01-02T15:04:05Z")
}

func (t *Tools) inspectDeployment(ctx context.Context, target Target) (*InspectResponse, error) {
	d, err := t.Kube.AppsV1().Deployments(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, d.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	replicas := int32(1)
	if d.Spec.Replicas != nil {
		replicas = *d.Spec.Replicas
	}
	desired := map[string]any{
		"replicas":          replicas,
		"selector":          d.Spec.Selector.MatchLabels,
		"strategy":          string(d.Spec.Strategy.Type),
		"min_ready_seconds": d.Spec.MinReadySeconds,
		"owner_references":  ownerRefs(d),
	}
	actual := map[string]any{
		"replicas":            d.Status.Replicas,
		"ready_replicas":      d.Status.ReadyReplicas,
		"available_replicas":  d.Status.AvailableReplicas,
		"updated_replicas":    d.Status.UpdatedReplicas,
		"observed_generation": d.Status.ObservedGeneration,
	}
	resp.DesiredState = desired
	resp.ActualState = actual
	for _, c := range d.Status.Conditions {
		resp.Conditions = append(resp.Conditions, Condition{
			Type:    string(c.Type),
			Status:  string(c.Status),
			Reason:  c.Reason,
			Message: c.Message,
		})
	}
	if d.Status.ReadyReplicas < d.Status.Replicas {
		resp.Anomalies = append(resp.Anomalies,
			fmt.Sprintf("deployment replicas mismatch: desired=%d ready=%d available=%d updated=%d",
				d.Status.Replicas, d.Status.ReadyReplicas, d.Status.AvailableReplicas, d.Status.UpdatedReplicas))
	}
	if d.Status.ObservedGeneration != d.Generation {
		resp.Anomalies = append(resp.Anomalies,
			fmt.Sprintf("deployment generation mismatch: generation=%d observed=%d", d.Generation, d.Status.ObservedGeneration))
	}
	return resp, nil
}

func (t *Tools) inspectReplicaSet(ctx context.Context, target Target) (*InspectResponse, error) {
	rs, err := t.Kube.AppsV1().ReplicaSets(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, rs.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	replicas := int32(1)
	if rs.Spec.Replicas != nil {
		replicas = *rs.Spec.Replicas
	}
	resp.DesiredState = map[string]any{
		"replicas":         replicas,
		"selector":         rs.Spec.Selector.MatchLabels,
		"owner_references": ownerRefs(rs),
	}
	resp.ActualState = map[string]any{
		"replicas":           rs.Status.Replicas,
		"ready_replicas":     rs.Status.ReadyReplicas,
		"available_replicas": rs.Status.AvailableReplicas,
	}
	if rs.Status.ReadyReplicas < rs.Status.Replicas {
		resp.Anomalies = append(resp.Anomalies,
			fmt.Sprintf("replicaset replicas mismatch: desired=%d ready=%d", rs.Status.Replicas, rs.Status.ReadyReplicas))
	}
	return resp, nil
}

func (t *Tools) inspectStatefulSet(ctx context.Context, target Target) (*InspectResponse, error) {
	s, err := t.Kube.AppsV1().StatefulSets(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, s.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	replicas := int32(1)
	if s.Spec.Replicas != nil {
		replicas = *s.Spec.Replicas
	}
	resp.DesiredState = map[string]any{
		"replicas":         replicas,
		"service_name":     s.Spec.ServiceName,
		"owner_references": ownerRefs(s),
	}
	resp.ActualState = map[string]any{
		"replicas":         s.Status.Replicas,
		"ready_replicas":   s.Status.ReadyReplicas,
		"current_replicas": s.Status.CurrentReplicas,
		"updated_replicas": s.Status.UpdatedReplicas,
	}
	if s.Status.ReadyReplicas < s.Status.Replicas {
		resp.Anomalies = append(resp.Anomalies,
			fmt.Sprintf("statefulset replicas mismatch: desired=%d ready=%d", s.Status.Replicas, s.Status.ReadyReplicas))
	}
	return resp, nil
}

func (t *Tools) inspectDaemonSet(ctx context.Context, target Target) (*InspectResponse, error) {
	ds, err := t.Kube.AppsV1().DaemonSets(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, ds.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	resp.DesiredState = map[string]any{
		"desired_number_scheduled": ds.Status.DesiredNumberScheduled,
		"selector":                 ds.Spec.Selector.MatchLabels,
	}
	resp.ActualState = map[string]any{
		"number_ready":        ds.Status.NumberReady,
		"number_available":    ds.Status.NumberAvailable,
		"number_misscheduled": ds.Status.NumberMisscheduled,
	}
	if ds.Status.NumberReady < ds.Status.DesiredNumberScheduled {
		resp.Anomalies = append(resp.Anomalies,
			fmt.Sprintf("daemonset replicas mismatch: desired=%d ready=%d", ds.Status.DesiredNumberScheduled, ds.Status.NumberReady))
	}
	return resp, nil
}

func (t *Tools) inspectNode(ctx context.Context, target Target) (*InspectResponse, error) {
	n, err := t.Kube.CoreV1().Nodes().Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, n.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	ready := false
	for _, c := range n.Status.Conditions {
		cond := nodeCondition(c)
		resp.Conditions = append(resp.Conditions, cond)
		if c.Type == corev1.NodeReady {
			ready = c.Status == corev1.ConditionTrue
		}
		if c.Status == corev1.ConditionTrue &&
			(c.Type == corev1.NodeMemoryPressure || c.Type == corev1.NodeDiskPressure ||
				c.Type == corev1.NodePIDPressure || c.Type == corev1.NodeNetworkUnavailable) {
			resp.Anomalies = append(resp.Anomalies, fmt.Sprintf("node condition %s=True: message=%q", c.Type, c.Message))
		}
	}
	resp.DesiredState = map[string]any{
		"unschedulable": n.Spec.Unschedulable,
		"labels":        n.Labels,
	}
	resp.ActualState = map[string]any{
		"ready":           ready,
		"phase":           n.Status.Phase,
		"kubelet_version": n.Status.NodeInfo.KubeletVersion,
		"os_image":        n.Status.NodeInfo.OSImage,
		"capacity":        resourceList(n.Status.Capacity),
		"allocatable":     resourceList(n.Status.Allocatable),
	}
	if !ready {
		resp.Anomalies = append(resp.Anomalies, "node is NotReady")
	}
	return resp, nil
}

func (t *Tools) inspectService(ctx context.Context, target Target) (*InspectResponse, error) {
	svc, err := t.Kube.CoreV1().Services(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, svc.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	resp.DesiredState = map[string]any{
		"type":     string(svc.Spec.Type),
		"selector": svc.Spec.Selector,
		"ports":    svcPorts(svc.Spec.Ports),
	}
	endpoints, err := t.Kube.CoreV1().Endpoints(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	endpointCount := 0
	if err == nil {
		for _, ss := range endpoints.Subsets {
			endpointCount += len(ss.Addresses)
		}
	}
	resp.ActualState = map[string]any{
		"cluster_ip":     svc.Spec.ClusterIP,
		"endpoint_count": endpointCount,
	}
	if svc.Spec.Type == corev1.ServiceTypeLoadBalancer {
		if len(svc.Status.LoadBalancer.Ingress) > 0 {
			resp.ActualState["load_balancer"] = svc.Status.LoadBalancer.Ingress[0].IP
		} else {
			resp.Anomalies = append(resp.Anomalies, "service is LoadBalancer type but has no load balancer ingress assigned")
		}
	}
	if svc.Spec.Type == corev1.ServiceTypeClusterIP && endpointCount == 0 && len(svc.Spec.Selector) > 0 {
		resp.Anomalies = append(resp.Anomalies, "service has selector but no ready endpoints")
	}
	return resp, nil
}

func svcPorts(ports []corev1.ServicePort) []map[string]any {
	out := make([]map[string]any, 0, len(ports))
	for _, p := range ports {
		out = append(out, map[string]any{
			"name":        p.Name,
			"port":        p.Port,
			"target_port": p.TargetPort.String(),
			"protocol":    string(p.Protocol),
		})
	}
	return out
}

func (t *Tools) inspectPVC(ctx context.Context, target Target) (*InspectResponse, error) {
	pvc, err := t.Kube.CoreV1().PersistentVolumeClaims(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, pvc.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	resp.DesiredState = map[string]any{
		"access_modes":      pvcAccessModes(pvc.Spec.AccessModes),
		"requested_storage": resourceList(pvc.Spec.Resources.Requests),
		"storage_class":     ptrString(pvc.Spec.StorageClassName),
	}
	actual := map[string]any{
		"phase":    string(pvc.Status.Phase),
		"capacity": resourceList(pvc.Status.Capacity),
		"volume":   pvc.Spec.VolumeName,
	}
	resp.ActualState = actual
	for _, c := range pvc.Status.Conditions {
		resp.Conditions = append(resp.Conditions, Condition{
			Type:    string(c.Type),
			Status:  string(c.Status),
			Reason:  c.Reason,
			Message: c.Message,
		})
	}
	if pvc.Status.Phase != corev1.ClaimBound {
		resp.Anomalies = append(resp.Anomalies, fmt.Sprintf("persistentvolumeclaim is %s (storage_class=%s)", pvc.Status.Phase, ptrString(pvc.Spec.StorageClassName)))
	}
	return resp, nil
}

func pvcAccessModes(modes []corev1.PersistentVolumeAccessMode) []string {
	out := make([]string, 0, len(modes))
	for _, m := range modes {
		out = append(out, string(m))
	}
	return out
}

func (t *Tools) inspectNamespace(ctx context.Context, target Target) (*InspectResponse, error) {
	ns, err := t.Kube.CoreV1().Namespaces().Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	resp := &InspectResponse{
		Target:      target,
		Exists:      true,
		UIDMismatch: t.uidMismatch(target, ns.UID),
		Conditions:  []Condition{},
		Anomalies:   []string{},
	}
	resp.DesiredState = map[string]any{"finalizers": ns.Spec.Finalizers}
	resp.ActualState = map[string]any{"phase": string(ns.Status.Phase)}
	if ns.Status.Phase == corev1.NamespaceTerminating {
		resp.Anomalies = append(resp.Anomalies, "namespace is Terminating")
	}
	return resp, nil
}

func ptrString(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// describeAnomalies returns anomalies joined for error reporting.
func describeAnomalies(a []string) string { return strings.Join(a, "; ") }
