#!/usr/bin/env bash
set -euo pipefail

# Import the exact Seanime revision audited for this rewrite. The source is
# vendored for adaptation; this does not install or launch a second service.
readonly upstream_repo="https://github.com/5rahim/seanime.git"
readonly upstream_revision="4bdff7b27b6c043dc87c0ccad3ead54596133a05"
readonly destination="upstream/seanime"
readonly checkout="${RUNNER_TEMP:-/tmp}/seanime-upstream"

rm -rf "$checkout"
git clone --filter=blob:none --no-checkout "$upstream_repo" "$checkout"
git -C "$checkout" checkout --detach "$upstream_revision"

mkdir -p "$destination"
# Keep the manager's own workflows out of the vendored snapshot. All upstream
# application source, frontend assets, module metadata, and license files are
# retained so package dependencies can be audited before integration.
rsync -a --delete --exclude='.git/' --exclude='.github/' "$checkout/" "$destination/"

cat > "$destination/UPSTREAM_REVISION" <<EOF
Repository: 5rahim/seanime
Commit: $upstream_revision
License: GNU GPL-3.0-or-later
Imported: $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

test -s "$destination/LICENSE"
test -s "$destination/go.mod"
echo "Imported Seanime $upstream_revision into $destination"
