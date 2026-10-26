"""账户并发与逐站发布：用事件控制快慢顺序，不访问真实站点。"""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import importlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from integrations.service import binding_of


class AsyncAccountTests(unittest.TestCase):
    def setUp(self):
        self.server = importlib.import_module("server")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "accounts.json"
        self.sites = [{"id": f"s{i}", "name": f"S{i}", "type": "newapi", "enabled": True,
                       "base_url": f"https://s{i}.invalid", "auth": {"management_token": "synthetic"}}
                      for i in range(6)]
        self.old = [self.snapshot(s, 1) for s in self.sites]
        self.path.write_text(json.dumps({"accounts": self.old}), encoding="utf-8")
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(self.server, "ACCOUNTS_FILE", self.path))
        stack.enter_context(patch.object(self.server, "load_sites", side_effect=lambda: copy.deepcopy(self.sites)))
        stack.enter_context(patch.object(self.server, "ensure_settings",
                                         return_value={"throttle_sec": 60, "refresh_concurrency": 3}))
        stack.enter_context(patch.object(self.server, "log_line"))
        stack.enter_context(patch.object(self.server.collector, "integration_runtime", return_value=Mock()))
        stack.enter_context(patch.dict(self.server.TOKEN_REVISIONS, {}, clear=True))

    @staticmethod
    def snapshot(site, amount=9):
        return {"site": site["id"], "site_name": site["name"], "ok": True, "user_id": 7,
                "binding_ref": binding_of(site), "quota_usd": amount,
                "wallet": {"balance_kind": "wallet", "unit": "USD", "amount": amount},
                "tokens": [{"id": 1, "group": "default", "enabled": True}], "token_count": 1,
                "group_keys": {}, "keys_complete": True, "topups_complete": True}

    def test_fast_station_publishes_before_first_slow_station_finishes(self):
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        progress = []

        def fetch(router, site, **kwargs):
            if site["id"] == "s0":
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Fixture gate was not released")
            return self.snapshot(site)

        def report(done, total, current, ok):
            progress.append((done, total, current, ok))
            if current != "S0":
                published.set()

        with patch.object(self.server.integration_service, "account_snapshot", side_effect=fetch), \
                ThreadPoolExecutor(max_workers=1) as driver:
            task = driver.submit(self.server.refresh_accounts, None, report)
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(published.wait(2))
                self.assertFalse(task.done())
                rows = json.loads(self.path.read_text(encoding="utf-8"))["accounts"]
                self.assertEqual(rows[0]["quota_usd"], 1)
                self.assertTrue(any(a["quota_usd"] == 9 for a in rows[1:]))
            finally:
                release.set()
            result = task.result(timeout=3)
        self.assertEqual([r[0] for r in progress], list(range(1, 7)))
        self.assertTrue(all(a["quota_usd"] == 9 for a in result["accounts"]))
        self.assertEqual([a["site"] for a in result["accounts"]], [s["id"] for s in self.sites])

    def probe_concurrency(self, settings, expected_peak):
        """用事件门控把并发卡住，检查同时最多只有 expected_peak 个站点在跑。"""
        lock, full, release = threading.Lock(), threading.Event(), threading.Event()
        active, peak, started = 0, 0, 0

        def fetch(router, site, **kwargs):
            nonlocal active, peak, started
            with lock:
                active += 1
                started += 1
                peak = max(peak, active)
                if active >= expected_peak:
                    full.set()
            try:
                if not release.wait(5):
                    raise TimeoutError("Fixture gate was not released")
                return self.snapshot(site)
            finally:
                with lock:
                    active -= 1

        with patch.object(self.server, "ensure_settings", return_value=settings), \
                patch.object(self.server.integration_service, "account_snapshot", side_effect=fetch), \
                ThreadPoolExecutor(max_workers=1) as driver:
            task = driver.submit(self.server.refresh_accounts)
            try:
                self.assertTrue(full.wait(2))
                self.assertEqual(started, expected_peak)
                self.assertFalse(task.done())
            finally:
                release.set()
            task.result(timeout=3)
        return peak, started

    def test_station_concurrency_follows_setting(self):
        # 设置里 refresh_concurrency=3 → 同时最多 3 个站点在刷新
        self.assertEqual(self.probe_concurrency({"throttle_sec": 60, "refresh_concurrency": 3}, 3), (3, 6))

    def test_station_concurrency_defaults_to_five(self):
        # 配置里没有这一项时按默认 5 路并发
        self.assertEqual(self.probe_concurrency({"throttle_sec": 60}, 5), (5, 6))

    def test_station_concurrency_is_clamped(self):
        # 99 收敛到上限 16（6 个站点就全部并发）；0 收敛到下限 1（逐个刷新）
        self.assertEqual(self.probe_concurrency({"throttle_sec": 60, "refresh_concurrency": 99}, 6), (6, 6))
        self.assertEqual(self.probe_concurrency({"throttle_sec": 60, "refresh_concurrency": 0}, 1), (1, 6))

    def test_failed_station_keeps_its_history_without_holding_other_stations(self):
        def fetch(router, site, **kwargs):
            if site["id"] == "s0":
                raise TimeoutError("Fixture timeout")
            return self.snapshot(site)

        with patch.object(self.server.integration_service, "account_snapshot", side_effect=fetch):
            result = self.server.refresh_accounts()
        self.assertTrue(result["accounts"][0]["stale"])
        self.assertEqual(result["accounts"][0]["quota_usd"], 1)
        self.assertTrue(all(a["quota_usd"] == 9 for a in result["accounts"][1:]))

    def test_later_publish_cannot_restore_a_key_deleted_after_earlier_publish(self):
        release = threading.Event()

        def fetch(router, site, **kwargs):
            if site["id"] == "s1" and not release.wait(5):
                raise TimeoutError("Fixture gate was not released")
            return self.snapshot(site)

        def report(done, total, current, ok):
            if current == "S0":
                self.server.update_token_snapshot("s0", deleted_id=1)
                release.set()

        with patch.object(self.server.integration_service, "account_snapshot", side_effect=fetch):
            try:
                result = self.server.refresh_accounts(["s0", "s1"], report)
            finally:
                release.set()
        self.assertEqual(result["accounts"][0]["tokens"], [])
        self.assertEqual(result["accounts"][0]["quota_usd"], 9)
        self.assertEqual(result["accounts"][2], self.old[2])

    def test_credentials_changed_during_refresh_reject_old_result(self):
        entered, release = threading.Event(), threading.Event()

        def fetch(router, site, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Fixture gate was not released")
            return self.snapshot(site)

        with patch.object(self.server.integration_service, "account_snapshot", side_effect=fetch), \
                ThreadPoolExecutor(max_workers=1) as driver:
            task = driver.submit(self.server.refresh_accounts, ["s0"])
            try:
                self.assertTrue(entered.wait(2))
                self.sites[0]["auth"]["management_token"] = "synthetic-replacement"
            finally:
                release.set()
            result = task.result(timeout=3)
        account = result["accounts"][0]
        self.assertFalse(account["ok"])
        self.assertEqual(account["error_kind"], "pending")
        self.assertNotIn("quota_usd", account)


if __name__ == "__main__":
    unittest.main()
