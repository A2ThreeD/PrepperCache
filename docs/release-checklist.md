# Release Checklist

Use this checklist when validating PrepperCache on a fresh Raspberry Pi or preparing a new release.

## Source Control

1. Push the latest commits from the main development machine.
2. Confirm the repository is clean before release:
   ```bash
   git status --short
   ```

## Automated Checks

GitHub Actions runs the unit, syntax, web installer, and browser smoke checks on pushes and pull requests. Run the same checks locally from the repository root. Use Bash, Node.js, and a Python virtual environment for test dependencies.

```bash
python3 -m pip install -r tests/requirements-unit.txt
bash scripts/check.sh
python3 -m pip install -r tests/requirements-smoke.txt
python3 -m playwright install chromium
bash scripts/check.sh --browser
```

The installer check stages web files in a temporary directory. The browser suite serves the checked-out pages locally and uses simulated API and map responses. It exercises setup, Settings links, download review, job progress, and missing-map feedback without starting downloads or changing a device. On Linux, install Chromium system dependencies if needed with `python3 -m playwright install --with-deps chromium`.

After a Pi deployment, check the live pages, assets, API responses, and library route with a read-only smoke test.

```bash
bash scripts/check.sh --live http://preppercache.local
```

Use the hardware checklist below to verify OS installation, access-point behavior, and real downloads on a Raspberry Pi.

## Fresh Pi Setup

1. Clone the repository on the Pi:
   ```bash
   git clone <repo-url> preppercache
   cd preppercache
   ```
2. Create the local runtime config:
   ```bash
   cp config/preppercache.env.example config/preppercache.env
   ```
3. Set the important values in `config/preppercache.env`:
   - `PREPPERCACHE_HOSTNAME`
   - `PREPPERCACHE_WIKIPEDIA_OPTION`
   - `PREPPERCACHE_ZIM_MODE`
   - `PREPPERCACHE_AP_ENABLED`
   - AP SSID/passphrase if AP mode will be tested

## Base Install

1. Run the installer:
   ```bash
   sudo ./scripts/install_preppercache.sh
   ```
2. Confirm core services are healthy:
   ```bash
   systemctl status preppercache-portal.service --no-pager
   systemctl status preppercache-kiwix.service --no-pager
   systemctl status nginx --no-pager
   ```

## Browser Validation

1. Open the main site:
   - `http://<pi-ip>/`
   - or `http://preppercache.local/` if mDNS resolves
2. Complete setup in the browser.
3. Click **Complete Setup**.
4. Confirm the dashboard becomes the default homepage after setup.

## Route Checks

Verify these locations:

- `/`
- `/admin/`
- `/maps/`
- `/kiwix/` serves Kiwix through the main site

## Kiwix Checks

1. Open Kiwix from the dashboard and check the topic filters against the installed archives. In Settings, confirm the online catalog loads and a selected ZIM can be downloaded and indexed.
2. Confirm downloaded ZIM files exist:
   ```bash
   ls -lh /library/zims/content
   ```
3. Confirm the Kiwix service is using the expected bind address and port:
   ```bash
   systemctl status preppercache-kiwix.service --no-pager
   ```

## Status And API Checks

1. Confirm the dashboard/admin status areas show:
   - disk usage
   - temperature
   - uptime
   - service health
   - Kiwix target URL
2. Confirm the API endpoints respond:
   ```bash
   curl http://127.0.0.1/api/state
   curl http://127.0.0.1/api/status
   curl http://127.0.0.1/api/apply
   ```

## AP Mode Checks

If AP mode is disabled:

```bash
systemctl status hostapd --no-pager
systemctl status dnsmasq --no-pager
```

Expected result:
- `hostapd` inactive
- `dnsmasq` inactive

If AP mode is enabled:

1. Confirm the AP SSID is visible.
2. Join the Pi directly from another device.
3. Confirm the site and Kiwix are reachable over the AP network.

## Release Notes To Capture

Record these items for each release validation:

- Raspberry Pi model
- OS version
- storage size
- whether AP mode was tested
- whether quick-test or full content mode was used
- any manual recovery steps needed during validation

## Git Hygiene

Do not commit Pi-local runtime files:

- `config/preppercache.env`
- `config/install-profile.env`
- `config/kiwix-zim-urls.txt`

Before finishing:

```bash
git status --short
```

The working tree should be clean or only contain intentionally local runtime changes.
