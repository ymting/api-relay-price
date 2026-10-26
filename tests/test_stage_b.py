"""阶段 B：契约及 F01/F02/F03 的纯逻辑验收，不访问真实站点或用户配置。

端到端重试/预算、凭据持久化和界面保存留在 C/D/E 验收。
"""
import ast
import json
import subprocess
import sys
import tempfile
import unittest
from urllib.error import URLError
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from integrations.contracts import (
    AuthKind, CapabilityStatus, ConnectionMode, ConnectionPolicy, ErrorCategory,
    ErrorInfo, FallbackReason, Feature, FeatureCapability, FeatureResult, FeatureStatus,
    MigrationStatus, PermissionStatus, ProviderKind, ScopeSource, SiteConnectionPolicy,
    VisibilityScope, WRITE_FEATURES, check_newapi_permission, classify_response,
    newapi_route_permission, parse_retry_after, safe_endpoint,
)


ROOT = Path(__file__).resolve().parents[1]
OLD_TIME = "2026-10-15T07:00:00+00:00"
NOW_TIME = "2026-10-15T08:00:00+00:00"


class LegacyAuthDiagnosticsTests(unittest.TestCase):
    def test_known_missing_user_header_is_actionable_and_never_refreshes_credentials(self):
        for status in (200, 401, 403):
            with self.subTest(status=status):
                error = classify_response(status, {"success": False, "message": "无权进行此操作，未提供 New-Api-User"})
                self.assertEqual(error.category, ErrorCategory.AUTH_USER_HEADER_REQUIRED)
                self.assertFalse(error.refresh_allowed)
                self.assertFalse(error.retryable_read)
                self.assertIn("账户 ID", error.message)

    def test_known_invalid_newapi_token_does_not_echo_remote_message(self):
        secret = "synthetic-private-value"
        error = classify_response(403, {"success": False, "message": "无权进行此操作，access token 无效 " + secret})
        self.assertEqual(error.category, ErrorCategory.INVALID_CREDENTIAL)
        self.assertNotIn(secret, json.dumps(error.to_dict()))
        self.assertFalse(error.refresh_allowed)

    def test_other_platform_invalid_access_token_still_allows_normal_refresh(self):
        error = classify_response(401, {"message": "invalid access token"})
        self.assertEqual(error.category, ErrorCategory.AUTH_REQUIRED)
        self.assertTrue(error.refresh_allowed)


