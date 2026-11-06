# Anime Manager — Go rewrite

A standalone Jellyfin-focused anime management application built by adapting the actual Go backend of [Seanime](https://github.com/5rahim/seanime).

**Status: implementation complete on the feature branch; the full CI pipeline passed for commit `850e183`.** A final importer safety improvement is being revalidated. This branch is not production-deployed. The previous Python application and its database are intentionally not being migrated.

## Design rules

- Reuse Seanime's actual Go implementation for features it already provides; do not recreate those features in parallel.
- Use Seanime's anime models and persistence conventions as the single catalog foundation.
- Extend that catalog with Jellyfin identifiers, physical paths, and reconciliation state rather than maintaining a second anime database.
- Treat Jellyfin and the filesystem as authoritative for what media physically exists.
- Add custom Go code only for functionality Seanime does not provide, especially Jellyfin-specific reconciliation, manual mapping overrides, approval policy, and media verification.
- Ship one standalone Go application and one Docker service. Seanime is source code, not a separately installed or running service.

## Upstream source and license

The audited Seanime snapshot is vendored in [upstream/seanime](upstream/seanime) at commit
`4bdff7b27b6c043dc87c0ccad3ead54596133a05`. Its upstream LICENSE and notices are retained.
See [upstream/seanime/UPSTREAM_REVISION](upstream/seanime/UPSTREAM_REVISION).

This project must preserve applicable GPL-3.0-or-later notices, identify modifications to upstream files, and provide corresponding source when distributing covered binaries.

## Implemented on this branch

- Seanime's pinned Go application is promoted to the repository root and remains the sole application runtime.
- Jellyfin series/episode synchronization is registered in Seanime's Echo API and uses Seanime's GORM/SQLite database.
- Jellyfin paths configure Seanime's native anime scanner.
- Manual AniList overrides are applied inside Seanime's native scan-matching hook, including fetching mapped AniList media when it is not already in the scanner's candidate set.
- Release approvals hand off to qBittorrent; completed torrents are copied into the selected Jellyfin series/season directory, with matching external subtitle sidecars. Original download files remain in place for seeding, and existing library files are never overwritten.
- Import state and errors are persisted so a failed import can be retried; Jellyfin refresh and FFprobe subtitle verification remain available.
- A lightweight dashboard is served at `/api/v1/manager/ui`.
- The Dockerfile builds the upstream React frontend, embeds it in the Go binary, and packages one runtime image.
- `.env.example`, `compose.example.yaml`, and `docs/MANAGER_API.md` describe configuration and endpoints.

CI validates the frontend build, Go tests, full Go application build, `go vet`, and Docker image build. The most recent fully green run is [run 42270531493](https://github.com/eastercreeper/manager/actions/runs/42270531493). For imports, mount the Jellyfin media path read-write at the same absolute path Jellyfin reports, and make qBittorrent's completed download path visible inside the manager container. No production deployment or existing data has been changed.
