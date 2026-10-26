"""请求前置校验：Host、Origin、JSON 与可信代理解析。"""
from io import BytesIO
import json
import unittest

from integrations.deploy_config import load_deploy_config
from integrations.request_guard import (GuardError, check_host, check_remote_without_auth,
                                        check_write_origin, client_address, cors_allow_origin,
                                        open_auth_hint, parse_json_body, startup_auth_problem)


def config(mode="local", **env):
    base = {"RELAY_MODE": mode}
    if mode == "server":
        base.update({"RELAY_STATE_DIR": "/tmp/relay-state",
                     "RELAY_ALLOWED_ORIGINS": "https://relay.example.com",
                     "RELAY_ALLOWED_HOSTS": "relay.example.com"})
    base.update(env)
    return load_deploy_config(env=base, root="/tmp")


class HostGuardTests(unittest.TestCase):
    def test_local_accepts_loopback_on_bound_port(self):
        cfg = config()
        for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765"):
            with self.subTest(host=host):
                check_host(host, 8765, cfg)

    def test_local_rejects_other_host_or_port(self):
        cfg = config()
        for host in ("127.0.0.1:8766", "127.0.0.1.evil:8765", "evil.example:8765",
                     "127.0.0.1:8765,evil", "", "user@127.0.0.1:8765"):
            with self.subTest(host=host):
                with self.assertRaises(GuardError):
                    check_host(host, 8765, cfg)

    def test_server_requires_exact_allowlist(self):
        cfg = config("server")
        check_host("relay.example.com", 8765, cfg)
        for host in ("relay.example.com:443", "relay.example.com.evil", "127.0.0.1:8765"):
            with self.subTest(host=host):
                with self.assertRaises(GuardError):
                    check_host(host, 8765, cfg)


class OriginGuardTests(unittest.TestCase):
    def test_local_requires_exact_origin(self):
        cfg = config()
        for origin in ("http://127.0.0.1:8765", "http://localhost:8765"):
            with self.subTest(origin=origin):
                check_write_origin(origin, 8765, cfg)
        for origin in ("null", None, "", "http://127.0.0.1:8766",
                       "http://localhost.evil:8765", "https://evil.example"):
            with self.subTest(origin=origin):
                with self.assertRaises(GuardError):
                    check_write_origin(origin, 8765, cfg)

    def test_null_origin_requires_explicit_opt_in(self):
        cfg = config(RELAY_LOCAL_ALLOW_NULL_ORIGIN="1")
        check_write_origin("null", 8765, cfg)
        self.assertEqual(cors_allow_origin("null", 8765, cfg), "null")
        with self.assertRaises(GuardError):
            check_write_origin(None, 8765, cfg)

    def test_server_ignores_null_opt_in(self):
        cfg = config("server", RELAY_LOCAL_ALLOW_NULL_ORIGIN="1")
        with self.assertRaises(GuardError):
            check_write_origin("null", 8765, cfg)
        check_write_origin("https://relay.example.com", 8765, cfg)
        self.assertIsNone(cors_allow_origin("https://evil.example", 8765, cfg))


class JsonBodyTests(unittest.TestCase):
    def headers(self, **overrides):
        base = {"Content-Type": "application/json", "Content-Length": "2"}
        base.update(overrides)
        return base

    def test_parses_object(self):
        body = parse_json_body(self.headers(), BytesIO(b"{}"), 1024)
        self.assertEqual(body, {})

    def test_rejects_wrong_content_type_and_encoding(self):
        for headers in (self.headers(**{"Content-Type": "text/plain"}),
                        self.headers(**{"Transfer-Encoding": "chunked"})):
            with self.subTest(headers=headers):
                with self.assertRaises(GuardError):
                    parse_json_body(headers, BytesIO(b"{}"), 1024)

    def test_rejects_missing_or_bad_length(self):
        for headers in ({"Content-Type": "application/json"},
                        self.headers(**{"Content-Length": "abc"}),
                        self.headers(**{"Content-Length": "-1"}),
                        self.headers(**{"Content-Length": "9999"})):
            with self.subTest(headers=headers):
                with self.assertRaises(GuardError):
                    parse_json_body(headers, BytesIO(b"{}"), 1024)

    def test_rejects_truncated_or_invalid_json(self):
        for raw, length in ((b"{", 2), (b"[]", 2), (b"\xff\xfe", 2)):
            with self.subTest(raw=raw):
                with self.assertRaises(GuardError):
                    parse_json_body(self.headers(**{"Content-Length": str(length)}),
                                    BytesIO(raw), 1024)


