#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Rebuild the local Kiwix library index.

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

if ! command -v kiwix-manage >/dev/null 2>&1; then
  echo "kiwix-manage is not installed. Install kiwix-tools first."
  exit 1
fi

install -d -m 0755 "$(dirname "$KIWIX_LIBRARY_XML")"

exec {library_lock}>"${KIWIX_LIBRARY_XML}.lock"
flock "$library_lock"

rejected_file="${KIWIX_LIBRARY_XML}.rejected"
rejected_tmp="$(mktemp "${rejected_file}.XXXXXX")"
library_tmp=""
trap 'rm -f "$rejected_tmp"; if [[ -n "$library_tmp" ]]; then rm -f "$library_tmp"; fi' EXIT
chmod 0644 "$rejected_tmp"

mapfile -d '' -t zims < <(find -L "$KIWIX_LIBRARY_DIR" -maxdepth 1 -type f -name '*.zim' -print0 | sort -z)

if [[ ${#zims[@]} -eq 0 ]]; then
  rm -f "$KIWIX_LIBRARY_XML"
  mv -f "$rejected_tmp" "$rejected_file"
  echo "No ZIM files found in $KIWIX_LIBRARY_DIR"
  exit 0
fi

# Keep the temporary index beside the live index so relative ZIM paths stay valid.
library_tmp="$(mktemp "${KIWIX_LIBRARY_XML}.XXXXXX")"
printf '<?xml version="1.0" encoding="UTF-8"?>\n<library version="20110515"/>\n' > "$library_tmp"
rejected=()
added=0
for zim in "${zims[@]}"; do
  echo "Adding to library: $zim"
  if kiwix-manage "$library_tmp" add "$zim"; then
    added=$((added + 1))
  else
    rejected+=("${zim##*/}")
    printf '%s\0' "${zim##*/}" >> "$rejected_tmp"
  fi
done

if (( added > 0 )); then
  python3 "$REPO_ROOT/scripts/tag_kiwix_library.py" "$library_tmp"
  chmod 0644 "$library_tmp"
  mv -f "$library_tmp" "$KIWIX_LIBRARY_XML"
  echo "Kiwix library rebuilt at $KIWIX_LIBRARY_XML"
fi

mv -f "$rejected_tmp" "$rejected_file"

for zim in "${rejected[@]}"; do
  printf 'Kiwix could not index: %s\n' "$zim"
done

if (( added == 0 )); then
  echo "Kiwix could not index any ZIM files. Kept the existing library index."
  exit 1
fi
