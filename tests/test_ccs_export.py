"""CCS 4.0.13/4.0.20 深链与真实 JS 提取器回归；只使用合成响应。"""
import base64
import json
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from integrations.ccs import build_ccswitch_link, newapi_usage, sub2api_usage
from integrations.configuration import merge_sites, public_site, write_json
from integrations.contracts import Feature
from integrations.providers import HttpResponse
from tests import test_stage_d as stage_d


def link_values(**kwargs):
    return parse_qs(urlsplit(build_ccswitch_link(app="codex", name="中文-Key-模型", **kwargs)).query)


@unittest.skipUnless(shutil.which("node"), "需要 Node 执行实际的 CCS JavaScript 提取器")
class CcsExtractorTests(unittest.TestCase):
    def extract(self, usage, response):
        # 只运行仓库内的固定脚本；合成响应走 stdin JSON，不拼接进可执行代码。
        runner = """const fs=require('fs'); const x=JSON.parse(fs.readFileSync(0,'utf8'));
const template=eval(x.script); process.stdout.write(JSON.stringify(template.extractor(x.response)));"""
        result = subprocess.run(["node", "-e", runner],
                                input=json.dumps({"script": usage["script"], "response": response}),
                                text=True, encoding="utf-8", capture_output=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def sub2(self, response):
        return self.extract(sub2api_usage(base_url="https://fixture.invalid", key="sk-fixture"), response)

    def test_standard_wallet_top_level_remaining_and_zero(self):
        for amount in (0, 2.5):
            with self.subTest(amount=amount):
                result = self.sub2({"isValid": True, "remaining": amount, "planName": "钱包余额", "unit": "USD"})
                self.assertEqual(result, {"isValid": True, "remaining": amount, "planName": "钱包余额", "unit": "USD"})

    def test_standard_subscription_retains_server_remaining_without_summing_windows(self):
        result = self.sub2({"isValid": True, "remaining": 3, "planName": "订阅组", "unit": "USD",
                            "subscription": {"daily_limit_usd": 10, "daily_usage_usd": 7,
                                             "weekly_limit_usd": 100, "weekly_usage_usd": 20}})
        self.assertEqual(result["remaining"], 3)
        self.assertEqual(result["planName"], "订阅组")
        self.assertNotIn("total", result)
        self.assertNotIn("used", result)

    def test_key_quota_contains_only_its_confirmed_totals(self):
        result = self.sub2({"remaining": 2, "mode": "quota_limited", "quota": {"limit": 5, "used": 3, "unit": "USD"}})
        self.assertEqual(result, {"isValid": True, "remaining": 2, "total": 5, "used": 3,
                                  "unit": "USD", "planName": "key_quota"})

    def test_unlimited_negative_missing_and_string_values_are_not_zero(self):
        for value in (-1, -3, "2", None):
            with self.subTest(value=value):
                result = self.sub2({"remaining": value, "subscription": {}, "quota": {"limit": 0, "used": -1}})
                self.assertNotIn("remaining", result)
                self.assertNotIn("total", result)
                self.assertNotIn("used", result)
                self.assertEqual(result["unit"], "unknown")
        self.assertNotIn("remaining", self.sub2({}))

    def test_invalid_key_preserves_invalid_state_and_reason(self):
        result = self.sub2({"isValid": False, "status": "expired", "planName": "测试组"})
        self.assertFalse(result["isValid"])
        self.assertEqual(result["invalidMessage"], "expired")
        self.assertNotIn("remaining", result)

    def test_legacy_wrapped_wallet_still_works_and_does_not_infer_subscription_wallet(self):
        self.assertEqual(self.sub2({"data": {"mode": "unrestricted", "balance": 0, "unit": "USD"}})["remaining"], 0)
        result = self.sub2({"subscription": {}, "balance": 20, "unit": "USD"})
        self.assertEqual(result["planName"], "subscription")
        self.assertNotIn("remaining", result)

    def test_newapi_scale_and_zero_are_executed_with_site_unit(self):
        usage = newapi_usage(base_url="https://fixture.invalid", token="fixture", quota_per_unit=1000, unit="CNY", user_id=7)
        result = self.extract(usage, {"success": True, "data": {"group": "default", "quota": 0, "used_quota": 2500}})
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(result["used"], 2.5)
        self.assertEqual(result["unit"], "CNY")
        self.assertTrue(result["isValid"])

    def test_newapi_missing_string_or_null_used_does_not_infer_zero_or_total(self):
        usage = newapi_usage(base_url="https://fixture.invalid", token="fixture", quota_per_unit=1000, unit="USD")
        for profile in ({"quota": 1000}, {"quota": 1000, "used_quota": "20"},
                        {"quota": 1000, "used_quota": None}, {"quota": 1000, "used_quota": True}):
            with self.subTest(profile=profile):
                result = self.extract(usage, {"success": True, "data": profile})
                self.assertEqual(result["remaining"], 1)
                self.assertNotIn("used", result)
                self.assertNotIn("total", result)

    def test_newapi_unknown_balance_retains_independently_confirmed_usage(self):
        usage = newapi_usage(base_url="https://fixture.invalid", token="fixture", quota_per_unit=1000, unit="USD")
        for profile in ({"used_quota": 0}, {"used_quota": 0, "quota": "1000"},
                        {"used_quota": 0, "quota": None}, {"used_quota": 0, "quota": False}):
            with self.subTest(profile=profile):
                result = self.extract(usage, {"success": True, "data": profile})
                self.assertEqual(result["used"], 0)
                self.assertNotIn("remaining", result)
                self.assertNotIn("total", result)
                self.assertTrue(result["isValid"])

    def test_newapi_confirmed_numeric_balance_and_usage_produce_total(self):
        usage = newapi_usage(base_url="https://fixture.invalid", token="fixture", quota_per_unit=1000, unit="USD")
        result = self.extract(usage, {"success": True, "data": {"quota": 1000, "used_quota": 2000}})
        self.assertEqual((result["remaining"], result["used"], result["total"]), (1, 2, 3))

    def test_newapi_auth_failure_retains_invalid_state(self):
        usage = newapi_usage(base_url="https://fixture.invalid", token="fixture", quota_per_unit=1000, unit="USD")
        result = self.extract(usage, {"success": False, "message": "令牌已失效"})
        self.assertEqual(result, {"isValid": False, "invalidMessage": "令牌已失效"})


class CcsLinkTests(unittest.TestCase):
    def test_script_and_explicit_enable_and_user_header_survive_encoding(self):
        usage = newapi_usage(base_url="https://fixture.invalid", token="fixture+&token", quota_per_unit=1000, unit="USD", user_id=7)
        values = link_values(api_key="sk-fixture", usage=usage)
        script = base64.b64decode(values["usageScript"][0]).decode("utf-8")
        self.assertIn('"New-Api-User": "{{userId}}"', script)
        self.assertEqual(values["usageUserId"], ["7"])
        self.assertEqual(values["usageAccessToken"], ["fixture+&token"])
        self.assertEqual(values["usageEnabled"], ["true"])
        self.assertEqual(values["name"], ["中文-Key-模型"])

    def test_unverified_user_id_and_invalid_scale_are_not_exported(self):
        for user_id in (None, 0, -1, True, "7\ninvalid"):
            with self.subTest(user_id=user_id):
                usage = newapi_usage(base_url="https://fixture.invalid", token="fixture", quota_per_unit=1000, unit="USD", user_id=user_id)
                self.assertNotIn("New-Api-User", usage["script"])
        for scale in (float("inf"), float("nan"), True, 0, -1, "1000"):
            with self.subTest(scale=scale):
                self.assertIsNone(newapi_usage(base_url="https://fixture.invalid", token="fixture", quota_per_unit=scale, unit="USD"))

    def test_sub2_base_url_has_single_v1_and_keeps_subpath(self):
        for base in ("https://fixture.invalid/prefix", "https://fixture.invalid/prefix/v1/"):
            with self.subTest(base=base):
                usage = sub2api_usage(base_url=base, key="sk-fixture")
                self.assertEqual(usage["base_url"], "https://fixture.invalid/prefix")
                self.assertIn('"User-Agent": "cc-switch/1.0"', usage["script"])

    def test_explicit_icons_are_encoded_and_no_icon_is_invented(self):
        values = link_values(icon="sub2api", icon_url="https://assets.example.com/logo.svg?v=1&x=2")
        self.assertEqual(values["icon"], ["sub2api"])
        self.assertEqual(values["iconUrl"], ["https://assets.example.com/logo.svg?v=1&x=2"])
        self.assertNotIn("icon", link_values())
        self.assertNotIn("iconUrl", link_values())
        self.assertNotIn("providerId", values)

    def test_invalid_icon_inputs_do_not_break_key_import(self):
        for url in ("http://assets.example.com/logo.png", "https://127.0.0.1/logo.png",
                    "https://localhost/logo.png", "https://user:password@example.com/logo.png",
                    "https://example.com:8443/logo.png", "data:image/png;base64,x", "https://example.com:invalid/x"):
            with self.subTest(url=url):
                values = link_values(api_key="sk-fixture", icon="bad icon", icon_url=url)
                self.assertNotIn("icon", values)
                self.assertNotIn("iconUrl", values)
                self.assertEqual(values["apiKey"], ["sk-fixture"])

    def test_icon_url_limit_counts_utf8_bytes(self):
        prefix = "https://assets.example.com/"
        exact = prefix + "a" * (2048 - len(prefix))
        self.assertEqual(link_values(icon_url=exact)["iconUrl"], [exact])
        self.assertNotIn("iconUrl", link_values(icon_url=exact + "a"))
        self.assertNotIn("iconUrl", link_values(icon_url=prefix + "图" * 700))

    def test_configuration_round_trip_preserves_explicit_icons(self):
        site = {**stage_d.SITE, "icon": "sub2api", "icon_url": "https://assets.example.com/logo.svg"}
        cleaned, errors = merge_sites([public_site(site)], [site])
        self.assertFalse(errors)
        self.assertEqual(cleaned[0]["icon"], site["icon"])
        self.assertEqual(cleaned[0]["icon_url"], site["icon_url"])


class CcsProviderTests(unittest.TestCase):
    def setUp(self):
        fixture = stage_d.RuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def test_newapi_export_reuses_verified_wallet_identity_without_extra_request(self):
        result = self.fixture.router().fetch(Feature.CCS_USAGE)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["user_id"], "7")
        self.assertEqual([c[1] for c in self.fixture.remote.calls], ["/api/user/self", "/api/status"])

    def test_negotiated_user_header_is_retained_by_export(self):
        remote = self.fixture.remote
        original = remote.request
        def request(method, url, **kwargs):
            if urlsplit(url).path == "/api/user/self" and "New-Api-User" not in kwargs["headers"]:
                remote.calls.append((method, "/api/user/self", dict(kwargs["headers"]), kwargs["body"]))
                return HttpResponse(403, {"success": False, "message": "无权进行此操作，未提供 New-Api-User"})
            return original(method, url, **kwargs)
        with patch.object(remote, "request", side_effect=request):
            result = self.fixture.router().fetch(Feature.CCS_USAGE)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["user_id"], "7")
        self.assertEqual(len(remote.calls), 3)
        self.assertEqual(remote.calls[1][2]["New-Api-User"], "7")

    def test_server_passes_explicit_site_brand_fields(self):
        import server
        fixture = self.fixture
        fixture.remote.rows = [{"id": 3, "name": "fixture-key", "group": "default"}]
        fixture.site.update(icon="sub2api", icon_url="https://assets.example.com/logo.svg")
        write_json(fixture.config, {"sites": [fixture.site]})
        with ExitStack() as stack:
            for name, value in (("TOKEN_SESSIONS", {}), ("TOKEN_REVISIONS", {}),
                                ("find_site", lambda _: fixture.site),
                                ("ensure_settings", lambda: {"proxy_mode": "direct"}),
                                ("ccswitch_info", lambda: {"api_format_ok": True})):
                stack.enter_context(patch.object(server, name, value))
            stack.enter_context(patch.object(server.collector, "integration_runtime", return_value=fixture.runtime))
            stack.enter_context(patch.object(server, "log_line"))
            result, status = server.export_ccswitch({"site": fixture.site["id"], "app": "codex", "model": "fixture-model",
                                                    "group": "default", "token_id": 3, "create": False, "with_usage": False})
        self.assertEqual(status, 200, result)
        values = parse_qs(urlsplit(result["link"]).query)
        self.assertEqual(values["icon"], ["sub2api"])
        self.assertEqual(values["iconUrl"], [fixture.site["icon_url"]])
        self.assertNotIn("usageScript", values)


if __name__ == "__main__":
    unittest.main()