class FeatureResultTests(unittest.TestCase):
    def result(self, **changes):
        fields = dict(feature=Feature.WALLET, status=FeatureStatus.SUCCESS, provider=ProviderKind.API,
                      data={"amount": None, "unit": "unknown"}, auth_kind=AuthKind.MANAGEMENT_TOKEN,
                      source_endpoint="/api/user/self", visibility_scope=VisibilityScope.ACCOUNT,
                      account_id=7, last_success_at=OLD_TIME, attempted_at=NOW_TIME,
                      config_revision="fixture-revision")
        return FeatureResult(**{**fields, **changes})

    def test_all_fifteen_features_and_business_write_boundary(self):
        self.assertEqual(len(Feature), 15)
        self.assertEqual({f.value for f in WRITE_FEATURES}, {"keys.create", "keys.update", "keys.delete"})
        self.assertNotIn(Feature.KEYS_REVEAL, WRITE_FEATURES)

    def test_required_fields_json_roundtrip(self):
        result = self.result()
        payload = result.to_dict()
        self.assertLessEqual({"feature", "status", "data", "provider", "auth_kind", "source_endpoint",
            "visibility_scope", "account_id", "last_success_at", "attempted_at", "stale", "error_category",
            "fallback_reason", "config_revision", "served_from_cache"}, payload.keys())
        restored = FeatureResult.from_dict(json.loads(json.dumps(payload)))
        self.assertEqual(restored, result)
        self.assertIsNone(restored.data["amount"])

    def test_zero_empty_and_unknown_remain_distinct(self):
        for data in ({"amount": 0}, [], None):
            with self.subTest(data=data):
                result = FeatureResult.from_dict(self.result(data=data).to_dict())
                self.assertEqual(result.data, data)

    def test_failed_attempt_preserves_old_success_time_and_cached_origin(self):
        result = self.result(status=FeatureStatus.ERROR, data={"amount": 12},
                             error_category=ErrorCategory.TIMEOUT, stale=True, served_from_cache=True,
                             provider=ProviderKind.COMPAT, fallback_reason=FallbackReason.MISSING_CREDENTIAL)
        restored = FeatureResult.from_dict(result.to_dict())
        self.assertFalse(restored.ok)
        self.assertEqual(restored.last_success_at, OLD_TIME)
        self.assertEqual(restored.attempted_at, NOW_TIME)
        self.assertEqual(restored.data, {"amount": 12})
        self.assertEqual(restored.provider, ProviderKind.COMPAT)
        self.assertTrue(restored.served_from_cache)

    def test_provider_and_cache_are_separate_dimensions(self):
        for provider in ProviderKind:
            for cached in (True, False):
                with self.subTest(provider=provider, cached=cached):
                    result = self.result(provider=provider, served_from_cache=cached)
                    self.assertEqual(result.to_dict()["provider"], provider.value)
        with self.assertRaises(ValueError):
            self.result(provider="cache")

    def test_no_aliasing_from_input_or_serialized_data(self):
        original = {"windows": [{"remaining": None}]}
        result = self.result(data=original)
        original["windows"][0]["remaining"] = 99
        dumped = result.to_dict()
        dumped["data"]["windows"][0]["remaining"] = 88
        self.assertIsNone(result.data["windows"][0]["remaining"])

    def test_plain_snapshot_and_repr_do_not_expose_secret_fields(self):
        credentials = {"key": "sk-synthetic", "refresh_token": "synthetic-refresh", "key_masked": "sk-***",
                       "nested": [{"Authorization": "Bearer synthetic-auth", "password": "synthetic-password"}]}
        result = self.result(data=credentials, source_endpoint="https://user:synthetic-password@fixture.invalid/api/self?access_token=synthetic-query#secret")
        serialized = json.dumps(result.to_dict()) + repr(result)
        for secret in ("sk-synthetic", "synthetic-refresh", "synthetic-auth", "synthetic-password", "synthetic-query"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(result.to_dict()["data"]["key_masked"], "sk-***")
        self.assertEqual(result.data["key"], "sk-synthetic")  # 明确复制时仅在内存取值。

    def test_endpoint_keeps_path_but_drops_credentials_query_and_fragment(self):
        self.assertEqual(safe_endpoint("/api/token/?p=1&access_token=synthetic#fragment"), "/api/token/")
        self.assertEqual(safe_endpoint("https://user:pass@fixture.invalid:443/api/self?a=1"),
                         "https://fixture.invalid:443/api/self")

    def test_invalid_state_is_rejected_instead_of_fabricated_success(self):
        for changes in (dict(last_success_at=None), dict(error_category=ErrorCategory.TIMEOUT),
                        dict(status=FeatureStatus.ERROR), dict(stale="false"), dict(config_revision=-1),
                        dict(config_revision=True), dict(attempted_at="2026-10-15T08:00:00"),
                        dict(data={"amount": float("nan")}), dict(fallback_reason="raw secret error")):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                self.result(**changes)

    def test_missing_attempt_time_is_not_replaced_by_now_on_deserialization(self):
        payload = self.result().to_dict()
        del payload["attempted_at"]
        with self.assertRaises(ValueError):
            FeatureResult.from_dict(payload)

    def test_cache_without_proven_success_time_is_rejected(self):
        with self.assertRaises(ValueError):
            self.result(status=FeatureStatus.ERROR, last_success_at=None,
                        error_category=ErrorCategory.TIMEOUT, stale=True)

    def test_unknown_schema_and_enum_are_not_silently_accepted(self):
        for field, value in (("schema_version", 2), ("schema_version", True), ("schema_version", 1.0),
                             ("feature", "future.feature"), ("status", "ok")):
            payload = self.result().to_dict()
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                FeatureResult.from_dict(payload)

    def test_specific_status_cannot_mislabel_the_remote_failure(self):
        for status in (FeatureStatus.NOT_MIGRATED, FeatureStatus.UNSUPPORTED, FeatureStatus.CANCELLED):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.result(status=status, error_category=ErrorCategory.AUTH_EXPIRED)


class CapabilityTests(unittest.TestCase):
    def test_not_migrated_is_local_and_remote_remains_unprobed(self):
        cap = FeatureCapability(Feature.WALLET)
        self.assertEqual(cap.status, CapabilityStatus.NOT_MIGRATED)
        self.assertEqual(cap.remote_status, CapabilityStatus.UNPROBED)
        self.assertEqual(FeatureCapability.from_dict(cap.to_dict()), cap)

    def test_remote_evidence_survives_migration_status_changes(self):
        cap = FeatureCapability(Feature.WALLET, remote_status=CapabilityStatus.SUPPORTED, last_probe_at=OLD_TIME)
        migrated = replace(cap, migration_status=MigrationStatus.MIGRATED)
        self.assertEqual(cap.status, CapabilityStatus.NOT_MIGRATED)
        self.assertEqual(migrated.status, CapabilityStatus.SUPPORTED)
        self.assertEqual(migrated.last_probe_at, OLD_TIME)

    def test_local_not_migrated_cannot_be_saved_as_remote_unsupported(self):
        with self.assertRaises(ValueError):
            FeatureCapability(Feature.WALLET, remote_status=CapabilityStatus.NOT_MIGRATED)
        result = classify_response(503, {})
        self.assertNotEqual(result.capability_status, CapabilityStatus.UNSUPPORTED)

    def test_inconsistent_persisted_derived_status_is_rejected(self):
        payload = FeatureCapability(Feature.WALLET).to_dict()
        payload["status"] = "unsupported"
        with self.assertRaises(ValueError):
            FeatureCapability.from_dict(payload)


class ResponseClassificationTests(unittest.TestCase):
    def test_f02_normal_profile_is_not_an_error(self):
        payload = {"success": True, "data": {"id": 7, "quota": 0}}
        self.assertIsNone(classify_response(200, payload, schema_valid=True))

    def test_f02_scope_session_and_expiry_are_distinct(self):
        cases = ((403, "ACCESS_TOKEN_SCOPE_DENIED", False), (403, "AUTH_SESSION_REQUIRED", False),
                 (401, "ACCESS_TOKEN_EXPIRED", True))
        for status, code, refresh in cases:
            with self.subTest(code=code):
                result = classify_response(status, {"success": False, "code": code})
                expected = ErrorCategory.AUTH_EXPIRED if code == "ACCESS_TOKEN_EXPIRED" else ErrorCategory(code)
                self.assertEqual(result.category, expected)
                self.assertEqual(result.refresh_allowed, refresh)
                self.assertFalse(result.retryable_read)

    def test_generic_json_403_never_refreshes(self):
        result = classify_response(403, {"message": "forbidden"})
        self.assertEqual(result.category, ErrorCategory.PERMISSION_DENIED)
        self.assertFalse(result.refresh_allowed)

    def test_html_403_is_access_rejection_without_assuming_token_permissions(self):
        for headers, body in (({"Content-Type": "text/html;charset=utf8"}, "Forbidden"),
                              ({}, "<!doctype html><html><title>403 Forbidden</title></html>")):
            with self.subTest(headers=headers):
                result = classify_response(403, body=body, headers=headers)
                self.assertEqual(result.category, ErrorCategory.ACCESS_BLOCKED)
                self.assertFalse(result.retryable_read)
                self.assertFalse(result.refresh_allowed)

    def test_nested_machine_error_codes(self):
        result = classify_response(403, {"error": {"code": "ACCESS_TOKEN_SCOPE_DENIED"}})
        self.assertEqual(result.category, ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED)

    def test_f02_html_challenge_has_priority_over_401_and_403(self):
        for status in (200, 401, 403, 503):
            with self.subTest(status=status):
                result = classify_response(status, body="<!doctype html><title>Just a moment...</title>",
                                           headers={"Content-Type": "text/html"})
                self.assertEqual(result.category, ErrorCategory.ACCESS_BLOCKED)
                self.assertFalse(result.refresh_allowed)
                self.assertFalse(result.retryable_read)

    def test_cloudflare_server_header_alone_is_not_a_challenge(self):
        self.assertIsNone(classify_response(200, {"success": True, "data": []}, headers={"server": "cloudflare"}))
        result = classify_response(403, headers={"CF-Mitigated": "challenge"})
        self.assertEqual(result.category, ErrorCategory.ACCESS_BLOCKED)

    def test_html_login_and_invalid_json_never_prompt_token_refresh(self):
        for status, body in ((401, "<html>Login</html>"), (200, "not-json")):
            with self.subTest(status=status):
                result = classify_response(status, body=body)
                self.assertEqual(result.category, ErrorCategory.PROTOCOL_ERROR)
                self.assertFalse(result.refresh_allowed)

    def test_f02_rate_limit_parses_header_and_does_not_refresh(self):
        result = classify_response(429, {"code": "TOKEN_EXPIRED"}, headers={"Retry-After": "90"})
        self.assertEqual(result.category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(result.retry_after, 90)
        self.assertTrue(result.retryable_read)
        self.assertFalse(result.refresh_allowed)

    def test_retry_after_date_is_not_shortened_and_past_date_is_zero(self):
        now = datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after("Thu, 15 Oct 2026 09:00:00 GMT", now=now), 3600)
        self.assertEqual(parse_retry_after("Thu, 15 Oct 2026 07:00:00 GMT", now=now), 0)
        for value in (None, True, "invalid", "NaN", "Infinity", -1, "-1", "-1e1"):
            with self.subTest(value=value):
                self.assertIsNone(parse_retry_after(value, now=now))

    def test_404_and_405_do_not_permanently_disable_a_feature_without_confirmation(self):
        for status, expected in ((404, ErrorCategory.ENDPOINT_NOT_FOUND), (405, ErrorCategory.METHOD_NOT_ALLOWED)):
            with self.subTest(status=status):
                result = classify_response(status, {})
                self.assertEqual(result.category, expected)
                self.assertEqual(result.capability_status, CapabilityStatus.UNPROBED)
                confirmed = classify_response(status, {}, endpoint_confirmed_unsupported=True)
                self.assertEqual(confirmed.capability_status, CapabilityStatus.UNSUPPORTED)

    def test_timeout_network_and_server_errors_are_temporary(self):
        cases = (classify_response(exception=TimeoutError("synthetic-secret")),
                 classify_response(exception=OSError("synthetic-secret")), classify_response(503, {}))
        self.assertEqual([result.category for result in cases],
                         [ErrorCategory.TIMEOUT, ErrorCategory.NETWORK_ERROR, ErrorCategory.SERVER_ERROR])
        self.assertTrue(all(result.retryable_read for result in cases))
        self.assertTrue(all(result.capability_status == CapabilityStatus.TEMPORARY_FAILURE for result in cases))
        self.assertEqual(classify_response(exception=URLError(TimeoutError())).category, ErrorCategory.TIMEOUT)

    def test_known_http_error_has_priority_over_a_body_read_exception(self):
        result = classify_response(429, exception=TimeoutError(), headers={"Retry-After": "45"})
        self.assertEqual(result.category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(result.retry_after, 45)

    def test_http_200_business_failure_and_schema_failure_are_not_success(self):
        cases = (({"success": False, "data": []}, None, ErrorCategory.BUSINESS_ERROR),
                 ({"success": True, "data": []}, False, ErrorCategory.PROTOCOL_ERROR),
                 ({"code": 500, "message": "synthetic error"}, None, ErrorCategory.BUSINESS_ERROR),
                 ({"code": "AUTH_SESSION_REQUIRED"}, None, ErrorCategory.AUTH_SESSION_REQUIRED),
                 ({}, None, ErrorCategory.PROTOCOL_ERROR))
        for payload, schema, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(classify_response(200, payload, schema_valid=schema).category, expected)

    def test_successful_empty_lists_and_explicit_no_content_are_distinct_from_failure(self):
        self.assertIsNone(classify_response(200, []))
        self.assertIsNone(classify_response(204, schema_valid=True))
        self.assertIsNone(classify_response(200, body='{"success":true,"data":{"items":[],"total":0}}'))

    def test_error_strings_and_non_boolean_success_cannot_become_success(self):
        for payload, expected in (({"error": "synthetic failure"}, ErrorCategory.BUSINESS_ERROR),
                                  ({"success": "false", "data": []}, ErrorCategory.PROTOCOL_ERROR),
                                  ({"success": 0, "data": []}, ErrorCategory.PROTOCOL_ERROR)):
            with self.subTest(payload=payload):
                self.assertEqual(classify_response(200, payload).category, expected)

    def test_second_factor_and_ban_are_not_expired_tokens(self):
        for code, expected in (("MFA_REQUIRED", ErrorCategory.SECOND_FACTOR_REQUIRED),
                               ("USER_BANNED", ErrorCategory.ACCOUNT_DISABLED)):
            with self.subTest(code=code):
                result = classify_response(403, {"code": code})
                self.assertEqual(result.category, expected)
                self.assertFalse(result.refresh_allowed)

    def test_diagnostics_never_echo_remote_body_or_exception_secrets(self):
        secrets = ("sk-synthetic-secret", "synthetic-password", "synthetic-refresh")
        for result in (classify_response(403, {"message": " ".join(secrets)}),
                       classify_response(exception=OSError(" ".join(secrets)))):
            for secret in secrets:
                self.assertNotIn(secret, json.dumps(result.to_dict()))

    def test_classifier_does_not_sleep_connect_or_refresh(self):
        with patch("time.sleep", side_effect=AssertionError("unexpected sleep")), \
             patch("socket.socket.connect", side_effect=AssertionError("unexpected network")):
            for _ in range(3):
                self.assertEqual(classify_response(429, {}, headers={"Retry-After": "10"}).retry_after, 10)
            self.assertEqual(classify_response(cancelled=True).category, ErrorCategory.CANCELLED)

    def test_write_outcome_unknown_is_not_retryable(self):
        result = ErrorInfo(ErrorCategory.OPERATION_UNKNOWN)
        self.assertFalse(result.retryable_read)
        self.assertFalse(result.refresh_allowed)
        self.assertIn("禁止自动重放", result.message)


class NewApiPermissionTests(unittest.TestCase):
    def test_f03_route_scope_table_matches_project_plan(self):
        cases = (("GET", "/api/pricing", None), ("GET", "/api/perf-metrics/summary", None),
                 ("GET", "/api/perf-metrics", None), ("GET", "/api/user/self", "profile:read"),
                 ("GET", "/api/user/self/groups", "profile:read"), ("GET", "/api/user/models", "profile:read"),
                 ("GET", "/api/user/topup/self", "wallet:read"), ("GET", "/api/token/", "api_key:read"),
                 ("GET", "/api/token/search?keyword=fixture", "api_key:read"),
                 ("GET", "/api/token/auto-groups", "api_key:read"), ("GET", "/api/token/7", "api_key:read"),
                 ("POST", "/api/token/7/key", "api_key:reveal"), ("POST", "/api/token/batch/keys", "api_key:reveal"),
                 ("POST", "/api/token/", "api_key:write"), ("PUT", "/api/token/", "api_key:write"),
                 ("DELETE", "/api/token/7", "api_key:write"), ("POST", "/api/token/batch", "api_key:write"))
        for method, path, scope in cases:
            with self.subTest(method=method, path=path):
                rule = newapi_route_permission(method, path)
                self.assertTrue(rule.known)
                self.assertEqual(rule.required_scope, scope)
                self.assertFalse(rule.browser_session)

    def test_f03_all_access_token_management_routes_require_browser_session(self):
        cases = (("GET", "/api/user/access_tokens"), ("POST", "/api/user/access_tokens"),
                 ("GET", "/api/user/access_tokens/scopes"), ("GET", "/api/user/access_tokens/catalog"),
                 ("PATCH", "/api/user/access_tokens/7"), ("DELETE", "/api/user/access_tokens/7"),
                 ("DELETE", "/api/user/access_tokens/legacy"))
        for method, path in cases:
            with self.subTest(path=path, method=method):
                result = check_newapi_permission(method, path, scopes=["api_key:write", "profile:read"],
                                                 scope_source=ScopeSource.VERIFIED_ASSIGNMENT)
                self.assertEqual(result.status, PermissionStatus.SESSION_REQUIRED)
                self.assertEqual(result.error_category, ErrorCategory.AUTH_SESSION_REQUIRED)

    def test_f03_dictionary_catalog_and_user_claims_never_grant_or_deny_actual_scopes(self):
        for source in (ScopeSource.DICTIONARY, ScopeSource.CATALOG, ScopeSource.USER_DECLARED, ScopeSource.UNVERIFIED):
            for scopes in ([], ["api_key:write"], {"resources": {"api_key:write": "write"}}):
                with self.subTest(source=source, scopes=scopes):
                    result = check_newapi_permission("POST", "/api/token/", scopes=scopes, scope_source=source)
                    self.assertEqual(result.status, PermissionStatus.UNVERIFIED)
                    self.assertIsNone(result.error_category)

    def test_read_write_and_reveal_are_independent_permissions(self):
        for scope in ("api_key:read", "api_key:write"):
            with self.subTest(scope=scope):
                result = check_newapi_permission("POST", "/api/token/7/key", scopes=[scope],
                                                 scope_source=ScopeSource.VERIFIED_ASSIGNMENT)
                self.assertEqual(result.status, PermissionStatus.DENIED)
                self.assertEqual(result.required_scope, "api_key:reveal")
        allowed = check_newapi_permission("POST", "/api/token/7/key", scopes=["api_key:reveal"],
                                         scope_source=ScopeSource.VERIFIED_ASSIGNMENT)
        self.assertEqual(allowed.status, PermissionStatus.ALLOWED)

    def test_profile_read_does_not_grant_wallet_read(self):
        result = check_newapi_permission("GET", "/api/user/topup/self", scopes=["profile:read"],
                                        scope_source=ScopeSource.VERIFIED_ASSIGNMENT)
        self.assertEqual(result.error_category, ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED)
        self.assertEqual(result.required_scope, "wallet:read")
        profile = check_newapi_permission("GET", "/api/user/self", scopes=["profile:read"],
                                         scope_source=ScopeSource.VERIFIED_ASSIGNMENT)
        self.assertEqual(profile.status, PermissionStatus.ALLOWED)

    def test_permission_precheck_is_not_authentication_or_write_support_proof(self):
        result = check_newapi_permission("GET", "/api/pricing", scopes=[],
                                        scope_source=ScopeSource.VERIFIED_ASSIGNMENT)
        self.assertEqual(result.status, PermissionStatus.UNVERIFIED)
        self.assertEqual(check_newapi_permission("GET", "/api/user/self").status, PermissionStatus.UNVERIFIED)

    def test_model_key_is_not_a_management_credential(self):
        result = check_newapi_permission("GET", "/api/user/self", auth_kind=AuthKind.MODEL_KEY)
        self.assertEqual(result.status, PermissionStatus.AUTH_REQUIRED)

    def test_public_read_is_not_forced_to_authenticate_by_the_scope_table(self):
        for path in ("/api/pricing", "/api/perf-metrics/summary"):
            with self.subTest(path=path):
                self.assertEqual(check_newapi_permission("GET", path, auth_kind=AuthKind.NONE).status,
                                 PermissionStatus.UNVERIFIED)

    def test_browser_session_does_not_inherit_management_token_scope_denials(self):
        result = check_newapi_permission("GET", "/api/user/self", auth_kind=AuthKind.COOKIE, scopes=[],
                                        scope_source=ScopeSource.VERIFIED_ASSIGNMENT)
        self.assertEqual(result.status, PermissionStatus.UNVERIFIED)
        self.assertEqual(check_newapi_permission("GET", "/api/user/access_tokens/scopes",
                                                auth_kind=AuthKind.BROWSER_SESSION).status, PermissionStatus.UNVERIFIED)

    def test_unrecognized_routes_are_not_implicitly_granted(self):
        for path in ("/api/user/self/access_tokens", "/api/token/7/other", "/api/token/-1/key"):
            with self.subTest(path=path):
                self.assertEqual(check_newapi_permission("GET", path).status, PermissionStatus.UNKNOWN_ROUTE)

    def test_verified_assignment_requires_a_scope_list_not_a_dictionary(self):
        with self.assertRaises(ValueError):
            check_newapi_permission("GET", "/api/user/self", scopes={"resources": []},
                                    scope_source=ScopeSource.VERIFIED_ASSIGNMENT)


class PolicyContractTests(unittest.TestCase):
    def test_f01_old_missing_policy_defaults_to_api_preferred_without_pinning_sites(self):
        self.assertEqual(ConnectionPolicy.from_dict(None).to_dict(), {"mode": "api_preferred", "compat_enabled": True})
        self.assertEqual(SiteConnectionPolicy.from_dict(None).to_dict(), {})
        self.assertEqual(SiteConnectionPolicy.from_dict({}).to_dict(), {})

    def test_policy_and_feature_override_roundtrip(self):
        payload = {"mode": "api_only", "features": {"topups": "compat_only", "wallet": "api_preferred"}}
        policy = SiteConnectionPolicy.from_dict(payload)
        self.assertEqual(policy.to_dict(), payload)
        self.assertEqual(SiteConnectionPolicy.from_dict(json.loads(json.dumps(policy.to_dict()))), policy)
        global_policy = ConnectionPolicy.from_dict({"compat_enabled": False, "mode": "compat_only"})
        self.assertFalse(global_policy.compat_enabled)  # D 阶段须明确报停用，不能悄悄改成其他模式。
        self.assertEqual(global_policy.mode, ConnectionMode.COMPAT_ONLY)

    def test_policy_validation_does_not_mutate_or_drop_surrounding_credentials(self):
        # 仅检验纯策略契约；不冒充 D 阶段的 validate_sites / 页面保存验收。
        fixture = {"auth": {"cookie": "synthetic-cookie", "username": "fixture", "password": "synthetic-password",
                            "management_token": "synthetic-manager", "refresh_token": "synthetic-refresh"},
                   "connection": {"features": {"topups": "compat_only"}}}
        original = json.dumps(fixture, sort_keys=True)
        SiteConnectionPolicy.from_dict(fixture["connection"])
        self.assertEqual(json.dumps(fixture, sort_keys=True), original)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "config.json"
            target.write_text(original, encoding="utf-8")
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), fixture)

    def test_invalid_modes_and_feature_typos_are_rejected(self):
        for value in ({"mode": "auto"}, {"features": {"key.create": "api_only"}},
                      {"features": {"wallet": "auto"}}, {"features": []}, {"compat_enabled": False}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SiteConnectionPolicy.from_dict(value)
        for value in ({"compat_enabled": "false"}, {"mode": "auto"}, {"unknown": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ConnectionPolicy.from_dict(value)

    def test_feature_overrides_are_detached_and_readonly(self):
        original = {"wallet": "api_only"}
        policy = SiteConnectionPolicy(features=original)
        original["wallet"] = "compat_only"
        self.assertEqual(policy.features[Feature.WALLET], ConnectionMode.API_ONLY)
        with self.assertRaises(TypeError):
            policy.features[Feature.WALLET] = ConnectionMode.COMPAT_ONLY


class ImportBoundaryTests(unittest.TestCase):
    def test_contracts_have_only_stdlib_dependencies_and_no_import_side_effects(self):
        tree = ast.parse((ROOT / "integrations" / "contracts.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module.split(".")[0])
        self.assertTrue(set(imported) <= sys.stdlib_module_names | {"__future__"})
        script = """
import json, sys
from unittest.mock import patch
sys.path.insert(0, ROOT)
with patch('socket.socket.connect', side_effect=AssertionError('network')), patch('builtins.open', side_effect=AssertionError('file operation')):
    import integrations.contracts
assert not any(name in sys.modules for name in ('fetch_prices', 'server', 'integrations.compat', 'integrations.router', 'integrations.providers'))
print('passive import OK')
""".replace("ROOT", repr(str(ROOT)))
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, "-c", script], cwd=temp, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(temp).iterdir()), [])
        self.assertIn("passive import OK", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
