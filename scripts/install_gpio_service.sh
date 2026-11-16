#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Install the Raspberry Pi front-panel service. SPDX-License-Identifier: MIT

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPPERCACHE_ENV_FILE:-$REPO_ROOT/config/preppercache.env}"
source "$REPO_ROOT/scripts/load_env.sh"
load_preppercache_env "$ENV_FILE"

if ! grep -aq 'Raspberry Pi' /proc/device-tree/model 2>/dev/null; then
  echo "GPIO service requires a Raspberry Pi."
  exit 0
fi

if ! /usr/bin/python3 -c 'import gpiozero, lgpio, luma.oled.device' 2>/dev/null; then
  apt-get install -y python3-gpiozero python3-lgpio python3-luma.oled
fi
install -d -m 0755 "$PREPPERCACHE_ROOT/app" "$PREPPERCACHE_DATA_ROOT"
install -m 0755 "$REPO_ROOT/app/preppercache_gpio.py" "$PREPPERCACHE_ROOT/app/preppercache_gpio.py"
cat > /etc/systemd/system/preppercache-gpio.service <<EOF
[Unit]
Description=PrepperCache shutdown button and OLED
After=local-fs.target

[Service]
ExecStart=/usr/bin/python3 "$PREPPERCACHE_ROOT/app/preppercache_gpio.py" --data-dir "$PREPPERCACHE_DATA_ROOT"
Environment=GPIOZERO_PIN_FACTORY=lgpio
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable preppercache-gpio.service
systemctl restart preppercache-gpio.service
