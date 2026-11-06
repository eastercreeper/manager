# Manager extension API

The dashboard is served at `/api/v1/manager/ui`. All manager API endpoints use the same Seanime server and database.

Set `JELLYFIN_URL` and `JELLYFIN_API_KEY` for Jellyfin synchronization. Set `QBITTORRENT_URL`, `QBITTORRENT_USERNAME`, and `QBITTORRENT_PASSWORD` to enable approval handoff. Set `MANAGER_API_TOKEN` to require an additional Bearer token for manager routes.

Use `POST /api/v1/manager/jellyfin/sync` to synchronize Jellyfin IDs, episode numbers, and paths. This also configures Seanime's native scanner to scan the folders Jellyfin reports.

Create a pending release with `POST /api/v1/manager/approvals` using JSON fields `title`, `link`, `savePath`, optional `category`, `mediaId`, and `episodeNumber`. The release is not sent to qBittorrent until `POST /api/v1/manager/approvals/{id}/approve`. Reject with the corresponding `/reject` endpoint.

Run `POST /api/v1/manager/downloads/reconcile` to check manager-tagged torrents. Completed downloads trigger a Jellyfin library refresh, but are not automatically marked as verified episodes. Run Jellyfin sync and then `POST /api/v1/manager/episodes/{jellyfinId}/verify` to check file readability and English ASS/SSA subtitle tracks with FFprobe.

Mount media paths into this container at the same absolute paths returned by Jellyfin. Keep the Jellyfin media mount read-only for verification. Configure qBittorrent's save path to a path visible to both qBittorrent and this container.
