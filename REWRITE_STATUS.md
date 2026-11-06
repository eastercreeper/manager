# Go rewrite status

This branch is a clean replacement for the previous Python application. The old Python implementation and its database are not part of the target runtime; no legacy database migration is planned.

## Architecture rules

1. **Reuse upstream before reimplementing.** Seanime's Go implementation is the source of truth for features it already provides. Adapt its actual code rather than recreating equivalent functionality.
2. **One anime catalog and one database.** Seanime's model and persistence conventions are the foundation. Jellyfin IDs, physical paths, and reconciliation state extend that catalog rather than creating a second competing anime database.
3. **Jellyfin owns physical-library reality.** Metadata providers describe anime; Seanime-derived services identify anime and releases; Jellyfin and the filesystem establish which media actually exists.
4. **Custom code is limited to missing Jellyfin-manager behavior.** Examples include Jellyfin synchronization, Jellyfin-to-anime reconciliation, manual mapping overrides, release approval policy, and media verification.
5. **One deployable application.** The final result is one Go application and one Docker service. Seanime is source code, not a separately installed or running service.

## Upstream

- Project: https://github.com/5rahim/seanime
- Upstream Go module: `seanime`
- License: GNU GPL-3.0-or-later; preserve upstream notices and license text.
- Audited upstream revision: `4bdff7b27b6c043dc87c0ccad3ead54596133a05`

The upstream repository contains features outside our target. Before integrating packages, inventory their dependency graph and exclude unrelated desktop, playback, and manga surfaces without breaking packages used by selected anime features. Keep attribution and a record of local modifications.

## Current implementation

- `go.mod`: starts the new Go module.
- `internal/jellyfin`: custom Jellyfin API client for connectivity and series discovery.
- Unit tests cover API-key authentication, configuration validation, and paginated series retrieval.

This is an initial integration slice, not yet the complete Seanime import or a deployable replacement. No production deployment or existing data has been changed.
