"""单日用量（金额 + Token）的取数、降级、预算保护与口径统一回归。

全部使用脚本化上游响应，不访问任何真实站点。
口径约定（与后端、前端三处一致，改一处要改三处）：
  · 金额 = 站点口径「今日」的实际成本；New API 由区间额度 ÷ quota_per_unit 折算。
  · Token = 输入 + 输出，**不含缓存**（Sub2API 的 today_tokens 含缓存，故弃用）。
"""
import unittest
from urllib.parse import urlsplit

from integrations.contracts import Feature
from integrations.providers import Credentials, HttpResponse, RequestContext, daily_window
from integrations.router import Router
from integrations.service import account_snapshot
from tests.test_stage_c import listing, newapi, provider, sub2


NEWAPI_SITE = {"id": "fixture", "name": "Fixture", "base_url": "https://fixture.invalid"}
SUB2_CREDENTIALS = Credentials(access_token="synthetic-panel", account_id=299, model_api_key="")


def newapi_remote(*, stat=0, data_rows=None, log_rows=None, log_total=None, quota_per_unit=500000):
    """按路径派发的脚本化上游；没覆盖到的路径直接断言失败，避免测试悄悄放过意外请求。"""
    def remote(call):
        path = urlsplit(call["url"]).path
        if path == "/api/user/self":
            return newapi({"id": 7, "quota": 1000000, "used_quota": 500000})
        if path == "/api/status":
            return newapi({"quota_per_unit": quota_per_unit})
        if path == "/api/user/self/groups":
            return newapi({"default": 1})
        if path == "/api/log/self/stat":
            return stat if isinstance(stat, HttpResponse) else newapi({"quota": stat, "rpm": 0, "tpm": 0})
        if path == "/api/data/self":
            return data_rows if isinstance(data_rows, HttpResponse) else newapi(data_rows or [])
        if path == "/api/log/self":
            return log_rows if isinstance(log_rows, HttpResponse) else listing(log_rows or [], total=log_total)
        if path in {"/api/user/topup/self", "/api/token/"}:
            return listing([])
        raise AssertionError("Unexpected path: " + path)
    return remote


def sub2_remote(stats):
    def remote(call):
        path = urlsplit(call["url"]).path
        if path == "/api/v1/user/profile":
            return sub2({"id": 299, "balance": 2, "username": "fixture"})
        if path == "/api/v1/usage/dashboard/stats":
            return stats if isinstance(stats, HttpResponse) else sub2(stats)
        raise AssertionError("Unexpected path: " + path)
    return remote


class NewApiDailyTests(unittest.TestCase):
    def test_aggregates_give_amount_tokens_and_requests(self):
        instance, _ = provider("newapi", newapi_remote(
            stat=28903, data_rows=[{"token_used": 37882, "count": 64, "quota": 28903}]))
        result = instance.fetch(Feature.USAGE)
        self.assertTrue(result.ok)
        daily = result.data["daily"]
        self.assertEqual(daily["date"], daily_window()[0])
        self.assertEqual(daily["raw_quota"], 28903)
        self.assertEqual(daily["tokens"], 37882)
        self.assertEqual(daily["requests"], 64)
        self.assertEqual(daily["source"], "data.self")
        self.assertTrue(daily["complete"])
        self.assertIsNone(daily["error"])

    def test_empty_aggregate_with_usage_falls_back_to_log_pages(self):
        """站点没开「数据导出」时聚合表恒空：只有确实有用量才值得翻明细。"""
        instance, transport = provider("newapi", newapi_remote(
            stat=28903, data_rows=[],
            log_rows=[{"prompt_tokens": 965, "completion_tokens": 40, "quota": 28}], log_total=64))
        daily = instance.fetch(Feature.USAGE).data["daily"]
        self.assertEqual(daily["tokens"], 1005)          # 输入 + 输出
        self.assertEqual(daily["requests"], 64)          # 请求数取自 total，不是本页条数
        self.assertEqual(daily["source"], "log.self")
        self.assertTrue(daily["complete"])
        self.assertIn("/api/log/self", [urlsplit(c["url"]).path for c in transport.calls])

    def test_empty_aggregate_without_usage_is_zero_not_unknown(self):
        """当天没用量就是 0；不能因为聚合表空就渲染成「取不到」。"""
        instance, transport = provider("newapi", newapi_remote(stat=0, data_rows=[]))
        daily = instance.fetch(Feature.USAGE).data["daily"]
        self.assertEqual(daily["tokens"], 0)
        self.assertEqual(daily["requests"], 0)
        self.assertTrue(daily["complete"])
        self.assertIsNone(daily["error"])
        self.assertNotIn("/api/log/self", [urlsplit(c["url"]).path for c in transport.calls])

    def test_pagination_over_limit_marks_incomplete(self):
        """翻满上限只给下界，绝不把部分和当成全天用量；请求数仍取自 total。"""
        rows = [{"prompt_tokens": 1, "completion_tokens": 1, "quota": 1} for _ in range(100)]
        instance, _ = provider("newapi", newapi_remote(
            stat=100, data_rows=[], log_rows=rows, log_total=5000))
        daily = instance.fetch(Feature.USAGE).data["daily"]
        self.assertEqual(daily["tokens"], 2000)          # 10 页 × 100 条 × 2
        self.assertEqual(daily["requests"], 5000)
        self.assertFalse(daily["complete"])
        self.assertIn("只统计到部分", daily["error"])

    def test_daily_failure_never_breaks_cumulative_usage(self):
        instance, _ = provider("newapi", newapi_remote(
            stat=HttpResponse(500, {"success": False}), data_rows=HttpResponse(404, {"success": False})))
        result = instance.fetch(Feature.USAGE)
        self.assertTrue(result.ok)                        # 累计用量照旧成功
        self.assertEqual(result.data["raw_used_quota"], 500000)
        self.assertIsNone(result.data["daily"]["tokens"])
        self.assertIsNotNone(result.data["daily"]["error"])

    def test_shared_budget_is_not_consumed_by_daily(self):
        """单日是增强项：预算不够就主动放弃，不能把账户刷新的预算耗光。"""
        instance, transport = provider("newapi", newapi_remote(stat=0, data_rows=[]))
        result = instance.fetch(Feature.USAGE, context=RequestContext(seconds=2))
        self.assertTrue(result.ok)
        daily = result.data["daily"]
        self.assertIn("预算不足", daily["error"])
        paths = [urlsplit(call["url"]).path for call in transport.calls]
        self.assertNotIn("/api/data/self", paths)
        self.assertNotIn("/api/log/self/stat", paths)


