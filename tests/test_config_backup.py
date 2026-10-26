"""配置备份还原的纯逻辑验收：不启动 HTTP、不读真实用户配置。

覆盖三件最容易出错的事：
  1. 脱敏导出不能漏出任何凭据；
  2. 备份文件不合法时必须整份拒绝，不能只还原一半；
  3. 落盘中途失败要回滚到导入前的状态。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations import config_backup as cb
from integrations.configuration import ConfigurationStore, read_json_with_backup
from integrations.contracts import looks_like_secret_mask


SITE = {
    "id": "demo",
    "name": "示例站",
    "type": "newapi",
    "base_url": "https://demo.example.com",
    "enabled": True,
    "auth": {"cookie": "session=abc", "password": "p@ss", "model_api_key": "sk-demo",
             "account_id": "42", "expires_at": None},
    "_revision": "rev-1",
    "credential_version": "v1",
    "_token_pair_ref": "digest",
}

SETTINGS = {
    "currency": "CNY",
    "rate": 7.2,
    "throttle_sec": 2,
    "schedule": {"enabled": True, "time": "09:30"},
    "favorite_models": ["gpt-4o"],
}


def make_payload(**overrides):
    payload = cb.build_export([SITE], SETTINGS, app_version="1.0.2-2",
                              exported_at="2026-10-26T13:00:00+08:00")
    payload.update(overrides)
    return payload


class ExportTests(unittest.TestCase):
    def test_export_keeps_secrets_and_drops_internal_state(self):
        payload = cb.build_export([SITE], SETTINGS, app_version="1.0.2-2",
                                  exported_at="2026-10-26T13:00:00+08:00")
        site = payload["sites"][0]
        self.assertEqual(payload["format"], cb.BACKUP_FORMAT)
        self.assertEqual(payload["schema_version"], cb.BACKUP_SCHEMA_VERSION)
        self.assertTrue(payload["includes_secrets"])
        self.assertEqual(site["auth"]["cookie"], "session=abc")
        self.assertEqual(site["auth"]["model_api_key"], "sk-demo")
        for key in ("_revision", "credential_version", "_token_pair_ref"):
            self.assertNotIn(key, site)

    def test_redacted_export_never_carries_secret_values(self):
        payload = cb.build_export([SITE], SETTINGS, include_secrets=False,
                                  exported_at="2026-10-26T13:00:00+08:00")
        site = payload["sites"][0]
        self.assertFalse(payload["includes_secrets"])
        for field in ("cookie", "password", "model_api_key", "management_token"):
            self.assertNotIn(field, site["auth"])
        # 只保留"这里原本有没有值"，值本身不带走
        self.assertTrue(site["has_secret"]["cookie"])
        self.assertFalse(site["has_secret"]["management_token"])
        self.assertNotIn("session=abc", json.dumps(payload, ensure_ascii=False))

    def test_auth_block_is_optional(self):
        self.assertNotIn("auth", make_payload())
        with_auth = cb.build_export([SITE], SETTINGS,
                                    auth={"enabled": True, "username": "u",
                                          "password_hash": "scrypt$v1$x", "auth_revision": 3})
        self.assertEqual(with_auth["auth"]["username"], "u")
        self.assertEqual(with_auth["auth"]["auth_revision"], 3)


class ParseTests(unittest.TestCase):
    def test_rejects_foreign_or_damaged_files(self):
        cases = {
            "不是对象": ["not-a-dict"],
            "format 不对": {"format": "other", "schema_version": 1, "sites": []},
            "版本不支持": {"format": cb.BACKUP_FORMAT, "schema_version": 99, "sites": []},
            "没有站点列表": {"format": cb.BACKUP_FORMAT, "schema_version": 1, "sites": {}},
            "设置不是对象": {"format": cb.BACKUP_FORMAT, "schema_version": 1, "sites": [],
                          "settings": []},
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(cb.BackupError):
                    cb.parse_export(payload)

    def test_rejects_oversized_site_count_and_site_body(self):
        many = {"format": cb.BACKUP_FORMAT, "schema_version": 1,
                "sites": [{"id": f"s{i}"} for i in range(cb.MAX_SITES + 1)]}
        with self.assertRaises(cb.BackupError):
            cb.parse_export(many)
        fat = {"format": cb.BACKUP_FORMAT, "schema_version": 1,
               "sites": [{"id": "a", "note": "x" * (cb.MAX_SITE_BYTES + 1)}]}
        with self.assertRaises(cb.BackupError):
            cb.parse_export(fat)

    def test_redacted_backup_produces_warning(self):
        payload = cb.build_export([SITE], SETTINGS, include_secrets=False,
                                  exported_at="2026-10-26T13:00:00+08:00")
        parsed = cb.parse_export(payload)
        self.assertTrue(any("不含凭据" in item for item in parsed["warnings"]))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.sites_path = self.root / "sites.json"
        self.settings_path = self.root / "settings.json"

    def tearDown(self):
        self._dir.cleanup()

    def _write_current(self):
        """当前配置：两个站点，导入后应该只剩备份里的那一个。"""
        self.sites_path.write_text(json.dumps(
            {"integration_schema_version": 1,
             "sites": [{"id": "old-a", "base_url": "https://a.example.com", "type": "newapi"},
                       {"id": "old-b", "base_url": "https://b.example.com", "type": "newapi"}]},
            ensure_ascii=False), encoding="utf-8")
        self.settings_path.write_text(json.dumps({"currency": "USD", "rate": 7.0}),
                                      encoding="utf-8")

    def test_replace_is_wholesale_not_merge(self):
        self._write_current()
        plan = cb.plan_restore(make_payload(),
                               sanitize_settings=lambda value: value)
        result = cb.apply_restore(plan, sites_path=self.sites_path,
                                  settings_path=self.settings_path)
        self.assertEqual(result["sites"], 1)
        stored = json.loads(self.sites_path.read_text(encoding="utf-8"))
        self.assertEqual([site["id"] for site in stored["sites"]], ["demo"])
        self.assertEqual(stored["integration_schema_version"], 1)
        # 凭据原样带回来，revision 由服务端重新生成
        self.assertEqual(stored["sites"][0]["auth"]["cookie"], "session=abc")
        self.assertNotEqual(stored["sites"][0]["_revision"], "rev-1")
        self.assertEqual(json.loads(self.settings_path.read_text(encoding="utf-8"))["currency"], "CNY")

    def test_import_archives_previous_version(self):
        self._write_current()
        plan = cb.plan_restore(make_payload(),
                               sanitize_settings=lambda value: value)
        cb.apply_restore(plan, sites_path=self.sites_path, settings_path=self.settings_path)
        archived = list((self.root / "sites.json.versions").glob("*.json"))
        self.assertTrue(archived, "导入前的配置必须留档，否则没法手工回滚")
        self.assertIn("old-a", archived[0].read_text(encoding="utf-8"))

    def test_invalid_site_rejects_whole_backup(self):
        self._write_current()
        broken = make_payload(sites=[dict(SITE, base_url="ftp://wrong.example.com")])
        with self.assertRaises(cb.BackupError):
            cb.plan_restore(broken,
                            sanitize_settings=lambda value: value)
        # 校验阶段就失败，磁盘上的旧配置一个字都没动
        self.assertIn("old-a", self.sites_path.read_text(encoding="utf-8"))

    def test_failure_midway_rolls_back(self):
        self._write_current()
        settings_before = self.settings_path.read_bytes()
        plan = cb.plan_restore(make_payload(),
                               sanitize_settings=lambda value: value)
        real_write = cb.write_json
        calls = {"n": 0}

        def flaky(path, value, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:      # 设置已写成功、站点写失败
                raise OSError("disk full")
            return real_write(path, value, **kwargs)

        with patch.object(cb, "write_json", flaky):
            with self.assertRaises(cb.BackupError):
                cb.apply_restore(plan, sites_path=self.sites_path,
                                 settings_path=self.settings_path)
        self.assertEqual(self.settings_path.read_bytes(), settings_before,
                         "设置写了一半必须回滚，不能留半套配置")
        self.assertIn("old-a", self.sites_path.read_text(encoding="utf-8"))

    def test_auth_visibility_is_independent_of_restore_flag(self):
        """预检时也要能看见"备份里有访问保护"，否则前端根本不显示那个勾选框。"""
        payload = cb.build_export([SITE], SETTINGS,
                                  auth={"enabled": True, "username": "u",
                                        "password_hash": "scrypt$v1$x", "auth_revision": 2})
        preview = cb.plan_restore(payload)          # 预检：不还原，但要知道有没有
        self.assertTrue(preview.summary()["has_auth"])
        self.assertFalse(preview.summary()["restore_auth"])
        wanted = cb.plan_restore(payload, restore_auth=True)
        self.assertTrue(wanted.summary()["restore_auth"])

    def test_empty_backup_clears_sites_with_warning(self):
        self._write_current()
        plan = cb.plan_restore(make_payload(sites=[]),
                               sanitize_settings=lambda value: value)
        self.assertTrue(any("没有任何站点" in item for item in plan.warnings))
        cb.apply_restore(plan, sites_path=self.sites_path, settings_path=self.settings_path)
        self.assertEqual(json.loads(self.sites_path.read_text(encoding="utf-8"))["sites"], [])

    def test_password_with_single_asterisk_restores(self):
        """真实密码里可以有 '*'：早先"含星号即掩码"的判定会让整份备份无法还原。"""
        site = dict(SITE, auth=dict(SITE["auth"], password="ir17tuQBxjnkR*"))
        plan = cb.plan_restore(make_payload(sites=[site]),
                               sanitize_settings=lambda value: value)
        self.assertEqual(plan.sites[0]["auth"]["password"], "ir17tuQBxjnkR*")

    def test_real_masks_are_still_rejected(self):
        for mask in ("********", "sk-abc***wxyz", "[REDACTED]", "sk-abc…wxyz", "••••"):
            with self.subTest(mask=mask):
                site = dict(SITE, auth=dict(SITE["auth"], password=mask))
                with self.assertRaises(cb.BackupError):
                    cb.plan_restore(make_payload(sites=[site]))


class SecretMaskTests(unittest.TestCase):
    """掩码判定本身：单个星号是合法字符，只有真正的掩码形态才算掩码。"""

    def test_single_asterisk_is_a_real_character(self):
        for value in ("ir17tuQBxjnkR*", "p*a*s*s", "sk-*abc"):
            with self.subTest(value=value):
                self.assertFalse(looks_like_secret_mask(value))

    def test_mask_shapes_are_detected(self):
        for value in ("********", "sk-abc***wxyz", "[REDACTED]", "[redacted]",
                      "sk-abc…wxyz", "•*•", "*"):
            with self.subTest(value=value):
                self.assertTrue(looks_like_secret_mask(value))

    def test_blank_and_non_string_values(self):
        for value in ("", "   ", None, 123, b"***"):
            with self.subTest(value=value):
                self.assertFalse(looks_like_secret_mask(value))


class ReadFallbackTests(unittest.TestCase):
    """主文件被写成坏数据时，读取要能回退到归档，而不是当成空配置。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.path = self.root / "sites.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_falls_back_to_latest_archive(self):
        archive = self.root / "sites.json.versions"
        archive.mkdir()
        (archive / "old.json").write_text(json.dumps({"sites": [{"id": "saved"}]}), encoding="utf-8")
        self.path.write_text('{"sites": [', encoding="utf-8")   # 被写成半截
        value, source = read_json_with_backup(self.path, {})
        self.assertEqual(value["sites"][0]["id"], "saved")
        self.assertNotEqual(source, self.path)

    def test_missing_file_returns_default(self):
        value, source = read_json_with_backup(self.path, {"sites": []})
        self.assertEqual(value, {"sites": []})
        self.assertEqual(source, self.path)

    def test_store_survives_corrupted_config(self):
        archive = self.root / "sites.json.versions"
        archive.mkdir()
        (archive / "old.json").write_text(json.dumps(
            {"integration_schema_version": 1, "sites": [{"id": "saved", "base_url": "https://s.example.com"}]}),
            encoding="utf-8")
        self.path.write_text("not json at all", encoding="utf-8")
        store = ConfigurationStore(self.path)
        self.assertEqual([site["id"] for site in store.sites()], ["saved"])


if __name__ == "__main__":
    unittest.main()
