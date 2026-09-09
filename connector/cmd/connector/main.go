package main

import (
	"log"

	"k8spilot/connector/internal/config"
	"k8spilot/connector/internal/datasource"
	"k8spilot/connector/internal/kube"
	"k8spilot/connector/internal/server"
	"k8spilot/connector/internal/tools"
)

func main() {
	cfg := config.Load()

	client, err := kube.NewClient(cfg)
	if err != nil {
		log.Fatalf("failed to create kubernetes client: %v", err)
	}

	t := tools.New(client.Clientset, cfg)

	if cfg.PrometheusURL != "" {
		t.Prom = &datasource.PrometheusClient{BaseURL: cfg.PrometheusURL}
	}
	if cfg.LokiURL != "" {
		t.Loki = &datasource.LokiClient{BaseURL: cfg.LokiURL}
	}

	srv := server.New(cfg, t)

	log.Printf("ai-agent-connector listening on %s", cfg.Addr())
	if err := srv.Run(); err != nil {
		log.Fatal(err)
	}
}