class Sub2ApiDailyTests(unittest.TestCase):
    STATS = {"total_actual_cost": 3.5, "total_requests": 17,
             "today_actual_cost": 0.9853101375, "today_requests": 118,
             "today_input_tokens": 2409075, "today_output_tokens": 32037,
             "today_cache_creation_tokens": 0, "today_cache_read_tokens": 6866743,
             "today_tokens": 9307855}

    def usage(self, stats=None):
        instance, transport = provider("sub2api", sub2_remote(stats or self.STATS),
                                       credentials=SUB2_CREDENTIALS)
        return instance.fetch(Feature.USAGE, scope="account"), transport

    def test_today_fields_are_read_without_extra_request(self):
        result, transport = self.usage()
        self.assertTrue(result.ok)
        daily = result.data["daily"]
        self.assertAlmostEqual(daily["amount"], 0.9853101375)
        # Token 只算输入 + 输出：9307855 里的 6866743 是缓存读，不能被算进来
        self.assertEqual(daily["tokens"], 2409075 + 32037)
        self.assertEqual(daily["cache_tokens"], 6866743)
        self.assertEqual(daily["requests"], 118)
        self.assertEqual(daily["source"], "dashboard.stats")
        self.assertTrue(daily["complete"])
        # 同一个 stats 回包就够，没有为了单日再多发一次请求
        self.assertEqual([urlsplit(c["url"]).path for c in transport.calls].count(
            "/api/v1/usage/dashboard/stats"), 1)

    def test_missing_today_fields_are_reported_not_zero(self):
        result, _ = self.usage({"total_actual_cost": 3.5, "total_requests": 17})
        self.assertTrue(result.ok)
        daily = result.data["daily"]
        self.assertEqual(daily["error"], "站点未返回今日用量")
        self.assertIsNone(daily["tokens"])
        self.assertFalse(daily["complete"])


class ServiceProjectionTests(unittest.TestCase):
    def test_newapi_raw_quota_uses_wallet_scale_and_keeps_cumulative(self):
        instance, _ = provider("newapi", newapi_remote(
            stat=28903, data_rows=[{"token_used": 37882, "count": 64, "quota": 28903}]))
        snapshot = account_snapshot(Router(instance), NEWAPI_SITE)
        daily = snapshot["daily_usage"]
        self.assertAlmostEqual(daily["amount"], 28903 / 500000)
        self.assertEqual(daily["unit"], "USD")
        self.assertEqual(daily["tokens"], 37882)
        self.assertEqual(daily["requests"], 64)
        # 新增单日不能影响既有口径
        self.assertEqual(snapshot["used_quota_usd"], 1)
        self.assertTrue(snapshot["usage_complete"])

    def test_unknown_scale_leaves_amount_unknown(self):
        """换算比例拿不到时金额就是未知，不能拿原始额度冒充美元。"""
        instance, _ = provider("newapi", newapi_remote(
            stat=28903, data_rows=[{"token_used": 10, "count": 1, "quota": 28903}], quota_per_unit=0))
        snapshot = account_snapshot(Router(instance), NEWAPI_SITE)
        self.assertIsNone(snapshot["daily_usage"]["amount"])
        self.assertEqual(snapshot["daily_usage"]["tokens"], 10)


if __name__ == "__main__":
    unittest.main()
