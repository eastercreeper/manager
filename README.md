# Anime Manager — MVP scaffold

This is a deliberately conservative first milestone:
- Manually add anime titles.
- Scan Nyaa's RSS feed every 30 minutes (configurable) for 1080p English-translated anime-category releases.
- Place matches in an approval queue.
- Only an explicit Approve click calls qBittorrent's Web API.
- Store tracked titles and release history in SQLite.

## Important limitations
- TsukiHime is not yet wired in. Its wiki links to API docs at https://api.tsukihime.org/v1/docs, but the schema needs to be inspected before implementing requests; no endpoints are guessed.
- This MVP does not rename, hardlink, copy, or import completed files into Jellyfin. It only adds approved torrents to qBittorrent.
- Nyaa RSS field formats can vary. Test with a single tracked title and inspect the approval queue before relying on it.
- Add authentication/reverse-proxy protection before exposing the dashboard beyond a trusted network.
- Do not expose qBittorrent credentials in chat or commit them to source control.

## Build
Extract this directory to:
`/mnt/.ix-apps/app_mounts/anime-manager`

From a shell on TrueNAS:
`cd /mnt/.ix-apps/app_mounts/anime-manager`
`docker build -t anime-manager:0.1 .`

Merge `compose-service.yml` into the existing Compose project. Add `8787:8787/tcp` to the existing `tailscale.ports` list. Keep the existing qBittorrent, Qui, and Tailscale definitions unchanged otherwise.

Because anime-manager uses `network_mode: service:tailscale`, it shares the same network namespace as qBittorrent and Qui. It can reach qBittorrent at `http://127.0.0.1:30024`, and its outbound requests follow the same Tailscale exit-node routing.

Set `QBITTORRENT_USERNAME` and `QBITTORRENT_PASSWORD` as environment variables in the Compose app's environment/secrets configuration. Do not paste them into chat.

Dashboard: `http://<TRUENAS-LAN-IP>:8787`

## First test
1. Confirm qBittorrent WebUI works at port 30024.
2. Create the qBittorrent category `anime`.
3. Ensure qBittorrent's `/downloads` path exists and that `/downloads/anime` is the intended save location.
4. Start the stack and open `/health`.
5. Add one anime title. The first scan runs on the configured schedule; use Scan sources now for an immediate scan.
6. Inspect results; only click Approve & add after checking the release page, uploader, and file details.

## Next development milestones
1. Verify Nyaa RSS metadata and improve magnet/torrent extraction.
2. Read the TsukiHime API OpenAPI schema and implement its provider.
3. Add season/cour/absolute-episode metadata and release-group preferences.
4. Add scheduled scans, per-show filters, and source deduplication.
5. Add safe post-download library import using hardlinks or copies, with seeding preserved.
6. Add dashboard authentication and audit logging.
