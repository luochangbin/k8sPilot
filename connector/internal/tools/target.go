package tools

import (
	"fmt"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Target identifies a Kubernetes resource in a cluster. UID is optional for
// relations/events/logs but must be present for inspect to guard against
// resource recreation.
type Target struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Namespace  string `json:"namespace"`
	Name       string `json:"name"`
	UID        string `json:"uid,omitempty"`
}

// Condition is a normalized resource condition.
type Condition struct {
	Type    string `json:"type"`
	Status  string `json:"status"`
	Reason  string `json:"reason,omitempty"`
	Message string `json:"message,omitempty"`
}

// ResourceRef is a normalized reference to a related resource.
type ResourceRef struct {
	Role       string `json:"role"`
	APIVersion string `json:"apiVersion,omitempty"`
	Kind       string `json:"kind"`
	Namespace  string `json:"namespace,omitempty"`
	Name       string `json:"name"`
}

// ErrNotSupported is returned when the requested Kind is outside the
// read-only surface the connector supports.
type ErrNotSupported struct{ Kind string }

func (e ErrNotSupported) Error() string {
	return fmt.Sprintf("kind %q is not supported", e.Kind)
}

// SupportedKinds lists resource kinds the connector can inspect. Phase 1 is
// read-only and focused on Pod diagnosis plus its relations.
var SupportedKinds = map[string]bool{
	"Pod":                   true,
	"Deployment":            true,
	"ReplicaSet":            true,
	"StatefulSet":           true,
	"DaemonSet":             true,
	"Service":               true,
	"Node":                  true,
	"PersistentVolumeClaim": true,
	"Namespace":             true,
}

// IsClusterScoped reports whether the kind is cluster-scoped (no namespace).
func IsClusterScoped(kind string) bool {
	switch kind {
	case "Node", "Namespace":
		return true
	}
	return false
}

// Validate checks the target is syntactically valid for a tool call.
func (t Target) Validate() error {
	if t.Name == "" {
		return fmt.Errorf("name is required")
	}
	if !SupportedKinds[t.Kind] {
		return ErrNotSupported{Kind: t.Kind}
	}
	if !IsClusterScoped(t.Kind) && t.Namespace == "" {
		return fmt.Errorf("namespace is required for kind %q", t.Kind)
	}
	return nil
}

// podCondition normalizes a corev1.PodCondition.
func podCondition(c corev1.PodCondition) Condition {
	return Condition{
		Type:    string(c.Type),
		Status:  string(c.Status),
		Reason:  c.Reason,
		Message: c.Message,
	}
}

// nodeCondition normalizes a corev1.NodeCondition.
func nodeCondition(c corev1.NodeCondition) Condition {
	return Condition{
		Type:    string(c.Type),
		Status:  string(c.Status),
		Reason:  c.Reason,
		Message: c.Message,
	}
}

func ownerRefs(obj metav1.Object) []ResourceRef {
	var refs []ResourceRef
	for _, or := range obj.GetOwnerReferences() {
		refs = append(refs, ResourceRef{
			Role:       "owner",
			APIVersion: or.APIVersion,
			Kind:       or.Kind,
			Namespace:  obj.GetNamespace(),
			Name:       or.Name,
		})
	}
	return refs
}
