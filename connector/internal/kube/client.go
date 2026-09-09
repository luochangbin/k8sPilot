package kube

import (
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"k8spilot/connector/internal/config"
)

// Client wraps a kubernetes clientset.
type Client struct {
	Clientset kubernetes.Interface
}

// NewClient builds a kubernetes clientset from in-cluster config, or falls
// back to kubeconfig loading rules (useful for local development/tests).
func NewClient(cfg *config.Config) (*Client, error) {
	var restCfg *rest.Config
	var err error

	if cfg.Kubeconfig != "" {
		restCfg, err = clientcmd.BuildConfigFromFlags("", cfg.Kubeconfig)
	} else {
		restCfg, err = rest.InClusterConfig()
		if err != nil {
			restCfg, err = clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
				clientcmd.NewDefaultClientConfigLoadingRules(),
				&clientcmd.ConfigOverrides{},
			).ClientConfig()
		}
	}
	if err != nil {
		return nil, err
	}

	cs, err := kubernetes.NewForConfig(restCfg)
	if err != nil {
		return nil, err
	}
	return &Client{Clientset: cs}, nil
}
