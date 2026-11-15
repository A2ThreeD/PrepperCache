# PrepperCache Software Plan

This document describes the software stack PrepperCache uses today on a Raspberry Pi, what the browser-driven setup flow now controls, and which work areas still need attention.

## Current Product Shape

PrepperCache is now an offline-first appliance-style Raspberry Pi stack with:

- a first-start setup flow on `/`
- a dashboard view after setup is complete
- a settings/admin interface on `/admin`
- an offline maps viewer on `/maps`
- Kiwix served behind the PrepperCache site
- an optional wireless access point mode

The project is no longer just a scaffold. The software plan should now be read as `current software baseline + next software priorities`.

## Core Runtime Stack

### Base System Packages

Installed by `scripts/bootstrap_pi.sh` and used by the current stack:

- `avahi-daemon` for LAN hostname discovery
- `ca-certificates` for HTTPS downloads
- `curl` and `wget` for scripted downloads
- `git` for source retrieval and `pmtiles` for regional map extracts
- `hostapd` for wireless access point mode
- `dnsmasq` for DHCP/DNS on the local access point
- `jq` for JSON-friendly shell workflows
- `kiwix-tools` for `kiwix-serve` and `kiwix-manage`
- `nginx` for the main site and reverse proxy layer
- `python3`, `python3-pip`, and `python3-venv` for portal and helper tooling
- `rsync` for file sync operations
- `sqlite3` for lightweight local state if needed later
- `ufw` for firewall management
- `unzip` and `xz-utils` for downloaded assets

### Runtime Services

The stack currently expects these service roles:

- `preppercache-portal.service` for setup state, status APIs, and background workflows
- `preppercache-kiwix.service` for `kiwix-serve`
- `nginx` for `/`, `/admin`, `/maps`, `/api`, and Kiwix proxying
- `hostapd` when wireless AP mode is enabled
- `dnsmasq` when wireless AP mode is enabled
- `preppercache-ap-network.service` when the device hotspot is enabled

## Current User-Facing Software Features

### 1. First-Start Setup

The setup flow on `/` now supports:

- module selection
- Wikipedia package selection
- offline map region selection
- network mode selection
- review + complete setup
- live apply status and refreshing logs
- storage estimation with warnings and hard blocking when space is insufficient
- resume behavior after reboot if setup work was interrupted

The setup flow is intentionally plain-language and optimized for non-technical users.

### 2. Offline Knowledge

PrepperCache currently supports:

- curated ZIM selection from `catalog/kiwix-categories.yaml`
- selectable Wikipedia packages from `catalog/wikipedia.yaml`
- generated manifests via `scripts/build_kiwix_zim_manifest.py`
- optional `quick-test` mode for smaller validation downloads
- installed-content management in `/admin`
- online catalog browsing for more ZIM downloads
- disk-usage preview for additional ZIM selections

### 3. Offline Maps

PrepperCache now supports:

- local PMTiles storage under `/maps`
- MapLibre-based browser viewing on `/maps`
- active-map centering from PMTiles bounds when possible
- country and regional map selection after initial setup
- region-based map management in `/admin`
- size-aware regional map selection with disk warnings
- background map extraction with progress and log output

### 4. Wireless Access Point

The admin system page currently supports:

- viewing AP status
- editing AP configuration
- enabling and disabling the AP
- applying AP settings without leaving the UI
- viewing connected clients

### 5. Kiwix Integration

The current Kiwix behavior includes:

- Kiwix running on its own service/port
- Nginx proxy handling so the site can route users cleanly into Kiwix
- library rebuild support after adding or removing ZIMs
- placeholder behavior when no library exists yet

## Current Configuration Sources

### Main Editable Runtime Config

- `config/preppercache.env`
- `config/preppercache.env.example`

This covers:

- hostname
- content mode
- Wikipedia choice
- map storage/config paths
- map catalog source
- wireless AP settings
- Kiwix-related paths and ports

### Install Profile

- `config/install-profile.env`
- `config/install-profile.env.example`

This currently tracks install-profile style choices such as optional education add-ons.

### Catalog Sources

- `catalog/kiwix-categories.yaml`
- `catalog/wikipedia.yaml`

These now act as PrepperCache’s local source-of-truth catalogs for:

- curated knowledge/content groupings
- Wikipedia package choices
- regional offline map choices

## Current Script Responsibilities

