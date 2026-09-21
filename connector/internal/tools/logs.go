package tools

import (
	"context"
	"errors"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// LogsResponse is returned by the logs tool. Data is truncated to the
// configured byte/tail limits.
type LogsResponse struct {
	Namespace string `json:"namespace"`
	Pod       string `json:"pod"`
	Container string `json:"container,omitempty"`
	Previous  bool   `json:"previous"`
	TailLines int64  `json:"tail_lines"`
	Data      string `json:"data"`
	Truncated bool   `json:"truncated"`
	ByteCount int64  `json:"byte_count"`
}

// LogsParams controls the logs tool query.
type LogsParams struct {
	Container  string
	Previous   bool
	TailLines  int64
	LimitBytes int64
}

// Logs returns pod logs from the Kubernetes Pod Logs API (not Loki). Only Pod
// targets are supported in Phase 1.
func (t *Tools) Logs(ctx context.Context, target Target, params LogsParams) (*LogsResponse, error) {
	if target.Kind != "Pod" {
		return nil, fmt.Errorf("logs tool only supports Pod targets, got %q", target.Kind)
	}
	if params.TailLines <= 0 {
		params.TailLines = t.Cfg.LogTail
	}
	if params.LimitBytes <= 0 {
		params.LimitBytes = t.Cfg.LogLimitKB * 1024
	}

	pod, err := t.Kube.CoreV1().Pods(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}
	if target.UID != "" && string(pod.UID) != target.UID {
		return nil, ErrTargetRecreated{Kind: target.Kind, Namespace: target.Namespace, Name: target.Name}
	}
	container := params.Container
	if container == "" && len(pod.Spec.Containers) > 0 {
		container = pod.Spec.Containers[0].Name
	}

	logOptions := &corev1.PodLogOptions{
		Container:  container,
		Previous:   params.Previous,
		TailLines:  &params.TailLines,
		LimitBytes: &params.LimitBytes,
	}
	req := t.Kube.CoreV1().Pods(target.Namespace).GetLogs(target.Name, logOptions)

	ctx, cancel := context.WithTimeout(ctx, t.Cfg.LogTimeout)
	defer cancel()

	data, err := req.Do(ctx).Raw()
	if err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return nil, fmt.Errorf("pod log request timed out after %s", t.Cfg.LogTimeout)
		}
		return nil, err
	}

	resp := &LogsResponse{
		Namespace: target.Namespace,
		Pod:       target.Name,
		Container: container,
		Previous:  params.Previous,
		TailLines: params.TailLines,
		Data:      string(data),
		ByteCount: int64(len(data)),
		Truncated: int64(len(data)) >= params.LimitBytes,
	}
	return resp, nil
}
