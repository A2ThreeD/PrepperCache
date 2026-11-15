#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Install the PrepperCache offline knowledge stack and web assets.

set -euo pipefail
trap 'result=$?; printf "PrepperCache installation stopped at line %s (exit %s): %s\n" "$LINENO" "$result" "$BASH_COMMAND" >&2; exit "$result"' ERR

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPPERCACHE_ENV_FILE:-$REPO_ROOT/config/preppercache.env}"
PROFILE_FILE="${PREPPERCACHE_PROFILE_FILE:-$REPO_ROOT/config/install-profile.env}"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0"
  exit 1
fi

if [[ ! -e "$ENV_FILE" && ! -L "$ENV_FILE" && -z "${PREPPERCACHE_ENV_FILE:-}" ]]; then
  (set -o noclobber; cat "$REPO_ROOT/config/preppercache.env.example" > "$ENV_FILE")
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing config file: $ENV_FILE"
  exit 1
fi

# shellcheck source=./load_env.sh
source "$REPO_ROOT/scripts/load_env.sh"
load_preppercache_env "$ENV_FILE"

if [[ -f "$PROFILE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$PROFILE_FILE"
fi

echo "Running base OS bootstrap..."
"$REPO_ROOT/scripts/bootstrap_pi.sh"

echo "Installing PrepperCache web pages..."
bash "$REPO_ROOT/scripts/install_web_pages.sh"

echo "Installing offline maps assets..."
"$REPO_ROOT/scripts/install_maps_assets.sh"
bash "$REPO_ROOT/scripts/install_pmtiles_cli.sh"

echo "Preparing Kiwix content directory..."
install -d -m 0755 "$KIWIX_LIBRARY_DIR"

echo "Generating default Kiwix manifest..."
python3 "$REPO_ROOT/scripts/build_kiwix_zim_manifest.py" \
  --source "$REPO_ROOT/catalog/kiwix-categories.yaml" \
  --output "$REPO_ROOT/config/kiwix-zim-urls.txt" \
  --profile "${PREPPERCACHE_ZIM_PROFILE:-essential}" \
  --wikipedia-options "$REPO_ROOT/catalog/wikipedia.yaml" \
  --wikipedia-choice "${PREPPERCACHE_WIKIPEDIA_OPTION:-top-mini}"

echo "Installing portal API service..."
"$REPO_ROOT/scripts/install_portal_service.sh"

echo "Installing Kiwix service..."
"$REPO_ROOT/scripts/install_kiwix_service.sh"

echo "Installing Nginx site..."
"$REPO_ROOT/scripts/install_nginx_site.sh"

echo "Selected install profile:"
echo "  Base stack: ${INSTALL_BASE_STACK:-1}"
echo "  Kiwix: ${INSTALL_KIWIX:-1}"
echo "  OpenStreetMaps: ${INSTALL_OPENSTREETMAPS:-1}"
echo "  Wikipedia option: ${PREPPERCACHE_WIKIPEDIA_OPTION:-top-mini}"
echo "  Kolibri: ${INSTALL_KOLIBRI:-0}"
echo "  KA Lite: ${INSTALL_KA_LITE:-0}"

echo "Installing optional components from profile..."
"$REPO_ROOT/scripts/install_optional_components.sh"

if [[ "${PREPPERCACHE_AP_ENABLED:-0}" == "1" ]]; then
  echo "Applying wireless access point configuration..."
  "$REPO_ROOT/scripts/configure_access_point.sh"
else
  echo "Wireless access point mode not enabled."
fi

echo "PrepperCache base installation complete."
echo
echo "Suggested next steps:"
echo "  1. sudo $REPO_ROOT/scripts/download_kiwix_zims.sh"
echo "  2. Visit http://$PREPPERCACHE_HOSTNAME.local/ or the Pi IP address on your local network"
echo "  3. Open /admin if you want to revisit setup preferences"
