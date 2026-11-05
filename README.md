# Anime Manager

Anime Manager is a self-hosted FastAPI application for tracking anime in Jellyfin, finding releases from Nyaa.si and TsukiHime, manually approving torrents in qBittorrent, and importing only media that contains English ASS/SSA subtitles.

## Implemented

- Jellyfin library discovery and restart-safe synchronization.
- The My Library page can manually synchronize Jellyfin, persist series/provider/alternate-title metadata, and reconcile Jellyfin episode records with physical media found under the mounted Shows path.
- Automatic tracking of discovered series plus manual tracking with custom folder names.
- Alternate/provider metadata storage, episode records, duplicate-safe release storage, and scan history in SQLite.
- Independent Nyaa RSS and schema-discovered TsukiHime adapters.
- TsukiHime requests are made only against a GET operation and query parameter discovered from its published OpenAPI document; if the schema cannot be reached or exposes no compatible operation, the adapter fails closed.
- Release score and explanation based on multi-audio/Japanese audio, English/Japanese subtitle hints, 1080p, and seeders.
- Explicit approval before qBittorrent submission; no automatic torrent downloads.
- qBittorrent progress monitoring and delayed visibility reconciliation keyed by info hash, with duplicate-submission protection and restart-safe download records.
- ffprobe verification of actual streams. English ASS/SSA is mandatory; SRT is never accepted as a substitute.
- Safe path mapping, path traversal protection, duplicate-safe imports, Jellyfin refresh, interrupted-import recovery, and a manual-review state for failed verification.
- Search page for Jellyfin/tracked metadata plus Nyaa/TsukiHime release comparison and explicit approval/rejection.
- Optional password authentication with signed 24-hour sessions and double-submit CSRF protection for state-changing browser forms. Set `ADMIN_PASSWORD` and `SESSION_SECRET` in the Compose environment.
- Season-aware single- and multi-episode importing that preserves the source container and leaves source files untouched.
- Dashboard, library, search, episodes, downloads, history, and settings pages.

## TrueNAS SCALE deployment

1. Copy this repository to `/mnt/.ix-apps/app_mounts/anime-manager`.
2. Merge `compose-service.yml` into the existing Compose project. The Anime Manager service intentionally uses `network_mode: service:tailscale`; add `8787:8787/tcp` to the existing `tailscale.ports` list and do not add a ports mapping to this service.
3. Adjust the two host-side volume paths if your existing mounts differ:
   - Jellyfin Shows -> `/ivan/Shows`
   - qBittorrent downloads -> `/downloads`
4. Set `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `QBITTORRENT_USERNAME`, and `QBITTORRENT_PASSWORD` in the Compose environment/secrets configuration. An `.env` file is not required.
5. Start the stack and open `http://<TRUENAS-LAN-IP>:8787`.

The Compose service uses the named volume `anime_manager_data` at `/data`; Docker creates it automatically. `/downloads` and `/ivan/Shows` are the existing host mounts. The image includes `ffprobe`, runs as an unprivileged user, listens on `0.0.0.0:8787`, and uses `tini` for graceful shutdown.

## Path mapping

Jellyfin may report `/ivan/Shows` or `/mnt/Jellyfin/JellyfinData/JP-BEAN/Shows`. The default mapping resolves the latter to the mounted `/ivan/Shows` path. Override `JELLYFIN_PATH_PREFIXES` with comma-separated `source=container` mappings when required.

## Operational notes

- The first synchronization can be started from the Dashboard's **Sync now** button.
- qBittorrent credentials are never rendered by the settings page.
- `ADMIN_PASSWORD` is intentionally required to enable browser authentication; without it the app remains suitable only for a trusted private network.
- Releases are only candidates until manually approved.
- Completed files without English ASS/SSA are flagged for review and are not imported.
- The Search page uses Jellyfin/tracked metadata; it is not a general-purpose external metadata catalog.
- Jellyfin-reported host paths are translated through `JELLYFIN_PATH_PREFIXES`; both `/ivan/Shows` and the default `/mnt/Jellyfin/JellyfinData/JP-BEAN/Shows` mapping are supported.
- The published TsukiHime host was DNS-unavailable during this audit, so live connectivity could not be verified here. The adapter is schema-driven and covered by mocked OpenAPI/API tests; it does not invent a fixed search endpoint.
- TsukiHime is isolated behind its adapter and fails closed if its published OpenAPI schema has no compatible search/release endpoint.
- Restrict the dashboard to a trusted network or place it behind an authenticated reverse proxy.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pytest -q
uvicorn app.main:app --host 0.0.0.0 --port 8787
```
