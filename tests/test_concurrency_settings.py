"""并发配置与限流闸门：只跑本地逻辑，不访问任何站点。

覆盖两件事：
1. `settings.json` 的 `health_concurrency` / `refresh_concurrency`（默认 5、1–16 收敛）；
2. `DynamicGate` —— 模型健康请求用的动态并发闸门（改配置不重启即生效、满了排队、可超时）。
"""
import threading
import time
import unittest
from unittest.mock import patch

import server


class ConcurrencyValueTests(unittest.TestCase):
    def test_defaults_are_five(self):
        self.assertEqual(server.DEFAULT_SETTINGS["health_concurrency"], 5)
        self.assertEqual(server.DEFAULT_SETTINGS["refresh_concurrency"], 5)

    def test_value_is_clamped_to_range(self):
        self.assertEqual(server.concurrency_of(8, 5), 8)
        self.assertEqual(server.concurrency_of("8", 5), 8)
        self.assertEqual(server.concurrency_of(3.9, 5), 3)
        self.assertEqual(server.concurrency_of(0, 5), 1)
        self.assertEqual(server.concurrency_of(-3, 5), 1)
        self.assertEqual(server.concurrency_of(99, 5), 16)
        # 缺失或非数字回落到兜底值
        self.assertEqual(server.concurrency_of(None, 5), 5)
        self.assertEqual(server.concurrency_of("abc", 5), 5)

    def test_sanitize_keeps_other_settings(self):
        clean = server.sanitize_settings({"health_concurrency": "7", "refresh_concurrency": 99,
                                          "currency": "CNY", "proxy": "http://127.0.0.1:7890"})
        self.assertEqual(clean["health_concurrency"], 7)
        self.assertEqual(clean["refresh_concurrency"], 16)
        self.assertEqual(clean["currency"], "CNY")
        self.assertTrue(clean["proxy"].startswith("http://127.0.0.1:7890"))

    def test_tier_expand_default_only_accepts_explicit_true(self):
        # 2026-10-16 新增：分档价格表默认展开开关，只有显式 true 才开，脏值一律回落关闭
        self.assertFalse(server.DEFAULT_SETTINGS["tier_expand_default"])
        self.assertTrue(server.sanitize_settings({"tier_expand_default": True})["tier_expand_default"])
        for bad in (None, "yes", 1, "true", {}):
            with self.subTest(value=bad):
                self.assertFalse(server.sanitize_settings({"tier_expand_default": bad})["tier_expand_default"])

    def test_sanitize_falls_back_on_garbage(self):
        clean = server.sanitize_settings({"health_concurrency": "", "refresh_concurrency": None})
        self.assertEqual(clean["health_concurrency"], 5)
        self.assertEqual(clean["refresh_concurrency"], 5)


class DynamicGateTests(unittest.TestCase):
    def test_limits_parallel_holders(self):
        """上限 2 时，4 个线程里同时最多 2 个拿到名额；满了的按超时失败。"""
        gate = server.DynamicGate(lambda: 2)
        release = threading.Event()
        counter = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def worker():
            gate.acquire()
            try:
                with lock:
                    counter["active"] += 1
                    counter["peak"] = max(counter["peak"], counter["active"])
                release.wait(3)
            finally:
                with lock:
                    counter["active"] -= 1
                gate.release()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        self.assertEqual(counter["peak"], 2)
        self.assertFalse(gate.acquire(timeout=0.05))    # 已满 → 等到超时也是失败
        release.set()
        for thread in threads:
            thread.join(3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(gate.acquire(timeout=0.2))      # 全部释放后重新可用
        gate.release()

    def test_limit_changes_take_effect_without_restart(self):
        """上限从配置读：调大后立刻能多拿一个名额，不用重启服务。"""
        limit = {"value": 1}
        gate = server.DynamicGate(lambda: limit["value"])
        self.assertTrue(gate.acquire())
        self.assertFalse(gate.acquire(timeout=0.05))
        limit["value"] = 2
        self.assertTrue(gate.acquire(timeout=0.05))
        gate.release()
        gate.release()

    def test_health_gate_reads_setting(self):
        with patch.object(server, "ensure_settings", return_value={"health_concurrency": 7}):
            self.assertEqual(server.HEALTH_GATE.limit(), 7)
        with patch.object(server, "ensure_settings", return_value={}):
            self.assertEqual(server.HEALTH_GATE.limit(), 5)   # 缺失时回落默认 5
        with patch.object(server, "ensure_settings", return_value={"health_concurrency": 0}):
            self.assertEqual(server.HEALTH_GATE.limit(), 1)   # 0 收敛到下限 1


if __name__ == "__main__":
    unittest.main()
