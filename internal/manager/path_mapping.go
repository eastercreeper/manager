package manager

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// resolveJellyfinPath translates a path reported by Jellyfin into the
// corresponding path mounted inside the manager container. Configure mappings
// as semicolon-separated Jellyfin-prefix=container-prefix pairs, e.g.
// JELLYFIN_PATH_MAP=/SMB/JP-BEAN=/ivan. A path already visible in the
// container is always preferred.
func resolveJellyfinPath(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	clean := filepath.Clean(raw)
	if st, err := os.Stat(clean); err == nil && st.IsDir() {
		return clean
	}

	type pathMap struct{ from, to string }
	var mappings []pathMap
	for _, entry := range strings.Split(os.Getenv("JELLYFIN_PATH_MAP"), ";") {
		from, to, ok := strings.Cut(strings.TrimSpace(entry), "=")
		if !ok || strings.TrimSpace(from) == "" || strings.TrimSpace(to) == "" {
			continue
		}
		from, to = filepath.Clean(strings.TrimSpace(from)), filepath.Clean(strings.TrimSpace(to))
		if filepath.IsAbs(from) && filepath.IsAbs(to) {
			mappings = append(mappings, pathMap{from: from, to: to})
		}
	}
	sort.Slice(mappings, func(i, j int) bool { return len(mappings[i].from) > len(mappings[j].from) })
	for _, mapping := range mappings {
		rel, err := filepath.Rel(mapping.from, clean)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			continue
		}
		if rel == "." {
			return mapping.to
		}
		return filepath.Join(mapping.to, rel)
	}
	return clean
}
