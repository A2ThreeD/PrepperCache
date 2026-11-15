#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.


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

install -d -m 0755 "$PREPPERCACHE_ROOT/app"
install -d -m 0755 "$PREPPERCACHE_DATA_ROOT"
install -m 0755 "$REPO_ROOT/app/preppercache_portal.py" "$PREPPERCACHE_ROOT/app/preppercache_portal.py"

cat > /etc/systemd/system/preppercache-portal.service <<EOF
[Unit]
Description=PrepperCache portal API
After=network.target

[Service]
Type=simple
Environment="PREPPERCACHE_ENV_FILE=$ENV_FILE"
ExecStart=/usr/bin/env python3 $PREPPERCACHE_ROOT/app/preppercache_portal.py --host 127.0.0.1 --repo-root "$REPO_ROOT" --data-dir "$PREPPERCACHE_DATA_ROOT" --port "$ADMIN_PORT"
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable preppercache-portal.service
systemctl restart preppercache-portal.service || systemctl start preppercache-portal.service

echo "PrepperCache portal API installed on port $ADMIN_PORT"
