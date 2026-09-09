package tools

import (
	"context"
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
)

// RelationsResponse is returned by the relations tool.
type RelationsResponse struct {
	Target    Target        `json:"target"`
	Relations []ResourceRef `json:"relations"`
}

// maxOwnerDepth bounds ownerReference chain traversal.
const maxOwnerDepth = 2

// Relations returns the resources related to the target: owner chain, and for
// Pods also node, matching services, PVCs and service account. It does not
// perform full graph scans.
func (t *Tools) Relations(ctx context.Context, target Target) (*RelationsResponse, error) {
	resp := &RelationsResponse{Target: target, Relations: []ResourceRef{}}

	switch target.Kind {
	case "Pod":
		return t.relationsPod(ctx, target, resp)
	case "Deployment", "ReplicaSet", "StatefulSet", "DaemonSet":
		return t.relationsWorkload(ctx, target, resp)
	case "Node", "Service", "PersistentVolumeClaim", "Namespace":
		return resp, nil
	default:
		return nil, ErrNotSupported{Kind: target.Kind}
	}
}

func (t *Tools) relationsPod(ctx context.Context, target Target, resp *RelationsResponse) (*RelationsResponse, error) {
	pod, err := t.Kube.CoreV1().Pods(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
	if err != nil {
		return nil, err
	}

	owners := t.ownerChain(ctx, target.Namespace, ownerRefs(pod))
	resp.Relations = append(resp.Relations, owners...)

	if pod.Spec.NodeName != "" {
		resp.Relations = append(resp.Relations, ResourceRef{Role: "scheduler", Kind: "Node", Name: pod.Spec.NodeName})
	}
	if pod.Spec.ServiceAccountName != "" {
		resp.Relations = append(resp.Relations, ResourceRef{Role: "service_account", Kind: "ServiceAccount", Namespace: pod.Namespace, Name: pod.Spec.ServiceAccountName})
	}

	// PVCs referenced by the pod's volumes.
	for _, v := range pod.Spec.Volumes {
		if v.PersistentVolumeClaim != nil {
			resp.Relations = append(resp.Relations, ResourceRef{
				Role: "volume", Kind: "PersistentVolumeClaim",
				Namespace: pod.Namespace, Name: v.PersistentVolumeClaim.ClaimName,
			})
		}
	}

	// Services in the same namespace whose selector matches the pod's labels.
	svcs, err := t.Kube.CoreV1().Services(pod.Namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, err
	}
	for i := range svcs.Items {
		svc := &svcs.Items[i]
		if len(svc.Spec.Selector) == 0 {
			continue
		}
		sel, err := metav1.LabelSelectorAsSelector(&metav1.LabelSelector{MatchLabels: svc.Spec.Selector})
		if err != nil {
			continue
		}
		if sel.Matches(labels.Set(pod.Labels)) {
			resp.Relations = append(resp.Relations, ResourceRef{Role: "service", Kind: "Service", Namespace: pod.Namespace, Name: svc.Name})
		}
	}
	return resp, nil
}

func (t *Tools) relationsWorkload(ctx context.Context, target Target, resp *RelationsResponse) (*RelationsResponse, error) {
	uid, err := t.workloadUID(ctx, target)
	if err != nil {
		return nil, err
	}
	pods, err := t.Kube.CoreV1().Pods(target.Namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, err
	}
	for i := range pods.Items {
		pod := &pods.Items[i]
		for _, or := range pod.OwnerReferences {
			if string(or.UID) == string(uid) {
				resp.Relations = append(resp.Relations, ResourceRef{
					Role: "child", Kind: "Pod", Namespace: pod.Namespace, Name: pod.Name, APIVersion: "v1",
				})
				break
			}
		}
	}
	return resp, nil
}

func (t *Tools) workloadUID(ctx context.Context, target Target) (types.UID, error) {
	switch target.Kind {
	case "Deployment":
		d, err := t.Kube.AppsV1().Deployments(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
		if err != nil {
			return "", err
		}
		return d.UID, nil
	case "ReplicaSet":
		rs, err := t.Kube.AppsV1().ReplicaSets(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
		if err != nil {
			return "", err
		}
		return rs.UID, nil
	case "StatefulSet":
		s, err := t.Kube.AppsV1().StatefulSets(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
		if err != nil {
			return "", err
		}
		return s.UID, nil
	case "DaemonSet":
		ds, err := t.Kube.AppsV1().DaemonSets(target.Namespace).Get(ctx, target.Name, metav1.GetOptions{})
		if err != nil {
			return "", err
		}
		return ds.UID, nil
	}
	return "", ErrNotSupported{Kind: target.Kind}
}

// ownerChain follows ownerReferences up the tree (bounded by maxOwnerDepth),
// returning the chain of owners excluding the starting refs themselves.
func (t *Tools) ownerChain(ctx context.Context, namespace string, refs []ResourceRef) []ResourceRef {
	var out []ResourceRef
	queue := append([]ResourceRef{}, refs...)
	seen := map[string]bool{}
	for depth := 0; depth < maxOwnerDepth && len(queue) > 0; depth++ {
		var next []ResourceRef
		for _, ref := range queue {
			key := fmt.Sprintf("%s/%s/%s", ref.Kind, ref.Namespace, ref.Name)
			if seen[key] {
				continue
			}
			seen[key] = true
			out = append(out, ref)
			owners, err := t.ownersOf(ctx, ref)
			if err != nil {
				continue
			}
			next = append(next, owners...)
		}
		queue = next
	}
	return out
}

func (t *Tools) ownersOf(ctx context.Context, ref ResourceRef) ([]ResourceRef, error) {
	switch ref.Kind {
	case "ReplicaSet":
		rs, err := t.Kube.AppsV1().ReplicaSets(ref.Namespace).Get(ctx, ref.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		return ownerRefs(rs), nil
	case "Deployment":
		d, err := t.Kube.AppsV1().Deployments(ref.Namespace).Get(ctx, ref.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		return ownerRefs(d), nil
	case "StatefulSet":
		s, err := t.Kube.AppsV1().StatefulSets(ref.Namespace).Get(ctx, ref.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		return ownerRefs(s), nil
	case "DaemonSet":
		ds, err := t.Kube.AppsV1().DaemonSets(ref.Namespace).Get(ctx, ref.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		return ownerRefs(ds), nil
	case "Pod":
		p, err := t.Kube.CoreV1().Pods(ref.Namespace).Get(ctx, ref.Name, metav1.GetOptions{})
		if err != nil {
			return nil, err
		}
		return ownerRefs(p), nil
	}
	return nil, nil
}

// compile-time interface assertions for the typed clients used.
var (
	_ = (*appsv1.Deployment)(nil)
	_ = (*corev1.Pod)(nil)
)
