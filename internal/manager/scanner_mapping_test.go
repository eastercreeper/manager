package manager

import (
	"path/filepath"
	"testing"

	"seanime/internal/library/anime"
)

func TestApplySeriesOverridesUsesMostSpecificPath(t *testing.T) {
	root := t.TempDir()
	showRoot := filepath.Join(root, "Show")
	seasonRoot := filepath.Join(showRoot, "Season 02")
	filePath := filepath.Join(seasonRoot, "episode.mkv")
	local := &anime.LocalFile{Path: filePath, MediaId: 99}
	shows := []JellyfinSeries{
		{Path: root, AniListID: 1},
		{Path: showRoot, AniListID: 2},
	}
	applySeriesOverrides([]*anime.LocalFile{local}, shows)
	if local.MediaId != 2 {
		t.Fatalf("mapped media ID = %d, want most-specific mapping 2", local.MediaId)
	}
}

func TestApplySeriesOverridesDoesNotMatchPathPrefixCollision(t *testing.T) {
	root := t.TempDir()
	local := &anime.LocalFile{Path: root + "-other/episode.mkv"}
	applySeriesOverrides([]*anime.LocalFile{local}, []JellyfinSeries{{Path: root, AniListID: 42}})
	if local.MediaId != 0 {
		t.Fatalf("path-prefix collision incorrectly mapped media ID %d", local.MediaId)
	}
}

func TestMatchedMediaIDsForPathCollectsUniqueMatches(t *testing.T) {
	root := t.TempDir()
	season := filepath.Join(root, "Season 01")
	one := &anime.LocalFile{Path: filepath.Join(season, "one.mkv"), MediaId: 42}
	two := &anime.LocalFile{Path: filepath.Join(season, "two.mkv"), MediaId: 42}
	outside := &anime.LocalFile{Path: root + "-other/three.mkv", MediaId: 99}
	ids := matchedMediaIDsForPath([]*anime.LocalFile{one, two, outside}, root)
	if len(ids) != 1 || !ids[42] {
		t.Fatalf("matched IDs = %v, want only 42", ids)
	}
}

func TestMatchedMediaIDsForPathKeepsAmbiguousMatchesVisible(t *testing.T) {
	root := t.TempDir()
	files := []*anime.LocalFile{
		{Path: filepath.Join(root, "one.mkv"), MediaId: 42},
		{Path: filepath.Join(root, "two.mkv"), MediaId: 84},
	}
	ids := matchedMediaIDsForPath(files, root)
	if len(ids) != 2 {
		t.Fatalf("matched IDs = %v, want both ambiguous IDs", ids)
	}
}
