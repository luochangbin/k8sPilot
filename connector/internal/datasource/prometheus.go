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

// TimeSeries is one labeled series with bounded points.
type TimeSeries struct {
	Labels map[string]string `json:"labels"`
	Points []Point           `json:"points"`
}

// Point is a (timestamp, value) sample.
type Point struct {
	Timestamp int64   `json:"timestamp"`
	Value     float64 `json:"value"`
}

// PrometheusClient talks to a Prometheus HTTP API v1.
type PrometheusClient struct {
	BaseURL string
	HTTP    *http.Client
}

func (c *PrometheusClient) httpClient() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return http.DefaultClient
}

// RangeQuery runs a PromQL range query and returns a bounded series list.
func (c *PrometheusClient) RangeQuery(ctx context.Context, query string,
	start, end time.Time, step time.Duration, maxSeries int) ([]TimeSeries, error) {
	u := fmt.Sprintf("%s/api/v1/query_range", stringsTrimRightSlash(c.BaseURL))
	q := url.Values{}
	q.Set("query", query)
	q.Set("start", strconv.FormatInt(start.Unix(), 10))
	q.Set("end", strconv.FormatInt(end.Unix(), 10))
	q.Set("step", fmt.Sprintf("%ds", int(step.Seconds())))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u+"?"+q.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.httpClient().Do(req)
	if err != nil {
		return nil, fmt.Errorf("prometheus request failed: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("prometheus HTTP %d: %s", resp.StatusCode, truncate(string(body), 300))
	}
	var payload struct {
		Status string `json:"status"`
		Data   struct {
			ResultType string `json:"resultType"`
			Result     []struct {
				Metric map[string]string `json:"metric"`
				Values [][2]any          `json:"values"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("prometheus parse error: %w", err)
	}
	if payload.Status == "error" {
		return nil, fmt.Errorf("prometheus query error")
	}
	series := make([]TimeSeries, 0, len(payload.Data.Result))
	for _, r := range payload.Data.Result {
		pts := make([]Point, 0, len(r.Values))
		for _, v := range r.Values {
			if len(v) != 2 {
				continue
			}
			ts, ok1 := v[0].(float64)
			val, ok2 := strconv.ParseFloat(fmt.Sprintf("%v", v[1]), 64)
			if !ok1 || ok2 != nil {
				continue
			}
			pts = append(pts, Point{Timestamp: int64(ts), Value: val})
		}
		series = append(series, TimeSeries{Labels: r.Metric, Points: pts})
		if len(series) >= maxSeries {
			break
		}
	}
	return series, nil
}