### Installation / System

- `scripts/bootstrap_pi.sh`
- `scripts/install_preppercache.sh`
- `scripts/install_portal_service.sh`
- `scripts/install_kiwix_service.sh`
- `scripts/install_nginx_site.sh`
- `scripts/install_maps_assets.sh`

### Content / Catalog

- `scripts/build_kiwix_zim_manifest.py`
- `scripts/build_wikipedia_options.py`
- `scripts/download_kiwix_zims.sh`
- `scripts/rebuild_kiwix_library.sh`

### Optional Components

- `scripts/install_optional_components.sh`
- `scripts/install_kolibri.sh`
- `scripts/install_ka_lite_legacy.sh`

### Wireless Networking

- `scripts/configure_access_point.sh`

## Current Design/UX Software Rules

These conventions are now part of the product and should be treated as implementation rules, not just preferences:

- plain language for non-technical users
- one main decision per screen or card where possible
- beveled pressable controls should look obviously interactive
- status boxes should read differently from buttons
- white scrollbars on dark scrollable panels
- PrepperCache branding stays primary in the user-facing UI
- upstream sources should be credited quietly, not used as primary feature branding

## Work That Is Implemented Now

The following items are no longer “future plan” items:

- browser-driven setup and apply flow
- setup-complete state with dashboard switching
- live apply progress/status API
- AP control UI in settings
- connected-clients view for the AP
- offline maps viewer using PMTiles
- region-based map selection and sync
- installed-content management in settings
- Kiwix proxy routing through Nginx

## Near-Term Software Priorities

These are the main areas still worth iterating on next:

- external storage usage capabilities and management
- better map metadata and descriptions where upstream data is sparse
- stronger documentation for local catalog refresh/update workflows
- possible backup/export workflows for saved setup state and content choices
- more explicit update/maintenance flows for already-installed devices
- better test/validation tooling for fresh Pi installs and browser flows
- optional health/status improvements around disk, temperature, and long-running sync jobs

## Longer-Term Software Ideas

- media hosting (plex/jellyfin)
- NAS capabilities
- optional battery/power awareness in the dashboard
- [offline address search](#planned-offline-address-search)
- better local diagnostics and recovery tools in `/admin`
- a more formal update channel for catalogs and application assets
- additional packaged offline services beyond Kiwix and maps when they fit PrepperCache’s storage and reliability goals

### Planned offline address search

Start with an Oregon pilot. Offer address indexes as optional downloads alongside map regions. Keep the address index separate from the PMTiles archive so each can be updated independently. The basemap includes some house numbers, but it does not contain a complete address index for search. [Protomaps basemap layers](https://docs.protomaps.com/basemaps/layers)

1. **Build the index off the Pi.** Start with points from the U.S. Department of Transportation’s National Address Database. Verify coverage in Oregon, including rural areas. Preserve the house number, street, city, state, ZIP code, and coordinates. Normalize common suffixes and directionals for matching while keeping the original address for display. Package a compressed SQLite index per state with the source, data date, and coverage information. [National Address Database](https://www.transportation.gov/gis/national-address-database)
2. **Search on the Pi.** Use SQLite FTS5 to query the installed indexes. Rank exact matches first and use the selected map to favor nearby matches. Return a short list with city and state so people can tell identical street addresses apart. [SQLite FTS5](https://www.sqlite.org/fts5.html)
3. **Add a search field to the viewer.** Put **Find an address** above **Choose a map**. Selecting a result drops a pin and zooms to it. If the address index is missing, link to Map Settings to install it. If an index is present and no address matches, show a separate message.

The pilot passes when a known Oregon address can be found and pinned with the Pi disconnected from the internet and the browser still connected to the local network. Measure the compressed download size, installed size, search latency on the Pi, and coverage on a sample of urban and rural addresses before expanding.

Use address points for the first version. Consider Census TIGER/Line ranges later where point coverage is sparse. Label interpolated results **Approximate** because the location is estimated along a street segment. [Census address ranges](https://www.census.gov/newsroom/blogs/research-matters/2016/04/understanding-census-bureau-address-ranges.html)

## Operational Guidance

For release validation and fresh Pi testing, use:

- [release-checklist.md](/home/prepper/preppercache/docs/release-checklist.md)

For structural and UX principles, use:

- [architecture.md](/home/prepper/preppercache/docs/architecture.md)
