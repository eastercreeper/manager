package manager

import (
	"crypto/sha256"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"seanime/internal/library/anime"
	qbittorrent "seanime/internal/torrent_clients/qbittorrent"
	qbmodel "seanime/internal/torrent_clients/qbittorrent/model"
)

var subtitleExtensions = map[string]bool{".ass": true, ".ssa": true, ".srt": true, ".vtt": true, ".sub": true, ".idx": true}

var videoExtensions = map[string]bool{
	".mkv": true, ".mp4": true, ".m4v": true, ".avi": true,
	".mov": true, ".ts": true, ".webm": true, ".wmv": true,
	".mpg": true, ".mpeg": true,
}

// importCompletedTorrent copies completed video files into Jellyfin's series/season
// layout. It deliberately leaves the qBittorrent source intact so seeding is not broken.
func (s *Service) importCompletedTorrent(approval *ReleaseApproval, torrent *qbmodel.Torrent, client *qbittorrent.Client) ([]string, error) {
	if approval == nil || torrent == nil || client == nil {
		return nil, fmt.Errorf("approval, torrent, and qBittorrent client are required")
	}
	if torrent.Progress < 0.9999 {
		return nil, fmt.Errorf("torrent is not complete")
	}

	var series JellyfinSeries
	if approval.JellyfinSeriesID != "" {
		if err := s.db.Where("jellyfin_id = ?", approval.JellyfinSeriesID).First(&series).Error; err != nil {
			return nil, fmt.Errorf("selected Jellyfin series was not found; sync the library again")
		}
	} else if approval.MediaID > 0 {
		var matches []JellyfinSeries
		if err := s.db.Where("ani_list_id = ?", approval.MediaID).Find(&matches).Error; err != nil {
			return nil, err
		}
		if len(matches) == 0 {
			return nil, fmt.Errorf("AniList ID %d is not mapped to a Jellyfin series", approval.MediaID)
		}
		if len(matches) > 1 {
			return nil, fmt.Errorf("AniList ID %d maps to multiple Jellyfin series; select a Jellyfin series explicitly", approval.MediaID)
		}
		series = matches[0]
	} else {
		return nil, fmt.Errorf("select a Jellyfin series or provide a mapped AniList ID before importing")
	}
	if !filepath.IsAbs(series.Path) {
		return nil, fmt.Errorf("Jellyfin series %q has no absolute filesystem path", series.Name)
	}
	seriesRoot := filepath.Clean(series.Path)
	if info, err := os.Stat(seriesRoot); err != nil || !info.IsDir() {
		return nil, fmt.Errorf("Jellyfin series path is not accessible from this container: %s", seriesRoot)
	}

	contents, err := client.Torrent.GetContents(torrent.Hash)
	if err != nil {
		return nil, fmt.Errorf("list torrent files: %w", err)
	}
	if len(contents) == 0 && torrent.ContentPath != "" {
		if info, statErr := os.Stat(torrent.ContentPath); statErr == nil && info.Mode().IsRegular() {
			contents = append(contents, &qbmodel.TorrentContent{Name: filepath.Base(torrent.ContentPath), Progress: torrent.Progress})
		}
	}
	if len(contents) == 0 {
		return nil, fmt.Errorf("qBittorrent returned no files for this torrent")
	}

	videoCount := 0
	for _, content := range contents {
		if content != nil && content.Progress >= 0.9999 && videoExtensions[strings.ToLower(filepath.Ext(content.Name))] {
			videoCount++
		}
	}
	imported := make([]string, 0, len(contents))
	importedVideos := make([]struct{ source, destination string }, 0, videoCount)
	for _, content := range contents {
		if content == nil || content.Progress < 0.9999 || !videoExtensions[strings.ToLower(filepath.Ext(content.Name))] {
			continue
		}
		source, err := torrentSourcePath(torrent, content.Name)
		if err != nil {
			return imported, err
		}
		if info, err := os.Stat(source); err != nil || !info.Mode().IsRegular() {
			return imported, fmt.Errorf("completed torrent file is not accessible: %s", source)
		}

		parsed := anime.NewLocalFileS(source, []string{torrent.SavePath})
		episode := 0
		season := approval.SeasonNumber
		if parsed != nil && parsed.ParsedData != nil {
			if n, err := strconv.Atoi(strings.TrimSpace(parsed.ParsedData.Episode)); err == nil && n > 0 {
				episode = n
			}
			if n, err := strconv.Atoi(strings.TrimSpace(parsed.ParsedData.Season)); err == nil && n >= 0 {
				season = n
			}
		}
		if episode <= 0 && videoCount == 1 {
			episode = approval.EpisodeNumber
		}
		if episode <= 0 {
			return imported, fmt.Errorf("could not determine episode number for %s; provide episodeNumber or use a release with parseable episode numbering", filepath.Base(source))
		}
		if season < 0 {
			season = 1
		}

		seasonDir, err := s.jellyfinSeasonDirectory(series, season)
		if err != nil {
			return imported, err
		}
		if err := os.MkdirAll(seasonDir, 0755); err != nil {
			return imported, fmt.Errorf("create Jellyfin season directory: %w", err)
		}
		ext := strings.ToLower(filepath.Ext(source))
		name := fmt.Sprintf("%s - S%02dE%02d%s", safeMediaName(series.Name), season, episode, ext)
		destination := filepath.Join(seasonDir, name)
		if err := copyFileWithoutOverwrite(source, destination); err != nil {
			return imported, err
		}
		imported = append(imported, destination)
		importedVideos = append(importedVideos, struct{ source, destination string }{source: source, destination: destination})
	}

	// Copy matching external subtitle sidecars alongside each imported video.
	// Keep language suffixes such as ".en.ass" so Jellyfin can identify the track.
	for _, content := range contents {
		if content == nil || content.Progress < 0.9999 || !subtitleExtensions[strings.ToLower(filepath.Ext(content.Name))] {
			continue
		}
		subtitleSource, err := torrentSourcePath(torrent, content.Name)
		if err != nil {
			return imported, err
		}
		for _, video := range importedVideos {
			if filepath.Dir(subtitleSource) != filepath.Dir(video.source) {
				continue
			}
			videoStem := strings.TrimSuffix(filepath.Base(video.source), filepath.Ext(video.source))
			subtitleBase := filepath.Base(subtitleSource)
			if subtitleBase != videoStem && !strings.HasPrefix(subtitleBase, videoStem+".") {
				continue
			}
			suffix := strings.TrimPrefix(subtitleBase, videoStem)
			if suffix == "" {
				suffix = filepath.Ext(subtitleSource)
			}
			destination := strings.TrimSuffix(video.destination, filepath.Ext(video.destination)) + suffix
			if err := copyFileWithoutOverwrite(subtitleSource, destination); err != nil {
				return imported, err
			}
			imported = append(imported, destination)
			break
		}
	}
	if len(imported) == 0 {
		return nil, fmt.Errorf("torrent has no completed video files to import")
	}
	return imported, nil
}

