#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Verify the web installer stages the files it needs without touching a device.
set -euo pipefail

stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
cat > "$stage/config.env" <<EOF
PREPPERCACHE_WEB_ROOT="$stage/www"
PREPPERCACHE_ADMIN_ROOT="$stage/www/admin"
PREPPERCACHE_MAPS_ROOT="$stage/www/maps"
EOF
PREPPERCACHE_ENV_FILE="$stage/config.env" bash scripts/install_web_pages.sh

for file in index.html admin/index.html maps/index.html theme.css theme.js job-status.js; do
  cmp "web/$file" "$stage/www/$file"
done
cmp images/preppercache-blue.png "$stage/www/images/preppercache-blue.png"
echo "Web installer smoke check passed."
