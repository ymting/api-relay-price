"""阶段 A 回归：合成远端、临时快照；不读取用户站点或凭据。"""
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import fetch_prices as fp


BASE = "https://fixture.invalid"
SITE = {"id": "fixture", "type": "newapi", "enabled": True,
        "base_url": BASE, "auth": {"cookie": "synthetic-cookie"}}


def page(items, total=None, **extra):
    data = {"items": items, **extra}
    if total is not None:
        data["total"] = total
    return 200, {"success": True, "data": data}


class ScriptedClient:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.writes = []
        self.stop_check = None
        self.timeout = 12
        self.tries = 3
        self.auth_header = {}

    def get_json(self, url):
        self.calls.append(url)
        response = next(self.replies)
        if isinstance(response, Exception):
            raise response
        return response

    def request(self, url, method="GET", body=None, headers=None):
        self.writes.append((url, method, body, self.tries))
        return 200, '{"success":true}'


class TierTests(unittest.TestCase):
    def test_condition_direction_boundaries_and_nested_tiers(self):
        low = 'tier("standard", p*2 + c*10 + cr*0.2 + cc*2.5 + cc1h*4)'
        high = 'tier("long", p*4 + c*20)'
        for expression in (
            f"len >= 272000 ? {high} : {low}",
            f"len > 272000 ? {high} : {low}",
            f"len < 272000 ? {low} : {high}",
            f"len <= 272000 ? {low} : {high}",
            f"272000 <= len ? {high} : {low}",
            f"len > 0 ? {high} : {low}",
            f"len < 0 ? {high} : {low}",
            f"len >= 1000000 ? {high} : (len >= 272000 ? {high} : {low})",
            f"len < 272000 ? (len < 1000 ? {low} : {high}) : {high}",
            f"((p < 100000)) ? ({low}) : ({high})",
        ):
            with self.subTest(expression=expression):
                result = fp.parse_tier_coefficients(expression)
                self.assertEqual(result["input"], 2)
                self.assertEqual(result["output"], 10)
                self.assertEqual(result["cache_write_1h"], 4)

    def test_linear_math_is_complete_and_sums_repeated_variables(self):
        self.assertEqual(fp.parse_tier_coefficients('tier("base", 1e+1*p + p*2 + 0*c)'),
                         {"input": 12, "output": 0})
        self.assertEqual(fp.parse_tier_coefficients("p*3e-6 + 2*c"), {"input": 3e-6, "output": 2})
        for expr in ('len + 1 ? tier("x", p*2) : tier("y", p*1)',
                     'tier("x", p*2 + c*p)', 'tier("x", p*1e999)',
                     'tier("x", p*2)/2', 'tier("x", p*2',
                     'tier("x", p*2 + img*3)',
                     'tier("x", p*2 + 7)', '__import__("os").system("echo nope")'):
            with self.subTest(expr=expr):
                self.assertIsNone(fp.parse_tier_coefficients(expr))

    def test_unknown_expression_never_falls_back_to_free_price(self):
        adapter = fp.NewApiAdapter(SITE, ScriptedClient([]))
        result = adapter.parse({"group_ratio": {"default": 1}, "data": [{
            "model_name": "fixture-model", "quota_type": 0, "billing_mode": "tiered_expr",
            "billing_expr": 'tier("x", p*c)', "enable_groups": ["default"]}]})[0]
        self.assertIsNone(result["input"])
        self.assertFalse(result["pricing_verified"])
        self.assertEqual(result["billing_expr"], 'tier("x", p*c)')

    def test_low_tier_is_multiplied_by_its_own_group_rate(self):
        result = fp.NewApiAdapter(SITE, ScriptedClient([])).parse({"group_ratio": {"vip": .5}, "data": [{
            "model_name": "fixture-model", "quota_type": 0, "billing_mode": "tiered_expr",
            "billing_expr": 'len >= 272000 ? tier("long", p*4+c*20) : tier("base", p*2+c*10)',
            "enable_groups": ["vip"]}]})[0]
        self.assertEqual((result["input"], result["output"]), (1, 5))
        self.assertEqual(result["pricing_basis"], "len=0")

    def test_service_tier_default_prices_preserve_full_expression(self):
        base = '((len < 272000) ? tier("base", p*2+c*10+cr*0.2) : tier("long", p*4+c*15))'
        expr = (base + ' * (param("service_tier") == "priority" ? 2 : 1)'
                + ' * (param("service_tier") == "fast" ? 2 : 1)'
                + ' * (param("service_tier") == "ultrafast" ? 6 : 1)')
        result = fp.NewApiAdapter(SITE, ScriptedClient([])).parse({"group_ratio": {"gpt_pro": .22}, "data": [{
            "model_name": "fixture-model", "quota_type": 0, "billing_mode": "tiered_expr",
            "billing_expr": expr, "enable_groups": ["gpt_pro"]}]})[0]
        self.assertAlmostEqual(result["input"], .44)
        self.assertAlmostEqual(result["output"], 2.2)
        self.assertAlmostEqual(result["cache_read"], .044)
        self.assertTrue(result["pricing_verified"])
        self.assertEqual(result["service_tier_basis"], "unspecified")
        self.assertEqual(result["billing_base_expr"], base)
        self.assertEqual(result["billing_expr"], expr)

    def test_unknown_service_tier_factors_do_not_guess(self):
        base = '(tier("base", p*2+c*10))'
        for factor in ('param("other") == "fast" ? 2 : 1',
                       'param("service_tier") == "fast" ? 2 : 3',
                       'param("service_tier") != "fast" ? 2 : 1',
                       'param("service_tier") == "unknown" ? 2 : 1',
                       'param("service_tier") == "fast" ? 1e999 : 1',
                       'param("service_tier") == "fast" ? 2 : malicious()'):
            with self.subTest(factor=factor):
                self.assertIsNone(fp.parse_tier_coefficients(f'{base} * ({factor})'))
        self.assertIsNone(fp.parse_tier_coefficients(
            '(tier("base", p*c)) * (param("service_tier") == "fast" ? 2 : 1)'))