func torrentSourcePath(torrent *qbmodel.Torrent, relativeName string) (string, error) {
	name := filepath.Clean(filepath.FromSlash(relativeName))
	if filepath.IsAbs(name) || name == "." || name == ".." || strings.HasPrefix(name, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("unsafe qBittorrent content path %q", relativeName)
	}
	bases := []string{}
	if torrent.SavePath != "" {
		bases = append(bases, torrent.SavePath)
	}
	if torrent.ContentPath != "" {
		if info, err := os.Stat(torrent.ContentPath); err == nil && info.Mode().IsRegular() {
			return torrent.ContentPath, nil
		}
		bases = append(bases, torrent.ContentPath)
	}
	for _, base := range bases {
		candidate := filepath.Join(base, name)
		rel, err := filepath.Rel(filepath.Clean(base), filepath.Clean(candidate))
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			continue
		}
		if info, err := os.Stat(candidate); err == nil && info.Mode().IsRegular() {
			return candidate, nil
		}
	}
	if len(bases) > 0 {
		return filepath.Join(bases[0], name), nil
	}
	return "", fmt.Errorf("qBittorrent did not provide a usable content path")
}

func (s *Service) jellyfinSeasonDirectory(series JellyfinSeries, season int) (string, error) {
	var existing JellyfinEpisode
	err := s.db.Where("series_id = ? AND season_number = ? AND path <> ''", series.JellyfinID, season).
		Order("episode_number asc").First(&existing).Error
	if err == nil {
		dir := filepath.Dir(existing.Path)
		rel, relErr := filepath.Rel(filepath.Clean(series.Path), filepath.Clean(dir))
		if relErr == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return dir, nil
		}
	}
	if season == 0 {
		return filepath.Join(series.Path, "Specials"), nil
	}
	return filepath.Join(series.Path, fmt.Sprintf("Season %02d", season)), nil
}

func safeMediaName(name string) string {
	name = strings.TrimSpace(name)
	replacer := strings.NewReplacer("/", " ", "\\", " ", ":", " -", "*", "", "?", "", "\"", "", "<", "", ">", "", "|", "")
	name = strings.Join(strings.Fields(replacer.Replace(name)), " ")
	if name == "" || name == "." || name == ".." {
		return "Anime"
	}
	return name
}

func copyFileWithoutOverwrite(source, destination string) error {
	if info, err := os.Stat(destination); err == nil {
		sourceInfo, sourceErr := os.Stat(source)
		if sourceErr == nil && info.Size() == sourceInfo.Size() {
			same, compareErr := sameFileContent(source, destination)
			if compareErr == nil && same {
				return nil
			}
		}
		return fmt.Errorf("refusing to overwrite existing Jellyfin file: %s", destination)
	} else if !os.IsNotExist(err) {
		return err
	}
	in, err := os.Open(source)
	if err != nil {
		return fmt.Errorf("open completed download: %w", err)
	}
	defer in.Close()
	tmp, err := os.CreateTemp(filepath.Dir(destination), ".anime-manager-*.partial")
	if err != nil {
		return fmt.Errorf("create temporary import file: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := io.Copy(tmp, in); err != nil {
		tmp.Close()
		return fmt.Errorf("copy episode into Jellyfin library: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0644); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, destination); err != nil {
		return fmt.Errorf("finalize Jellyfin import: %w", err)
	}
	return nil
}

func markImported(a *ReleaseApproval, paths []string) {
	now := time.Now().UTC()
	a.Status = "imported"
	a.ImportedAt = &now
	a.ImportedPaths = strings.Join(paths, "\n")
	a.ImportError = ""
}

func sameFileContent(a, b string) (bool, error) {
	hash := func(path string) ([32]byte, error) {
		var sum [32]byte
		file, err := os.Open(path)
		if err != nil {
			return sum, err
		}
		defer file.Close()
		h := sha256.New()
		if _, err := io.Copy(h, file); err != nil {
			return sum, err
		}
		copy(sum[:], h.Sum(nil))
		return sum, nil
	}
	left, err := hash(a)
	if err != nil {
		return false, err
	}
	right, err := hash(b)
	if err != nil {
		return false, err
	}
	return left == right, nil
}
