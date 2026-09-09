package datasource

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// LogEntry is one Loki log line.
type LogEntry struct {
	Timestamp int64             `json:"timestamp"`
	Labels    map[string]string `json:"labels"`
	Line      string            `json:"line"`
}

// LokiClient talks to a Loki HTTP API.
type LokiClient struct {
	BaseURL string
	HTTP    *http.Client
}

func (c *LokiClient) httpClient() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return http.DefaultClient
}

// QueryRange runs a LogQL range query, returning a bounded list of entries.
func (c *LokiClient) QueryRange(ctx context.Context, query string,
	start, end time.Time, limit int) ([]LogEntry, error) {
	u := fmt.Sprintf("%s/loki/api/v1/query_range", stringsTrimRightSlash(c.BaseURL))
	q := url.Values{}
	q.Set("query", query)
	q.Set("start", strconv.FormatInt(start.UnixNano(), 10))
	q.Set("end", strconv.FormatInt(end.UnixNano(), 10))
	q.Set("limit", strconv.Itoa(limit))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u+"?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, fmt.Errorf("loki request failed: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("loki HTTP %d: %s", resp.StatusCode, truncate(string(body), 300))
	}
	var payload struct {
		Status string `json:"status"`
		Data   struct {
			ResultType string `json:"resultType"`
			Result     []struct {
				Stream map[string]string `json:"stream"`
				Values [][2]string       `json:"values"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("loki parse error: %w", err)
	}
	if payload.Status != "success" {
		return nil, fmt.Errorf("loki query status %q", payload.Status)
	}
	var entries []LogEntry
	for _, r := range payload.Data.Result {
		for _, v := range r.Values {
			if len(v) != 2 {
				continue
			}
			ns, err := strconv.ParseInt(v[0], 10, 64)
			if err != nil {
				continue
			}
			entries = append(entries, LogEntry{
				Timestamp: ns / 1e6, // ms
				Labels:    r.Stream,
				Line:      v[1],
			})
			if len(entries) >= limit {
				return entries, nil
			}
		}
	}
	return entries, nil
}
