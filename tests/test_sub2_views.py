"""Sub2API 充值去重、分页完整性与监控统计范围的合成回归。"""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from integrations.contracts import ErrorCategory, Feature
from integrations.providers import Credentials, HttpResponse, RequestContext
from integrations.router import Router
from integrations.service import account_snapshot, health_snapshot
from tests.test_stage_c import provider, sub2
from tests.test_sub2_account_usage import SITE


MODEL = "raw /模型+name"


def metric(rate=.99, *, empty=False):
    return {"success_rate":rate, "request_count":0, "rpm":0, "tpm":0,
            "duration":{"avg_ms":None if empty else 2100,"sample_count":0},
            "ttft":{"avg_ms":None if empty else 250,"sample_count":0}}


def snapshot(rate=.99, *, empty=False, complete=True):
    return {"metrics":metric(rate,empty=empty), "health":{"overall":"unknown" if empty else "healthy"},
            "coverage":{"requested_start":"2026-10-15T01:00:00Z", "requested_end":"2026-10-16T01:00:00Z",
                        "coverage_complete":complete},
            "trend":[{"bucket_start":"2026-10-16T00:00:00Z","metrics":metric(rate,empty=empty),
                      "health":{"overall":"unknown" if empty else "healthy"}}]}


def monitor(group="vip", rate=95, model=MODEL):
    return {"id":1,"name":"fixture", "primary_model":model,"primary_status":"operational",
            "primary_latency_ms":300,"group_name":group,"availability_7d":rate,"timeline":[]}


