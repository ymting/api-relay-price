"""阶段 C：固定上游响应形态、脚本化传输及临时凭据回写，禁止真实站点操作。"""
import ast
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from integrations.contracts import (AuthKind, ErrorCategory, Feature, FeatureStatus, ScopeSource, VisibilityScope)
from integrations.providers import (Credentials, CredentialSession, HttpResponse, NewApiProvider, ProviderFailure,
    RequestContext, Sub2ApiProvider, parse_key_billing, parse_sub2api_usage, price_with_key_multiplier)
from integrations.pricing import parse_newapi_pricing


SITE = {"id": "fixture", "name": "Fixture", "base_url": "https://fixture.invalid"}


def newapi(data=None, status=200, **extra):
    return HttpResponse(status, {"success": status < 400, "data": data, **extra})


def sub2(data=None, status=200):
    return HttpResponse(status, {"code": 0 if status < 400 else status, "data": data})


def listing(rows, total=None, *, sub=False):
    data = {"items": rows, "total": len(rows) if total is None else total, "page_size": 100}
    return sub2(data) if sub else newapi(data)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses if callable(responses) else iter(responses)
        self.calls = []
        self.lock = threading.Lock()

    def request(self, method, url, *, headers, body=None, timeout=25):
        call = {"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        with self.lock:
            self.calls.append(call)
            response = self.responses if callable(self.responses) else next(self.responses)
        if callable(response):
            response = response(call)
        if isinstance(response, Exception):
            raise response
        return response


class Hooks:
    def __init__(self):
        self.events = []

    def prepare(self, **metadata):
        self.events.append(("prepared", metadata))
        return "fixture-operation"

    def sent(self, operation_id):
        self.events.append(("sent", operation_id))

    def finish(self, operation_id, status, remote_id):
        self.events.append((status, remote_id))


def provider(platform, replies, credentials=None, *, session=None, **kwargs):
    transport = replies if isinstance(replies, FakeTransport) else FakeTransport(replies)
    credentials = credentials or (Credentials(management_token="synthetic-management", account_id=7, account_verified=True)
                                  if platform == "newapi" else Credentials(access_token="synthetic-panel", account_id=7, account_verified=True))
    session = session or CredentialSession(credentials)
    cls = NewApiProvider if platform == "newapi" else Sub2ApiProvider
    return cls(SITE, session, transport=transport, read_retries=0, **kwargs), transport


def bound_key(**changes):
    return Credentials(model_api_key="sk-synthetic-model", model_key_id=11, model_group_id=2,
                       model_account_id=7, model_binding_verified=True, **changes)


class BillingAndUsageTests(unittest.TestCase):
    def billing(self, credentials=None):
        return parse_key_billing({"object": "sub2api.key_billing", "schema_version": 1, "billing_scope": "token",
            "group_rate_multiplier": 2, "resolved_rate_multiplier": .5, "peak_rate_multiplier": 2,
            "effective_rate_multiplier": 1, "observed_at": "2026-10-15T08:00:00Z"},
            site_id="fixture", credentials=credentials or bound_key())

    def test_f05_effective_multiplier_is_applied_exactly_once(self):
        credentials = bound_key()
        billing = self.billing(credentials)
        price = price_with_key_multiplier(3e-6, billing, site_id="fixture", account_id=7,
                                         group_id=2, key_ref=credentials.key_ref)
        self.assertEqual(price, 3)
        self.assertEqual(billing["observed_at"], "2026-10-15T08:00:00Z")
        self.assertNotIn("amount", billing)

    def test_f05_multipliers_do_not_cross_account_group_key_or_site(self):
        credentials = bound_key()
        args = dict(site_id="fixture", account_id=7, group_id=2, key_ref=credentials.key_ref)
        for key, value in (("account_id", 8), ("group_id", 3), ("key_ref", "other-key"), ("site_id", "other-site")):
            with self.subTest(key=key), self.assertRaises(ProviderFailure):
                price_with_key_multiplier(3e-6, self.billing(credentials), **{**args, key: value})

    def test_f05_unverified_key_binding_is_not_repriced_as_an_account_price(self):
        credentials = Credentials(model_api_key="sk-synthetic")
        billing = self.billing(credentials)
        self.assertIsNone(billing["account_id"])
        with self.assertRaises(ProviderFailure):
            price_with_key_multiplier(3e-6, billing, site_id="fixture", account_id=None,
                                      group_id=None, key_ref=credentials.key_ref)

    def test_f05_simple_mode_404_only_disables_billing(self):
        instance, transport = provider("sub2api", [HttpResponse(404, {"error": {"type": "not_found_error",
            "message": "Billing information is not supported in simple mode"}}), sub2({"id": 7, "balance": 9})],
            credentials=bound_key(access_token="panel", account_id=7))
        self.assertEqual(instance.billing().status, FeatureStatus.UNSUPPORTED)
        result = instance.fetch(Feature.PROFILE)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["balance"], 9)
        self.assertEqual(len(transport.calls), 2)

    def usage(self, payload, credentials=None):
        return parse_sub2api_usage(payload, site_id="fixture", credentials=credentials or bound_key())

    def test_f06_wallet_uses_explicit_balance_and_deduplicates_by_account(self):
        first = self.usage({"mode": "unrestricted", "balance": 12, "remaining": 12, "unit": "USD"})
        second = self.usage({"mode": "unrestricted", "balance": 12, "remaining": 12, "unit": "USD"},
                            replace(bound_key(), model_api_key="sk-other", model_key_id=12))
        self.assertEqual(first["balance_kind"], "wallet")
        self.assertEqual(first["scope_id"], second["scope_id"])
        self.assertEqual(first["amount"], 12)

    def test_f06_total_key_quota_is_not_wallet(self):
        result = self.usage({"mode": "quota_limited", "remaining": 8, "unit": "USD",
                             "quota": {"limit": 10, "used": 2, "remaining": 8}, "isValid": True})
        self.assertEqual(result["balance_kind"], "key_quota")
        self.assertEqual(result["amount"], 8)
        self.assertTrue(result["scope_id"].startswith("fixture:key:"))

    def test_f06_window_limits_remain_separate_and_no_top_level_remaining_is_invented(self):
        result = self.usage({"mode": "quota_limited", "rate_limits": [
            {"window": "5h", "limit": 10, "used": 2, "remaining": 8},
            {"window": "1d", "limit": 100, "used": 50, "remaining": 50}]})
        self.assertIsNone(result["amount"])
        self.assertEqual([r["remaining"] for r in result["windows"]], [8, 50])
        self.assertTrue(result["windows_complete"])

    def test_f06_missing_window_stats_are_unknown(self):
        result = self.usage({"mode": "quota_limited"})
        self.assertIsNone(result["amount"])
        self.assertFalse(result["windows_complete"])
        self.assertEqual(result["unit"], "unknown")

    def test_f07_subscription_windows_are_not_added_together(self):
        result = self.usage({"mode": "unrestricted", "remaining": 3, "unit": "USD", "subscription": {
            "daily_limit_usd": 10, "daily_usage_usd": 7, "weekly_limit_usd": 100,
            "weekly_usage_usd": 40, "monthly_limit_usd": 300, "monthly_usage_usd": 100}})
        self.assertEqual(result["balance_kind"], "subscription")
        self.assertEqual(result["amount"], 3)
        self.assertEqual([row["limit"] for row in result["windows"]], [10, 100, 300])

    def test_f07_missing_subscription_and_remaining_never_become_wallet_zero(self):
        result = self.usage({"mode": "unrestricted", "planName": "subscription", "isValid": True, "unit": "USD"})
        self.assertEqual(result["balance_kind"], "unknown")
        self.assertIsNone(result["amount"])

    def test_f07_unlimited_subscription_is_not_negative_money(self):
        result = self.usage({"mode": "unrestricted", "remaining": -1, "subscription": {}})
        self.assertTrue(result["unlimited"])
        self.assertIsNone(result["amount"])

    def test_isvalid_expired_key_is_not_a_health_signal(self):
        instance, transport = provider("sub2api", [HttpResponse(200, {"mode": "quota_limited", "status": "expired", "isValid": True})],
                                        credentials=bound_key())
        result = instance.fetch(Feature.USAGE)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["key_status"], "expired")
        self.assertNotIn("health", result.data)
        self.assertEqual(instance.fetch(Feature.USAGE, scope="account").error_category, ErrorCategory.MISSING_CREDENTIAL)
        self.assertEqual(instance.fetch(Feature.HEALTH_SUMMARY, recent_models=["m"]).error_category, ErrorCategory.MISSING_CREDENTIAL)
        self.assertEqual(len(transport.calls), 1)


