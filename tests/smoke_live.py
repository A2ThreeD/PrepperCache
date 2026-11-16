# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Read-only checks against a running PrepperCache installation.

import argparse
import json
from urllib.error import HTTPError
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    base = parser.parse_args().url.rstrip("/")

    for path, marker in {
        "/": 'id="dashboard-view"',
        "/admin/": 'data-panel-group="about"',
        "/maps/": 'id="map-select"',
        "/theme.css": "charcoal-blue",
        "/job-status.js": "preppercache-job-status",
    }.items():
        with urlopen(base + path, timeout=10) as response:
            if marker not in response.read().decode("utf-8"):
                raise SystemExit(f"{path} did not serve the expected PrepperCache page or asset.")
        print(f"OK {path}")

    with urlopen(base + "/api/admin/session", timeout=10) as response:
        session = json.load(response)
    locked = session.get("enabled") is True
    if session.get("authenticated") is not (not locked):
        raise SystemExit("The guest admin session returned unexpected access.")
    print("OK /api/admin/session")

    for path, key in {"/api/state": "admin_locked" if locked else "setup_complete",
                      "/maps/state.json": "available_files"}.items():
        with urlopen(base + path, timeout=10) as response:
            payload = json.load(response)
            if not isinstance(payload, dict) or key not in payload:
                raise SystemExit(f"{path} did not return the expected JSON field {key}.")
        print(f"OK {path}")

    for path in ("/api/apply", "/api/maps/sync"):
        try:
            with urlopen(base + path, timeout=10) as response:
                if locked or "status" not in json.load(response):
                    raise SystemExit(f"{path} returned unexpected guest access.")
        except HTTPError as error:
            if not locked or error.code != 401:
                raise
        print(f"OK {path}" + (" (locked)" if locked else ""))

    # A new installation can serve the built-in placeholder instead of Kiwix.
    with urlopen(base + "/kiwix/", timeout=10) as response:
        if not response.read(1):
            raise SystemExit("/kiwix/ served an empty response.")
    print("OK /kiwix/")


if __name__ == "__main__":
    main()
