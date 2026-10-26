# -*- coding: utf-8 -*-
"""Cindy 导入链接（cindy://provider/import）与导出端点的回归测试。

覆盖：
  · 链接编码与**字段白名单**——Cindy 是严格校验，多一个未知键就整条拒绝；
  · app → harness、apiFormat → protocol 的映射；
  · 端点默认值（anthropic 给根地址、openai 系带 /v1）；
  · 上限与非法输入拦截（宁可本地报错，也不要生成一条会被 Cindy 拒掉的链接）；
  · 服务端 export_cindy 的离线校验分支（站点不存在 / 引擎不支持）。

规范来源：makecindy/cindy 的 docs/provider-import-links.md 与
apps/desktop/src/main/provider-import/providerImport.ts。

常量口径以本机 Cindy **v0.1.114**（2026-10-23 自动更新后）的 app.asar 为准：
该版本把线协议改回 `openai-chat`（`openai-completions` 已被移除）、
并把 `pi` 重新加进 AGENTS。写错任一常量，链接都会被 Cindy 整条拒绝。
"""
import base64
import json
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations import cindy  # noqa: E402


def decode_link(link: str) -> dict:
    """把 cindy:// 深链里的 data 解回 JSON（Cindy 用的是不带填充的 base64url）。"""
    if not link.startswith("cindy://provider/import?v=1&data="):
        raise AssertionError(f"链接前缀不对：{link[:60]}")
    data = link.split("data=", 1)[1]
    return json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8"))


class CindyLinkEncodingTests(unittest.TestCase):
    def test_payload_only_contains_cindy_allowed_fields(self):
        """顶层与端点都只能出现 Cindy 白名单里的键，否则会被整条拒绝。"""
        payload = decode_link(cindy.build_cindy_import_link(
            name="云端网-测试Key", endpoint="https://api.example.cc", api_key="sk-test",
            protocol="anthropic-messages", target="claude-code", provider_id="yunserver"))
        self.assertEqual(set(payload), {"kind", "name", "auth", "endpoints", "id"})
        self.assertEqual(set(payload["endpoints"][0]), {"protocol", "baseUrl", "targets"})
        self.assertEqual(payload["kind"], "custom")
        self.assertEqual(payload["auth"], {"method": "apiKey", "apiKey": "sk-test"})
        self.assertEqual(payload["endpoints"][0]["targets"], ["claude-code"])
        self.assertEqual(payload["id"], "yunserver")

    def test_models_are_intentionally_omitted(self):
        """不写 models —— 交给 Cindy 导入后自己拉，避免写死清单与链接超长。"""
        payload = decode_link(cindy.build_cindy_import_link(
            name="x", endpoint="https://a.cc", api_key="k",
            protocol="openai-chat", target="codex"))
        self.assertNotIn("models", payload["endpoints"][0])

    def test_reserved_provider_id_is_dropped_not_rejected(self):
        """id 命中 Cindy 保留字时不传（不传合法），而不是生成一条会被拒的链接。"""
        payload = decode_link(cindy.build_cindy_import_link(
            name="x", endpoint="https://a.cc", api_key="k",
            protocol="openai-chat", target="codex", provider_id="gemini"))
        self.assertNotIn("id", payload)

    def test_name_is_truncated_to_cindy_limit(self):
        payload = decode_link(cindy.build_cindy_import_link(
            name="站点" * 60, endpoint="https://a.cc", api_key="k",
            protocol="openai-chat", target="codex"))
        self.assertLessEqual(len(payload["name"]), cindy.CINDY_MAX_NAME_LEN)

    def test_long_api_key_still_fits(self):
        """2 KiB 的 Key 仍能生成链接（Cindy 自定义 Key 上限 4 KiB）。"""
        link = cindy.build_cindy_import_link(
            name="x", endpoint="https://a.cc", api_key="sk-" + "a" * 2000,
            protocol="openai-chat", target="codex")
        self.assertTrue(link.startswith("cindy://provider/import?v=1&data="))

    def test_protocol_table_matches_cindy_0_1_114(self):
        """v0.1.114 的 PROTOCOLS 用回 openai-chat，openai-completions 已被移除。"""
        self.assertEqual(cindy.CINDY_PROTOCOLS,
                         ("anthropic-messages", "openai-responses",
                          "openai-chat", "google-generative-ai"))
        self.assertNotIn("openai-completions", cindy.CINDY_PROTOCOLS)
        with self.assertRaises(ValueError):
            cindy.build_cindy_import_link(name="x", endpoint="https://a.cc", api_key="k",
                                          protocol="openai-completions", target="codex")

    def test_pi_is_importable_again(self):
        """v0.1.114 把 pi 加回了 AGENTS，v0.1.109 期间的移除状态已失效。"""
        self.assertEqual(cindy.CINDY_AGENTS, ("claude-code", "codex", "pi"))
        payload = decode_link(cindy.build_cindy_import_link(
            name="x", endpoint="https://a.cc/v1", api_key="k",
            protocol="openai-chat", target="pi"))
        self.assertEqual(payload["endpoints"][0]["targets"], ["pi"])


