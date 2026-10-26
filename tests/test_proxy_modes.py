"""全局代理的三种取用方式（直连 / 跟随系统 / 手填）。

全程用合成环境，不读本机真实代理、不发真实网络请求，
避免换一台机器结果就不一样。
"""
import http.cookiejar
import unittest
from unittest.mock import patch
import urllib.request

import fetch_prices
from integrations import configuration as config_module
from integrations.configuration import (normalize_proxy_mode, resolve_proxy,
                                        system_proxy_address, system_proxies)
import server


def _active_proxy_tables(opener):
    """收集 opener 里**真正会生效**的代理表。

    注意 urllib 的细节：ProxyHandler 只有在 proxies 非空时才会注册出 http_open / https_open，
    才会被 OpenerDirector 收进 handlers。所以「空表 = 直连」的表现就是这里查不到任何代理表，
    这也是判断禁用是否生效的可靠口径。
    """
    return [handler.proxies for handler in opener.handlers
            if isinstance(handler, urllib.request.ProxyHandler) and getattr(handler, "proxies", None)]


class ProxyModeTests(unittest.TestCase):
    def test_known_modes_pass_through(self):
        for mode in ("direct", "system", "manual"):
            with self.subTest(mode=mode):
                self.assertEqual(normalize_proxy_mode(mode), mode)

    def test_legacy_config_without_mode_is_inferred(self):
        """老配置没有 proxy_mode 字段：填了地址按手填，没填才跟随系统。"""
        self.assertEqual(normalize_proxy_mode(None, "http://127.0.0.1:7897"), "manual")
        self.assertEqual(normalize_proxy_mode("", "127.0.0.1:7897"), "manual")
        self.assertEqual(normalize_proxy_mode(None, ""), "system")
        self.assertEqual(normalize_proxy_mode(None, None), "system")

    def test_unknown_mode_falls_back(self):
        self.assertEqual(normalize_proxy_mode("bogus", "http://127.0.0.1:7897"), "manual")
        self.assertEqual(normalize_proxy_mode("DIRECT", ""), "direct")

    def test_direct_resolves_to_empty(self):
        self.assertEqual(resolve_proxy("direct", "http://127.0.0.1:7897"), "")

    def test_manual_uses_configured_address(self):
        self.assertEqual(resolve_proxy("manual", "127.0.0.1:7897"), "http://127.0.0.1:7897")
        # 手填模式但没填地址：等同直连，不要偷偷退回系统代理
        self.assertEqual(resolve_proxy("manual", ""), "")

    def test_system_reads_os_proxy(self):
        with patch.object(config_module, "system_proxies",
                          return_value={"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:2"}):
            self.assertEqual(resolve_proxy("system", "http://127.0.0.1:7897"), "http://127.0.0.1:2")

    def test_system_without_os_proxy_is_direct(self):
        with patch.object(config_module, "system_proxies", return_value={}):
            self.assertEqual(resolve_proxy("system", "http://127.0.0.1:7897"), "")

    def test_site_proxy_wins_over_every_mode(self):
        """站点卡片里填了「本站代理」，三种全局模式都得让位。"""
        site = {"proxy": "http://site.invalid:8080"}
        for mode in ("direct", "system", "manual"):
            with self.subTest(mode=mode):
                self.assertEqual(resolve_proxy(mode, "http://global.invalid:8080", site),
                                 "http://site.invalid:8080")

    def test_system_proxy_address_prefers_https(self):
        with patch.object(config_module, "system_proxies",
                          return_value={"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:2"}):
            self.assertEqual(system_proxy_address(), "http://127.0.0.1:2")

    def test_system_proxy_address_empty_when_os_has_none(self):
        with patch.object(config_module, "system_proxies", return_value={}):
            self.assertEqual(system_proxy_address(), "")

    def test_system_proxies_survives_os_error(self):
        with patch.object(config_module.urllib.request, "getproxies", side_effect=OSError("boom")):
            self.assertEqual(system_proxies(), {})


class HttpClientProxyTests(unittest.TestCase):
    def test_empty_proxy_disables_proxy_rather_than_following_system(self):
        """留空必须等于直连。

        urllib 的 build_opener 默认会补一个读系统代理的 handler，
        不显式禁用的话「直连」模式会失效，页面上看不出来、实际一直在走代理。
        """
        with patch.object(config_module.urllib.request, "getproxies",
                          return_value={"https": "http://127.0.0.1:9999"}):
            client = fetch_prices.HttpClient(proxy="")
        self.assertEqual(_active_proxy_tables(client.opener), [])

    def test_without_explicit_disable_urllib_would_follow_system(self):
        """对照组：证明上面那条断言不是白写的。

        不显式挂空 ProxyHandler 时，build_opener 真的会把系统代理带进来。
        """
        with patch.object(config_module.urllib.request, "getproxies",
                          return_value={"https": "http://127.0.0.1:9999"}):
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.assertEqual(_active_proxy_tables(opener), [{"https": "http://127.0.0.1:9999"}])

    def test_manual_proxy_is_applied_to_both_schemes(self):
        client = fetch_prices.HttpClient(proxy="http://127.0.0.1:7897")
        self.assertEqual(_active_proxy_tables(client.opener),
                         [{"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}])


class ServerProxySettingsTests(unittest.TestCase):
    def test_settings_proxy_follows_mode(self):
        self.assertEqual(server.settings_proxy({"proxy_mode": "direct", "proxy": "http://x"}), "")
        self.assertEqual(server.settings_proxy({"proxy_mode": "manual", "proxy": "http://x"}), "http://x")
        with patch.object(config_module, "system_proxies", return_value={"https": "http://sys.invalid:3"}):
            self.assertEqual(server.settings_proxy({"proxy_mode": "system", "proxy": "http://x"}),
                             "http://sys.invalid:3")

    def test_sanitize_keeps_explicit_mode(self):
        clean = server.sanitize_settings({"proxy": "http://127.0.0.1:7897", "proxy_mode": "direct"})
        self.assertEqual(clean["proxy_mode"], "direct")
        self.assertEqual(clean["proxy"], "http://127.0.0.1:7897")

    def test_sanitize_infers_manual_for_legacy_config(self):
        """旧配置升级：手填的代理不能被推断成 system 而失效。"""
        clean = server.sanitize_settings({"proxy": "http://127.0.0.1:7897"})
        self.assertEqual(clean["proxy_mode"], "manual")

    def test_default_settings_has_proxy_mode(self):
        self.assertIn(server.DEFAULT_SETTINGS["proxy_mode"], config_module.PROXY_MODES)


if __name__ == "__main__":
    unittest.main()