class Sub2ViewsTests(unittest.TestCase):
    def instance(self, *, orders=None, redemptions=None, summary=None, details=None, v1=None):
        def listing(value):
            if isinstance(value,HttpResponse): return value
            return sub2({"items":value or [],"total":len(value or [])})

        def remote(call):
            path=urlsplit(call["url"]).path
            self.assertEqual(call["method"],"GET")
            self.assertEqual(call["headers"]["Authorization"],"Bearer synthetic-panel")
            self.assertNotIn("Cookie",call["headers"])
            if path=="/api/v1/user/profile": return sub2({"id":299,"balance":2})
            if path=="/api/v1/payment/orders/my": return listing(orders)
            if path=="/api/v1/redeem/history": return listing(redemptions)
            if path=="/api/v1/channel-monitor-v2/snapshot": return summary if isinstance(summary,HttpResponse) else sub2(summary or snapshot())
            if path=="/api/v1/channel-monitor-v2/matrix": return details if isinstance(details,HttpResponse) else sub2(details or {"items":[]})
            if path=="/api/v1/channel-monitors": return v1 if isinstance(v1,HttpResponse) else sub2({"items":v1 or []})
            if path=="/api/v1/usage/dashboard/stats": return sub2({"total_actual_cost":1,"total_requests":3})
            if path=="/api/v1/groups/available": return sub2([])
            if path=="/api/v1/groups/rates": return sub2({})
            if path=="/api/v1/keys": return listing([])
            raise AssertionError("Unexpected route "+path)
        return provider("sub2api",remote,credentials=Credentials(access_token="synthetic-panel",account_id=299))

    def order(self, **changes):
        row={"id":1,"user_id":299,"order_type":"balance","status":"COMPLETED","amount":10,
             "pay_amount":70,"refund_amount":0,"currency":"CNY","completed_at":"2026-10-16T00:00:00Z",
             "recharge_code":"PRIVATE-CODE","out_trade_no":"PRIVATE-TRADE","provider_snapshot":{"secret":"PRIVATE"}}
        row.update(changes)
        return row

    def redeem(self, **changes):
        row={"id":2,"type":"balance","status":"used","value":10,"used_by":299,
             "code":"PRIVATE-CODE","notes":"PRIVATE-NOTES","used_at":"2026-10-16T00:00:00Z"}
        row.update(changes)
        return row

    def test_payment_and_its_redemption_are_not_double_counted_or_exposed(self):
        api,_=self.instance(orders=[self.order()],redemptions=[self.redeem(),self.redeem(id=3,value=2)])
        result=account_snapshot(Router(api),SITE)
        self.assertEqual(result["recharge_usd"],12)
        self.assertEqual(result["recharge_count"],2)
        self.assertEqual(len(result["topup_records"]["items"]),3)
        self.assertEqual(result["topup_records"]["payment_totals"]["CNY"]["paid"],70)
        self.assertIsNone(result["recharge_money"])
        for secret in ("PRIVATE-CODE","PRIVATE-TRADE","PRIVATE-NOTES"):
            self.assertNotIn(secret,json.dumps(result,ensure_ascii=False))

    def test_refunds_currencies_subscriptions_and_admin_changes_stay_separate(self):
        api,_=self.instance(orders=[self.order(status="PARTIALLY_REFUNDED",refund_amount=5),
                                   self.order(id=2,currency="USD",pay_amount=10),
                                   self.order(id=3,order_type="subscription",pay_amount=999),
                                   self.order(id=4,status="PENDING",completed_at=None)],
                            redemptions=[self.redeem(),self.redeem(id=3,type="admin_balance",value=-3),
                                         self.redeem(id=4,type="subscription",value=30)])
        data=api.fetch(Feature.TOPUPS).data
        self.assertEqual(data["credit_amount"],10)
        self.assertEqual(data["payment_totals"]["CNY"]["paid"],70)
        self.assertEqual(data["payment_totals"]["USD"]["paid"],10)
        self.assertEqual(data["refunded_credit_amount"],5)
        self.assertNotIn("net",data["payment_totals"]["CNY"])
        self.assertEqual(data["payment_totals"]["CNY"]["count"],1)

    def test_refund_request_is_not_completed_refund_and_excess_credit_is_rejected(self):
        api,_=self.instance(orders=[self.order(status="REFUND_REQUESTED",refund_amount=10)])
        self.assertEqual(api.fetch(Feature.TOPUPS).data["refunded_credit_amount"],0)
        api,_=self.instance(orders=[self.order(status="REFUNDED",refund_amount=11)])
        self.assertEqual(api.fetch(Feature.TOPUPS).error_category,ErrorCategory.PROTOCOL_ERROR)

    def test_missing_refunded_wallet_amount_stays_unknown_without_reducing_cash_paid(self):
        # 退款状态已完成但未给出钱包退款额时，不能填零，也不能从付款金额猜退款现金。
        api,_=self.instance(orders=[self.order(status="REFUNDED",refund_amount=None)],
                            redemptions=[self.redeem()])
        result=api.fetch(Feature.TOPUPS)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["credit_amount"],10)
        self.assertIsNone(result.data["refunded_credit_amount"])
        self.assertEqual(result.data["payment_totals"]["CNY"],{"paid":70,"count":1})

    def test_zero_records_are_real_zero_but_a_missing_source_is_partial(self):
        api,_=self.instance()
        result=api.fetch(Feature.TOPUPS)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["credit_amount"],0)
        api,_=self.instance(redemptions=HttpResponse(404,{"message":"missing"}),orders=[self.order()])
        result=api.fetch(Feature.TOPUPS)
        self.assertEqual(result.error_category,ErrorCategory.PARTIAL_DATA)
        self.assertIsNone(result.data["credit_amount"])
        self.assertFalse(result.data["sources"]["redemptions"]["complete"])

    def test_complete_pagination_reads_all_pages_and_rejects_truncated_legacy_arrays(self):
        rows=[self.redeem(id=i) for i in range(1,102)]
        def remote(call):
            u=urlsplit(call["url"])
            if u.path.endswith("/profile"): return sub2({"id":299,"balance":2})
            if u.path.endswith("/orders/my"): return sub2({"items":[],"total":0})
            page=int(parse_qs(u.query)["page"][0])
            return sub2({"items":rows[(page-1)*100:page*100],"total":101,"page_size":100})
        api,transport=provider("sub2api",remote,credentials=Credentials(access_token="synthetic-panel",account_id=299))
        result=api.fetch(Feature.TOPUPS)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["credit_count"],101)
        self.assertEqual(len(transport.calls),4)
        api,_=self.instance(redemptions=sub2([self.redeem()]))
        result=api.fetch(Feature.TOPUPS)
        self.assertEqual(result.error_category,ErrorCategory.PARTIAL_DATA)
        self.assertIsNone(result.data["credit_amount"])

    def test_cross_account_order_or_redeem_is_rejected(self):
        for args in ({"orders":[self.order(user_id=300)]},{"redemptions":[self.redeem(used_by=300)]}):
            api,_=self.instance(**args)
            self.assertEqual(api.fetch(Feature.TOPUPS).error_category,ErrorCategory.ACCOUNT_MISMATCH)

    def test_actual_topup_failure_preserves_previous_records_amount_and_time(self):
        api,_=self.instance(redemptions=[self.redeem()])
        old=account_snapshot(Router(api),SITE)
        api,_=self.instance(orders=HttpResponse(403,{"message":"forbidden"}))
        result=account_snapshot(Router(api),SITE,previous=old)
        self.assertTrue(result["topups_stale"])
        self.assertFalse(result["topups_complete"])
        self.assertEqual(result["recharge_usd"],10)
        self.assertEqual(result["topups_updated_at"],old["topups_updated_at"])
        self.assertIn("HTTP 403",result["topups_error"])

    def test_v2_uses_exact_filter_converts_ratio_once_and_keeps_tpm_separate(self):
        raw=snapshot();raw["metrics"]["tpm"]=1200
        details={"items":[{"model":MODEL,"group_id":8,"group_name":"vip",
                            "metrics":metric(0),"health":{"overall":"critical"}}]}
        api,transport=self.instance(summary=raw,details=details)
        result=health_snapshot(Router(api),MODEL)
        m=result["model_summary"]
        self.assertEqual(m["success_rate"],99)
        self.assertEqual(m["traffic_tpm"],1200)
        self.assertIsNone(m["avg_tps"])
        self.assertEqual(result["groups"][0]["success_rate"],0)
        self.assertTrue(result["groups"][0]["has_samples"])
        self.assertEqual(len(transport.calls),3)
        for call in transport.calls[1:]:
            q=parse_qs(urlsplit(call["url"]).query)
            self.assertEqual(q["model"],[MODEL]);self.assertEqual(q["range"],["24h"])
        self.assertEqual(parse_qs(urlsplit(transport.calls[-1]["url"]).query)["group_by"],["platform_group_model"])

    def test_redacted_zero_counts_do_not_clear_real_zero_success(self):
        api,_=self.instance(summary=snapshot(0))
        self.assertEqual(health_snapshot(Router(api),MODEL)["model_summary"]["success_rate"],0)
        api,_=self.instance(summary=snapshot(0,empty=True))
        self.assertIsNone(health_snapshot(Router(api),MODEL)["model_summary"]["success_rate"])

    def test_v2_empty_matrix_placeholders_are_ignored_without_binding_model(self):
        # 上游为未匹配任何模型的分组保留空行；既不报错，也不把空行冒充请求模型的分组。
        placeholder={"group_id":3,"group_name":"empty","platform":"openai","buckets":[],
                     "metrics":metric(0,empty=True),"health":{"overall":"unknown"}}
        real={"model":MODEL,"group_id":8,"group_name":"vip","buckets":[],
              "metrics":metric(0),"health":{"overall":"critical"}}
        for model_field in ({},{"model":""}):
            with self.subTest(model_field=model_field):
                empty={**placeholder,**model_field}
                api,_=self.instance(details={"items":[empty]})
                result=api.fetch(Feature.HEALTH_DETAIL,model=MODEL)
                self.assertTrue(result.ok)
                self.assertEqual(result.data["groups"],[])
                api,_=self.instance(details={"items":[empty,real]})
                result=api.fetch(Feature.HEALTH_DETAIL,model=MODEL)
                self.assertTrue(result.ok)
                self.assertEqual([row["group_id"] for row in result.data["groups"]],[8])
                self.assertEqual(result.data["groups"][0]["success_rate"],0)

    def test_v2_missing_model_with_samples_or_ambiguous_buckets_is_rejected(self):
        placeholder={"group_id":3,"group_name":"empty","buckets":[],
                     "metrics":metric(0,empty=True),"health":{"overall":"unknown"}}
        cases=[{"metrics":metric(.5,empty=True)}, {"metrics":metric(0)},
               {"metrics":{**metric(0,empty=True),"rpm":1}},
               {"metrics":{**metric(0,empty=True),"tpm":1}},
               {"health":{"overall":"critical"}}, {"health":{}},
               {"buckets":[{}]}, {"buckets":None},
               {"buckets":{}}, {"model":None}, {"model":1}, {"model":"other"}]
        rows=[{**placeholder,**changes} for changes in cases]
        rows.append({key:value for key,value in placeholder.items() if key!="buckets"})
        for row in rows:
            with self.subTest(row=row):
                api,_=self.instance(details={"items":[row]})
                self.assertEqual(api.fetch(Feature.HEALTH_DETAIL,model=MODEL).error_category,
                                 ErrorCategory.PROTOCOL_ERROR)

    def test_v2_empty_placeholder_requires_valid_group_and_metric_shapes(self):
        placeholder={"group_id":3,"group_name":"empty","buckets":[],
                     "metrics":metric(0,empty=True),"health":{"overall":"unknown"}}
        for changes in ({"group_id":None},{"group_name":None},{"metrics":[]},{"health":[]}):
            with self.subTest(changes=changes):
                api,_=self.instance(details={"items":[{**placeholder,**changes}]})
                self.assertEqual(api.fetch(Feature.HEALTH_DETAIL,model=MODEL).error_category,
                                 ErrorCategory.PROTOCOL_ERROR)

    def test_invalid_ratio_or_other_model_details_do_not_fabricate_data(self):
        api,_=self.instance(summary=snapshot(99))
        self.assertEqual(api.fetch(Feature.HEALTH_SUMMARY,recent_models=[MODEL]).error_category,ErrorCategory.PROTOCOL_ERROR)
        api,_=self.instance(details={"items":[{"model":"other","group_id":1,"group_name":"vip",
                                             "metrics":metric(),"health":{"overall":"healthy"}}]})
        self.assertEqual(api.fetch(Feature.HEALTH_DETAIL,model=MODEL).error_category,ErrorCategory.PROTOCOL_ERROR)

    def test_v1_fallback_preserves_7_day_probe_scope_and_never_averages_monitors(self):
        missing=HttpResponse(404,{"message":"missing"})
        for monitors, rate in (([monitor()],95),([monitor("a",100),monitor("b",0)],None)):
            api,transport=self.instance(summary=missing,v1=monitors)
            result=health_snapshot(Router(api),MODEL)
            self.assertEqual(result["model_summary"]["success_rate"] if result["model_summary"] else None,rate)
            self.assertEqual(result["source"],"sub2api_monitor_v1")
            self.assertEqual(result["groups"][0]["window_label"],"7天探测")
            self.assertEqual(len(transport.calls),3)
            self.assertFalse(any("/matrix" in call["url"] for call in transport.calls))

    def test_explicit_mode_mismatch_allows_v1_but_generic_forbidden_does_not(self):
        disabled=HttpResponse(403,{"code":403,"reason":"CHANNEL_MONITOR_MODE_MISMATCH","message":"mode mismatch"})
        api,_=self.instance(summary=disabled,v1=[monitor()])
        self.assertEqual(health_snapshot(Router(api),MODEL)["model_summary"]["success_rate"],95)
        for code in (401,403,429):
            api,transport=self.instance(summary=HttpResponse(code,{"message":"denied"}))
            with self.assertRaises(Exception): health_snapshot(Router(api),MODEL)
            self.assertEqual(len(transport.calls),2)

    def test_empty_disabled_monitor_does_not_mean_model_failure(self):
        api,_=self.instance(summary=HttpResponse(404,{"message":"missing"}),v1=[])
        result=health_snapshot(Router(api),MODEL)
        self.assertIsNone(result["model_summary"])
        self.assertEqual(result["groups"],[])

    def test_unrelated_primary_model_is_not_reused_for_extra_models(self):
        row=monitor(model="other");row["extra_models"]=[{"model":MODEL,"status":"operational","latency_ms":200}]
        api,_=self.instance(summary=HttpResponse(404,{"message":"missing"}),v1=[row])
        self.assertIsNone(health_snapshot(Router(api),MODEL)["model_summary"])

    def test_v1_zero_without_recent_samples_is_unknown_but_recent_failures_stay_zero(self):
        missing=HttpResponse(404,{"message":"missing"})
        row=monitor(rate=0);row["primary_status"]="failed"
        api,_=self.instance(summary=missing,v1=[row])
        self.assertIsNone(health_snapshot(Router(api),MODEL)["model_summary"]["success_rate"])
        row["timeline"]=[{"checked_at":datetime.now(timezone.utc).isoformat(),"status":"failed"}]
        api,_=self.instance(summary=missing,v1=[row])
        self.assertEqual(health_snapshot(Router(api),MODEL)["model_summary"]["success_rate"],0)

    def test_v1_stale_or_future_samples_do_not_make_zero_percent_a_recent_failure(self):
        # 七天前或明显位于未来的探测记录，不能把默认 0% 解释成本期真实失败。
        now=datetime.now(timezone.utc)
        for checked_at in (now-timedelta(days=8),now+timedelta(hours=1)):
            with self.subTest(checked_at=checked_at.isoformat()):
                row=monitor(rate=0)
                row["primary_status"]="failed"
                row["timeline"]=[{"checked_at":checked_at.isoformat(),"status":"failed"}]
                api,_=self.instance(summary=HttpResponse(404,{"message":"missing"}),v1=[row])
                result=health_snapshot(Router(api),MODEL)
                self.assertIsNone(result["model_summary"]["success_rate"])
                self.assertFalse(result["model_summary"]["has_samples"])
                self.assertEqual(result["model_summary"]["window_label"],"7天探测")

    def test_failed_summary_retains_original_metric_and_coverage_warning(self):
        api,_=self.instance(summary=snapshot(complete=False))
        old=health_snapshot(Router(api),MODEL)
        self.assertFalse(old["model_summary"]["coverage_complete"])
        api,_=self.instance(summary=HttpResponse(500,{"message":"server error"}))
        result=health_snapshot(Router(api),MODEL,previous=copy.deepcopy(old))
        self.assertTrue(result["model_summary_stale"])
        self.assertEqual(result["model_summary"]["fetched_at"],old["model_summary"]["fetched_at"])

    def test_cancelled_budget_and_invalid_model_do_not_trigger_fallback_requests(self):
        api,transport=self.instance()
        self.assertEqual(api.fetch(Feature.HEALTH_SUMMARY,recent_models=[]).error_category,ErrorCategory.VALIDATION_ERROR)
        self.assertFalse(transport.calls)
        result=api.fetch(Feature.TOPUPS,context=RequestContext(should_stop=lambda:True))
        self.assertEqual(result.error_category,ErrorCategory.CANCELLED)
        self.assertFalse(transport.calls)


if __name__ == "__main__":
    unittest.main()
