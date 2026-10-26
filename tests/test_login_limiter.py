"""登录限速：来源、账号与进程总量三层，且不信任未配置的可信代理。"""
import unittest

from integrations.auth import LoginLimiter
from integrations.deploy_config import load_deploy_config
from integrations.request_guard import client_address


class LoginLimiterTests(unittest.TestCase):
    def test_cooldown_grows_and_caps(self):
        limiter = LoginLimiter(max_failures=2, cooldown_base=2, cooldown_max=8)
        self.assertEqual(limiter.check("1.2.3.4", "admin", now=100), 0)
        self.assertEqual(limiter.record_failure("1.2.3.4", "admin", now=100), 0)
        self.assertEqual(limiter.record_failure("1.2.3.4", "admin", now=101), 2)
        self.assertGreater(limiter.check("1.2.3.4", "admin", now=102), 0)
        # 冷却到期即解除，不会被窗口内的历史失败一直续命。
        self.assertEqual(limiter.check("1.2.3.4", "admin", now=104), 0)
        # 窗口内的再次失败会按指数继续加长退避（不是永久锁死，但有上限）。
        self.assertGreater(limiter.record_failure("1.2.3.4", "admin", now=104), 0)
        # 超出统计窗口后重新计数，退避从零开始。
        self.assertEqual(limiter.record_failure("1.2.3.4", "admin", now=1000), 0)

    def test_success_clears_failures(self):
        limiter = LoginLimiter(max_failures=1, cooldown_base=5, cooldown_max=5)
        limiter.record_failure("1.2.3.4", "admin", now=10)
        self.assertGreater(limiter.check("1.2.3.4", "admin", now=10), 0)
        limiter.record_success("1.2.3.4", "admin")
        self.assertEqual(limiter.check("1.2.3.4", "admin", now=10), 0)

    def test_account_lock_is_case_insensitive_and_global(self):
        limiter = LoginLimiter(max_failures=1, cooldown_base=10, cooldown_max=10)
        limiter.record_failure("1.2.3.4", "Admin", now=10)
        self.assertGreater(limiter.check("1.2.3.4", "admin", now=10), 0)
        # 账号层跨 IP 生效，避免换来源继续爆破。
        self.assertGreater(limiter.check("5.6.7.8", "admin", now=10), 0)

    def test_global_limit_caps_total_attempts(self):
        limiter = LoginLimiter(max_failures=100, global_limit=2, global_window_seconds=60)
        limiter.record_failure("1.1.1.1", "a", now=10)
        limiter.record_failure("2.2.2.2", "b", now=11)
        self.assertGreater(limiter.check("3.3.3.3", "c", now=12), 0)
        self.assertEqual(limiter.check("3.3.3.3", "c", now=80), 0)


class ForwardedHeaderTests(unittest.TestCase):
    def test_spoofed_header_is_ignored_without_trusted_proxy(self):
        cfg = load_deploy_config(env={"RELAY_MODE": "local"}, root=".")
        self.assertEqual(client_address("127.0.0.1", "203.0.113.9", cfg), "127.0.0.1")

    def test_trusted_proxy_uses_rightmost_hop(self):
        cfg = load_deploy_config(env={
            "RELAY_MODE": "server", "RELAY_STATE_DIR": "state",
            "RELAY_ALLOWED_ORIGINS": "https://relay.example.com",
            "RELAY_ALLOWED_HOSTS": "relay.example.com",
            "RELAY_TRUSTED_PROXIES": "10.0.0.5"}, root=".")
        self.assertEqual(client_address("10.0.0.5", "203.0.113.9, 198.51.100.7", cfg),
                         "198.51.100.7")


if __name__ == "__main__":
    unittest.main()
