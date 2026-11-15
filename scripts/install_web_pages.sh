#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Publish the PrepperCache interface without restarting services.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPPERCACHE_ENV_FILE:-$REPO_ROOT/config/preppercache.env}"

source "$REPO_ROOT/scripts/load_env.sh"
load_preppercache_env "$ENV_FILE"

install -d -m 0755 "$PREPPERCACHE_WEB_ROOT" "$PREPPERCACHE_ADMIN_ROOT" "$PREPPERCACHE_MAPS_ROOT" "$PREPPERCACHE_WEB_ROOT/kiwix-skin"
install -m 0644 "$REPO_ROOT/web/theme.css" "$REPO_ROOT/web/theme.js" "$PREPPERCACHE_WEB_ROOT/"
install -m 0644 "$REPO_ROOT/web/job-status.js" "$PREPPERCACHE_WEB_ROOT/job-status.js"
install -m 0644 "$REPO_ROOT/web/index.html" "$PREPPERCACHE_WEB_ROOT/index.html"
install -m 0644 "$REPO_ROOT/web/admin/index.html" "$PREPPERCACHE_ADMIN_ROOT/index.html"
install -m 0644 "$REPO_ROOT/web/maps/index.html" "$PREPPERCACHE_MAPS_ROOT/index.html"
install -m 0644 "$REPO_ROOT/web/kiwix-skin/viewer.js" "$PREPPERCACHE_WEB_ROOT/kiwix-skin/viewer.js"
install -d -m 0755 "$PREPPERCACHE_WEB_ROOT/images"
install -m 0644 "$REPO_ROOT/images/preppercache.png" "$PREPPERCACHE_WEB_ROOT/images/preppercache.png"
install -d -m 0755 "$PREPPERCACHE_WEB_ROOT/kiwix-placeholder/images"
install -m 0644 "$REPO_ROOT/web/kiwix-placeholder/index.html" "$PREPPERCACHE_WEB_ROOT/kiwix-placeholder/index.html"
install -m 0644 "$REPO_ROOT/images/preppercache.png" "$PREPPERCACHE_WEB_ROOT/kiwix-placeholder/images/preppercache.png"
install -m 0644 "$REPO_ROOT/images/preppercache-blue.png" "$PREPPERCACHE_WEB_ROOT/images/"
install -m 0644 "$REPO_ROOT/images/preppercache-blue.png" "$PREPPERCACHE_WEB_ROOT/kiwix-placeholder/images/"
install -m 0644 "$REPO_ROOT/web/theme.css" "$REPO_ROOT/web/theme.js" "$PREPPERCACHE_WEB_ROOT/kiwix-placeholder/"
echo "PrepperCache web pages installed."
