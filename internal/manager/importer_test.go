package manager

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	qbmodel "seanime/internal/torrent_clients/qbittorrent/model"
)

func TestTorrentSourcePathUsesSavePathRelativeName(t *testing.T) {
	root := t.TempDir()
	contentRoot := filepath.Join(root, "Show")
	if err := os.MkdirAll(contentRoot, 0755); err != nil {
		t.Fatal(err)
	}
	want := filepath.Join(contentRoot, "episode.mkv")
	if err := os.WriteFile(want, []byte("video"), 0644); err != nil {
		t.Fatal(err)
	}
	torrent := &qbmodel.Torrent{
		SavePath: root, ContentPath: contentRoot,
	}
	got, err := torrentSourcePath(torrent, "Show/episode.mkv")
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("torrentSourcePath() = %q, want %q", got, want)
	}
}

func TestTorrentSourcePathRejectsTraversal(t *testing.T) {
	_, err := torrentSourcePath(&qbmodel.Torrent{SavePath: t.TempDir()}, "../outside.mkv")
	if err == nil || !strings.Contains(err.Error(), "unsafe") {
		t.Fatalf("expected unsafe-path error, got %v", err)
	}
}

func TestSafeMediaName(t *testing.T) {
	got := safeMediaName("../A:B? C")
	if strings.ContainsAny(got, "/\\:*?") || got == "" {
		t.Fatalf("unsafe sanitized name %q", got)
	}
}

func TestCopyFileWithoutOverwriteIsIdempotentForSameSize(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "source.mkv")
	destination := filepath.Join(dir, "destination.mkv")
	if err := os.WriteFile(source, []byte("same"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(destination, []byte("same"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := copyFileWithoutOverwrite(source, destination); err != nil {
		t.Fatalf("same-size retry should be idempotent: %v", err)
	}
	if err := os.WriteFile(destination, []byte("different-size"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := copyFileWithoutOverwrite(source, destination); err == nil {
		t.Fatal("expected existing different-size file to be protected")
	}
}