class ReadProviderTests(unittest.TestCase):
    def test_newapi_management_token_no_new_api_user_header(self):
        instance, transport = provider("newapi", [newapi({"id": 7, "quota": 0, "used_quota": 0})])
        result = instance.fetch(Feature.PROFILE)
        self.assertTrue(result.ok)
        self.assertEqual(result.account_id, 7)
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer synthetic-management")
        self.assertNotIn("New-Api-User", transport.calls[0]["headers"])

    def test_profile_reuse_never_crosses_credential_sessions(self):
        cache = {}
        first, first_transport = provider("newapi", [newapi({"id": 7, "used_quota": 10})])
        second, second_transport = provider("newapi", [newapi({"id": 7, "used_quota": 20})])
        for instance, amount in ((first, 10), (second, 20)):
            self.assertTrue(instance.fetch(Feature.PROFILE, context=RequestContext(profile_cache=cache)).ok)
            usage = instance.fetch(Feature.USAGE, context=RequestContext(profile_cache=cache))
            self.assertTrue(usage.ok)
            self.assertEqual(usage.data["raw_used_quota"], amount)
            self.assertEqual(usage.auth_kind, AuthKind.MANAGEMENT_TOKEN)
            self.assertEqual(usage.account_id, 7)
        # 单日用量会额外请求 /api/log/self/stat 与 /api/data/self，请求总数不该钉死；
        # 真正要钉的是「每个会话的 profile 只取了一次」，即 USAGE 复用了本会话的缓存。
        for transport in (first_transport, second_transport):
            profiles = [call for call in transport.calls if urlsplit(call["url"]).path == "/api/user/self"]
            self.assertEqual(len(profiles), 1)

    def test_legacy_user_header_is_negotiated_only_after_explicit_requirement(self):
        required = HttpResponse(403, {"success": False, "message": "无权进行此操作，未提供 New-Api-User"})
        instance, transport = provider("newapi", [required, newapi({"id": 7}), listing([])])
        self.assertTrue(instance.fetch(Feature.PROFILE).ok)
        self.assertTrue(instance.fetch(Feature.KEYS_LIST).ok)
        self.assertNotIn("New-Api-User", transport.calls[0]["headers"])
        self.assertEqual(transport.calls[1]["headers"]["New-Api-User"], "7")
        self.assertEqual(transport.calls[2]["headers"]["New-Api-User"], "7")
        self.assertTrue(all("Cookie" not in c["headers"] for c in transport.calls))

    def test_legacy_user_header_without_expected_id_is_not_guessed(self):
        required = HttpResponse(403, {"success": False, "message": "无权进行此操作，未提供 New-Api-User"})
        instance, transport = provider("newapi", [required], credentials=Credentials(management_token="synthetic"))
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.AUTH_USER_HEADER_REQUIRED)
        self.assertEqual(len(transport.calls), 1)

    def test_legacy_user_header_never_accepts_another_account(self):
        required = HttpResponse(403, {"success": False, "message": "无权进行此操作，未提供 New-Api-User"})
        instance, transport = provider("newapi", [required, newapi({"id": 8})])
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.ACCOUNT_MISMATCH)
        self.assertIsNone(instance._user_header_id)
        self.assertEqual(len(transport.calls), 2)

    def test_invalid_management_token_has_specific_diagnostic_without_retry(self):
        instance, transport = provider("newapi", [HttpResponse(403, {
            "success": False, "message": "无权进行此操作，access token无效"})])
        result = instance.fetch(Feature.PROFILE)
        self.assertEqual(result.error_category, ErrorCategory.INVALID_CREDENTIAL)
        self.assertEqual(len(transport.calls), 1)

    def test_generic_permission_rejection_never_enables_legacy_user_header(self):
        instance, transport = provider("newapi", [HttpResponse(403, {"success": False, "message": "forbidden"})])
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.PERMISSION_DENIED)
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("New-Api-User", transport.calls[0]["headers"])

    def test_account_refresh_sends_accepted_client_headers_without_cookies(self):
        from integrations.providers import USER_AGENT, ACCEPT_LANGUAGE
        from integrations.router import Router
        from integrations.service import account_snapshot

        def upstream(call):
            headers, path = call["headers"], urlsplit(call["url"]).path
            if headers.get("User-Agent") != USER_AGENT or headers.get("Accept-Language") != ACCEPT_LANGUAGE:
                return HttpResponse(403, {"error": "request rejected"})
            self.assertNotIn("Cookie", headers)
            if path == "/api/status":
                self.assertNotIn("Authorization", headers)
                return newapi({"quota_per_unit": 500000})
            self.assertEqual(headers.get("Authorization"), "Bearer synthetic-management")
            if "New-Api-User" not in headers:
                return HttpResponse(403, {"success": False, "message": "无权进行此操作，未提供 New-Api-User"})
            self.assertEqual(headers["New-Api-User"], "7")
            if path == "/api/user/self":
                return newapi({"id": 7, "quota": 1000000, "used_quota": 500000})
            if path == "/api/user/self/groups":
                return newapi({"default": 1})
            return listing([])

        instance, transport = provider("newapi", upstream)
        snapshot = account_snapshot(Router(instance), SITE)
        self.assertTrue(snapshot["ok"])
        self.assertTrue(all(feature["status"] == "success" for feature in snapshot["features"].values()))
        self.assertEqual(snapshot["quota_usd"], 2)
        self.assertEqual(snapshot["used_quota_usd"], 1)
        self.assertEqual(snapshot["token_count"], 0)
        # 账户快照 6 次（profile / wallet / usage / topups / groups / keys），
        # 再加单日用量的 /api/log/self/stat 与 /api/data/self 各一次，共 8 次。
        self.assertEqual(len(transport.calls), 8)

    def test_failed_account_refresh_preserves_safe_http_diagnostics(self):
        from integrations.service import account_snapshot
        from integrations.router import Router
        from integrations.retirement import EvidenceMatrix
        from integrations.contracts import FeatureResult
        secret = "synthetic-upstream-secret"
        cases = ((HttpResponse(403, {"message": secret}, {"Content-Type": "application/json"}),
                  ErrorCategory.PERMISSION_DENIED, "json"),
                 (HttpResponse(403, headers={"Content-Type": "text/html"},
                               text="<html>Forbidden " + secret + "</html>"),
                  ErrorCategory.ACCESS_BLOCKED, "html"))
        for response, category, response_format in cases:
            with self.subTest(response_format=response_format), tempfile.TemporaryDirectory() as folder:
                instance, transport = provider("newapi", lambda call: response)
                matrix = EvidenceMatrix(Path(folder) / "evidence.jsonl")
                snapshot = account_snapshot(Router(instance, matrix=matrix), SITE)
                profile = snapshot["features"]["profile"]
                self.assertEqual(profile["error_category"], category.value)
                self.assertEqual(profile["error_http_status"], 403)
                self.assertEqual(profile["error_response_format"], response_format)
                self.assertEqual(FeatureResult.from_dict(profile).to_dict(), profile)
                self.assertIn("HTTP 403", snapshot["error"])
                self.assertIn(response_format.upper(), snapshot["error"])
                self.assertIsNone(snapshot["token_count"])
                evidence = matrix.path.read_text(encoding="utf-8")
                self.assertEqual(json.loads(evidence.splitlines()[0])["error_response_format"], response_format)
                self.assertNotIn(secret, json.dumps(snapshot) + evidence)
                self.assertTrue(all("Cookie" not in call["headers"] for call in transport.calls))

    def test_successful_retry_does_not_keep_previous_http_error_diagnostics(self):
        instance, transport = provider("newapi", [HttpResponse(503), newapi({"id": 7})])
        instance.read_retries = 1
        result = instance.fetch(Feature.PROFILE)
        self.assertTrue(result.ok)
        self.assertIsNone(result.error_http_status)
        self.assertIsNone(result.error_response_format)
        self.assertEqual(len(transport.calls), 2)

    def test_negotiated_legacy_header_keeps_key_creation_single_write(self):
        required = HttpResponse(403, {"success": False, "message": "无权进行此操作，未提供 New-Api-User"})
        instance, transport = provider("newapi", [required, newapi({"id": 7}), listing([]),
            newapi({"id": 8})], write_hooks=Hooks())
        self.assertTrue(instance.fetch(Feature.PROFILE).ok)
        self.assertTrue(instance.fetch(Feature.KEYS_CREATE, name="fixture", group="default").ok)
        writes = [c for c in transport.calls if c["method"] == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["headers"]["New-Api-User"], "7")

    def test_newapi_missing_quota_conversion_keeps_raw_amount(self):
        instance, _ = provider("newapi", [newapi({"id": 7, "quota": 500000}), newapi({})])
        result = instance.fetch(Feature.WALLET)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["raw_amount"], 500000)
        self.assertIsNone(result.data["amount"])
        self.assertEqual(result.data["unit"], "unknown")

    def test_newapi_conversion_honors_explicit_scale_and_currency(self):
        instance, _ = provider("newapi", [newapi({"id": 7, "quota": 500000}),
                                        newapi({"quota_per_unit": 500000, "quota_currency": "USD"})])
        result = instance.fetch(Feature.WALLET)
        self.assertEqual(result.data["amount"], 1)
        self.assertEqual(result.source_endpoint, "/api/user/self")
        self.assertEqual(result.auth_kind, AuthKind.MANAGEMENT_TOKEN)

    def test_newapi_standard_status_without_currency_recovers_usd_wallet(self):
        instance, transport = provider("newapi", [newapi({"id": 7, "quota": 2794077}),
                                                  newapi({"quota_per_unit": 500000})])
        result = instance.fetch(Feature.WALLET)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.data["amount"], 5.588154)
        self.assertEqual(result.data["unit"], "USD")
        self.assertEqual(result.data["unit_source"], "newapi.quota_per_unit")
        self.assertEqual(len(transport.calls), 2)

    def test_newapi_display_currency_does_not_change_internal_usd(self):
        for display in ("USD", "CNY", "TOKENS", "CUSTOM"):
            with self.subTest(display=display):
                instance, _ = provider("newapi", [newapi({"id": 7, "quota": 7000}),
                    newapi({"quota_per_unit": 1000, "quota_display_type": display,
                            "usd_exchange_rate": 7, "custom_currency_exchange_rate": 3})])
                result = instance.fetch(Feature.WALLET)
                self.assertTrue(result.ok)
                self.assertEqual((result.data["amount"], result.data["unit"]), (7, "USD"))

    def test_newapi_missing_or_invalid_scale_still_keeps_money_unknown(self):
        for scale in (None, 0, -1000):
            with self.subTest(scale=scale):
                instance, _ = provider("newapi", [newapi({"id": 7, "quota": 500000}),
                    newapi({"quota_per_unit": scale, "quota_display_type": "USD"})])
                result = instance.fetch(Feature.WALLET)
                self.assertTrue(result.ok)
                self.assertEqual(result.data["raw_amount"], 500000)
                self.assertIsNone(result.data["amount"])
                self.assertEqual(result.data["unit"], "unknown")

    def test_newapi_explicit_fork_currency_is_not_overridden_by_usd_default(self):
        for declared, expected in (("CNY", "CNY"), (" cny ", "CNY"), ("EUR", "unknown")):
            with self.subTest(declared=declared):
                instance, _ = provider("newapi", [newapi({"id": 7, "quota": 7000}),
                    newapi({"quota_per_unit": 1000, "quota_currency": declared})])
                result = instance.fetch(Feature.WALLET)
                self.assertTrue(result.ok)
                self.assertEqual(result.data["unit"], expected)
                self.assertEqual(result.data["amount"], None if expected == "unknown" else 7)

    def test_newapi_zero_and_negative_wallet_are_not_missing_values(self):
        for quota, amount in ((0, 0), (-500000, -1)):
            with self.subTest(quota=quota):
                instance, _ = provider("newapi", [newapi({"id": 7, "quota": quota}),
                    newapi({"quota_per_unit": 500000})])
                result = instance.fetch(Feature.WALLET)
                self.assertTrue(result.ok)
                self.assertEqual((result.data["amount"], result.data["unit"]), (amount, "USD"))

    def test_public_and_personal_prices_send_different_credentials(self):
        payload = {"success": True, "data": [{"model_name": "fixture-model", "quota_type": 0,
            "model_ratio": 1, "completion_ratio": 2, "enable_groups": ["default"]}], "group_ratio": {"default": .5}}
        instance, transport = provider("newapi", [HttpResponse(200, payload), HttpResponse(200, payload)])
        public = instance.fetch(Feature.PRICING_PUBLIC)
        personal = instance.fetch(Feature.PRICING_PERSONAL)
        self.assertTrue(public.ok and personal.ok)
        self.assertEqual(public.visibility_scope, VisibilityScope.PUBLIC)
        self.assertEqual(personal.visibility_scope, VisibilityScope.ACCOUNT)
        self.assertNotIn("Authorization", transport.calls[0]["headers"])
        self.assertIn("Authorization", transport.calls[1]["headers"])
        self.assertEqual(public.data["offers"][0]["input"], 1)

    def test_missing_price_does_not_become_free_and_zero_output_ratio_survives(self):
        site_payload = {"data": [{"model_name": "fixture", "quota_type": 0, "enable_groups": ["default"]}],
                        "group_ratio": {"default": 1}}
        self.assertIsNone(parse_newapi_pricing(SITE, site_payload)["offers"][0]["input"])
        site_payload["data"][0].update(model_ratio=1, completion_ratio=0)
        self.assertEqual(parse_newapi_pricing(SITE, site_payload)["offers"][0]["output"], 0)

    def test_sub2_personal_prices_unit_and_dynamic_metadata(self):
        payload = {"groups": [{"id": 2, "name": "vip", "rate_multiplier": 2, "user_rate_multiplier": .5,
                               "peak_rate_enabled": True, "peak_rate_multiplier": 2,
                               "models": [{"name": "fixture", "pricing": {"input_price": 3e-6}}]}]}
        instance, _ = provider("sub2api", [sub2(payload)])
        result = instance.fetch(Feature.PRICING_PERSONAL)
        self.assertTrue(result.ok)
        quote = result.data["offers"][0]
        self.assertEqual(quote["input"], 1.5)
        self.assertFalse(quote["pricing_verified"])
        self.assertTrue(quote["peak_rate_enabled"])

    def test_profile_account_mismatch_returns_no_private_data(self):
        instance, _ = provider("newapi", [newapi({"id": 99, "quota": 500000})])
        result = instance.fetch(Feature.PROFILE)
        self.assertEqual(result.error_category, ErrorCategory.ACCOUNT_MISMATCH)
        self.assertIsNone(result.data)

    def test_sub2_wallet_key_quota_is_kept_separate_then_profile_is_read(self):
        instance, transport = provider("sub2api", [HttpResponse(200, {"mode": "quota_limited", "remaining": 2}),
             sub2({"id": 7, "balance": 30})], credentials=bound_key(access_token="synthetic-panel", account_id=7))
        result = instance.fetch(Feature.WALLET)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["amount"], 30)
        self.assertEqual([urlsplit(call["url"]).path for call in transport.calls], ["/v1/usage", "/api/v1/user/profile"])

    def test_sub2_confirmed_key_wallet_needs_no_panel_login(self):
        instance, transport = provider("sub2api", [HttpResponse(200, {"mode": "unrestricted", "balance": 9,
            "remaining": 9, "unit": "USD"})], credentials=bound_key())
        self.assertEqual(instance.fetch(Feature.WALLET).data["amount"], 9)
        self.assertEqual(len(transport.calls), 1)

    def test_unverified_key_wallet_is_not_merged_with_panel_account(self):
        instance, transport = provider("sub2api", [HttpResponse(200, {"mode": "unrestricted", "balance": 999,
            "remaining": 999, "unit": "USD"}), sub2({"id": 7, "balance": 9})],
            credentials=Credentials(model_api_key="sk-unverified", access_token="panel", account_id=7))
        result = instance.fetch(Feature.WALLET)
        self.assertEqual(result.data["amount"], 9)
        self.assertEqual(result.account_id, 7)
        self.assertEqual(len(transport.calls), 2)

    def test_known_model_and_panel_accounts_differ_no_request_is_sent(self):
        instance, transport = provider("sub2api", [], credentials=bound_key(access_token="panel", account_id=99))
        self.assertEqual(instance.fetch(Feature.WALLET).error_category, ErrorCategory.ACCOUNT_MISMATCH)
        self.assertEqual(len(transport.calls), 0)

    def test_sub2_missing_subscription_without_panel_is_partial_unknown(self):
        instance, _ = provider("sub2api", [HttpResponse(200, {"mode": "unrestricted", "isValid": True})], credentials=bound_key())
        result = instance.fetch(Feature.WALLET)
        self.assertEqual(result.status, FeatureStatus.PARTIAL)
        self.assertIsNone(result.data["key_usage"]["amount"])

    def test_verified_permission_denial_prevents_remote_reveal(self):
        instance, transport = provider("newapi", [], credentials=Credentials(management_token="manager",
            scopes=("api_key:read",), scope_source=ScopeSource.VERIFIED_ASSIGNMENT))
        self.assertEqual(instance.fetch(Feature.KEYS_REVEAL, id=7).error_category, ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED)
        self.assertEqual(transport.calls, [])

    def test_403_does_not_refresh_or_retry(self):
        instance, transport = provider("sub2api", [HttpResponse(403, {"code": "AUTH_SESSION_REQUIRED"})],
            credentials=Credentials(access_token="panel", refresh_token="refresh"))
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.AUTH_SESSION_REQUIRED)
        self.assertEqual(len(transport.calls), 1)

    def test_rate_limit_beyond_remaining_budget_does_not_retry_or_switch(self):
        instance, transport = provider("newapi", [HttpResponse(429, {"message": "limited"}, {"Retry-After": "100"})])
        instance.read_retries = 1
        result = instance.fetch(Feature.PROFILE, context=RequestContext(seconds=1))
        self.assertEqual(result.error_category, ErrorCategory.BUDGET_EXHAUSTED)
        self.assertEqual(len(transport.calls), 1)

    def test_cancellation_stops_before_the_next_page(self):
        stop = threading.Event()
        def reply(call):
            stop.set()
            return listing([{"id": i} for i in range(100)], 205)
        instance, transport = provider("newapi", FakeTransport(reply))
        result = instance.fetch(Feature.KEYS_LIST, context=RequestContext(should_stop=stop.is_set))
        self.assertEqual(result.error_category, ErrorCategory.CANCELLED)
        self.assertEqual(len(transport.calls), 1)

    def test_list_all_pages_and_mask_plaintext_sub2_keys(self):
        rows = [{"id": i, "key": "sk-synthetic-secret", "name": "fixture"} for i in range(205)]
        instance, transport = provider("sub2api", [listing(rows[:100], 205, sub=True),
            listing(rows[100:200], 205, sub=True), listing(rows[200:], 205, sub=True)])
        result = instance.fetch(Feature.KEYS_LIST)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["total"], 205)
        self.assertNotIn("sk-synthetic-secret", json.dumps(result.to_dict()))
        self.assertEqual([parse_qs(urlsplit(c["url"]).query)["page"] for c in transport.calls], [["1"], ["2"], ["3"]])

    def test_second_page_failure_never_returns_a_complete_shortened_list(self):
        instance, _ = provider("newapi", [listing([{"id": i} for i in range(100)], 205), TimeoutError()])
        result = instance.fetch(Feature.KEYS_LIST)
        self.assertFalse(result.ok)
        self.assertIsNone(result.data)

    def test_newapi_old_pagination_parameter_aliases_are_bounded(self):
        instance, transport = provider("newapi", [HttpResponse(400, {"message": "page_size unsupported"}),
            HttpResponse(422, {"message": "ps unsupported"}), listing([])])
        self.assertTrue(instance.fetch(Feature.KEYS_LIST).ok)
        self.assertEqual([next(key for key in ("page_size", "ps", "size") if key in parse_qs(urlsplit(c["url"]).query))
                          for c in transport.calls], ["page_size", "ps", "size"])

    def test_newapi_403_does_not_trigger_pagination_alias_fallback(self):
        instance, transport = provider("newapi", [HttpResponse(403, {"message": "page_size denied"})])
        self.assertEqual(instance.fetch(Feature.KEYS_LIST).error_category, ErrorCategory.PERMISSION_DENIED)
        self.assertEqual(len(transport.calls), 1)

    def test_sub2_topups_health_and_ccs_do_not_call_newapi_legacy_routes(self):
        instance, transport = provider("sub2api", [], credentials=Credentials(model_api_key="sk-synthetic"))
        for feature, params in ((Feature.TOPUPS, {}), (Feature.HEALTH_SUMMARY, {"recent_models":["m"]}),
                                (Feature.HEALTH_DETAIL, {"model":"m"})):
            self.assertEqual(instance.fetch(feature, **params).error_category, ErrorCategory.MISSING_CREDENTIAL)
        self.assertEqual(transport.calls, [])

    def test_reverse_proxy_base_path_and_custom_api_prefix_are_preserved(self):
        transport = FakeTransport([sub2({"id": 7, "balance": 1})])
        instance = Sub2ApiProvider({**SITE, "base_url": SITE["base_url"] + "/mount"},
            CredentialSession(Credentials(access_token="panel")), transport=transport, api_prefix="/panel")
        self.assertTrue(instance.fetch(Feature.PROFILE).ok)
        self.assertEqual(urlsplit(transport.calls[0]["url"]).path, "/mount/panel/user/profile")

    def test_credentials_session_cannot_cross_site_or_proxy(self):
        session = CredentialSession(Credentials(access_token="panel"))
        Sub2ApiProvider(SITE, session)
        with self.assertRaises(ValueError):
            Sub2ApiProvider({**SITE, "base_url": "https://other.invalid"}, session)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-c-rotation-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "credentials.json"
        self.rotations = []
        self.credentials = Credentials(access_token="old-access", refresh_token="old-refresh", account_id=7)
        self.path.write_text(json.dumps({"access_token": "old-access", "refresh_token": "old-refresh",
                                        "config_revision": 0, "token_version": 0}), encoding="utf-8")

    def save(self, expected, replacement):
        current = json.loads(self.path.read_text(encoding="utf-8"))
        if current["config_revision"] != expected.config_revision or current["refresh_token"] != expected.credentials.refresh_token:
            return False
        data = {"access_token": replacement.credentials.access_token, "refresh_token": replacement.credentials.refresh_token,
                "expires_at": replacement.credentials.expires_at, "config_revision": replacement.config_revision,
                "token_version": replacement.token_version}
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temp, self.path)
        self.rotations.append(data)
        return True

    def response(self, call):
        if urlsplit(call["url"]).path.endswith("/auth/refresh"):
            self.assertNotIn("Authorization", call["headers"])
            self.assertEqual(call["body"], {"refresh_token": "old-refresh"})
            return sub2({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})
        if call["headers"].get("Authorization") == "Bearer old-access":
            return HttpResponse(401, {"code": 401, "message": "expired"})
        return sub2({"id": 7, "balance": 12})

    def test_f08_401_refreshes_once_retries_once_and_atomically_rotates_pair(self):
        session = CredentialSession(self.credentials, save_rotation=self.save)
        instance, transport = provider("sub2api", FakeTransport(self.response), session=session)
        result = instance.fetch(Feature.PROFILE)
        self.assertTrue(result.ok)
        self.assertEqual(len(transport.calls), 3)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual((saved["access_token"], saved["refresh_token"]), ("new-access", "new-refresh"))
        self.assertGreater(saved["expires_at"], time.time())
        self.assertEqual(len(self.rotations), 1)

    def test_f08_concurrent_expired_reads_share_one_refresh(self):
        barrier = threading.Barrier(2)
        def reply(call):
            if call["headers"].get("Authorization") == "Bearer old-access":
                barrier.wait(timeout=3)
            return self.response(call)
        session = CredentialSession(self.credentials, save_rotation=self.save)
        instance, transport = provider("sub2api", FakeTransport(reply), session=session)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [pool.submit(instance.fetch, Feature.PROFILE) for _ in range(2)]
            self.assertTrue(first.result(timeout=5).ok)
            self.assertTrue(second.result(timeout=5).ok)
        self.assertEqual(sum(call["url"].endswith("/auth/refresh") for call in transport.calls), 1)
        self.assertEqual(len(self.rotations), 1)
        self.assertEqual(len(transport.calls), 5)

    def test_f08_proactive_expiry_refreshes_before_the_first_read(self):
        session = CredentialSession(replace(self.credentials, expires_at=time.time() - 1), save_rotation=self.save)
        instance, transport = provider("sub2api", FakeTransport(self.response), session=session)
        self.assertTrue(instance.fetch(Feature.PROFILE).ok)
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(transport.calls[0]["url"].endswith("/auth/refresh"))

    def test_f08_config_change_during_refresh_cannot_overwrite_new_credentials(self):
        started, release = threading.Event(), threading.Event()
        def reply(call):
            if call["url"].endswith("/auth/refresh"):
                started.set()
                self.assertTrue(release.wait(3))
            return self.response(call)
        session = CredentialSession(self.credentials, save_rotation=self.save)
        instance, _ = provider("sub2api", FakeTransport(reply), session=session)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(instance.fetch, Feature.PROFILE)
            self.assertTrue(started.wait(2))
            self.path.write_text(json.dumps({"access_token": "manual-access", "refresh_token": "manual-refresh",
                                            "config_revision": 1, "token_version": 1}), encoding="utf-8")
            session.replace_credentials(Credentials(access_token="manual-access", refresh_token="manual-refresh", account_id=99), 1)
            release.set()
            self.assertEqual(future.result(timeout=5).error_category, ErrorCategory.CONFIG_CHANGED)
        self.assertEqual(self.rotations, [])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["access_token"], "manual-access")
        self.assertEqual(session.snapshot(1).credentials.account_id, 99)

    def test_f08_cancelled_waiter_does_not_send_its_read_or_a_second_refresh(self):
        started, release, cancel, waiting = (threading.Event() for _ in range(4))
        checks = 0
        def stop_check():
            nonlocal checks
            checks += 1
            if checks >= 3:
                waiting.set()
            return cancel.is_set()
        def reply(call):
            if call["url"].endswith("/auth/refresh"):
                started.set()
                self.assertTrue(release.wait(3))
            return self.response(call)
        session = CredentialSession(replace(self.credentials, expires_at=time.time() - 1), save_rotation=self.save)
        instance, transport = provider("sub2api", FakeTransport(reply), session=session)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(instance.fetch, Feature.PROFILE)
            self.assertTrue(started.wait(2))
            second = pool.submit(instance.fetch, Feature.PROFILE, context=RequestContext(should_stop=stop_check))
            self.assertTrue(waiting.wait(2))
            cancel.set()
            self.assertEqual(second.result(timeout=3).error_category, ErrorCategory.CANCELLED)
            release.set()
            self.assertTrue(first.result(timeout=3).ok)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(self.rotations), 1)

    def test_f08_refresh_revocation_is_not_repeated_by_the_next_read(self):
        def reply(call):
            return HttpResponse(401, {"code": 401})
        session = CredentialSession(self.credentials, save_rotation=self.save)
        instance, transport = provider("sub2api", FakeTransport(reply), session=session)
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.AUTH_REQUIRED)
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.AUTH_REQUIRED)
        self.assertEqual(sum(call["url"].endswith("/auth/refresh") for call in transport.calls), 1)
        self.assertEqual(self.rotations, [])

    def test_f08_failed_atomic_save_does_not_use_unsaved_access_token(self):
        session = CredentialSession(self.credentials, save_rotation=lambda *args: False)
        instance, transport = provider("sub2api", FakeTransport(self.response), session=session)
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.CREDENTIAL_SAVE_FAILED)
        self.assertEqual(session.snapshot(0).credentials.access_token, "old-access")
        self.assertEqual(len(transport.calls), 2)

    def test_cancel_after_refresh_was_sent_preserves_rotated_credentials_but_stops_read(self):
        stop = threading.Event()
        def reply(call):
            response = self.response(call)
            if call["url"].endswith("/auth/refresh"):
                stop.set()
            return response
        session = CredentialSession(replace(self.credentials, expires_at=time.time() - 1), save_rotation=self.save)
        instance, transport = provider("sub2api", FakeTransport(reply), session=session)
        result = instance.fetch(Feature.PROFILE, context=RequestContext(should_stop=stop.is_set))
        self.assertEqual(result.error_category, ErrorCategory.CANCELLED)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["refresh_token"], "new-refresh")

    def test_no_persistence_hook_means_no_refresh_request(self):
        instance, transport = provider("sub2api", [HttpResponse(401, {"code": 401})], credentials=self.credentials)
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.CREDENTIAL_SAVE_FAILED)
        self.assertEqual(len(transport.calls), 1)

    def test_refreshed_access_still_401_does_not_loop(self):
        def reply(call):
            return self.response(call) if call["url"].endswith("/auth/refresh") else HttpResponse(401, {"code": 401})
        session = CredentialSession(self.credentials, save_rotation=self.save)
        instance, transport = provider("sub2api", FakeTransport(reply), session=session)
        self.assertEqual(instance.fetch(Feature.PROFILE).error_category, ErrorCategory.AUTH_REQUIRED)
        self.assertEqual(len(transport.calls), 3)

    def test_config_change_during_successful_read_cannot_publish_old_account(self):
        session = CredentialSession(self.credentials, save_rotation=self.save)
        def reply(call):
            session.replace_credentials(Credentials(access_token="manual", account_id=99), 1)
            return sub2({"id": 7, "balance": 12})
        instance, _ = provider("sub2api", FakeTransport(reply), session=session)
        result = instance.fetch(Feature.PROFILE)
        self.assertEqual(result.error_category, ErrorCategory.CONFIG_CHANGED)
        self.assertIsNone(result.data)