class Sub2PriceTests(unittest.TestCase):
    def fetch(self, pricing=None, group=None, wrapped=False):
        group = {"name": "vip", "rate_multiplier": 2, "user_rate_multiplier": .5, **(group or {})}
        group["models"] = [{"name": "fixture-model", "pricing": pricing or {
            "input_price": 3e-6, "output_price": 15e-6,
            "cache_read_price": .3e-6, "cache_write_price": 3.75e-6}}]
        payload = {"groups": [group]}
        if wrapped:
            payload = {"code": 0, "data": payload}
        client = ScriptedClient([(200, payload)])
        return fp.Sub2ApiAdapter({**SITE, "type": "sub2api"}, client).fetch()["offers"][0]

    def test_per_token_units_and_personal_rate(self):
        result = self.fetch(wrapped=True)
        self.assertAlmostEqual(result["input"], 1.5)
        self.assertAlmostEqual(result["output"], 7.5)
        self.assertAlmostEqual(result["cache_read"], .15)
        self.assertAlmostEqual(result["cache_write"], 1.875)
        self.assertEqual(result["group_ratio"], .5)
        self.assertTrue(result["pricing_verified"])

    def test_per_request_price_keeps_per_request_unit(self):
        result = self.fetch({"per_request_price": .04})
        self.assertEqual(result["per_call"], .02)
        self.assertIsNone(result["input"])
        self.assertEqual(result["price_unit"], "USD/request")

    def test_zero_rate_is_not_replaced_with_one(self):
        result = self.fetch(group={"user_rate_multiplier": 0})
        self.assertEqual(result["input"], 0)

    def test_unit_sentinel_and_invalid_values(self):
        for value in (3e-12, -1, "NaN", "Infinity", 1e308):
            with self.subTest(value=value), self.assertRaises(fp.FetchError):
                self.fetch({"input_price": value})

    def test_dynamic_metadata_enters_minima_as_base_rate(self):
        # 2026-10-16 行为变更：动态计费报价按底价参与比价，只标「待确认 · 参考价」，不再排除
        result = self.fetch(group={"time_pricing": {"enabled": True},
                                   "image_rate_independent": True, "image_rate_multiplier": 3})
        self.assertEqual(result["image_rate_multiplier"], 3)
        self.assertTrue(result["dynamic"])
        self.assertFalse(result["pricing_verified"])
        model = fp.build_models([result], .9)[0]
        self.assertAlmostEqual(model["min_input"], 1.5)
        self.assertAlmostEqual(
            fp.build_index({"models": [model]})["models"][0]["by_site"]["fixture"]["input"], 1.5)
        self.assertEqual(model["offers"][0]["input"], 1.5)

    def test_business_failure_and_missing_schema_do_not_empty_prices(self):
        for payload in ({"success": False, "groups": []}, {"code": 400, "groups": []}, {}):
            with self.subTest(payload=payload), self.assertRaises(fp.FetchError):
                fp.Sub2ApiAdapter(SITE, ScriptedClient([(200, payload)])).fetch()


