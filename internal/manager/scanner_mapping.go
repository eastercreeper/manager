package manager

import (
	"context"
	"path/filepath"
	"sort"
	"strings"

	"seanime/internal/hook"
	"seanime/internal/hook_resolver"
	"seanime/internal/library/anime"
	"seanime/internal/library/scanner"
)

// installScannerMappingHook makes a saved Jellyfin-to-AniList override authoritative
// during Seanime's native scan. The scanner and hydrator still do all normal work.
func (s *Service) installScannerMappingHook() {
	hook.GlobalHookManager.OnScanMatchingStarted().BindFunc(func(resolver hook_resolver.Resolver) error {
		event, ok := resolver.(*scanner.ScanMatchingStartedEvent)
		if !ok {
			return resolver.Next()
		}

		var shows []JellyfinSeries
		if err := s.db.Where("ani_list_id > 0").Find(&shows).Error; err != nil {
			s.app.Logger.Error().Err(err).Msg("manager: load AniList overrides for scanner")
			return resolver.Next()
		}
		sort.Slice(shows, func(i, j int) bool { return len(shows[i].Path) > len(shows[j].Path) })

		mediaByID := make(map[int]bool, len(event.NormalizedMedia))
		for _, media := range event.NormalizedMedia {
			if media != nil {
				mediaByID[media.ID] = true
			}
		}
		needed := make(map[int]bool)
		for _, show := range shows {
			if show.AniListID > 0 && !mediaByID[show.AniListID] {
				needed[show.AniListID] = true
			}
		}
		for id := range needed {
			client := s.app.AnilistClientRef.Get()
			if client == nil {
				continue
			}
			complete, err := client.CompleteAnimeByID(context.Background(), &id)
			if err != nil || complete == nil || complete.GetMedia() == nil {
				s.app.Logger.Warn().Err(err).Int("anilistId", id).Msg("manager: could not fetch manually mapped AniList media")
				continue
			}
			event.NormalizedMedia = append(event.NormalizedMedia, anime.NewNormalizedMedia(complete.GetMedia().ToBaseAnime()))
			mediaByID[id] = true
		}

		for _, localFile := range event.LocalFiles {
			if localFile == nil || localFile.MediaId != 0 {
				continue
			}
			filePath, err := filepath.Abs(filepath.Clean(localFile.Path))
			if err != nil {
				continue
			}
			for _, show := range shows {
				if show.AniListID <= 0 || !mediaByID[show.AniListID] || strings.TrimSpace(show.Path) == "" {
					continue
				}
				root, err := filepath.Abs(filepath.Clean(show.Path))
				if err != nil {
					continue
				}
				rel, err := filepath.Rel(root, filePath)
				if err == nil && rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
					localFile.MediaId = show.AniListID
					break
				}
			}
		}
		return resolver.Next()
	})

	// Also apply overrides after Seanime merges locked/skipped files back into
	// the scan result. Those files bypass the matching phase above.
	hook.GlobalHookManager.OnScanCompleted().BindFunc(func(resolver hook_resolver.Resolver) error {
		event, ok := resolver.(*scanner.ScanCompletedEvent)
		if !ok {
			return resolver.Next()
		}
		var shows []JellyfinSeries
		if err := s.db.Where("ani_list_id > 0").Find(&shows).Error; err != nil {
			s.app.Logger.Error().Err(err).Msg("manager: load AniList overrides for completed scan")
			return resolver.Next()
		}
		applySeriesOverrides(event.LocalFiles, shows)
		// Persist Seanime's native matcher results back to Jellyfin records.
		// Existing mappings without a source are treated as manual for safety.
		for _, show := range shows {
			if show.MappingSource == "manual" || (show.AniListID > 0 && show.MappingSource == "") {
				continue
			}
			ids := matchedMediaIDsForPath(event.LocalFiles, show.Path)
			if len(ids) == 1 {
				for id := range ids {
					if id > 0 && id != show.AniListID {
						if err := s.db.Model(&JellyfinSeries{}).Where("jellyfin_id = ? AND (mapping_source = '' OR mapping_source = 'auto')", show.JellyfinID).Updates(map[string]any{"ani_list_id": id, "mapping_source": "auto"}).Error; err != nil {
							s.app.Logger.Error().Err(err).Str("jellyfinId", show.JellyfinID).Msg("manager: persist Seanime AniList match")
						}
					} else if id > 0 && show.MappingSource != "auto" {
						_ = s.db.Model(&JellyfinSeries{}).Where("jellyfin_id = ? AND (mapping_source = '' OR mapping_source = 'auto')", show.JellyfinID).Update("mapping_source", "auto").Error
					}
				}
			}
		}
		return resolver.Next()
	})
}

func applySeriesOverrides(localFiles []*anime.LocalFile, shows []JellyfinSeries) {
	sort.Slice(shows, func(i, j int) bool { return len(shows[i].Path) > len(shows[j].Path) })
	for _, localFile := range localFiles {
		if localFile == nil {
			continue
		}
		filePath, err := filepath.Abs(filepath.Clean(localFile.Path))
		if err != nil {
			continue
		}
		for _, show := range shows {
			if show.AniListID <= 0 || strings.TrimSpace(show.Path) == "" {
				continue
			}
			root, err := filepath.Abs(filepath.Clean(show.Path))
			if err != nil {
				continue
			}
			rel, err := filepath.Rel(root, filePath)
			if err == nil && rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
				localFile.MediaId = show.AniListID
				break
			}
		}
	}
}


// matchedMediaIDsForPath returns the distinct non-zero AniList IDs assigned by
// Seanime to files beneath one Jellyfin series directory.
func matchedMediaIDsForPath(localFiles []*anime.LocalFile, seriesPath string) map[int]bool {
	ids := make(map[int]bool)
	if strings.TrimSpace(seriesPath) == "" { return ids }
	root, err := filepath.Abs(filepath.Clean(seriesPath))
	if err != nil { return ids }
	for _, localFile := range localFiles {
		if localFile == nil || localFile.MediaId <= 0 { continue }
		filePath, err := filepath.Abs(filepath.Clean(localFile.Path))
		if err != nil { continue }
		rel, err := filepath.Rel(root, filePath)
		if err == nil && rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			ids[localFile.MediaId] = true
		}
	}
	return ids
}
