package config

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// Config holds runtime configuration for the connector.
type Config struct {
	Port       int
	Kubeconfig string
	EventLimit int
	EventSince time.Duration
	LogTail    int64
	LogLimitKB int64
	LogTimeout time.Duration

	// Optional Phase 3 data sources. Empty disables the capability.
	PrometheusURL string
	LokiURL       string
	DataSourceTimeout time.Duration
	MetricsMaxSeries int
}

// Load reads configuration from environment variables with sensible defaults.
func Load() *Config {
	return &Config{
		Port:       getInt("CONNECTOR_PORT", 8080),
		Kubeconfig: os.Getenv("KUBECONFIG"),
		EventLimit: getInt("EVENT_LIMIT", 50),
		EventSince: time.Duration(getInt("EVENT_SINCE_HOURS", 1)) * time.Hour,
		LogTail:    int64(getInt("LOG_TAIL_LINES", 300)),
		LogLimitKB: int64(getInt("LOG_LIMIT_KB", 64)),
		LogTimeout: time.Duration(getInt("LOG_TIMEOUT_SECONDS", 30)) * time.Second,

		PrometheusURL:     os.Getenv("PROMETHEUS_URL"),
		LokiURL:           os.Getenv("LOKI_URL"),
		DataSourceTimeout: time.Duration(getInt("DATASOURCE_TIMEOUT_SECONDS", 10)) * time.Second,
		MetricsMaxSeries:  getInt("METRICS_MAX_SERIES", 5),
	}
}

// Addr returns the listen address.
func (c *Config) Addr() string {
	return fmt.Sprintf(":%d", c.Port)
}

func getInt(name string, def int) int {
	if v := os.Getenv(name); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