class PaginationTests(unittest.TestCase):
    def test_f04_zero_records_is_success(self):
        client = ScriptedClient([page([], 0)])
        result = fp.read_newapi_pages(client, BASE + "/api/token/")
        self.assertTrue(result["complete"])
        self.assertEqual(result["items"], [])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(parse_qs(urlsplit(client.calls[0]).query), {"p": ["1"], "page_size": ["100"]})

    def test_f04_business_failure_is_not_empty_success(self):
        for reply in ((200, {"success": False, "data": {"items": [], "total": 0}}),
                      (403, {"message": "denied"}), (200, {"success": True, "data": {}})):
            client = ScriptedClient([reply])
            with self.subTest(reply=reply):
                self.assertFalse(fp.read_newapi_pages(client, BASE)["complete"])
                self.assertEqual(len(client.calls), 1)

    def test_f04_205_records_three_pages(self):
        client = ScriptedClient([page([{"id": i} for i in range(a, b)], 205)
                                 for a, b in ((1, 101), (101, 201), (201, 206))])
        result = fp.read_newapi_pages(client, BASE + "/api/token/?search=fixture")
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["items"]), 205)
        self.assertEqual([parse_qs(urlsplit(c).query)["p"][0] for c in client.calls], ["1", "2", "3"])
        self.assertTrue(all(parse_qs(urlsplit(c).query)["search"] == ["fixture"] for c in client.calls))

    def test_f04_overlap_ids_are_deduplicated(self):
        client = ScriptedClient([page([{"id": 1}, {"id": 2}], 3),
                                 page([{"id": 2}, {"id": 3}], 3)])
        result = fp.read_newapi_pages(client, BASE, page_size=2)
        self.assertTrue(result["complete"])
        self.assertEqual([r["id"] for r in result["items"]], [1, 2, 3])
        self.assertEqual(len(client.calls), 2)

    def test_f04_second_page_timeout_is_partial(self):
        client = ScriptedClient([page([{"id": i} for i in range(100)], 205), fp.FetchError("fixture timeout")])
        result = fp.read_newapi_pages(client, BASE)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(len(client.calls), 2)

    def test_missing_total_uses_reported_page_size_and_empty_end(self):
        client = ScriptedClient([page([{"id": 1}, {"id": 2}], page_size=2),
                                 page([], page_size=2)])
        self.assertTrue(fp.read_newapi_pages(client, BASE)["complete"])
        self.assertEqual(len(client.calls), 2)

    def test_changed_total_and_repeated_page_are_incomplete(self):
        for replies in (
            [page([{"id": 1}], 3), page([{"id": 2}], 4)],
            [page([{"id": 1}], 3), page([{"id": 1}], 3)],
            [page([{"id": 1}]), page([{"id": 1}])],
        ):
            client = ScriptedClient(replies)
            with self.subTest(replies=replies):
                self.assertFalse(fp.read_newapi_pages(client, BASE, page_size=1)["complete"])
                self.assertEqual(len(client.calls), 2)

    def test_short_page_cannot_override_total(self):
        result = fp.read_newapi_pages(ScriptedClient([page([{"id": 1}], 205)]), BASE)
        self.assertFalse(result["complete"])

    def test_invalid_total_never_becomes_missing_total_success(self):
        for total in ("bad", -1, 2.5, float("inf")):
            with self.subTest(total=total):
                result = fp.read_newapi_pages(ScriptedClient([page([], total)]), BASE)
                self.assertFalse(result["complete"])

    def test_safety_bound_when_total_never_arrives(self):
        client = ScriptedClient([page([{"id": i}]) for i in range(3)])
        self.assertFalse(fp.read_newapi_pages(client, BASE, max_pages=3, page_size=1)["complete"])
        self.assertEqual(len(client.calls), 3)

    def test_old_size_aliases_only_after_explicit_parameter_rejection(self):
        client = ScriptedClient([(400, {"message": "page_size unsupported"}),
                                 (422, {"message": "ps unsupported"}), page([], 0)])
        self.assertTrue(fp.read_newapi_pages(client, BASE)["complete"])
        self.assertEqual([next(k for k in ("page_size", "ps", "size") if k in parse_qs(urlsplit(c).query))
                          for c in client.calls], ["page_size", "ps", "size"])

    def test_cancel_prevents_next_request(self):
        client = ScriptedClient([page([{"id": 1}], 3)])
        client.stop_check = lambda: len(client.calls) == 1
        with self.assertRaisesRegex(fp.FetchError, "已手动停止"):
            fp.read_newapi_pages(client, BASE, page_size=1)
        self.assertEqual(len(client.calls), 1)


