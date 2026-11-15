#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Install the base operating system dependencies.

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

PACKAGES=(
  avahi-daemon
  ca-certificates
  curl
  git
  hostapd
  jq
  libjs-leaflet
  nginx
  python3
  python3-pip
  python3-venv
  python3-yaml
  rsync
  sqlite3
  dnsmasq
  ufw
  unzip
  wget
  xz-utils
)

echo "Updating package lists..."
apt update

echo "Upgrading installed packages..."
apt full-upgrade -y

echo "Installing required packages..."
apt install -y "${PACKAGES[@]}"

echo "Creating project directories..."
install -d -m 0755 "$PREPPERCACHE_ROOT"
install -d -m 0755 "$PREPPERCACHE_SRC_DIR"
install -d -m 0755 "$PREPPERCACHE_WEB_ROOT"
install -d -m 0755 "$PREPPERCACHE_DATA_ROOT"
install -d -m 0755 "$PREPPERCACHE_ADMIN_ROOT"
install -d -m 0755 "$PREPPERCACHE_MAPS_ROOT"
install -d -m 0755 "$KIWIX_LIBRARY_DIR"

echo "Configuring hostname..."
if [[ -n "${PREPPERCACHE_HOSTNAME:-}" ]]; then
  hostnamectl set-hostname "$PREPPERCACHE_HOSTNAME"
fi

echo "Enabling core services..."
systemctl enable --now avahi-daemon
systemctl enable nginx

echo "Bootstrap complete."