class CindyMappingTests(unittest.TestCase):
    def test_app_to_harness(self):
        self.assertEqual(cindy.cindy_target_for_app("claude"), "claude-code")
        self.assertEqual(cindy.cindy_target_for_app("codex"), "codex")
        self.assertEqual(cindy.cindy_target_for_app("pi"), "pi")

    def test_apps_without_cindy_harness_are_empty(self):
        """Cindy 自定义导入只有三个 harness，其余 CC Switch 的 app 无法导入。"""
        for app in ("gemini", "grokbuild", "opencode", "openclaw", "hermes", "", "  "):
            with self.subTest(app=app):
                self.assertEqual(cindy.cindy_target_for_app(app), "")

    def test_protocol_defaults_follow_cindy_priority(self):
        self.assertEqual(cindy.cindy_protocol_for("claude", ""), "anthropic-messages")
        self.assertEqual(cindy.cindy_protocol_for("codex", ""), "openai-responses")
        self.assertEqual(cindy.cindy_protocol_for("pi", ""), "openai-chat")

    def test_explicit_api_format_wins(self):
        self.assertEqual(cindy.cindy_protocol_for("claude", "openai_chat"), "openai-chat")
        self.assertEqual(cindy.cindy_protocol_for("codex", "anthropic"), "anthropic-messages")
        self.assertEqual(cindy.cindy_protocol_for("claude", "gemini_native"), "google-generative-ai")

    def test_unknown_api_format_falls_back_to_default(self):
        self.assertEqual(cindy.cindy_protocol_for("codex", "no-such-format"), "openai-responses")

    def test_endpoint_default_per_protocol(self):
        self.assertEqual(cindy.cindy_default_endpoint("https://a.cc/", "anthropic-messages"), "https://a.cc")
        self.assertEqual(cindy.cindy_default_endpoint("https://a.cc/", "openai-chat"), "https://a.cc/v1")
        self.assertEqual(cindy.cindy_default_endpoint("https://a.cc/", "openai-responses"), "https://a.cc/v1")