class AccountTests(unittest.TestCase):
    def snapshot(self, replies, user=None):
        client = ScriptedClient(replies)
        with patch.object(fp, "auth_client", return_value=(client, BASE, "cookie", user or {
            "id": 7, "group": "default", "quota": 500000, "used_quota": 500000})):
            return fp.fetch_account_snapshot(SITE), client

    def test_correct_topup_path_and_successful_zero(self):
        snapshot, client = self.snapshot([page([], 0), page([], 0)])
        self.assertEqual(urlsplit(client.calls[0]).path, "/api/user/topup/self")
        self.assertEqual(snapshot["recharge_usd"], 0)
        self.assertTrue(snapshot["recharge_supported"])
        self.assertTrue(snapshot["keys_complete"])
        self.assertEqual(snapshot["token_count"], 0)

    def test_failures_are_unknown_and_do_not_destroy_wallet(self):
        snapshot, _ = self.snapshot([(404, {}), (200, {"success": False})])
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["quota_usd"], 1)
        for field in ("recharge_count", "recharge_usd", "token_count", "tokens", "group_keys"):
            self.assertIsNone(snapshot[field])
        self.assertFalse(snapshot["topups_complete"])
        self.assertFalse(snapshot["keys_complete"])

    def test_missing_amount_is_unknown_not_zero(self):
        snapshot, _ = self.snapshot([page([{"id": 1, "status": "success"}], 1), page([], 0)],
                                    user={"id": 7, "group": "default"})
        self.assertIsNone(snapshot["quota_usd"])
        self.assertIsNone(snapshot["used_quota_usd"])
        self.assertIsNone(snapshot["recharge_usd"])
        self.assertEqual(snapshot["recharge_count"], 1)

    def test_failed_payments_excluded_and_all_pages_summed(self):
        snapshot, client = self.snapshot([page([{"id": 1, "status": "success", "amount": 500000, "money": 7},
                                               {"id": 2, "status": "pending", "amount": 900000}], 3, page_size=2),
                                           page([{"id": 3, "status": "success", "amount": 1000000, "money": 14}], 3, page_size=2),
                                           page([], 0)])
        self.assertEqual((snapshot["recharge_usd"], snapshot["recharge_count"], snapshot["recharge_money"]), (3, 2, 21))
        self.assertEqual(len(client.calls), 3)

    def test_partial_sections_keep_complete_snapshot_and_times_on_disk(self):
        server = importlib.import_module("server")
        previous = {"site": "fixture", "ok": True, "user_id": 7, "quota_usd": 8,
                    "fetched_at": "old-profile-time", "topups_updated_at": "old-topup-time",
                    "keys_updated_at": "old-key-time", "recharge_usd": 25, "recharge_count": 1,
                    "recharge_supported": True, "tokens": [{"id": 1}], "token_count": 1,
                    "group_keys": {"default": {"count": 1}}}
        snapshot, _ = self.snapshot([(403, {}), (403, {})])
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "accounts.json"
            fp.write_json_atomic(target, {"accounts": [previous]})
            with patch.object(server, "ACCOUNTS_FILE", target), patch.object(server, "load_sites", return_value=[SITE]), \
                 patch.object(server, "ensure_settings", return_value={}), patch.object(server, "log_line"), \
                 patch.object(server.integration_service, "account_snapshot", return_value=snapshot):
                server.refresh_accounts(["fixture"])
            saved = json.loads(target.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(saved["quota_usd"], 1)
        self.assertEqual(saved["recharge_usd"], 25)
        self.assertEqual(saved["token_count"], 1)
        self.assertEqual(saved["topups_updated_at"], "old-topup-time")
        self.assertEqual(saved["keys_updated_at"], "old-key-time")
        self.assertTrue(saved["topups_stale"])
        self.assertFalse(saved["keys_complete"])

    def test_other_user_data_is_not_reused(self):
        server = importlib.import_module("server")
        snapshot, _ = self.snapshot([(403, {}), (403, {})])
        previous = {"user_id": 99, "token_count": 12, "recharge_usd": 500}
        server.preserve_account_sections(snapshot, previous)
        self.assertIsNone(snapshot["token_count"])
        self.assertIsNone(snapshot["recharge_usd"])

    def test_concurrent_key_mutation_does_not_mark_partial_list_complete(self):
        server = importlib.import_module("server")
        previous = {"site": "fixture", "ok": True, "user_id": 7, "group": "default",
                    "keys_complete": False, "tokens": [{"id": 1, "group": "default", "enabled": True}],
                    "token_count": 1}
        refreshed = {**previous, "keys_complete": True, "quota_usd": 10}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "accounts.json"
            fp.write_json_atomic(target, {"accounts": [previous]})
            def fetch(*args, **kwargs):
                server.update_token_snapshot("fixture", deleted_id=1)
                return dict(refreshed)
            with patch.object(server, "ACCOUNTS_FILE", target), patch.object(server, "load_sites", return_value=[SITE]), \
                 patch.object(server, "ensure_settings", return_value={}), patch.object(server, "log_line"), \
                 patch.dict(server.TOKEN_REVISIONS, {}, clear=True), patch.object(server.integration_service, "account_snapshot", side_effect=fetch):
                server.refresh_accounts(["fixture"])
            saved = json.loads(target.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(saved["token_count"], 0)
        self.assertFalse(saved["keys_complete"])
        self.assertEqual(saved["quota_usd"], 10)


class KeyListSafetyTests(unittest.TestCase):
    def test_incomplete_list_prevents_create_and_export(self):
        for operation in ("create", "export"):
            client = ScriptedClient([page([{"id": i} for i in range(100)], 205), fp.FetchError("timeout")])
            session = (client, BASE, "cookie", {"id": 7, "group": "default"})
            with self.subTest(operation=operation), self.assertRaises(fp.FetchError):
                if operation == "create":
                    fp.create_newapi_token(SITE, name="fixture", session=session)
                else:
                    fp.resolve_export_key(SITE, session=session)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(client.writes, [])

    def test_export_finds_specified_key_after_first_hundred(self):
        rows = [{"id": i, "group": "default", "status": 1} for i in range(1, 206)]
        client = ScriptedClient([page(rows[:100], 205), page(rows[100:200], 205), page(rows[200:], 205)])
        with patch.object(fp, "get_newapi_token_key", return_value={"key": "sk-synthetic"}) as reveal:
            result = fp.resolve_export_key(SITE, token_id=205, create=False,
                session=(client, BASE, "cookie", {"group": "default"}))
        self.assertEqual(result["key"], "sk-synthetic")
        self.assertEqual(reveal.call_args.args[2], 205)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.writes, [])

    def test_create_post_is_single_attempt_and_ambiguous_matches_not_revealed(self):
        client = ScriptedClient([page([], 0), page([
            {"id": 1, "name": "same", "group": "vip"},
            {"id": 2, "name": "same", "group": "vip"}], 2)])
        with patch.object(fp, "get_newapi_token_key") as reveal, self.assertRaisesRegex(fp.FetchError, "无法唯一确认"):
            fp.create_newapi_token(SITE, name="same", group="vip",
                                  session=(client, BASE, "cookie", {"group": "vip"}))
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][3], 1)
        self.assertEqual(client.tries, 3)
        reveal.assert_not_called()

    def test_create_reconciles_all_pages_before_returning_new_id(self):
        old = [{"id": i, "name": "old", "group": "vip"} for i in range(1, 206)]
        new = {"id": 206, "name": "new", "group": "vip"}
        replies = [page(old[:100], 205), page(old[100:200], 205), page(old[200:], 205),
                   page(old[:100], 206), page(old[100:200], 206), page(old[200:] + [new], 206)]
        client = ScriptedClient(replies)
        with patch.object(fp, "get_newapi_token_key", return_value={"id": 206, "key": "sk-synthetic"}):
            result = fp.create_newapi_token(SITE, name="new", group="vip",
                session=(client, BASE, "cookie", {"group": "vip"}))
        self.assertEqual(result["id"], 206)
        self.assertEqual(len(client.calls), 6)
        self.assertEqual(len(client.writes), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
