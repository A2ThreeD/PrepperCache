# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Regression coverage for content download recovery. SPDX-License-Identifier: MIT

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
import unittest
from unittest.mock import Mock

from test_kiwix_catalog import portal


class ContentRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = portal.PortalState.__new__(portal.PortalState)
        state.repo_root = Path(temporary.name)
        state.data_dir = state.repo_root
        state.preppercache_env = state.data_dir / "preppercache.env"
        state.apply_state_file = state.data_dir / "apply.json"
        state.apply_log_file = state.data_dir / "apply.log"
        state.apply_log_file.touch()
        state.apply_lock = Lock()
        state.map_sync_lock = Lock()
        state.apply_thread = None
        state.launch_apply = Mock(return_value={"status": "running"})
        self.state = state

    def test_interrupted_setup_resumes_at_download_step(self):
        self.state.save_apply_state({"status": "running", "action": "full", "current_step_index": 5})
        self.state.load_map_sync_state = Mock(return_value={"status": "idle"})
        self.state.recover_interrupted_apply()
        launch = self.state.launch_apply.call_args
        self.assertEqual(launch.args, ("full",))
        self.assertEqual(launch.kwargs["start_index"], 5)
        self.assertFalse(launch.kwargs["clear_log"])

    def test_rejected_zims_survive_log_rotation_and_clear_after_a_clean_rebuild(self):
        library = self.state.data_dir / "custom-library.xml"
        self.state.preppercache_env.write_text(f"KIWIX_LIBRARY_XML={library}\n")
        report = library.with_suffix(".xml.rejected")
        rejected = [f"archive-{number:02}.zim" for number in range(40)]
        report.write_bytes(b"".join(name.encode() + b"\0" for name in rejected))
        self.state.apply_log_file.write_text("Kiwix could not index: old.zim\n" + "Other output\n" * 200)
        self.assertEqual(self.state.load_apply_state()["rejected_zim_names"], rejected)

        report.write_bytes(b"")
        self.state.apply_log_file.write_text("Kiwix could not index: old.zim\n")
        self.assertEqual(self.state.load_apply_state()["rejected_zim_names"], [])

    def test_indexing_failure_alone_does_not_offer_to_resume_a_download(self):
        self.state.apply_log_file.write_text(
            "PROGRESS_DOWNLOAD_TOTAL|1\n"
            "Kiwix could not index any ZIM files. Kept the existing library index.\n"
        )
        state = self.state.save_apply_state({
            "status": "failed", "action": "refresh-content", "step": "Downloading selected Kiwix content",
        })
        self.assertFalse(state["can_retry_download"])

        with self.state.apply_log_file.open("a") as log:
            log.write("PROGRESS_DOWNLOAD_FAILED|other.zim\n")
        self.assertTrue(self.state.load_apply_state()["can_retry_download"])

    def test_network_failures_retry_with_a_limit_and_manual_override(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.state.apply_log_file.write_text(
            "PROGRESS_DOWNLOAD_TOTAL|1\nFailed: sample.zim (wget exited with code 4)\n"
            "PROGRESS_DOWNLOAD_FAILED|sample.zim\n"
        )
        self.state.save_apply_state({
            "status": "succeeded", "action": "change-content-set", "finished_at": past,
        })
        self.state.recover_interrupted_apply()
        launch = self.state.launch_apply.call_args.kwargs
        self.assertEqual(launch["retry_names"], ["sample.zim"])
        self.assertEqual(launch["automatic_retries"], 1)
        self.state.save_apply_state({"automatic_retries": 3})
        self.state.launch_apply.reset_mock()
        self.assertIsNone(self.state.recover_interrupted_apply())
        self.state.launch_apply.assert_not_called()
        self.state.recover_interrupted_apply(manual=True)
        self.assertEqual(self.state.launch_apply.call_args.kwargs["automatic_retries"], 0)

    def test_permanent_failure_and_active_job_do_not_restart_automatically(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.state.apply_log_file.write_text(
            "Failed: old.zim (404 Not Found)\nPROGRESS_DOWNLOAD_FAILED|old.zim\n"
        )
        self.state.save_apply_state({
            "status": "succeeded", "action": "change-content-set", "finished_at": past,
        })
        self.assertIsNone(self.state.recover_interrupted_apply())
        self.state.apply_thread = Mock()
        self.state.apply_thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, "already running"):
            self.state.recover_interrupted_apply(manual=True)
        self.state.launch_apply.assert_not_called()
