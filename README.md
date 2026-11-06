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

## Current work

- Created the isolated `rewrite/go-backend` branch.
- Imported the complete pinned Seanime source snapshot (including its Go packages, frontend source, module metadata, and license).
- Added a reproducible GitHub Actions importer so the exact upstream snapshot can be refreshed deliberately.
- Kept the first Jellyfin API integration package and its tests as a reference for the custom integration layer.
- Added Go CI for the current custom package.

## Next integration work

1. Audit the imported package graph and identify the smallest Seanime anime/metadata/scanner/downloader core that can be built without desktop, playback, and manga runtime features.
2. Make Seanime's anime models and persistence the application's single database foundation.
3. Wire Jellyfin library reconciliation into that catalog.
4. Add manager-only manual AniList mapping, release approval, qBittorrent policy, and media verification.
5. Replace the current Python Docker build with one Go application image.

No production deployment or existing data has been changed.
