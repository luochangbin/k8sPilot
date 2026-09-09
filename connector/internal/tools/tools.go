package tools

import (
	"k8s.io/client-go/kubernetes"

	"k8spilot/connector/internal/config"
	"k8spilot/connector/internal/datasource"
)

// Tools provides the read-only fact-gathering capabilities exposed to the
// Agent. It only fetches facts; it performs no reasoning.
type Tools struct {
	Kube kubernetes.Interface
	Cfg  *config.Config
	Prom *datasource.PrometheusClient // nil => prometheus.metrics capability disabled
	Loki *datasource.LokiClient       // nil => loki.logs capability disabled
}

// New builds a Tools instance bound to a kubernetes clientset.
func New(kube kubernetes.Interface, cfg *config.Config) *Tools {
	return &Tools{Kube: kube, Cfg: cfg}
}

// PrometheusEnabled reports whether the prometheus.metrics capability is on.
func (t *Tools) PrometheusEnabled() bool { return t.Prom != nil }

// LokiEnabled reports whether the loki.logs capability is on.
func (t *Tools) LokiEnabled() bool { return t.Loki != nil }