class WriteSafetyTests(unittest.TestCase):
    def test_unsupported_create_quota_options_cannot_silently_become_unlimited(self):
        for platform in ("newapi", "sub2api"):
            with self.subTest(platform=platform):
                instance, transport = provider(platform, [], write_hooks=Hooks())
                result = instance.fetch(Feature.KEYS_CREATE, name="new", quota_usd=5)
                self.assertEqual(result.error_category, ErrorCategory.VALIDATION_ERROR)
                self.assertEqual(transport.calls, [])

    def test_write_without_journal_hook_is_not_migrated_and_never_sent(self):
        instance, transport = provider("newapi", [])
        result = instance.fetch(Feature.KEYS_CREATE, name="fixture")
        self.assertEqual(result.error_category, ErrorCategory.NOT_MIGRATED)
        self.assertEqual(transport.calls, [])

    def test_newapi_success_without_id_reconciles_ids_and_all_parameters(self):
        created = {"id": 9, "name": "new", "group": "vip", "remain_quota": 0, "unlimited_quota": True,
                   "expired_time": -1, "model_limits_enabled": False, "model_limits": "", "allow_ips": ""}
        hooks = Hooks()
        instance, transport = provider("newapi", [listing([]), newapi(), listing([created])], write_hooks=hooks)
        result = instance.fetch(Feature.KEYS_CREATE, name="new", group="vip")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["id"], 9)
        self.assertTrue(result.data["needs_reveal"])
        self.assertEqual([event[0] for event in hooks.events], ["prepared", "sent", "confirmed"])
        self.assertEqual(sum(call["method"] == "POST" for call in transport.calls), 1)

    def test_newapi_create_timeout_is_unknown_and_never_replayed(self):
        instance, transport = provider("newapi", [listing([]), TimeoutError()], write_hooks=Hooks())
        result = instance.fetch(Feature.KEYS_CREATE, name="new")
        self.assertEqual(result.status, FeatureStatus.UNKNOWN)
        self.assertEqual(result.error_category, ErrorCategory.OPERATION_UNKNOWN)
        self.assertEqual(len(transport.calls), 2)

    def test_newapi_same_name_but_other_parameters_does_not_match(self):
        instance, transport = provider("newapi", [listing([]), newapi(), listing([{"id": 9, "name": "new"}])], write_hooks=Hooks())
        self.assertEqual(instance.fetch(Feature.KEYS_CREATE, name="new").error_category, ErrorCategory.OPERATION_UNKNOWN)
        self.assertEqual(len(transport.calls), 3)

    def test_sub2_create_returns_key_in_memory_but_not_snapshot(self):
        instance, transport = provider("sub2api", [sub2({"id": 7, "name": "new", "key": "sk-created-secret"})], write_hooks=Hooks())
        result = instance.fetch(Feature.KEYS_CREATE, name="new", group_id=2)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["key"], "sk-created-secret")
        self.assertNotIn("sk-created-secret", json.dumps(result.to_dict()))
        self.assertEqual(len(transport.calls), 1)

    def test_sub2_write_401_is_not_replayed_after_a_refresh(self):
        instance, transport = provider("sub2api", [HttpResponse(401, {"code": 401})], write_hooks=Hooks(),
            credentials=Credentials(access_token="panel", refresh_token="refresh", account_id=7, account_verified=True))
        result = instance.fetch(Feature.KEYS_CREATE, name="new", group_id=2)
        self.assertEqual(result.error_category, ErrorCategory.AUTH_REQUIRED)
        self.assertEqual(len(transport.calls), 1)

    def test_create_success_with_missing_id_is_unknown_not_a_retriable_validation_error(self):
        instance, transport = provider("sub2api", [sub2({"name": "new"})], write_hooks=Hooks())
        result = instance.fetch(Feature.KEYS_CREATE, name="new", group_id=2)
        self.assertEqual(result.error_category, ErrorCategory.OPERATION_UNKNOWN)
        self.assertEqual(result.data["operation_id"], "fixture-operation")
        self.assertEqual(len(transport.calls), 1)

    def test_create_accepted_but_reconciliation_incomplete_is_unknown(self):
        instance, transport = provider("newapi", [listing([]), newapi(), listing([], 1)], write_hooks=Hooks())
        result = instance.fetch(Feature.KEYS_CREATE, name="new")
        self.assertEqual(result.error_category, ErrorCategory.OPERATION_UNKNOWN)
        self.assertEqual(result.data["operation_id"], "fixture-operation")
        self.assertEqual(len(transport.calls), 3)

    def test_delete_is_verified_using_a_complete_list(self):
        for platform in ("newapi", "sub2api"):
            with self.subTest(platform=platform):
                instance, transport = provider(platform, [newapi() if platform == "newapi" else sub2(),
                    listing([], sub=platform == "sub2api")], write_hooks=Hooks())
                self.assertTrue(instance.fetch(Feature.KEYS_DELETE, id=7).ok)
                self.assertEqual([call["method"] for call in transport.calls], ["DELETE", "GET"])

    def test_delete_timeout_keeps_result_unknown_and_no_recreate(self):
        instance, transport = provider("sub2api", [TimeoutError()], write_hooks=Hooks())
        self.assertEqual(instance.fetch(Feature.KEYS_DELETE, id=7).error_category, ErrorCategory.OPERATION_UNKNOWN)
        self.assertEqual(len(transport.calls), 1)

    def test_newapi_update_preserves_other_fields_and_verifies_target(self):
        current = {"id": 7, "name": "old", "group": "vip", "remain_quota": 123, "unlimited_quota": False,
                   "expired_time": 9999999999, "model_limits_enabled": True, "model_limits": "fixture-model",
                   "allow_ips": "127.0.0.1", "cross_group_retry": False}
        instance, transport = provider("newapi", [newapi(current), newapi(), newapi({**current, "name": "new"})], write_hooks=Hooks())
        self.assertTrue(instance.fetch(Feature.KEYS_UPDATE, id=7, changes={"name": "new"}).ok)
        self.assertEqual(transport.calls[1]["body"], {**current, "name": "new"})

    def test_newapi_status_uses_status_only_route(self):
        instance, transport = provider("newapi", [newapi({"id": 7, "status": 1}), newapi(), newapi({"id": 7, "status": 2})], write_hooks=Hooks())
        self.assertTrue(instance.fetch(Feature.KEYS_UPDATE, id=7, changes={"status": 2}).ok)
        self.assertIn("status_only=true", transport.calls[1]["url"])

    def test_journal_failure_prevents_write(self):
        class Broken(Hooks):
            def prepare(self, **kwargs):
                raise OSError("fixture save failed")
        instance, transport = provider("sub2api", [], write_hooks=Broken())
        self.assertEqual(instance.fetch(Feature.KEYS_CREATE, name="new", group_id=2).error_category, ErrorCategory.OPERATION_SAVE_FAILED)
        self.assertEqual(transport.calls, [])

    def test_unverified_account_identity_cannot_prepare_or_send_a_write(self):
        hooks = Hooks()
        instance, transport = provider("sub2api", [], credentials=Credentials(access_token="panel", account_id=7), write_hooks=hooks)
        result = instance.fetch(Feature.KEYS_CREATE, name="new", group_id=2)
        self.assertEqual(result.error_category, ErrorCategory.AUTH_REQUIRED)
        self.assertEqual(transport.calls, [])
        self.assertEqual(hooks.events, [])


class ImportTests(unittest.TestCase):
    def test_providers_have_no_dependency_on_legacy_or_compat_modules(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "integrations" / "providers.py").read_text(encoding="utf-8"))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertNotIn("fetch_prices", imports)
        self.assertFalse(any(name and (name.endswith("compat") or name.endswith("router")) for name in imports))


if __name__ == "__main__":
    unittest.main(verbosity=2)
