"""认证存储：哈希往返、损坏失败关闭、首次默认开关与 CLI 初始化。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.auth import (AuthConfigError, AuthState, AuthStore, hash_password,
                               verify_password)


class PasswordHashTests(unittest.TestCase):
    def test_round_trip_and_wrong_password(self):
        encoded = hash_password("correct horse battery")
        self.assertTrue(encoded.startswith("scrypt$v1$"))
        self.assertTrue(verify_password("correct horse battery", encoded))
        self.assertFalse(verify_password("wrong password", encoded))

    def test_rejects_short_password(self):
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_tampered_parameters_are_rejected(self):
        encoded = hash_password("correct horse battery")
        parts = encoded.split("$")
        parts[2] = str(2 ** 21)          # 超出允许成本，防拖垮进程
        self.assertFalse(verify_password("correct horse battery", "$".join(parts)))
        self.assertFalse(verify_password("x", "not-a-hash"))


class AuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_file_uses_first_run_default(self):
        self.assertTrue(AuthStore(self.state, default_enabled=True).load().enabled)
        self.assertFalse(AuthStore(self.state, default_enabled=False).load().enabled)

    def test_saved_state_round_trips_and_is_private(self):
        store = AuthStore(self.state)
        store.save(AuthState(True, "admin", hash_password("secret-pass-1"), 3))
        loaded = AuthStore(self.state).load()
        self.assertEqual((loaded.enabled, loaded.username, loaded.auth_revision),
                         (True, "admin", 3))
        if os.name != "nt":
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("secret-pass-1", store.path.read_text(encoding="utf-8"))

    def test_corrupt_or_incomplete_file_fails_closed(self):
        store = AuthStore(self.state)
        for payload in ("{", json.dumps({"enabled": True}),
                        json.dumps({"enabled": True, "username": "a",
                                    "password_hash": "", "auth_revision": 1})):
            with self.subTest(payload=payload):
                store.path.write_text(payload, encoding="utf-8")
                with self.assertRaises(AuthConfigError):
                    store.load()

    def test_verify_and_change_require_current_password(self):
        store = AuthStore(self.state)
        store.set_initial("admin", "secret-pass-1")
        self.assertTrue(store.verify("admin", "secret-pass-1"))
        self.assertFalse(store.verify("admin", "nope"))
        with self.assertRaises(AuthConfigError):
            store.change("wrong", new_password="secret-pass-2")
        changed = store.change("secret-pass-1", new_password="secret-pass-2")
        self.assertEqual(changed.auth_revision, 2)
        self.assertTrue(store.verify("admin", "secret-pass-2"))


class InitAuthCliTests(unittest.TestCase):
    def test_cli_writes_state_without_listening(self):
        import server
        with tempfile.TemporaryDirectory() as temp:
            store = AuthStore(Path(temp))
            with patch.object(server, "AUTH", store), \
                 patch.object(server, "StateLock", autospec=True) as lock, \
                 patch.object(server, "ThreadingHTTPServer", create=True) as httpd, \
                 patch.object(server, "LocalHTTPServer") as local, \
                 patch("builtins.input", side_effect=["y", "admin"]), \
                 patch("getpass.getpass", side_effect=["secret-pass-1", "secret-pass-1"]), \
                 patch.object(server, "DEPLOY_ERROR", None), \
                 patch.object(server.sys, "argv", ["server.py", "--init-auth"]):
                lock.return_value.acquire.return_value = True
                self.assertEqual(server.main(), 0)
            local.assert_not_called()
            httpd.assert_not_called()
            self.assertTrue(store.verify("admin", "secret-pass-1"))


if __name__ == "__main__":
    unittest.main()