class CindyRejectsBadInputTests(unittest.TestCase):
    def test_invalid_combinations_raise_locally(self):
        cases = [
            dict(name="x", endpoint="https://a.cc", api_key="k",
                 protocol="gemini-native", target="codex"),          # 协议名不是 Cindy 的写法
            dict(name="x", endpoint="https://a.cc", api_key="k",
                 protocol="openai-completions", target="codex"),     # 已被 v0.1.114 移除的旧写法
            dict(name="x", endpoint="https://a.cc", api_key="k",
                 protocol="openai-chat", target="gemini"),           # 引擎不在 harness 白名单里
            dict(name="x", endpoint="https://a.cc", api_key="",
                 protocol="openai-chat", target="codex"),            # 缺 Key
            dict(name="x", endpoint="", api_key="k",
                 protocol="openai-chat", target="codex"),            # 缺端点
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    cindy.build_cindy_import_link(**case)


class CindyExportEndpointTests(unittest.TestCase):
    """隔离真实站点与客户端，检查服务端校验及最终导入名称。"""

    def setUp(self):
        import server
        self.server = server

    def test_missing_site_returns_404(self):
        payload, status = self.server.export_cindy({"site": "不存在的站点", "app": "claude"})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_unsupported_app_returns_400_with_hint(self):
        site = (self.server.load_sites() or [{}])[0]
        if not site.get("id"):
            self.skipTest("本机没有站点配置，跳过")
        payload, status = self.server.export_cindy({"site": site["id"], "app": "opencode"})
        self.assertEqual(status, 400)
        self.assertIn("Cindy 只支持", payload["error"])

    def test_route_is_registered(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("/api/export/cindy", source)

    def test_name_includes_real_key_and_model_without_binding_model_list(self):
        site = {"id": "fixture", "name": "测试中转站&", "base_url": "https://fixture.invalid"}
        model = "模型/v2?#🙂"
        for key_name in ("真实Key&?#🔑", ""):
            with self.subTest(key_name=key_name):
                resolved = {"key": "sk-synthetic", "created": False, "token_name": key_name,
                            "group": "default", "base_url": site["base_url"], "token_id": 7}
                with patch.object(self.server, "find_site", return_value=site), \
                     patch.object(self.server, "ensure_settings", return_value={}), \
                     patch.object(self.server, "token_session", return_value=nullcontext(object())), \
                     patch.object(self.server.integration_service, "resolve_export", return_value=resolved) as resolve, \
                     patch.object(self.server, "cindy_info", return_value={"installed": True}), \
                     patch.object(self.server, "_cindy_running", return_value=False), \
                     patch.object(self.server, "log_line"):
                    result, status = self.server.export_cindy({
                        "site": "fixture", "app": "codex", "model": "  " + model + "  ",
                        "token_id": 7, "create": False, "key_name": "不能冒充真实名称"})
                self.assertEqual(status, 200)
                payload = decode_link(result["link"])
                self.assertEqual(payload["name"], f"{site['name']}-{key_name or '未命名 Key'}-{model}")
                self.assertEqual(resolve.call_args.kwargs["model"], model)
                self.assertNotIn("models", payload["endpoints"][0])

    def test_missing_model_is_rejected_before_key_resolution(self):
        site = {"id": "fixture", "name": "测试站", "base_url": "https://fixture.invalid"}
        with patch.object(self.server, "find_site", return_value=site), \
             patch.object(self.server, "token_session") as session:
            result, status = self.server.export_cindy({"site": "fixture", "app": "codex", "model": " "})
        self.assertEqual(status, 400)
        self.assertIn("没有指定模型", result["error"])
        session.assert_not_called()


class CindyCloseTests(unittest.TestCase):
    """「关闭正在运行的 Cindy」接口：导入前腾出单实例锁的唯一自动路径。"""

    def setUp(self):
        import server
        self.server = server

    def test_noop_when_cindy_not_running(self):
        """没在运行就直接返回，连 taskkill 都不该调。"""
        with patch.object(self.server, "_cindy_running", return_value=False), \
             patch.object(self.server.subprocess, "run") as run:
            payload, status = self.server.close_cindy()
        self.assertEqual(status, 200)
        self.assertTrue(payload["already_closed"])
        self.assertFalse(payload["closed"])
        run.assert_not_called()

    def test_normal_close_succeeds(self):
        """正常关闭请求后进程消失 → 视为关成功，不强制。"""
        states = iter([True, True, False])

        def fake_running():
            try:
                return next(states)
            except StopIteration:
                return False

        with patch.object(self.server, "_cindy_running", side_effect=fake_running), \
             patch.object(self.server, "_cindy_pids", return_value=[1234]), \
             patch.object(self.server.subprocess, "run") as run, \
             patch.object(self.server.time, "sleep"):
            payload, status = self.server.close_cindy(wait_seconds=1)
        self.assertEqual(status, 200)
        self.assertTrue(payload["closed"])
        self.assertFalse(payload["force"])
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["taskkill", "/IM", "Cindy.exe"])
        self.assertNotIn("/F", args)

    def test_unresponsive_cindy_asks_for_force_instead_of_killing(self):
        """关不掉（最小化到托盘）时不要自作主张强杀，先回 needs_force 让用户再确认。"""
        with patch.object(self.server, "_cindy_running", return_value=True), \
             patch.object(self.server, "_cindy_pids", return_value=[1234]), \
             patch.object(self.server.subprocess, "run") as run, \
             patch.object(self.server.time, "sleep"):
            payload, status = self.server.close_cindy(wait_seconds=0.5)
        self.assertEqual(status, 409)
        self.assertTrue(payload["needs_force"])
        self.assertNotIn("/F", run.call_args[0][0])

    def test_force_close_appends_slash_f(self):
        with patch.object(self.server, "_cindy_running", return_value=True), \
             patch.object(self.server, "_cindy_pids", return_value=[1234]), \
             patch.object(self.server.subprocess, "run") as run, \
             patch.object(self.server.time, "sleep"):
            payload, status = self.server.close_cindy(force=True, wait_seconds=0.5)
        self.assertEqual(status, 409)
        self.assertIn("/F", run.call_args[0][0])

    def test_route_is_registered(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("/api/cindy/close", source)


if __name__ == "__main__":
    unittest.main()
