"""Sub2API 账户累计实际费用、Key 用量隔离及未接入能力的回归。"""
import copy
import unittest
from urllib.parse import urlsplit

from integrations.contracts import Feature, ErrorCategory
from integrations.providers import Credentials, HttpResponse
from integrations.router import Router
from integrations.service import account_snapshot
from tests.test_stage_c import provider, sub2


SITE = {"id":"fixture", "type":"sub2api", "base_url":"https://fixture.invalid",
        "auth":{"access_token":"synthetic-panel", "account_id":299}}


class Sub2AccountUsageTests(unittest.TestCase):
    def instance(self, stats=None, *, profile_id=299, model_key=False):
        stats = {"total_actual_cost":3.5, "total_cost":99, "today_actual_cost":0.5,
                 "total_requests":17} if stats is None else stats

        def remote(call):
            path = urlsplit(call["url"]).path
            self.assertEqual(call["method"], "GET")
            self.assertNotIn("Cookie", call["headers"])
            if path == "/v1/usage":
                self.assertEqual(call["headers"]["Authorization"], "Bearer sk-synthetic-model")
                return HttpResponse(200, {"mode":"quota_limited", "remaining":100, "unit":"USD"})
            self.assertEqual(call["headers"]["Authorization"], "Bearer synthetic-panel")
            if path == "/api/v1/user/profile":
                return sub2({"id":profile_id,"balance":2,"username":"fixture"})
            if path == "/api/v1/usage/dashboard/stats":
                return stats if isinstance(stats, HttpResponse) else sub2(stats)
            if path == "/api/v1/groups/available":
                return sub2([{"id":1,"name":"default"}])
            if path == "/api/v1/groups/rates":
                return sub2({})
            if path == "/api/v1/keys":
                return sub2({"items":[], "total":0})
            if path in {"/api/v1/payment/orders/my", "/api/v1/redeem/history"}:
                return sub2({"items":[], "total":0})
            raise AssertionError("Unexpected path: " + path)

        credentials = Credentials(access_token="synthetic-panel", account_id=299,
                                  model_api_key="sk-synthetic-model" if model_key else "")
        return provider("sub2api", remote, credentials=credentials)

    def test_access_token_reads_account_actual_cost_not_standard_price_or_today(self):
        instance, transport = self.instance()
        result = account_snapshot(Router(instance), SITE)
        self.assertTrue(result["ok"])
        self.assertEqual(result["used_quota_usd"],3.5)
        self.assertEqual(result["quota_usd"],2)
        self.assertEqual(result["request_count"],17)
        self.assertTrue(result["usage_complete"])
        self.assertEqual(result["features"]["usage"]["account_id"],299)
        self.assertEqual(result["features"]["usage"]["data"]["period"],"all_time")
        self.assertTrue(result["topups_supported"])
        self.assertTrue(result["recharge_supported"])
        self.assertEqual(result["recharge_usd"],0)
        paths = [urlsplit(call["url"]).path for call in transport.calls]
        self.assertEqual(paths.count("/api/v1/user/profile"),1)
        self.assertNotIn("/api/user/topup/self",paths)
        self.assertNotIn("/v1/usage",paths)

    def test_model_key_does_not_replace_account_total_when_panel_token_exists(self):
        instance, _ = self.instance(model_key=True)
        site = copy.deepcopy(SITE)
        site["auth"]["model_api_key"]="sk-synthetic-model"
        result = account_snapshot(Router(instance),site)
        self.assertEqual(result["features"]["usage"]["data"]["usage_scope"],"account")
        self.assertEqual(result["used_quota_usd"],3.5)
        self.assertEqual(result["quota_usd"],2)

    def test_account_mismatch_stops_before_reading_statistics(self):
        instance, transport = self.instance(profile_id=300)
        result = instance.fetch(Feature.USAGE,scope="account")
        self.assertEqual(result.error_category,ErrorCategory.ACCOUNT_MISMATCH)
        self.assertEqual(len(transport.calls),1)

    def test_missing_actual_cost_never_substitutes_list_price(self):
        instance, _ = self.instance({"total_cost":99,"total_requests":17})
        result = instance.fetch(Feature.USAGE,scope="account")
        self.assertEqual(result.error_category,ErrorCategory.PARTIAL_DATA)
        self.assertIsNone(result.data)

    def test_zero_is_valid_but_negative_boolean_and_fractional_count_are_rejected(self):
        instance, _ = self.instance({"total_actual_cost":0,"total_requests":0})
        result = instance.fetch(Feature.USAGE,scope="account")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["actual_cost"],0)
        for stats in ({"total_actual_cost":True,"total_requests":1},
                      {"total_actual_cost":-1,"total_requests":1},
                      {"total_actual_cost":1,"total_requests":1.5}):
            with self.subTest(stats=stats):
                instance, _ = self.instance(stats)
                self.assertEqual(instance.fetch(Feature.USAGE,scope="account").error_category,ErrorCategory.PROTOCOL_ERROR)

    def test_real_usage_failure_retains_previous_amount_and_failure_evidence(self):
        first, _ = self.instance()
        previous = account_snapshot(Router(first),SITE)
        instance, _ = self.instance(HttpResponse(403,{"message":"forbidden"}))
        result = account_snapshot(Router(instance),SITE,previous=previous)
        self.assertEqual(result["used_quota_usd"],3.5)
        self.assertFalse(result["usage_complete"])
        self.assertIn("HTTP 403",result["usage_error"])
        self.assertTrue(result["features"]["usage"]["stale"])
        self.assertEqual(result["features"]["usage"]["last_success_at"],previous["features"]["usage"]["last_success_at"])


if __name__ == "__main__":
    unittest.main()
