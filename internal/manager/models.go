package manager

import "time"

// Manager records extend Seanime's own GORM database. Jellyfin IDs and paths
// are external-library references, not a second anime metadata catalog.
type JellyfinSeries struct {
 ID uint `gorm:"primaryKey" json:"id"`
 JellyfinID string `gorm:"uniqueIndex;size:128" json:"jellyfinId"`
 Name string `gorm:"size:512" json:"name"`
 Path string `gorm:"size:2048" json:"path"`
 AniListID int `json:"anilistId,omitempty"`
 LastSeenAt time.Time `json:"lastSeenAt"`
 CreatedAt time.Time `json:"createdAt"`
 UpdatedAt time.Time `json:"updatedAt"`
}
type JellyfinEpisode struct {
 ID uint `gorm:"primaryKey" json:"id"`
 JellyfinID string `gorm:"uniqueIndex;size:128" json:"jellyfinId"`
 SeriesID string `gorm:"index;size:128" json:"seriesId"`
 SeriesName string `gorm:"size:512" json:"seriesName"`
 Name string `gorm:"size:512" json:"name"`
 Path string `gorm:"size:2048" json:"path"`
 SeasonNumber int `json:"seasonNumber"`
 EpisodeNumber int `json:"episodeNumber"`
 LastSeenAt time.Time `json:"lastSeenAt"`
 CreatedAt time.Time `json:"createdAt"`
 UpdatedAt time.Time `json:"updatedAt"`
}
type ReleaseApproval struct {
 ID uint `gorm:"primaryKey" json:"id"`
 Title string `gorm:"size:512" json:"title"`
 Link string `gorm:"size:4096" json:"link"`
 MediaID int `json:"mediaId"`
 EpisodeNumber int `json:"episodeNumber"`
 SeasonNumber int `json:"seasonNumber"`
 JellyfinSeriesID string `gorm:"index;size:128" json:"jellyfinSeriesId,omitempty"`
 SavePath string `gorm:"size:2048" json:"savePath"`
 Category string `gorm:"size:128" json:"category"`
 Status string `gorm:"index;size:32" json:"status"`
 QBittorrentHash string `gorm:"size:128" json:"qbittorrentHash,omitempty"`
 ImportedAt *time.Time `json:"importedAt,omitempty"`
 ImportedPaths string `gorm:"type:text" json:"importedPaths,omitempty"`
 ImportError string `gorm:"type:text" json:"importError,omitempty"`
 DecisionAt *time.Time `json:"decisionAt,omitempty"`
 CreatedAt time.Time `json:"createdAt"`
 UpdatedAt time.Time `json:"updatedAt"`
}
type MediaVerification struct {
 ID uint `gorm:"primaryKey" json:"id"`
 JellyfinEpisodeID string `gorm:"uniqueIndex;size:128" json:"jellyfinEpisodeId"`
 Path string `gorm:"size:2048" json:"path"`
 Readable bool `json:"readable"`
 HasEnglishASS bool `json:"hasEnglishASS"`
 SubtitleTracks string `gorm:"type:text" json:"subtitleTracks"`
 Error string `gorm:"type:text" json:"error,omitempty"`
 CheckedAt time.Time `json:"checkedAt"`
 CreatedAt time.Time `json:"createdAt"`
 UpdatedAt time.Time `json:"updatedAt"`
}
