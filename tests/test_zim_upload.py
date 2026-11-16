# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Regression tests for publishing complete uploads without replacing archives.
# SPDX-License-Identifier: MIT

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
import unittest
from unittest.mock import Mock

from test_kiwix_catalog import portal


class ZimUploadTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Path(temporary.name)
        state = portal.PortalState.__new__(portal.PortalState)
        state.zim_upload_lock = Lock()
        state.apply_lock = Lock()
        state.preferred_zim_install_dir = lambda: self.library
        state.kiwix_library_dir = lambda: self.library
        state.install_destinations = lambda _: [{"path": str(self.library), "mounted": True}]
        state.sync_external_content_links = Mock()
        self.state = state
        self.zim = b"ZIM\x04" + bytes(128)

    def test_upload_publishes_file_and_keeps_existing_copy(self):
        self.state.upload_zim("book.ZIM", len(self.zim), BytesIO(self.zim))
        self.assertEqual((self.library / "book.zim").read_bytes(), self.zim)
        with self.assertRaises(FileExistsError):
            self.state.upload_zim("book.zim", len(self.zim), BytesIO(self.zim[::-1]))
        self.assertEqual((self.library / "book.zim").read_bytes(), self.zim)

    def test_rejects_bad_header_and_cleans_up_truncated_transfer(self):
        with self.assertRaisesRegex(ValueError, "valid ZIM header"):
            self.state.upload_zim("invalid.zim", len(self.zim), BytesIO(bytes(len(self.zim))))
        with self.assertRaisesRegex(ValueError, "full file arrived"):
            self.state.upload_zim("partial.zim", len(self.zim) + 1, BytesIO(self.zim))
        self.assertEqual(list(self.library.glob("*.zim")), [])
        self.assertEqual(list(self.library.glob(".preppercache-upload-tmp/*")), [])

    def test_rejects_paths_outside_the_library(self):
        for name in ("../book.zim", "/book.zim", "sub\\book.zim"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "filename"):
                self.state.upload_zim(name, len(self.zim), BytesIO(self.zim))
