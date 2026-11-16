# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Regression coverage for content set changes and available disk space.

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_kiwix_catalog import portal


class ContentSetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.library = root / "library"
        self.wikipedia = root / "wikipedia"
        self.library.mkdir()
        self.wikipedia.mkdir()

        state = self.state = portal.PortalState.__new__(portal.PortalState)
        state.data_dir = root
        state.preppercache_env = root / "preppercache.env"
        state.preppercache_env.write_text("PREPPERCACHE_ZIM_PROFILE=essential\n")
        state.maps_env = lambda: {"PREPPERCACHE_WIKIPEDIA_OPTION": "top-mini"}
        state.preferred_zim_install_dir = lambda: self.library
        state.preferred_wikipedia_install_dir = lambda: self.wikipedia
        state.install_destinations = lambda _: [
            {"path": str(path), "mounted": True} for path in (self.library, self.wikipedia)
        ]
        state.list_installed_zims = lambda: [{"name": "already.zim"}]
        self.resources = [
            {"filename": name, "size_mb": 10, "url": f"https://example.org/zim/test/{name}"}
            for name in ("already.zim", "extra.zim", "wikipedia_test.zim")
        ]
        state.curated_resources = Mock(return_value=self.resources)
        state.content_status = lambda: portal.read_env_file(state.preppercache_env)
        state.load_apply_state = lambda: {"status": "idle"}
        state.launch_apply = Mock(return_value={"status": "running"})
        state.apply_lock = threading.Lock()
        state.map_sync_lock = threading.Lock()

    def test_combines_space_for_two_directories_on_the_same_disk(self):
        mb = 1024**2
        with patch.object(portal.shutil, "disk_usage", return_value=Mock(free=15 * mb)):
            plan, _ = self.state.content_set_plan("standard")
            self.assertEqual(plan["required_bytes"], 20 * mb)
            self.assertEqual(len(plan["disks"]), 1)
            self.assertIn("Not enough free space", plan["problem"])
            with self.assertRaisesRegex(ValueError, "Not enough free space"):
                self.state.change_content_set("standard")
        self.assertEqual(self.state.content_status()["PREPPERCACHE_ZIM_PROFILE"], "essential")
        self.state.launch_apply.assert_not_called()

    def test_change_queues_only_missing_files_and_keeps_the_existing_library(self):
        existing = self.library / "already.zim"
        existing.touch()
        with patch.object(portal.shutil, "disk_usage", return_value=Mock(free=1024**3)):
            result = self.state.change_content_set("standard")
        self.assertEqual(result["content"]["PREPPERCACHE_ZIM_PROFILE"], "standard")
        self.assertTrue(existing.is_file())
        manifest = self.state.data_dir / "content-set-urls.txt"
        self.assertEqual(manifest.read_text().splitlines(), [item["url"] for item in self.resources[1:]])
        self.state.launch_apply.assert_called_once_with("change-content-set", clear_log=True)

    def test_switch_can_complete_without_starting_a_download(self):
        self.state.curated_resources.return_value = self.resources[:1]
        result = self.state.change_content_set("comprehensive")
        self.assertIsNone(result["apply"])
        self.assertEqual(result["content"]["PREPPERCACHE_ZIM_PROFILE"], "comprehensive")
        self.state.launch_apply.assert_not_called()

    def test_downgrade_requires_confirmation_and_rechecks_before_deleting(self):
        state = self.state
        state.maps_env = lambda: {"PREPPERCACHE_ZIM_PROFILE": "standard"}
        state.kiwix_library_dir = lambda: self.library
        state.sync_external_content_links = Mock()
        state.custom_zim_selection_path = lambda: state.data_dir / "selection.json"
        state.curated_resources.side_effect = lambda tier, _: self.resources[:1] if tier == "essential" else self.resources
        kept = self.library / "already.zim"
        extra = self.library / "extra.zim"
        external_extra = self.wikipedia / "extra.zim"
        for path in (kept, extra, external_extra):
            path.write_bytes(b"test archive")
        plan, _ = state.content_set_plan("essential")
        self.assertTrue(plan["downgrade"])
        self.assertEqual(len(plan["remove_files"]), 2)
        with self.assertRaisesRegex(ValueError, "confirm"):
            state.change_content_set("essential")
        self.assertTrue(extra.exists())
        self.assertTrue(external_extra.exists())
        state.change_content_set("essential", plan["review_id"])
        state.launch_apply.assert_called_once_with("downgrade-content-set", clear_log=True)
        job = portal.read_json(state.data_dir / "content-downgrade.json", {})
        state.list_installed_zims = lambda: []
        with self.assertRaisesRegex(ValueError, "incomplete"):
            state.remove_downgrade_files(job)
        state.list_installed_zims = lambda: [{"name": "already.zim"}]
        extra.write_bytes(b"new file")
        with self.assertRaisesRegex(ValueError, "changed"):
            state.remove_downgrade_files(job)
        self.assertTrue(external_extra.exists())
        current, _ = state.content_set_plan("essential")
        state.remove_downgrade_files({"required": ["already.zim"], "remove": current["_removals"]})
        self.assertTrue(kept.exists())
        self.assertFalse(extra.exists())
        self.assertFalse(external_extra.exists())
