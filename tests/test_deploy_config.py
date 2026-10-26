"""部署配置解析：local 保持旧用法，server 缺项必须拒绝启动。"""
import tempfile
import unittest
from pathlib import Path

from integrations.deploy_config import DeployConfigError, auth_default_enabled, load_deploy_config


class DeployConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def load(self, **env):
        return load_deploy_config(env=env, root=self.root)

    def test_defaults_to_local_with_project_state(self):
        cfg = self.load()
        self.assertEqual(cfg.mode, "local")
        self.assertEqual(cfg.state_dir, self.root.resolve())
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertFalse(cfg.strict_port)
        self.assertFalse(cfg.allow_null_origin)
        self.assertFalse(auth_default_enabled(env={}))

    def test_server_requires_state_dir_origins_and_hosts(self):
        for missing in ("RELAY_STATE_DIR", "RELAY_ALLOWED_ORIGINS", "RELAY_ALLOWED_HOSTS"):
            env = {"RELAY_MODE": "server", "RELAY_STATE_DIR": str(self.root / "state"),
                   "RELAY_ALLOWED_ORIGINS": "https://relay.example.com",
                   "RELAY_ALLOWED_HOSTS": "relay.example.com"}
            env.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(DeployConfigError):
                    self.load(**env)

    def test_server_parses_strict_values(self):
        cfg = self.load(RELAY_MODE="server", RELAY_STATE_DIR=str(self.root / "state"),
                        RELAY_ALLOWED_ORIGINS="https://relay.example.com",
                        RELAY_ALLOWED_HOSTS="relay.example.com,127.0.0.1:8765",
                        RELAY_TRUSTED_PROXIES="127.0.0.1")
        self.assertTrue(cfg.strict_port)
        self.assertEqual(cfg.allowed_origins, ("https://relay.example.com",))
        self.assertEqual(cfg.trusted_proxies, ("127.0.0.1",))
        self.assertTrue(cfg.secure_cookies)
        self.assertTrue(auth_default_enabled(env={"RELAY_MODE": "server"}))

    def test_local_rejects_server_only_settings(self):
        with self.assertRaises(DeployConfigError):
            self.load(RELAY_ALLOWED_ORIGINS="https://relay.example.com")

    def test_mixed_https_and_public_http_is_rejected(self):
        with self.assertRaises(DeployConfigError):
            self.load(RELAY_MODE="server", RELAY_STATE_DIR=str(self.root / "state"),
                      RELAY_ALLOWED_ORIGINS="https://relay.example.com,http://relay.example.com",
                      RELAY_ALLOWED_HOSTS="relay.example.com")

    def test_origin_rejects_wildcard_and_path(self):
        for origin in ("*", "https://relay.example.com/app", "null"):
            with self.subTest(origin=origin):
                with self.assertRaises(DeployConfigError):
                    self.load(RELAY_MODE="server", RELAY_STATE_DIR=str(self.root / "state"),
                              RELAY_ALLOWED_ORIGINS=origin,
                              RELAY_ALLOWED_HOSTS="relay.example.com")

    def test_retention_settings_are_validated(self):
        cfg = self.load(RELAY_VERSION_KEEP="5", RELAY_VERSION_KEEP_DAYS="7")
        self.assertEqual((cfg.version_keep, cfg.version_keep_days), (5, 7))
        with self.assertRaises(DeployConfigError):
            self.load(RELAY_VERSION_KEEP="0")


if __name__ == "__main__":
    unittest.main()
