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
        self.content_profile = "essential"
        self.page.route("**/api/**", self.api_fixture)

    def api_fixture(self, route):
        path = route.request.url.split("?", 1)[0].removeprefix(self.origin)
        if route.request.method != "GET":
            self.posts.append(path)
            route.fulfill(status=405, json={"error": "Smoke tests cannot change settings."})
            return

        disk = {"total_bytes": 16_000_000_000, "used_bytes": 4_000_000_000, "free_bytes": 12_000_000_000}
        fixture = {
            "/api/admin/session": {"enabled": False, "authenticated": True},
            "/api/state": {"setup_complete": self.completed_setup, "profile": {}, "storage": {"disk": disk}},
            "/api/apply": self.jobs,
            "/api/maps/sync": {"status": "idle"},
            "/api/content": {"mode": "full", "profile": self.content_profile, "installed_count": 4, "automatic_updates": {"enabled": False}},
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

    def test_optional_admin_password_can_be_enabled_unlocked_and_disabled(self):
        session = {"enabled": False, "authenticated": True}

        def admin(route):
            action = route.request.url.split("/")[-1]
            if action == "password":
                session.update(enabled=route.request.post_data_json["enabled"], authenticated=True)
            elif action == "logout":
                session["authenticated"] = False
            elif action == "login":
                if route.request.post_data_json != {"password": "test password"}:
                    route.fulfill(status=403, json={"error": "Incorrect admin password."})
                    return
                session["authenticated"] = True
            route.fulfill(json=session)

        self.page.route("**/api/admin/*", admin)
        self.page.goto(self.origin + "/admin/#system")
        checkbox = self.page.get_by_label("Require an admin password")
        indicator = self.page.locator("#admin-protection-status")
        save = self.page.get_by_role("button", name="Save Admin Access")
        expect(indicator).to_have_attribute("data-tone", "error")
        expect(save).not_to_have_class(re.compile(r"\bprimary\b"))
        expect(checkbox).not_to_be_checked()
        checkbox.check()
        expect(save).to_have_class(re.compile(r"\bprimary\b"))
        expect(indicator).to_have_attribute("data-tone", "error")
        self.page.get_by_label("New admin password").fill("test password")
        self.page.get_by_label("Confirm admin password").fill("different password")
        self.page.get_by_role("button", name="Save Admin Access").click()
        expect(self.page.locator("#admin-password-status")).to_have_text("The new passwords do not match.")
        self.page.get_by_label("Confirm admin password").fill("test password")
        self.page.get_by_role("button", name="Save Admin Access").click()
        expect(self.page.locator("#admin-password-status")).to_have_text("Admin password saved.")
        expect(indicator).to_have_attribute("data-tone", "good")
        expect(save).not_to_have_class(re.compile(r"\bprimary\b"))
        self.page.get_by_label("New admin password").fill("new test password")
        expect(save).to_have_class(re.compile(r"\bprimary\b"))
        self.page.get_by_label("New admin password").clear()
        expect(save).not_to_have_class(re.compile(r"\bprimary\b"))

        self.page.get_by_role("button", name="Sign Out").click()
        expect(self.page.locator("#admin-settings")).to_be_hidden()
        expect(self.page.locator("#admin-login-form input")).to_have_count(1)
        self.page.get_by_label("Admin password", exact=True).fill("wrong password")
        self.page.get_by_role("button", name="Login").click()
        expect(self.page.locator("#admin-gate-status")).to_have_text("Incorrect admin password.")
        self.page.get_by_label("Admin password", exact=True).fill("test password")
        self.page.get_by_role("button", name="Login").click()
        expect(checkbox).to_be_checked()

        checkbox.uncheck()
        expect(save).to_have_class(re.compile(r"\bprimary\b"))
        expect(indicator).to_have_attribute("data-tone", "good")
        self.page.get_by_label("Current admin password").fill("test password")
        self.page.get_by_role("button", name="Save Admin Access").click()
        expect(self.page.locator("#admin-password-status")).to_have_text("Admin password protection is off.")
        expect(indicator).to_have_attribute("data-tone", "error")
        expect(save).not_to_have_class(re.compile(r"\bprimary\b"))
        self.page.reload()
        expect(self.page.get_by_role("heading", name="Admin Access")).to_be_visible()

    def test_locked_admin_leaves_home_dashboard_available(self):
        self.page.route("**/api/state", lambda route: route.fulfill(json={"admin_locked": True}))
        self.page.route("**/api/apply", lambda route: route.fulfill(status=401, json={}))
        self.page.goto(self.origin)
        expect(self.page.locator("#dashboard-view")).to_be_visible()
        expect(self.page.get_by_role("button", name="Search", exact=True)).to_be_visible()
        self.page.goto(self.origin + "/?mode=setup")
        expect(self.page).to_have_url(self.origin + "/admin/")

    def test_settings_links_back_refresh_and_download_review(self):
        self.page.goto(self.origin + "/admin/#about")
        expect(self.page.get_by_role("heading", name="About PrepperCache")).to_be_visible()
        self.page.get_by_role("button", name="Menu").click()
        self.page.locator('.section-tabs a[href="#content"]').click()
        expect(self.page).to_have_url(self.origin + "/admin/#content")
        self.page.go_back()
        expect(self.page.get_by_role("heading", name="About PrepperCache")).to_be_visible()
        self.page.get_by_role("button", name="Menu").click()
        self.page.locator('.section-tabs a[href="#content"]').click()
        self.page.reload()
        expect(self.page.get_by_role("heading", name="Installed Content")).to_be_visible()
        expect(self.page.locator("#content-progress-shell")).to_be_hidden()
        expect(self.page.locator("#content-local-list")).to_be_hidden()
        self.page.locator("#installed-zim-heading").click()
        expect(self.page.locator("#content-local-list")).to_be_visible()
        self.page.locator("#toggle-add-content-button").click()
        expect(self.page.locator("#content-local-list")).to_be_hidden()
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

    def test_content_set_review_blocks_oversized_download_and_requires_downgrade_confirmation(self):
        self.content_profile = "standard"

        def content_set(route):
            too_large = route.request.url.endswith("comprehensive")
            route.fulfill(json={
                "download_count": int(too_large), "required_bytes": 2 * 1024**3, "unknown_sizes": 0,
                "problem": "Not enough free space." if too_large else None,
                "disks": [{"destinations": ["/library"], "required_bytes": 2 * 1024**3, "free_bytes": 1024**3}] if too_large else [],
                "downgrade": not too_large, "review_id": "preview-123",
                "added_titles": ["Repair Manual"] if too_large else [],
                "remove_files": [{"path": "/library/extra.zim"}], "remove_bytes": 1024**3,
            })

        self.page.route("**/api/content/set?profile=*", content_set)
        self.jobs = {
            "status": "succeeded", "action": "refresh-content", "download_failed": 1,
            "failed_download_names": ["handbook.zim"], "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        self.page.goto(self.origin + "/admin/#content")
        expect(self.page.locator("#content-failed-files")).to_contain_text("handbook.zim")
        expect(self.page.get_by_role("button", name="Standard", exact=True)).to_have_attribute("aria-pressed", "true")
        self.page.get_by_role("button", name="Comprehensive", exact=True).click()
        review = self.page.get_by_role("dialog")
        expect(review).to_contain_text("Not enough free space")
        expect(review.get_by_role("button", name="Apply Content Set")).to_be_disabled()
        review.get_by_text("What’s included in addition to your current set").click()
        expect(review.get_by_text("Repair Manual")).to_be_visible()
        review.get_by_role("button", name="Cancel").click()
        self.page.get_by_role("button", name="Essential", exact=True).click()
        confirm = review.get_by_role("button", name="Confirm Downgrade")
        expect(confirm).to_be_disabled()
        review.get_by_text("Files to remove", exact=False).click()
        expect(review.get_by_text("/library/extra.zim")).to_be_visible()
        review.get_by_role("checkbox").check()
        expect(confirm).to_be_enabled()
        self.assertEqual(self.posts, [])

    def test_download_and_indexing_failures_have_separate_recovery_steps(self):
        self.jobs = {
            "status": "succeeded", "action": "refresh-content", "can_retry_download": True,
            "failed_download_names": ["interrupted.zim"],
            "rejected_zim_names": ["broken.zim", "old.zim"],
        }
        self.page.goto(self.origin + "/admin/#content")
        downloads = self.page.get_by_role("list", name="Failed downloads")
        rejected = self.page.get_by_role("list", name="Files Kiwix could not index")
        expect(downloads.get_by_role("listitem")).to_have_text(["interrupted.zim"])
        expect(rejected.get_by_role("listitem")).to_have_text(["broken.zim", "old.zim"])
        expect(self.page.get_by_role("button", name="Resume Content Download")).to_be_visible()
        expect(self.page.locator("#content-index-issues")).to_contain_text("Rebuild Kiwix Library")

    def test_interrupted_content_can_be_resumed_from_settings(self):
        self.jobs = {
            "status": "running", "action": "change-content-set", "can_retry_download": True,
        }

        def resume(route):
            self.assertEqual(route.request.method, "POST")
            self.jobs = {"status": "running", "action": "change-content-set", "can_retry_download": False}
            route.fulfill(status=202, json=self.jobs)

        self.page.route("**/api/content/resume-download", resume)
        self.page.goto(self.origin + "/admin/#content")
        expect(self.page.locator("#content-action-status")).to_contain_text("Content download interrupted")
        button = self.page.get_by_role("button", name="Resume Content Download")
        button.click()
        expect(button).to_be_hidden()
        expect(self.page.locator("#content-action-status")).to_contain_text("Working on your content")

    def test_file_management_rebuilds_kiwix_without_leaving_the_panel(self):
        actions = []

        def apply(route):
            if route.request.method == "POST":
                actions.append(route.request.post_data_json)
                self.jobs = {"status": "running", "action": "rebuild-library"}
                route.fulfill(status=202, json=self.jobs)
            else:
                route.fulfill(json=self.jobs)

        self.page.route("**/api/apply", apply)
        self.page.goto(self.origin + "/admin/#files")
        button = self.page.get_by_role("button", name="Rebuild Kiwix Library")
        expect(button).to_be_enabled()
        button.click()
        status = self.page.locator("#files-rebuild-status")
        expect(status).to_have_text("Rebuilding Kiwix library…")
        expect(button).to_be_disabled()
        self.jobs = {"status": "succeeded", "action": "rebuild-library", "rejected_zim_names": []}
        expect(status).to_have_text("Kiwix library rebuilt.", timeout=10000)
        expect(button).to_be_enabled()
        expect(self.page).to_have_url(self.origin + "/admin/#files")
        self.assertEqual(actions, [{"action": "rebuild-library"}])

    def test_upload_zim_from_content_and_file_management(self):
        uploaded = []

        def upload(route):
            uploaded.append(route.request.post_data_buffer)
            route.fulfill(status=201, json={"name": "local.zim"})

        self.page.route("**/api/content/upload?*", upload)
        self.page.route("**/api/apply", lambda route: route.fulfill(
            status=202 if route.request.method == "POST" else 200,
            json={"status": "running", "action": "rebuild-library"} if route.request.method == "POST" else self.jobs,
        ))
        for panel in ("content", "files"):
            self.page.goto(self.origin + f"/admin/#{panel}")
            section = self.page.locator(".panel-group.active [data-zim-upload]")
            expect(section.get_by_role("button", name="Choose ZIM Files")).to_be_visible()
            if panel == "content":
                section.locator('input[type="file"]').set_input_files({
                    "name": "local.zim", "mimeType": "application/octet-stream", "buffer": b"example archive",
                })
            else:
                transfer = self.page.evaluate_handle("""() => {
                    const transfer = new DataTransfer();
                    transfer.items.add(new File(["example archive"], "local.zim", {type: "application/octet-stream"}));
                    return transfer;
                }""")
                section.locator(".zim-upload-drop").dispatch_event("drop", {"dataTransfer": transfer})
                transfer.dispose()
            expect(section.get_by_role("status")).to_contain_text("Kiwix library rebuild started")
        self.assertEqual(uploaded, [b"example archive", b"example archive"])

    def test_settings_opens_dashboard_and_phone_menu_navigates(self):
        self.page.goto(self.origin + "/admin/")
        expect(self.page.get_by_role("heading", name="Welcome to PrepperCache")).to_be_visible()
        expect(self.page.locator("#dashboard-library")).to_have_text("4")
        expect(self.page.get_by_label("Settings Layout")).to_have_count(0)
        navigation = self.page.get_by_role("navigation", name="Settings sections")
        expect(navigation).to_be_hidden()
        self.page.get_by_role("button", name="Menu").click()
        navigation.get_by_role("link", name="About").click()
        expect(navigation).to_be_hidden()
        expect(self.page.get_by_role("heading", name="About PrepperCache")).to_be_visible()

    def test_download_progress_survives_navigation_and_clears(self):
        self.jobs = {
            "status": "running", "action": "full", "progress_percent": 62,
            "step": "Downloading selected Kiwix content", "downloads_active": True,
            "download_current": 2, "download_total": 8,
        }
        self.page.goto(self.origin + "/admin/#content")
        expect(self.page.locator("#content-library-details")).to_be_hidden()
        expect(self.page.locator("#content-progress-shell")).to_be_visible()
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
                            const canvas = document.querySelector("#map");
                            const sources = Object.values(options.style.sources);
                            canvas.dataset.testSources = sources.length;
                            canvas.dataset.testCameraZoom = options.maxZoom;
                            canvas.dataset.testTileZoom = sources[0].maxzoom;
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
        expect(self.page.locator("#map")).to_have_attribute("data-test-camera-zoom", "20")
        expect(self.page.locator("#map")).to_have_attribute("data-test-tile-zoom", "14")
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
