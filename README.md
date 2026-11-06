# Anime Manager — Go rewrite

A standalone Jellyfin-focused anime management application being rebuilt on the actual Go backend of [Seanime](https://github.com/5rahim/seanime).

**Status: early development.** This branch is not yet deployable. The previous Python application is intentionally not being migrated, and its database is not part of the new design.

## Design rules

- Reuse Seanime's actual Go implementation for features it already provides; do not recreate those features in the old Python code or in parallel Go services.
- Use Seanime's anime models and database conventions as the single catalog foundation.
- Extend that catalog with Jellyfin identifiers, physical paths, and reconciliation state rather than maintaining a second anime database.
- Treat Jellyfin and the filesystem as authoritative for what media physically exists.
- Add custom Go code only for functionality Seanime does not provide, especially Jellyfin-specific reconciliation, manual mapping overrides, approval policy, and media verification.
- Ship one standalone Go application and one Docker service. Seanime is not installed or run as a separate service.

## Upstream license

Seanime is licensed under GNU GPL-3.0-or-later. This project will preserve upstream copyright notices and license text, identify modified upstream components, and provide corresponding source when distributing builds.

## Current work

- Go module initialized.
- Initial Jellyfin API integration package added for server connectivity and paginated series discovery.
- Unit tests added for Jellyfin authentication, configuration validation, and pagination.
- Architecture and upstream integration rules documented in [REWRITE_STATUS.md](REWRITE_STATUS.md).

## Not implemented yet

- Import and integration of Seanime's actual backend source.
- Adaptation of Seanime's database models and startup lifecycle.
- Jellyfin-to-Seanime anime/episode reconciliation.
- Custom manual AniList mapping and conflict resolution.
- Final web interface, Docker image, and deployment workflow.

See [REWRITE_STATUS.md](REWRITE_STATUS.md) for the implementation status and upstream revision being audited.
