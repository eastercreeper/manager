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
}
