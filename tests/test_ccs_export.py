"""CCS 4.0.13/4.0.20 深链与真实 JS 提取器回归；只使用合成响应。"""
import base64
import json
import shutil
import subprocess
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from integrations.ccs import (CC_APPS, CC_APP_FORMATS, build_ccswitch_link, cc_api_formats_for,
                              cc_default_format, cc_endpoint_for, newapi_usage, sub2api_usage)
from integrations import service as integration_service
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


class CcsAppCatalogTests(unittest.TestCase):
    """应用表要与 CC Switch 4.0.29 的 AppType 对齐，且每个都能拼出合法链接。"""

    def test_catalog_covers_every_ccswitch_app(self):
        self.assertEqual(list(CC_APPS), [
            "claude", "claude-desktop", "codex", "gemini", "agy", "grokbuild", "opencode",
            "openclaw", "hermes", "pi", "mcode", "kimi", "dsh", "omp", "copilot"])
        self.assertEqual(CC_APPS["agy"], "Antigravity")
        self.assertEqual(CC_APPS["mcode"], "MiniMax Code")
        self.assertEqual(CC_APPS["omp"], "Oh My Pi")

    def test_default_format_matches_ccswitch_default_for(self):
        self.assertEqual(cc_default_format("claude"), "anthropic")
        self.assertEqual(cc_default_format("claude-desktop"), "anthropic")
        self.assertEqual(cc_default_format("codex"), "openai_responses")
        self.assertEqual(cc_default_format("grokbuild"), "openai_responses")
        self.assertEqual(cc_default_format("gemini"), "gemini_native")
        self.assertEqual(cc_default_format("agy"), "gemini_native")
        for app in ("opencode", "openclaw", "hermes", "pi", "mcode", "kimi", "dsh", "omp", "copilot"):
            with self.subTest(app=app):
                self.assertEqual(cc_default_format(app), "openai_chat")

    def test_every_app_declares_its_native_format(self):
        for app, formats in CC_APP_FORMATS.items():
            self.assertIn(formats, cc_api_formats_for(app))

    def test_endpoint_stops_at_root_for_anthropic_and_gemini(self):
        for fmt in ("anthropic", "gemini_native"):
            with self.subTest(fmt=fmt):
                self.assertEqual(cc_endpoint_for("https://fixture.invalid", fmt), "https://fixture.invalid")
                # 站点地址自带宽 /v1 时不能再拼一层
                self.assertEqual(cc_endpoint_for("https://fixture.invalid/v1/", fmt), "https://fixture.invalid")

    def test_endpoint_adds_single_v1_for_openai_formats(self):
        for fmt in ("openai_chat", "openai_responses", ""):
            with self.subTest(fmt=fmt):
                self.assertEqual(cc_endpoint_for("https://fixture.invalid", fmt), "https://fixture.invalid/v1")
                self.assertEqual(cc_endpoint_for("https://fixture.invalid/v1", fmt), "https://fixture.invalid/v1")
        self.assertEqual(cc_endpoint_for("", "openai_chat"), "")

    def test_every_app_builds_a_link_and_keeps_api_format(self):
        for app in CC_APPS:
            with self.subTest(app=app):
                link = build_ccswitch_link(app=app, name="站点-Key-模型", endpoint="https://fixture.invalid",
                                           api_key="sk-fixture", model="fixture-model")
                self.assertTrue(link.startswith("ccswitch://v1/import?"))
                self.assertIn(f"app={app}", link)


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

    def _export(self, **overrides):
        """跑一次真实 export_ccswitch，只把网络与落盘换成 fixture。"""
        import server
        fixture = self.fixture
        fixture.remote.rows = [{"id": 3, "name": "fixture-key", "group": "default"}]
        write_json(fixture.config, {"sites": [fixture.site]})
        body = {"site": fixture.site["id"], "model": "fixture-model", "group": "default",
                "token_id": 3, "create": False, "with_usage": False, **overrides}
        with ExitStack() as stack:
            for name, value in (("TOKEN_SESSIONS", {}), ("TOKEN_REVISIONS", {}),
                                ("find_site", lambda _: fixture.site),
                                ("ensure_settings", lambda: {"proxy_mode": "direct"}),
                                ("ccswitch_info", lambda: {"api_format_ok": True})):
                stack.enter_context(patch.object(server, name, value))
            stack.enter_context(patch.object(server.collector, "integration_runtime", return_value=fixture.runtime))
            stack.enter_context(patch.object(server, "log_line"))
            return server.export_ccswitch(body)

    def test_endpoint_and_format_follow_the_selected_app(self):
        for app, endpoint, api_format in (
                ("claude", "https://fixture.invalid", "anthropic"),
                ("claude-desktop", "https://fixture.invalid", "anthropic"),
                ("codex", "https://fixture.invalid/v1", "openai_responses"),
                ("gemini", "https://fixture.invalid", "gemini_native"),
                ("agy", "https://fixture.invalid", "gemini_native"),
                ("kimi", "https://fixture.invalid/v1", "openai_chat"),
                ("copilot", "https://fixture.invalid/v1", "openai_chat")):
            with self.subTest(app=app):
                result, status = self._export(app=app)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["endpoint"], endpoint)
                self.assertEqual(result["api_format"], api_format)
                self.assertEqual(result["app_name"], CC_APPS[app])

    def test_explicit_api_format_overrides_default_and_rejects_unsupported(self):
        result, status = self._export(app="claude", api_format="openai_chat")
        self.assertEqual(status, 200, result)
        values = parse_qs(urlsplit(result["link"]).query)
        self.assertEqual(values["apiFormat"], ["openai_chat"])
        # OpenAI 系格式要带 /v1，端点随格式一起变
        self.assertEqual(result["endpoint"], "https://fixture.invalid/v1")

        for app, fmt in (("gemini", "openai_chat"), ("agy", "anthropic"), ("hermes", "gemini_native")):
            with self.subTest(app=app, fmt=fmt):
                result, status = self._export(app=app, api_format=fmt)
                self.assertEqual(status, 400, result)
                self.assertFalse(result["ok"])

    def test_old_ccswitch_version_drops_api_format_but_keeps_endpoint(self):
        import server
        fixture = self.fixture
        fixture.remote.rows = [{"id": 3, "name": "fixture-key", "group": "default"}]
        write_json(fixture.config, {"sites": [fixture.site]})
        with ExitStack() as stack:
            for name, value in (("TOKEN_SESSIONS", {}), ("TOKEN_REVISIONS", {}),
                                ("find_site", lambda _: fixture.site),
                                ("ensure_settings", lambda: {"proxy_mode": "direct"}),
                                ("ccswitch_info", lambda: {"api_format_ok": False, "version": "4.0.9"})):
                stack.enter_context(patch.object(server, name, value))
            stack.enter_context(patch.object(server.collector, "integration_runtime", return_value=fixture.runtime))
            stack.enter_context(patch.object(server, "log_line"))
            result, status = server.export_ccswitch({"site": fixture.site["id"], "app": "gemini",
                                                     "api_format": "gemini_native",
                                                     "model": "fixture-model", "group": "default",
                                                     "token_id": 3, "create": False, "with_usage": False})
        self.assertEqual(status, 200, result)
        values = parse_qs(urlsplit(result["link"]).query)
        self.assertNotIn("apiFormat", values)
        # 即使链接不带 apiFormat，端点仍按应用默认格式（Gemini 原生）停在站点根
        self.assertEqual(result["endpoint"], "https://fixture.invalid")
        self.assertTrue(any("4.0.9" in w for w in result["warn"]))


