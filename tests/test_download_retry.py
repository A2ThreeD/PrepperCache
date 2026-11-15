# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Regression coverage for retrying a partially successful download batch.

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from test_kiwix_catalog import portal


class DownloadRetryTests(unittest.TestCase):
    def test_retry_reuses_failed_urls_without_downloading_successes_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "extra.txt"
            succeeded = "https://example.org/test/ready.zim"
            failed = "https://example.org/test/retry.zim"
            manifest.write_text(f"{succeeded}\n{failed}\n")
            state = portal.PortalState.__new__(portal.PortalState)
            state.apply_lock = threading.Lock()
            state.map_sync_lock = threading.Lock()
            state.apply_log_file = root / "apply.log"
            state.apply_log_file.write_text("PROGRESS_DOWNLOAD_TOTAL|2\nPROGRESS_DOWNLOAD_FAILED|retry.zim\n")
            progress = state.parse_apply_progress({"status": "succeeded"})
            state.load_apply_state = lambda: {
                "status": "succeeded", "action": "download-extra-content", **progress
            }
            state.extra_zim_manifest_path = lambda: manifest
            state.launch_apply = Mock(return_value={"status": "running"})

            self.assertEqual(state.retry_extra_content()["status"], "running")
            self.assertEqual(manifest.read_text(), failed + "\n")
            state.launch_apply.assert_called_once_with("download-extra-content", clear_log=True)
