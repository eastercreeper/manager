package jellyfin

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestSystemInfoAuthenticatesWithEmbyToken(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/System/Info" { t.Errorf("path = %q", r.URL.Path) }
		if r.Header.Get("X-Emby-Token") != "secret" { t.Errorf("missing API token") }
		_ = json.NewEncoder(w).Encode(SystemInfo{ServerName: "Jellyfin", Version: "10.10.7"})
	}))
	defer s.Close()
	c, err := NewClient(s.URL, "secret", s.Client())
	if err != nil { t.Fatal(err) }
	info, err := c.GetSystemInfo(context.Background())
	if err != nil { t.Fatal(err) }
	if info.ServerName != "Jellyfin" || info.Version != "10.10.7" { t.Fatalf("unexpected info: %#v", info) }
}

func TestNewClientRejectsInvalidSettings(t *testing.T) {
	if _, err := NewClient("not a URL", "key", nil); err == nil { t.Fatal("expected URL error") }
	if _, err := NewClient("http://localhost:8096", "", nil); err == nil { t.Fatal("expected API-key error") }
}

func TestListSeriesPaginates(t *testing.T) {
	requests := 0
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.URL.Query().Get("IncludeItemTypes") != "Series" { t.Errorf("missing series filter") }
		if r.URL.Query().Get("StartIndex") == "0" {
			_ = json.NewEncoder(w).Encode(ItemsResponse{Items: []Item{{ID:"one", Name:"One"}}, TotalRecordCount:2})
			return
		}
		_ = json.NewEncoder(w).Encode(ItemsResponse{Items: []Item{{ID:"two", Name:"Two"}}, TotalRecordCount:2})
	}))
	defer s.Close()
	c, err := NewClient(s.URL, "key", s.Client())
	if err != nil { t.Fatal(err) }
	items, err := c.ListSeries(context.Background(), 1)
	if err != nil { t.Fatal(err) }
	if len(items) != 2 || requests != 2 { t.Fatalf("got %d items across %d requests", len(items), requests) }
}
