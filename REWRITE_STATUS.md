# Go rewrite status

This branch is a clean replacement for the previous Python application. The old Python implementation and its database are not part of the target runtime; no legacy database migration is planned.

## Non-negotiable architecture rules

1. **Reuse upstream before reimplementing.** Seanime's Go implementation is the source of truth for features it already provides. Adapt its actual code rather than recreating equivalent functionality.
2. **One anime catalog and one database.** Seanime's model and persistence conventions are the foundation. Jellyfin IDs, physical paths, and reconciliation state extend that catalog rather than creating a second competing anime database.
3. **Jellyfin owns physical-library reality.** Metadata providers describe anime; Seanime-derived services identify anime and releases; Jellyfin and the filesystem establish which media actually exists.
4. **Custom code is limited to missing Jellyfin-manager behavior.** Examples include Jellyfin synchronization, Jellyfin-to-anime reconciliation, manual mapping overrides, release approval policy, and media verification.
5. **One deployable application.** The final result is one Go application and one Docker service. Seanime is source code, not a separately installed or running service.

## Upstream snapshot

- Project: https://github.com/5rahim/seanime
- Audited revision: `4bdff7b27b6c043dc87c0ccad3ead54596133a05`
- Snapshot: [upstream/seanime](upstream/seanime)
- License: GNU GPL-3.0-or-later; the original LICENSE and source notices are retained.
- Import script: `.github/scripts/import-seanime.sh`
- Import workflow: `.github/workflows/import-seanime.yml`

The source import is complete. It is intentionally kept under `upstream/seanime` as an auditable source snapshot; it is not yet wired into the application runtime. Do not mistake vendoring for completed integration.

## Integration audit plan

Before selecting packages, inspect the actual import graph and runtime initialization. Prioritize:

- `database/models` and `database/db`: Seanime's data model and persistence conventions.
- `api/anilist` and `core/anilist.go`: metadata and AniList functionality.
- `library/anime`, `library/scanner`, and `library/autoscanner`: anime/episode identification and library scanning.
- `library/autodownloader`, `torrents`, `torrent_clients`, and `handlers/torrent_*`: release discovery and downloader integration.

Exclude unrelated desktop, player, manga, and media-streaming surfaces only after confirming they are not dependencies of the selected packages.

## Current custom integration

- `internal/jellyfin`: API-key authenticated Jellyfin client, server-info lookup, and paginated series discovery.
- Tests cover URL/config validation, authentication headers, and pagination.

## Remaining work

1. Complete package dependency/runtime audit and select the reusable Seanime application core.
2. Integrate Seanime models and database into the single app database.
3. Add Jellyfin-to-Seanime catalog and episode reconciliation.
4. Add custom manual AniList mapping/conflict resolution, release approval, qBittorrent workflow, and media/subtitle verification.
5. Replace the legacy Python Docker build with a single Go application image and test it before any deployment.

No production deployment or existing data has been changed.
