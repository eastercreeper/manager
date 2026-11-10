#!/usr/bin/env bash
set -euo pipefail

src="upstream/seanime"
test -s "$src/go.mod"
test -s "$src/main.go"
test -s "$src/LICENSE"

# Promote Seanime to the repository root so its internal imports and original
# application lifecycle remain intact. Keep manager workflows and README.
# Overlay upstream internal packages but preserve our custom Jellyfin package.
rsync -a "$src/" ./ \
  --exclude='.git/' \
  --exclude='.github/' \
  --exclude='README.md' \
  --exclude='UPSTREAM_REVISION' \
  --exclude='internal/jellyfin/'

cat > UPSTREAM_MODIFICATIONS.md <<'EOF'
# Upstream modifications

This repository adapts Seanime (https://github.com/5rahim/seanime) under the
upstream GNU GPL-3.0-or-later license. The pinned source snapshot remains
under upstream/seanime/. Root-level Seanime files are promoted as the app.

Manager-specific additions include Jellyfin integration and Jellyfin-focused
workflows. Modified upstream files should be identified in change notes.
The upstream license and copyright notices must be retained.
EOF

echo "Promoted Seanime source into the root Go module."