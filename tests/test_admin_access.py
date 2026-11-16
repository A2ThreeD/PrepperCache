# PrepperCache
# Copyright (c) 2026 Aaron Morris / A2ThreeD.
# Regression checks for optional Settings access. SPDX-License-Identifier: MIT

from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from test_kiwix_catalog import portal


class AdminAccessTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "admin-password.json"
        iterations = patch.object(portal.AdminAccess, "HASH_ITERATIONS", 1000)
        self.addCleanup(iterations.stop)
        iterations.start()
        self.access = portal.AdminAccess(self.path)
        self.password = "initial admin password"

    def test_enable_change_restart_and_disable(self):
        self.assertEqual(self.access.status(""), {"enabled": False, "authenticated": True})
        token = self.access.configure("", True, "", self.password)
        self.assertTrue(self.access.status(token)["authenticated"])
        self.assertFalse(self.access.status("")["authenticated"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn(self.password, self.path.read_text())

        restarted = portal.AdminAccess(self.path)
        self.assertFalse(restarted.status(token)["authenticated"])
        with self.assertRaises(portal.AdminAccessError):
            restarted.configure(token, False, self.password, "")
        active = restarted.login(self.password, "")
        other = restarted.login(self.password, "")
        with self.assertRaisesRegex(portal.AdminAccessError, "Incorrect"):
            restarted.configure(active, False, "wrong password", "")

        new_password = "changed admin password"
        rotated = restarted.configure(active, True, self.password, new_password)
        self.assertFalse(restarted.status(active)["authenticated"])
        self.assertFalse(restarted.status(other)["authenticated"])
        restarted.logout(rotated)
        self.assertFalse(restarted.status(rotated)["authenticated"])
        active = restarted.login(new_password, "")
        self.assertEqual(restarted.configure(active, False, new_password, ""), "")
        self.assertFalse(self.path.exists())
        self.assertTrue(portal.AdminAccess(self.path).status("")["authenticated"])

    def test_bad_passwords_are_throttled(self):
        self.access.configure("", True, "", self.password)
        for _ in range(self.access.ATTEMPT_LIMIT):
            with self.assertRaisesRegex(portal.AdminAccessError, "Incorrect"):
                self.access.login("wrong password", "")
        with self.assertRaises(portal.AdminAccessError) as error:
            self.access.login(self.password, "")
        self.assertEqual(error.exception.status, 429)
        with patch.object(portal.time, "monotonic", return_value=self.access.failures[-1] + 61):
            self.assertTrue(self.access.login(self.password, ""))

    def handler(self, path, body=None, **headers):
        handler = portal.PortalHandler.__new__(portal.PortalHandler)
        handler.path = path
        handler.portal_state = Mock(admin_access=self.access)
        handler.send_json = Mock()
        handler.headers = Message()
        handler.headers["Host"] = "preppercache.local"
        for key, value in headers.items():
            handler.headers[key.replace("_", "-")] = value
        if body is not None:
            encoded = json.dumps(body).encode()
            handler.headers["Content-Type"] = "application/json"
            handler.headers["Content-Length"] = str(len(encoded))
            handler.rfile = BytesIO(encoded)
        return handler

    def test_http_gate_blocks_private_reads_and_mutations_before_dispatch(self):
        token = self.access.configure("", True, "", self.password)
        for path in ("/api/files", "/api/system/access-point", "/api/diagnostics/logs"):
            handler = self.handler(path)
            handler.do_GET()
            self.assertEqual(handler.send_json.call_args.kwargs["status"], 401)
        for path in ("/api/setup", "/api/system/power", "/api/system/storage/prepare", "/api/content/upload?name=test.zim"):
            handler = self.handler(path)
            handler.do_POST()
            self.assertEqual(handler.send_json.call_args.kwargs["status"], 401)

        guest = self.handler("/api/state")
        guest.do_GET()
        self.assertTrue(guest.send_json.call_args.args[0]["admin_locked"])
        guest.portal_state.load_state.assert_not_called()

        admin = self.handler("/api/files", Cookie=f"{portal.AdminAccess.COOKIE}={token}")
        admin.portal_state.file_manager_list.return_value = {"entries": []}
        admin.do_GET()
        admin.portal_state.file_manager_list.assert_called_once()

    def test_http_password_endpoints_require_same_origin_and_set_a_session_cookie(self):
        endpoint = "/api/admin/password"
        cross_origin = self.handler(endpoint, {"enabled": True, "new_password": self.password},
                                    Origin="http://elsewhere.local")
        cross_origin.do_POST()
        self.assertEqual(cross_origin.send_json.call_args.kwargs["status"], 403)
        self.assertFalse(self.access.status("")["enabled"])

        enable = self.handler(endpoint, {"enabled": True, "new_password": self.password},
                              Origin="http://preppercache.local")
        enable.do_POST()
        cookie = enable.send_json.call_args.kwargs["cookie"]
        self.assertIn("Path=/api/", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn(self.password, cookie)

        disable = self.handler(endpoint, {"enabled": False, "current_password": self.password})
        disable.do_POST()
        self.assertEqual(disable.send_json.call_args.kwargs["status"], 401)
