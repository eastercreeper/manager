# Seanime integration audit

Audited source: `upstream/seanime`, revision `4bdff7b27b6c043dc87c0ccad3ead54596133a05`.

## Confirmed upstream building blocks

### Persistence and models

- Seanime uses GORM with SQLite (`github.com/glebarez/sqlite`).
- `internal/database/db.NewDatabase(appDataDir, dbName, logger)` creates the SQLite database, configures WAL/busy-timeout settings, and calls the upstream table migration routine.
- `internal/database/models` contains the upstream GORM models, including settings, local-file cache, auto-downloader rules/profiles/items, scan summaries, accounts, and other application state.
- The existing application should adopt this persistence layer and these models rather than introducing a parallel anime catalog.

### Anime discovery and automatic downloading

- `internal/library/autoscanner` exposes an AutoScanner constructed with the database, platform, event manager, AutoDownloader, metadata provider, and scan configuration.
- `internal/library/autodownloader` exposes an AutoDownloader constructed with torrent/debrid repositories, database, metadata provider, event manager, and offline-state reference.
- The AutoDownloader depends on Seanime's AniList collection and metadata-provider abstractions. It is not a stand-alone torrent helper that can safely be copied without its service graph.
- `internal/library/anime` contains anime scheduling types and logic that use the upstream AniList collection.

### Application lifecycle

- Upstream `main.go` embeds the `web` directory and calls `internal/server.StartServer`.
- `internal/server` creates `core.App`, creates the Echo server, registers the upstream routes, and starts cron jobs.
- `core.App` imports and owns a broad set of subsystems: anime metadata, scanner, downloader, torrent/debrid clients, manga, playback, media streaming, native players, plugins, and other Seanime services.
- This means simply importing one Seanime package into the current small Go module would not reuse the full feature set. The most faithful foundation is to make the rewrite a fork/adaptation of Seanime's application module, then remove or disable unneeded runtime surfaces carefully.

## Integration decisions

1. Promote the pinned upstream application source to the rewrite's Go module root so its `seanime/internal/...` imports and `internal` package boundaries remain valid.
2. Keep our Jellyfin-specific code as an additional package inside that same module. Do not copy Seanime models into new manager-owned parallel types.
3. Extend the upstream persistence models/migrations only for Jellyfin identifiers, item/episode reconciliation, approval state, and verification results.
4. Add Jellyfin routes/services to the existing Echo application lifecycle. Jellyfin/filesystem observations remain authoritative for physical file existence.
5. Reuse upstream torrent providers, torrent client abstractions, and AutoDownloader where their existing contracts fit. Add manager policy around them for explicit approval, target paths, and post-download verification.
6. Keep manual AniList mapping as an override layer around upstream metadata identification, not a second AniList client.
7. Remove player, manga, and unrelated surfaces only after the dependency graph proves they are not required by the retained anime/download services.

## Important caveats

- The upstream `core.App` is intentionally broad. Removing its player/manga fields or initialization prematurely could break package initialization and event wiring. First get the adapted application building, then prune in dependency-safe steps.
- The upstream module declares `module seanime`; keeping that module path initially avoids rewriting hundreds of internal imports. The fork can retain that path internally while the repository remains named `manager`.
- The upstream frontend is part of the same repository. We should adapt the existing web application rather than deploy a separate Seanime process or create a second web server.
- The current top-level `go.mod`, `Dockerfile`, and Python app files are still the pre-integration scaffolding. The vendored snapshot is not yet the runtime foundation. The next implementation change must promote upstream source into the module root and wire Jellyfin into the upstream server lifecycle.
