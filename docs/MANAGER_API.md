# Manager extension API

The dashboard is served at `/api/v1/manager/ui`. All manager API endpoints use the same Seanime server and database.

Set `JELLYFIN_URL` and `JELLYFIN_API_KEY` for Jellyfin synchronization. Set `QBITTORRENT_URL`, `QBITTORRENT_USERNAME`, and `QBITTORRENT_PASSWORD` to enable approval handoff. Set `MANAGER_API_TOKEN` to require an additional Bearer token for manager routes.

Use `POST /api/v1/manager/jellyfin/sync` to synchronize Jellyfin series IDs, episode numbers, and physical paths. This also configures Seanime's native scanner to scan the folders Jellyfin reports. A saved AniList ID override is now applied to files under that series path during Seanime's matching phase; the mapped AniList media is fetched into the native scanner's metadata set when necessary.

Create a pending release with `POST /api/v1/manager/approvals` using JSON fields `title`, `link`, `savePath`, optional `category`, `mediaId`, `episodeNumber`, `seasonNumber`, and `jellyfinSeriesId`. Select the Jellyfin series explicitly when the same AniList ID maps to multiple Jellyfin series/seasons. The release is not sent to qBittorrent until `POST /api/v1/manager/approvals/{id}/approve`. Reject with the corresponding `/reject` endpoint.

Run `POST /api/v1/manager/downloads/reconcile` to check manager-tagged torrents. Completed video files are copied into the selected Jellyfin series folder and season directory (for example, `Season 01/Series Name - S01E03.mkv`). The source download is intentionally retained so qBittorrent can continue seeding. Existing destination files are never overwritten. Import errors are recorded on the approval and can be retried by running reconciliation again.

After import, the manager requests a Jellyfin library refresh. Run Jellyfin sync and then `POST /api/v1/manager/episodes/{jellyfinId}/verify` to check file readability and English ASS/SSA subtitle tracks with FFprobe.

Mount Jellyfin media paths into this container at the same absolute paths returned by Jellyfin. The media mount must be writable for imports (read-only is sufficient only for verification). Configure qBittorrent's save path so the completed source files are visible at the same absolute path inside this container. Ensure the container user can read the download and write to the Jellyfin library.
