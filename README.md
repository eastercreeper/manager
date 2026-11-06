# Anime Manager — Go rewrite

A standalone Jellyfin-focused anime management application built by adapting the actual Go backend of [Seanime](https://github.com/5rahim/seanime).

**Status: Seanime root application and Jellyfin manager extensions implemented; CI validation in progress.** This branch is not production-deployed. The previous Python application and its database are intentionally not being migrated.

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
- Manual AniList overrides, release approvals, qBittorrent handoff, completion reconciliation, and FFprobe subtitle verification are available through the manager extension.
- A lightweight dashboard is served at `/api/v1/manager/ui`.
- The Dockerfile builds the upstream React frontend, embeds it in the Go binary, and packages one runtime image.
- `.env.example`, `compose.example.yaml`, and `docs/MANAGER_API.md` describe configuration and endpoints.

CI is validating frontend build, Go tests, full Go application build, vet, and Docker image build. No production deployment or existing data has been changed.
