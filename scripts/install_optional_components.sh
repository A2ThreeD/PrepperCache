#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.


set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PREPPERCACHE_ENV_FILE:-$REPO_ROOT/config/preppercache.env}"
PROFILE_FILE="${PREPPERCACHE_PROFILE_FILE:-$REPO_ROOT/config/install-profile.env}"

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

if [[ -f "$PROFILE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$PROFILE_FILE"
fi

if [[ "${INSTALL_KOLIBRI:-0}" == "1" ]]; then
  "$REPO_ROOT/scripts/install_kolibri.sh"
else
  echo "Kolibri not selected."
fi

if [[ "${INSTALL_KA_LITE:-0}" == "1" ]]; then
  "$REPO_ROOT/scripts/install_ka_lite_legacy.sh"
else
  echo "KA Lite not selected."
fi
