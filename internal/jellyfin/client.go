package jellyfin

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Client reads Jellyfin's physical-library state. Anime metadata and release
// identification remain the responsibility of the Seanime-derived core.
type Client struct {
	baseURL string
	apiKey string
	http *http.Client
}

func NewClient(baseURL, apiKey string, hc *http.Client) (*Client, error) {
	u, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil || u.Scheme == "" || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return nil, fmt.Errorf("invalid Jellyfin URL %q", baseURL)
	}
	if strings.TrimSpace(apiKey) == "" {
		return nil, fmt.Errorf("Jellyfin API key is required")
	}
	if hc == nil {
		hc = &http.Client{Timeout: 15 * time.Second}
	}
	return &Client{baseURL: strings.TrimRight(u.String(), "/"), apiKey: apiKey, http: hc}, nil
}

type SystemInfo struct {
	ServerName string `json:"ServerName"`
	Version string `json:"Version"`
	ID string `json:"Id"`
}

type Item struct {
	ID string `json:"Id"`
	Name string `json:"Name"`
	Type string `json:"Type"`
	SeriesID string `json:"SeriesId,omitempty"`
	Path string `json:"Path,omitempty"`
	IndexNumber *int `json:"IndexNumber,omitempty"`
	ParentIndexNumber *int `json:"ParentIndexNumber,omitempty"`
}

type ItemsResponse struct {
	Items []Item `json:"Items"`
	TotalRecordCount int `json:"TotalRecordCount"`
}

func (c *Client) get(ctx context.Context, path string, q url.Values, dst any) error {
	endpoint := c.baseURL + path
	if len(q) > 0 {
		endpoint += "?" + q.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil { return fmt.Errorf("create Jellyfin request: %w", err) }
	req.Header.Set("X-Emby-Token", c.apiKey)
	req.Header.Set("Accept", "application/json")
	resp, err := c.http.Do(req)
	if err != nil { return fmt.Errorf("request Jellyfin: %w", err) }
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("Jellyfin returned %s for %s", resp.Status, path)
	}
	if err := json.NewDecoder(resp.Body).Decode(dst); err != nil {
		return fmt.Errorf("decode Jellyfin response: %w", err)
	}
	return nil
}

func (c *Client) GetSystemInfo(ctx context.Context) (*SystemInfo, error) {
	var info SystemInfo
	if err := c.get(ctx, "/System/Info", nil, &info); err != nil { return nil, err }
	return &info, nil
}

// ListSeries fetches every Jellyfin series page. Jellyfin remains authoritative
// for the set of series and paths physically present in the library.
func (c *Client) ListSeries(ctx context.Context, pageSize int) ([]Item, error) {
	if pageSize <= 0 || pageSize > 500 { pageSize = 200 }
	var all []Item
	for start := 0; ; start += pageSize {
		q := url.Values{
			"Recursive": {"true"},
			"IncludeItemTypes": {"Series"},
			"Fields": {"Path"},
			"StartIndex": {fmt.Sprint(start)},
			"Limit": {fmt.Sprint(pageSize)},
		}
		var page ItemsResponse
		if err := c.get(ctx, "/Items", q, &page); err != nil { return nil, err }
		all = append(all, page.Items...)
		if len(page.Items) == 0 || start+len(page.Items) >= page.TotalRecordCount { return all, nil }
	}
}
