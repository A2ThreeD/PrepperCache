# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Browser smoke checks using local pages and deterministic fixtures.

from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import unittest

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]


class PortalFiles(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        if path.startswith("/images/"):
            return super().translate_path(path).replace("/web/images/", "/images/", 1)
        return super().translate_path(path)

    def log_message(self, *args):
        pass


class BrowserSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(PortalFiles, directory=ROOT / "web"))
        cls.addClassCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.addClassCleanup(server.shutdown)
        cls.origin = f"http://127.0.0.1:{server.server_port}"
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = playwright.chromium.launch()
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(6000)
        self.jobs = {"status": "idle"}
        self.completed_setup = True
        self.posts = []
        self.page.route("**/api/**", self.api_fixture)

    def api_fixture(self, route):
        path = route.request.url.split("?", 1)[0].removeprefix(self.origin)
        if route.request.method != "GET":
            self.posts.append(path)
            route.fulfill(status=405, json={"error": "Smoke tests cannot change settings."})
            return

        disk = {"total_bytes": 16_000_000_000, "used_bytes": 4_000_000_000, "free_bytes": 12_000_000_000}
        fixture = {
            "/api/state": {"setup_complete": self.completed_setup, "profile": {}, "storage": {"disk": disk}},
            "/api/apply": self.jobs,
            "/api/maps/sync": {"status": "idle"},
            "/api/content": {"mode": "full", "profile": "essential", "installed_count": 4, "automatic_updates": {"enabled": False}},
            "/api/content/catalog": {
                "items": [{
                    "path": "test/handbook.zim", "name": "handbook.zim", "category": "test",
                    "size_bytes": 1_000_000_000, "installed": False
                }],
                "source": {"label": "Test catalog"}
            },
        }
        route.fulfill(json=fixture.get(path, {}))

    def test_setup_opens_on_an_unconfigured_device(self):
        self.completed_setup = False
        self.page.goto(self.origin)
        expect(self.page.get_by_role("heading", name="PrepperCache Setup")).to_be_visible()
        expect(self.page.locator("#dashboard-view")).to_be_hidden()
        self.page.locator("#next-step-button").click()
        expect(self.page.locator(".step-panel.active")).to_contain_text("Wikipedia")

    def test_settings_links_back_refresh_and_download_review(self):
        self.page.goto(self.origin + "/admin/#about")
        expect(self.page.get_by_role("heading", name="About PrepperCache")).to_be_visible()
        self.page.locator('.section-tabs a[href="#content"]').click()
        expect(self.page).to_have_url(self.origin + "/admin/#content")
        self.page.go_back()
        expect(self.page.get_by_role("heading", name="About PrepperCache")).to_be_visible()
        self.page.locator('.section-tabs a[href="#content"]').click()
        self.page.reload()
        expect(self.page.get_by_role("heading", name="Installed Content")).to_be_visible()
        self.page.locator("#toggle-add-content-button").click()
        self.page.locator("#refresh-zim-catalog-button").click()
        self.page.locator("#zim-catalog-list input[type=checkbox]").check()
        self.page.locator("#download-selected-zims-button").click()
        review = self.page.get_by_role("dialog")
        expect(review).to_be_visible()
        expect(review).to_contain_text("handbook.zim")
        expect(review).to_contain_text("free afterward")
        review.get_by_role("button", name="Back").click()
        expect(review).to_be_hidden()
        self.assertEqual(self.posts, [])

    def test_content_set_review_blocks_oversized_download_and_names_failed_file(self):
        def content_set(route):
            too_large = route.request.url.endswith("comprehensive")
            route.fulfill(json={
                "download_count": 1, "required_bytes": 2 * 1024**3, "unknown_sizes": 0,
                "problem": "Not enough free space." if too_large else None,
                "disks": [{
                    "destinations": ["/library"], "required_bytes": 2 * 1024**3,
                    "free_bytes": (1 if too_large else 4) * 1024**3,
                }],
            })

        self.page.route("**/api/content/set?profile=*", content_set)
        self.jobs = {
            "status": "succeeded", "action": "refresh-content", "download_failed": 1,
            "failed_download_names": ["handbook.zim"], "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        self.page.goto(self.origin + "/admin/#content")
        expect(self.page.locator("#content-failed-files")).to_contain_text("handbook.zim")
        self.page.get_by_text("Change Content Set", exact=True).click()
        select = self.page.get_by_label("New content set")
        confirm = self.page.get_by_role("button", name="Apply Content Set")
        select.select_option("comprehensive")
        expect(self.page.locator("#content-set-estimate")).to_contain_text("Not enough free space")
        expect(confirm).to_be_disabled()
        select.select_option("standard")
        expect(self.page.locator("#content-set-estimate")).to_contain_text("2.0 GB estimated additional space")
        expect(confirm).to_be_enabled()
        self.assertEqual(self.posts, [])

    def test_download_progress_survives_navigation_and_clears(self):
        self.jobs = {
            "status": "running", "action": "full", "progress_percent": 62,
            "step": "Downloading selected Kiwix content", "downloads_active": True,
            "download_current": 2, "download_total": 8,
        }
        self.page.goto(self.origin + "/admin/#content")
        expect(self.page.locator("#content-library-details")).to_be_hidden()
        popup = self.page.locator("#preppercache-job-status aside")
        expect(popup).to_contain_text("62%")
        self.page.goto(self.origin)
        expect(popup).to_contain_text("62%")
        self.jobs = {"status": "succeeded", "action": "full"}
        expect(popup).to_be_hidden()
        self.page.goto(self.origin + "/admin/#content")
        expect(self.page.locator("#content-storage-summary")).to_contain_text("4 ZIMs")

    def mock_map_assets(self, files):
        self.page.route("**/maps/state.json", lambda route: route.fulfill(json={
            "available_files": files,
            "state_regions": {"state-oregon.pmtiles": {"id": "pacific", "name": "Pacific", "title": "Oregon"}},
        }))
        self.page.route("**/maps/config.json", lambda route: route.fulfill(json={}))
        assets = {
            "maplibre-gl.js": """
                const maplibregl = {
                    addProtocol() {}, NavigationControl: class {},
                    Map: class {
                        constructor(options) {
                            document.querySelector("#map").dataset.testSources =
                                Object.keys(options.style.sources).length;
                        }
                        addControl() {} remove() {} on() {}
                        getCenter() { return {lat: 0, wrap: () => ({lng: 0})}; }
                        getZoom() { return 4; }
                    }
                };
            """,
            "pmtiles.js": """
                const pmtiles = { TileType: {Mvt: 1}, Protocol: class { add() {} }, PMTiles: class {
                    async getHeader() {
                        if (!window.testMapsInstalled) throw new Error("Archive unavailable");
                        return {tileType: 1, maxZoom: 14};
                    }
                } };
            """,
            "basemaps.js": "const basemaps = { layers() { return []; }, namedFlavor() {} };",
        }
        self.page.add_init_script(f"window.testMapsInstalled = {str(bool(files)).lower()}")
        self.page.route(re.compile(r"/maps/vendor/.*\.js$"), lambda route: route.fulfill(
            content_type="text/javascript", body=assets[route.request.url.rsplit("/", 1)[-1]]
        ))

    def test_viewer_switches_maps_and_remembers_the_choice(self):
        self.mock_map_assets(["basemap.pmtiles", "state-oregon.pmtiles"])
        self.page.goto(self.origin + "/maps/")
        picker = self.page.get_by_label("Choose a map")
        expect(self.page.locator("#map")).to_have_attribute("data-test-sources", "2")
        expect(picker.locator("optgroup")).to_have_count(2)
        picker.select_option("map:state-oregon.pmtiles")
        expect(self.page.locator("#map")).to_have_attribute("data-test-sources", "1")
        self.page.reload()
        expect(picker).to_have_value("map:state-oregon.pmtiles")
        expect(self.page.locator("#map")).to_have_attribute("data-test-sources", "1")
        expect(self.page.locator(".coordinates")).not_to_have_attribute("open", "")
        self.page.get_by_text("Go to coordinates", exact=True).click()
        expect(self.page.get_by_label("Latitude")).to_be_visible()

    def test_viewer_without_maps_offers_settings(self):
        self.mock_map_assets([])
        self.page.goto(self.origin + "/maps/")
        expect(self.page.locator("#map-select")).to_be_disabled()
        expect(self.page.get_by_text("No offline maps are installed.", exact=False)).to_be_visible()
        expect(self.page.locator("#preppercache-job-status").get_by_role("link", name="Map Settings")).to_have_attribute("href", "/admin/#maps")


if __name__ == "__main__":
    unittest.main(verbosity=2)