class ExportCacheTests(unittest.TestCase):
    """「一键导入」的短会话缓存：同一个 Key 再导一次不该重走境外请求。"""

    def setUp(self):
        fixture = stage_d.RuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        fixture.remote.rows = [{"id": 3, "name": "fixture-key", "group": "default"}]

    def _router(self):
        """模拟 server.token_session 给短会话挂上的三份交互缓存。"""
        router = self.fixture.router()
        router.interactive_profile_cache = {}
        router.interactive_token_cache = {}
        router.interactive_key_list_cache = {}
        return router

    def test_second_export_reuses_list_and_reveal(self):
        router = self._router()
        kwargs = {"group": "default", "create": False, "token_id": 3, "model": "fixture-model"}
        first = integration_service.resolve_export(router, self.fixture.site, **kwargs)
        before = len(self.fixture.remote.calls)
        second = integration_service.resolve_export(router, self.fixture.site, **kwargs)
        self.assertEqual(second["key"], first["key"])
        self.assertEqual(len(self.fixture.remote.calls), before, "第二次导入不应再发任何请求")

    def test_list_cache_is_reused_but_a_new_key_still_reveals(self):
        self.fixture.remote.rows = [{"id": 3, "name": "k3", "group": "default"},
                                    {"id": 4, "name": "k4", "group": "default"}]
        router = self._router()
        integration_service.resolve_export(router, self.fixture.site, group="default",
                                           create=False, token_id=3, model="fixture-model")
        before = len(self.fixture.remote.calls)
        integration_service.resolve_export(router, self.fixture.site, group="default",
                                           create=False, token_id=4, model="fixture-model")
        paths = [call[1] for call in self.fixture.remote.calls[before:]]
        self.assertNotIn("/api/token/", paths, "列表已缓存，不该重新分页")
        self.assertIn("/api/token/4/key", paths, "新 Key 仍要现取明文")

    def test_clearing_the_session_cache_forces_fresh_requests(self):
        router = self._router()
        kwargs = {"group": "default", "create": False, "token_id": 3, "model": "fixture-model"}
        integration_service.resolve_export(router, self.fixture.site, **kwargs)
        before = len(self.fixture.remote.calls)
        # 短会话过期（或任一步失败）时 server 会把这些缓存清空，下次必须重新核实
        router.interactive_profile_cache = {}
        router.interactive_token_cache = {}
        router.interactive_key_list_cache = {}
        integration_service.resolve_export(router, self.fixture.site, **kwargs)
        self.assertGreater(len(self.fixture.remote.calls), before, "清空缓存后必须重新请求")

    def test_repeat_export_endpoint_hits_every_short_session_cache(self):
        import server
        fixture = self.fixture
        write_json(fixture.config, {"sites": [fixture.site]})
        body = {"site": fixture.site["id"], "app": "codex", "model": "fixture-model", "group": "default",
                "token_id": 3, "create": False, "with_usage": True}
        with ExitStack() as stack:
            for name, value in (("TOKEN_SESSIONS", {}), ("TOKEN_REVISIONS", {}),
                                ("find_site", lambda _: fixture.site),
                                ("ensure_settings", lambda: {"proxy_mode": "direct"}),
                                ("ccswitch_info", lambda: {"api_format_ok": True})):
                stack.enter_context(patch.object(server, name, value))
            stack.enter_context(patch.object(server.collector, "integration_runtime", return_value=fixture.runtime))
            stack.enter_context(patch.object(server, "log_line"))
            first, first_status = server.export_ccswitch(dict(body))
            before = len(fixture.remote.calls)
            second, second_status = server.export_ccswitch(dict(body))
        self.assertEqual((first_status, second_status), (200, 200), second)
        self.assertEqual(first["link"], second["link"])
        self.assertEqual(len(fixture.remote.calls), before, "重复导入应全部命中短会话缓存")


if __name__ == "__main__":
    unittest.main()
