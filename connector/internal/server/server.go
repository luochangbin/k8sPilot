package server

import (
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	k8serrors "k8s.io/apimachinery/pkg/api/errors"

	"k8spilot/connector/internal/alerts"
	"k8spilot/connector/internal/config"
	"k8spilot/connector/internal/tools"
)

// Server exposes the connector's read-only tools over HTTP.
type Server struct {
	cfg    *config.Config
	tools  *tools.Tools
	alerts *alerts.Forwarder
}

// New builds a Server.
func New(cfg *config.Config, t *tools.Tools) *Server {
	return &Server{
		cfg:    cfg,
		tools:  t,
		alerts: alerts.NewForwarder(cfg.AgentURL, cfg.AlertForwardTimeout, t, cfg.AlertSnapshotEventLimit),
	}
}

// Handler returns the HTTP router.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("GET /capabilities", s.capabilities)
	mux.HandleFunc("POST /tools/inspect", s.handleInspect)
	mux.HandleFunc("POST /tools/relations", s.handleRelations)
	mux.HandleFunc("POST /tools/events", s.handleEvents)
	mux.HandleFunc("POST /tools/logs", s.handleLogs)
	mux.HandleFunc("POST /tools/query_metrics", s.handleQueryMetrics)
	mux.HandleFunc("POST /tools/query_logs", s.handleQueryLogs)
	mux.HandleFunc("POST /alerts", s.handleAlerts)
	return mux
}

// Run starts the HTTP server.
func (s *Server) Run() error {
	addr := s.cfg.Addr()
	srv := &http.Server{
		Addr:              addr,
		Handler:           s.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	return srv.ListenAndServe()
}

// toolRequest is the common body for tool endpoints.
type toolRequest struct {
	Target tools.Target `json:"target"`

	Limit      *int   `json:"limit,omitempty"`
	SinceHours *int   `json:"since_hours,omitempty"`

	Container string `json:"container,omitempty"`
	Previous  bool   `json:"previous,omitempty"`
	TailLines *int64 `json:"tail_lines,omitempty"`
	LimitKB   *int64 `json:"limit_kb,omitempty"`

	// query_metrics
	Metric       string `json:"metric,omitempty"`
	RangeMinutes *int   `json:"range_minutes,omitempty"`

	// query_logs
	Filter   string `json:"filter,omitempty"`
	MaxLines *int   `json:"max_lines,omitempty"`
}

// toolError is a machine-readable error body.
type toolError struct {
	Code  string `json:"code"`
	Error string `json:"error"`
}

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) capabilities(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"kubernetes.resources": true,
		"prometheus.metrics":   s.tools.PrometheusEnabled(),
		"loki.logs":            s.tools.LokiEnabled(),
	})
}

func (s *Server) handleInspect(w http.ResponseWriter, r *http.Request) {
	req, ok := s.decode(w, r)
	if !ok {
		return
	}
	resp, err := s.tools.Inspect(r.Context(), req.Target)
	if err != nil {
		s.writeToolError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleRelations(w http.ResponseWriter, r *http.Request) {
	req, ok := s.decode(w, r)
	if !ok {
		return
	}
	resp, err := s.tools.Relations(r.Context(), req.Target)
	if err != nil {
		s.writeToolError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	req, ok := s.decode(w, r)
	if !ok {
		return
	}
	params := tools.EventsParams{}
	if req.Limit != nil {
		params.Limit = *req.Limit
	}
	if req.SinceHours != nil {
		params.Since = time.Duration(*req.SinceHours) * time.Hour
	}
	resp, err := s.tools.Events(r.Context(), req.Target, params)
	if err != nil {
		s.writeToolError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	req, ok := s.decode(w, r)
	if !ok {
		return
	}
	params := tools.LogsParams{
		Container: req.Container,
		Previous:  req.Previous,
	}
	if req.TailLines != nil {
		params.TailLines = *req.TailLines
	}
	if req.LimitKB != nil {
		params.LimitBytes = *req.LimitKB * 1024
	}
	resp, err := s.tools.Logs(r.Context(), req.Target, params)
	if err != nil {
		s.writeToolError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleQueryMetrics(w http.ResponseWriter, r *http.Request) {
	req, ok := s.decode(w, r)
	if !ok {
		return
	}
	params := tools.MetricsParams{
		Kind:      req.Target.Kind,
		Namespace: req.Target.Namespace,
		Name:      req.Target.Name,
		Metric:    req.Metric,
	}
	if req.RangeMinutes != nil {
		params.RangeMinutes = *req.RangeMinutes
	}
	resp, err := s.tools.QueryMetrics(r.Context(), req.Target, params)
	if err != nil {
		s.writeToolError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleQueryLogs(w http.ResponseWriter, r *http.Request) {
	req, ok := s.decode(w, r)
	if !ok {
		return
	}
	params := tools.LokiLogsParams{
		Namespace:    req.Target.Namespace,
		Pod:          req.Target.Name,
		Filter:       req.Filter,
	}
	if req.RangeMinutes != nil {
		params.RangeMinutes = *req.RangeMinutes
	}
	if req.MaxLines != nil {
		params.MaxLines = *req.MaxLines
	}
	resp, err := s.tools.QueryLogs(r.Context(), req.Target, params)
	if err != nil {
		s.writeToolError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleAlerts(w http.ResponseWriter, r *http.Request) {
	if token := s.cfg.AlertWebhookToken; token != "" {
		want := "Bearer " + token
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte(want)) != 1 {
			writeJSON(w, http.StatusUnauthorized,
				toolError{Code: "unauthorized", Error: "invalid alert webhook token"})
			return
		}
	}
	var wh alerts.Webhook
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&wh); err != nil {
		writeJSON(w, http.StatusBadRequest, toolError{Code: "invalid_request", Error: err.Error()})
		return
	}
	if len(wh.Alerts) == 0 {
		writeJSON(w, http.StatusBadRequest,
			toolError{Code: "invalid_request", Error: "webhook contains no alerts"})
		return
	}
	if max := s.cfg.AlertMaxBatch; max > 0 && len(wh.Alerts) > max {
		writeJSON(w, http.StatusRequestEntityTooLarge, toolError{
			Code:  "too_many_alerts",
			Error: fmt.Sprintf("webhook carries %d alerts, limit is %d", len(wh.Alerts), max),
		})
		return
	}
	summary := s.alerts.Handle(r.Context(), wh)
	// Keep Alertmanager retrying: any agent failure yields a non-2xx status.
	writeJSON(w, summary.RetryableStatus(), summary)
}

func (s *Server) decode(w http.ResponseWriter, r *http.Request) (*toolRequest, bool) {
	var req toolRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, toolError{Code: "invalid_request", Error: err.Error()})
		return nil, false
	}
	if err := req.Target.Validate(); err != nil {
		code := "invalid_request"
		if _, ok := err.(tools.ErrNotSupported); ok {
			code = "not_supported"
		}
		writeJSON(w, http.StatusBadRequest, toolError{Code: code, Error: err.Error()})
		return nil, false
	}
	return &req, true
}

func (s *Server) writeToolError(w http.ResponseWriter, err error) {
	switch {
	case k8serrors.IsNotFound(err):
		writeJSON(w, http.StatusNotFound, toolError{Code: "not_found", Error: err.Error()})
	case k8serrors.IsForbidden(err):
		writeJSON(w, http.StatusForbidden, toolError{Code: "forbidden", Error: err.Error()})
	default:
		log.Printf("tool error: %v", err)
		writeJSON(w, http.StatusInternalServerError, toolError{Code: "internal", Error: err.Error()})
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