class UnauthenticatedRemoteTests(unittest.TestCase):
    """没开访问保护时，外部来源必须被挡住，且提示要能直接照抄执行。"""

    def test_server_mode_refuses_to_start_without_auth(self):
        problem = startup_auth_problem(config("server"), auth_enabled=False)
        self.assertIsNotNone(problem)
        self.assertIn("--init-auth", problem)

    def test_server_mode_starts_when_auth_enabled(self):
        self.assertIsNone(startup_auth_problem(config("server"), auth_enabled=True))

    def test_local_starts_quietly_on_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            with self.subTest(host=host):
                self.assertIsNone(startup_auth_problem(config(RELAY_HOST=host),
                                                       auth_enabled=False))

    def test_local_refuses_to_start_when_bound_publicly(self):
        for host in ("0.0.0.0", "::", "192.168.1.10"):
            with self.subTest(host=host):
                problem = startup_auth_problem(config(RELAY_HOST=host), auth_enabled=False)
                self.assertIsNotNone(problem)
                self.assertIn(host, problem)

    def test_escape_hatch_allows_unauthenticated_remote(self):
        cfg = config("server", RELAY_ALLOW_UNAUTHENTICATED_REMOTE="1")
        self.assertIsNone(startup_auth_problem(cfg, auth_enabled=False))
        check_remote_without_auth("203.0.113.9", auth_enabled=False, cfg=cfg)

    def test_remote_peer_is_rejected_with_actionable_hint(self):
        cfg = config()
        with self.assertRaises(GuardError) as caught:
            check_remote_without_auth("203.0.113.9", auth_enabled=False, cfg=cfg)
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("访问保护", caught.exception.message)
        # 服务在跑，提示要指向能立刻点进去的页面，而不是让人去抢实例锁
        self.assertIn("系统设置 → 访问保护", caught.exception.message)

    def test_loopback_peers_pass(self):
        cfg = config()
        for peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1", ""):
            with self.subTest(peer=peer):
                check_remote_without_auth(peer, auth_enabled=False, cfg=cfg)

    def test_auth_enabled_lets_remote_through(self):
        check_remote_without_auth("203.0.113.9", auth_enabled=True, cfg=config())

    def test_hint_names_the_right_entry_point_per_mode(self):
        # 运行时（服务在跑）→ 页面路径最短，不让人去敲会抢实例锁的 --init-auth
        for cfg in (config(), config("server")):
            hint = open_auth_hint(cfg)
            self.assertIn("系统设置", hint)
            self.assertNotIn("--init-auth", hint)
        # 启动被拦（服务没起来）→ 只能给命令，说"打开浏览器"是废话
        self.assertIn("docker compose run", open_auth_hint(config("server"), page_available=False))
        self.assertIn("python server.py --init-auth", open_auth_hint(config(), page_available=False))

    def test_startup_message_never_tells_user_to_open_a_page(self):
        problem = startup_auth_problem(config("server"), auth_enabled=False)
        self.assertNotIn("浏览器打开本服务", problem)
        self.assertIn("docker compose run", problem)
        self.assertIn("RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1", problem)


class ClientAddressTests(unittest.TestCase):
    def test_forwarded_header_is_ignored_without_trusted_proxy(self):
        cfg = config()
        self.assertEqual(client_address("127.0.0.1", "203.0.113.9", cfg), "127.0.0.1")

    def test_uses_rightmost_hop_from_trusted_proxy(self):
        cfg = config(RELAY_TRUSTED_PROXIES="127.0.0.1")
        self.assertEqual(client_address("127.0.0.1", "203.0.113.9, 198.51.100.7", cfg),
                         "198.51.100.7")
        self.assertEqual(client_address("127.0.0.1", "not-an-ip", cfg), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
