"""请求前置校验：Host、Origin、JSON 与可信代理解析。"""
from io import BytesIO
import json
import unittest

from integrations.deploy_config import load_deploy_config
from integrations.request_guard import (GuardError, check_host, check_write_origin,
                                        client_address, cors_allow_origin, parse_json_body)


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
