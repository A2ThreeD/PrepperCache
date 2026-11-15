#!/usr/bin/env bash
# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Run project checks without modifying a PrepperCache installation.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

case "${1:-}" in
  "")
    python3 -m unittest discover -s tests -v
    for script in scripts/*.sh tests/*.sh; do bash -n "$script"; done
    python3 tests/check_web_syntax.py
    bash tests/smoke_web_install.sh
    ;;
  --browser)
    python3 tests/smoke_browser.py
    ;;
  --live)
    python3 tests/smoke_live.py "${2:?Supply the PrepperCache URL}"
    ;;
  *)
    echo "Usage: bash scripts/check.sh [--browser | --live URL]" >&2
    exit 2
    ;;
esac
