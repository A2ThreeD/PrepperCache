#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Install the Kiwix service and its runtime dependencies.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPPERCACHE_ENV_FILE:-$REPO_ROOT/config/preppercache.env}"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing config file: $ENV_FILE"
  exit 1
fi

# shellcheck source=./load_env.sh
source "$REPO_ROOT/scripts/load_env.sh"
load_preppercache_env "$ENV_FILE"

echo "Installing Kiwix tools..."
apt install -y kiwix-tools

install -d -m 0755 "$(dirname "$KIWIX_LIBRARY_XML")"
install -d -m 0755 "$PREPPERCACHE_ROOT/app"
install -d -m 0755 "$PREPPERCACHE_WEB_ROOT/kiwix-placeholder/images"
install -m 0755 "$REPO_ROOT/scripts/run_kiwix_service.sh" "$PREPPERCACHE_ROOT/app/run_kiwix_service.sh"
install -d -m 0755 "$PREPPERCACHE_ROOT/scripts"
install -m 0644 "$REPO_ROOT/scripts/load_env.sh" "$PREPPERCACHE_ROOT/scripts/load_env.sh"
install -m 0644 "$REPO_ROOT/web/kiwix-placeholder/index.html" "$PREPPERCACHE_WEB_ROOT/kiwix-placeholder/index.html"

install -m 0644 "$REPO_ROOT/images/preppercache.png" "$PREPPERCACHE_WEB_ROOT/kiwix-placeholder/images/preppercache.png"

cat > /etc/systemd/system/preppercache-kiwix.service <<EOF
[Unit]
Description=PrepperCache Kiwix Service
After=network.target

[Service]
Type=simple
Environment=PREPPERCACHE_ENV_FILE=$ENV_FILE
ExecStart=$PREPPERCACHE_ROOT/app/run_kiwix_service.sh
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable preppercache-kiwix.service

if [[ -d "$KIWIX_LIBRARY_DIR" ]]; then
  "$REPO_ROOT/scripts/rebuild_kiwix_library.sh" || true
fi

systemctl restart preppercache-kiwix.service || systemctl start preppercache-kiwix.service

echo "Kiwix service installed on port $KIWIX_PORT"
