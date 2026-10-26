"""D: synthetic credentials, isolated config files and observable request counts."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, parse_qs

from integrations.configuration import (ConfigurationStore, credentials_of, normalize_proxy,
    public_site, read_json, site_proxy, write_json)
from integrations.contracts import (ConnectionPolicy, ConnectionMode, SiteConnectionPolicy,
    Feature, FeatureResult, FeatureStatus, ProviderKind, ErrorCategory, utc_now)
from integrations.providers import (Credentials, CredentialSession, HttpResponse,
    ProviderFailure, RequestContext)
from integrations.router import Router, effective_mode
from integrations.runtime import IntegrationRuntime
from integrations.service import (account_snapshot, pricing_snapshot, create_token,
    resolve_export)
from integrations.ccs import build_ccswitch_link, newapi_usage, sub2api_usage

SITE = {"id": "fixture", "name": "Fixture", "type": "newapi", "base_url": "https://fixture.invalid",
        "enabled": True, "auth": {"management_token": "synthetic-management", "account_id": 7}}


def outcome(feature, category=None, *, account=7, provider=ProviderKind.API, data=None):
    status = {ErrorCategory.NOT_MIGRATED: FeatureStatus.NOT_MIGRATED,
              ErrorCategory.UNSUPPORTED: FeatureStatus.UNSUPPORTED}.get(category, FeatureStatus.ERROR)
    return FeatureResult(feature, FeatureStatus.SUCCESS if category is None else status, provider,
        data=data, account_id=account, last_success_at=utc_now() if category is None else None,
        error_category=category, config_revision="r")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sites.json"
        self.old = {**copy.deepcopy(SITE), "auth": {**SITE["auth"], "cookie": "synthetic-cookie", "password": "synthetic-password",
            "username": "old", "access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "model_api_key": "sk-synthetic",
            "expires_at": 12345}, "extension": {"x": 1}}
        write_json(self.path, {"sites": [self.old], "proxy": "old-proxy", "extension": True})
        self.store = ConfigurationStore(self.path)

    def test_public_state_contains_presence_but_no_saved_secrets(self):
        public = public_site(self.old)
        encoded = json.dumps(public)
        for key in ("cookie", "password", "management_token", "access_token", "refresh_token", "model_api_key"):
            self.assertTrue(public["has_secret"][key])
            self.assertNotIn(self.old["auth"][key], encoded)

    def test_round_trip_preserves_all_credentials_extensions_and_global_fields(self):
        public = public_site(self.old)
        public["name"] = "Renamed"
        cleaned, errors = self.store.save([public])
        self.assertFalse(errors)
        for key, value in self.old["auth"].items():
            self.assertEqual(cleaned[0]["auth"].get(key), value)
        self.assertEqual(read_json(self.path)["proxy"], "old-proxy")
        self.assertEqual(cleaned[0]["extension"], {"x": 1})

    def test_empty_inputs_keep_and_explicit_clear_removes_only_target(self):
        raw = public_site(self.old)
        raw["auth"].update(cookie="", management_token="")
        raw["secret_actions"] = {"management_token": "clear"}
        cleaned, errors = self.store.save([raw])
        self.assertFalse(errors)
        self.assertEqual(cleaned[0]["auth"]["cookie"], "synthetic-cookie")
        self.assertEqual(cleaned[0]["auth"]["management_token"], "")
        self.assertEqual(cleaned[0]["auth"]["refresh_token"], "synthetic-refresh")

    def test_mask_cannot_replace_secret_and_invalid_card_does_not_delete_config(self):
        before = self.path.read_bytes()
        raw = public_site(self.old)
        raw["auth"]["management_token"] = "abc***xyz"
        cleaned, errors = self.store.save([raw, {"id": "broken"}])
        self.assertTrue(errors)
        self.assertEqual(self.path.read_bytes(), before)

    def test_new_credentials_cannot_claim_verified_binding_or_scope(self):
        raw = public_site(self.old)
        raw["auth"].update(management_token="replacement", account_verified=True, model_binding_verified=True,
                           scope_source="verified_assignment")
        cleaned, errors = self.store.save([raw])
        self.assertFalse(errors)
        self.assertFalse(cleaned[0]["auth"]["account_verified"])
        self.assertFalse(cleaned[0]["auth"]["model_binding_verified"])
        self.assertEqual(cleaned[0]["auth"]["scope_source"], "unverified")

    def test_versioned_backups_never_replace_previous_version(self):
        for name in ("one", "two"):
            public = public_site(self.store.sites()[0])
            public["name"] = name
            self.store.save([public])
        versions = list(self.path.with_name(self.path.name + ".versions").glob("*.json"))
        self.assertEqual(len(versions), 2)
        self.assertEqual({json.loads(p.read_text(encoding="utf-8"))["sites"][0]["name"] for p in versions}, {"Fixture", "one"})

    def test_nullable_empty_ui_metadata_and_policy_inheritance(self):
        raw = public_site(self.old)
        raw["auth"].update(account_id="", expires_at="")
        cleaned, errors = self.store.save([raw])
        self.assertFalse(errors)
        self.assertIsNone(cleaned[0]["auth"]["account_id"])
        self.assertEqual(cleaned[0]["connection"], {})

    def test_rotation_cas_cannot_overwrite_manually_replaced_pair(self):
        expected = CredentialSession(credentials_of(self.old)).snapshot(0)
        from dataclasses import replace
        replacement = replace(expected, credentials=replace(expected.credentials, access_token="new", refresh_token="new-refresh"), token_version=1)
        raw = public_site(self.old)
        raw["auth"]["refresh_token"] = "manually-replaced"
        self.store.save([raw])
        self.assertFalse(self.store.rotate(self.old, expected, replacement))
        self.assertEqual(self.store.sites()[0]["auth"]["refresh_token"], "manually-replaced")


class RouterTests(unittest.TestCase):
    def api(self, category, credentials=None):
        api = Mock(config_revision="r")
        api.session = CredentialSession(credentials or Credentials(), "r")
        api.fetch.side_effect = lambda feature, **kw: outcome(feature, category)
        return api

    def test_feature_site_global_precedence(self):
        global_policy = ConnectionPolicy(mode="api_only")
        site = SiteConnectionPolicy(mode="compat_only", features={"wallet": "api_preferred"})
        self.assertEqual(effective_mode(global_policy, site, "wallet"), ConnectionMode.API_PREFERRED)
        self.assertEqual(effective_mode(global_policy, site, "profile"), ConnectionMode.COMPAT_ONLY)
        self.assertEqual(effective_mode(global_policy, SiteConnectionPolicy(), "profile"), ConnectionMode.API_ONLY)

    def test_missing_api_credential_uses_compat_once_with_shared_budget(self):
        api, alternate = self.api(ErrorCategory.MISSING_CREDENTIAL), Mock()
        alternate.fetch.return_value = outcome(Feature.WALLET, provider=ProviderKind.COMPAT)
        router = Router(api, compat_factory=lambda: alternate)
        context = RequestContext()
        result = router.fetch(Feature.WALLET, context=context)
        self.assertTrue(result.ok)
        self.assertEqual(router.attempts, ["api", "compat"])
        self.assertIs(api.fetch.call_args.kwargs["context"], alternate.fetch.call_args.kwargs["context"])

    def test_no_compat_for_blocking_or_transient_errors(self):
        categories = [ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED, ErrorCategory.AUTH_SESSION_REQUIRED,
            ErrorCategory.PERMISSION_DENIED, ErrorCategory.SECOND_FACTOR_REQUIRED, ErrorCategory.ACCOUNT_DISABLED,
            ErrorCategory.RATE_LIMITED, ErrorCategory.TIMEOUT, ErrorCategory.NETWORK_ERROR, ErrorCategory.SERVER_ERROR,
            ErrorCategory.ACCESS_BLOCKED, ErrorCategory.ENDPOINT_NOT_FOUND, ErrorCategory.METHOD_NOT_ALLOWED,
            ErrorCategory.PROTOCOL_ERROR, ErrorCategory.PARTIAL_DATA, ErrorCategory.OPERATION_UNKNOWN]
        for category in categories:
            with self.subTest(category=category):
                factory = Mock()
                router = Router(self.api(category), compat_factory=factory)
                router.fetch(Feature.WALLET)
                factory.assert_not_called()

    def test_api_only_never_falls_back(self):
        factory = Mock()
        router = Router(self.api(ErrorCategory.MISSING_CREDENTIAL), policy=ConnectionPolicy(mode="api_only"), compat_factory=factory)
        router.fetch(Feature.WALLET)
        factory.assert_not_called()

    def test_global_disable_overrides_compat_only_without_calling_factory(self):
        api, factory = self.api(None), Mock()
        router = Router(api, policy=ConnectionPolicy(mode="compat_only", compat_enabled=False), compat_factory=factory)
        self.assertEqual(router.fetch(Feature.WALLET).error_category, ErrorCategory.COMPAT_DISABLED)
        api.fetch.assert_not_called()
        factory.assert_not_called()

    def test_unknown_private_api_identity_prevents_cross_channel_fallback(self):
        api = self.api(ErrorCategory.AUTH_EXPIRED, Credentials(management_token="token"))
        api.fetch.side_effect = lambda feature, **kw: outcome(feature, ErrorCategory.AUTH_EXPIRED, account=None)
        factory = Mock()
        Router(api, compat_factory=factory).fetch(Feature.WALLET)
        factory.assert_not_called()

    def test_verified_but_different_compat_account_is_rejected(self):
        alternate = Mock()
        alternate.fetch.return_value = outcome(Feature.WALLET, provider=ProviderKind.COMPAT, account=99)
        api = self.api(ErrorCategory.AUTH_EXPIRED, Credentials(management_token="token", account_id=7, account_verified=True))
        result = Router(api, compat_factory=lambda: alternate).fetch(Feature.WALLET)
        self.assertEqual(result.error_category, ErrorCategory.ACCOUNT_MISMATCH)

    def test_sent_write_never_crosses_channel(self):
        context = RequestContext()
        context.write_sent = True
        factory = Mock()
        api = self.api(ErrorCategory.MISSING_CREDENTIAL)
        Router(api, compat_factory=factory).fetch(Feature.KEYS_CREATE, context=context)
        factory.assert_not_called()

    def test_local_not_migrated_is_preserved_in_attempt_evidence(self):
        api, alternate = self.api(ErrorCategory.NOT_MIGRATED), Mock()
        alternate.fetch.return_value = outcome(Feature.TOPUPS, provider=ProviderKind.COMPAT)
        router = Router(api, compat_factory=lambda: alternate)
        self.assertEqual(router.fetch(Feature.TOPUPS).fallback_reason.value, "not_migrated")
        self.assertEqual(api.fetch.call_count, 1)
        self.assertEqual(alternate.fetch.call_count, 1)


class Remote:
    def __init__(self):
        self.calls, self.rows, self.writes = [], [], 0
        self.fail_topups = False
        self.fail_after_write = False
        self.reveal_denied = False
        self.hook = None

    def request(self, method, url, *, headers, body, timeout):
        path = urlsplit(url).path
        self.calls.append((method, path, dict(headers), body))
        if self.hook:
            self.hook(method, path)
        value = None
        if path == "/api/pricing":
            return HttpResponse(200, {"success": True, "data": [{"model_name": "m", "model_ratio": 1, "completion_ratio": 2, "enable_groups": ["default"]}], "group_ratio": {"default": 1}, "usable_group": {"default": "Default"}})
        if path == "/api/status":
            value = {"quota_per_unit": 500000, "quota_display_type": "CNY", "usd_exchange_rate": 7}
        elif path == "/api/user/self":
            value = {"id": 7, "username": "fixture", "group": "default", "quota": 500000, "used_quota": 1000000}
        elif path == "/api/user/topup/self":
            if self.fail_topups:
                return HttpResponse(403, {"message": "forbidden"})
            value = {"items": [{"id": 4, "status": "success", "amount": 1500000}], "total": 1}
        elif path == "/api/user/self/groups":
            value = {"default": {"ratio": 1}}
        elif path == "/api/perf-metrics/summary":
            value = {"models": []}
        elif path == "/api/perf-metrics":
            value = {"model_name": parse_qs(urlsplit(url).query)["model"][0], "groups": []}
        elif path == "/api/token/" and method == "GET":
            value = {"items": copy.deepcopy(self.rows), "total": len(self.rows)}
        elif path == "/api/token/" and method == "POST":
            self.writes += 1
            self.rows.append({"id": self.writes, **body})
            if self.fail_after_write:
                raise TimeoutError()
            value = {"id": self.writes}
        elif path.endswith("/key"):
            if self.reveal_denied:
                return HttpResponse(403, {"message": "access token scope denied: api_key:reveal"})
            value = {"key": "synthetic-full-model-key"}
        elif path.startswith("/api/token/") and method == "DELETE":
            self.writes += 1
            self.rows = [r for r in self.rows if str(r["id"]) != path.rsplit("/", 1)[1]]
            if self.fail_after_write:
                raise TimeoutError()
            value = {}
        else:
            return HttpResponse(404, {"message": "candidate route not found"})
        return HttpResponse(200, {"success": True, "data": value})


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config, self.settings = self.folder / "sites.json", self.folder / "settings.json"
        self.site = copy.deepcopy(SITE)
        write_json(self.config, {"sites": [self.site]})
        write_json(self.settings, {"connection_policy": {"mode": "api_only", "compat_enabled": False}})
        self.remote = Remote()
        self.runtime = IntegrationRuntime(self.config, self.settings, self.folder / "data", transport_factory=lambda *args: self.remote)

    def router(self):
        return self.runtime.router(self.site)

    def test_account_projection_retains_legacy_fields_and_feature_evidence(self):
        snapshot = account_snapshot(self.router(), self.site)
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["quota_usd"], 1)
        self.assertEqual(snapshot["used_quota_usd"], 2)
        self.assertEqual(snapshot["recharge_usd"], 3)
        self.assertEqual(snapshot["token_count"], 0)
        self.assertTrue(snapshot["keys_complete"])
        self.assertEqual(snapshot["features"]["keys.list"]["account_id"], 7)

    def test_standard_wallet_scale_also_restores_key_amounts_and_ccs_usage(self):
        self.remote.rows = [{"id": 1, "name": "fixture-key", "group": "default",
                             "remain_quota": 250000, "used_quota": 125000,
                             "unlimited_quota": False}]
        snapshot = account_snapshot(self.router(), self.site)
        self.assertEqual(snapshot["wallet"]["unit"], "USD")
        self.assertEqual(snapshot["wallet"]["unit_source"], "newapi.quota_per_unit")
        self.assertEqual(snapshot["tokens"][0]["remain_usd"], .5)
        self.assertEqual(snapshot["tokens"][0]["used_usd"], .25)
        result = self.router().fetch(Feature.CCS_USAGE)
        self.assertTrue(result.ok)
        self.assertIn('unit: "USD"', result.data["script"])
        self.assertIn("500000.0", result.data["script"])

    def test_profile_is_read_once_per_refresh_and_fresh_again_next_time(self):
        for refresh in range(2):
            snapshot = account_snapshot(self.router(), self.site)
            self.assertTrue(snapshot["ok"])
            calls = [c for c in self.remote.calls if c[1] == "/api/user/self"]
            self.assertEqual(len(calls), refresh + 1)
            self.assertEqual(snapshot["used_quota_usd"], 2)
            self.assertEqual(snapshot["wallet"]["amount"], 1)

    def test_partial_feature_retains_its_value_and_actual_success_time(self):
        previous = account_snapshot(self.router(), self.site)
        self.remote.fail_topups = True
        snapshot = account_snapshot(self.router(), self.site, previous=previous)
        self.assertEqual(snapshot["recharge_usd"], 3)
        self.assertEqual(snapshot["topups_updated_at"], previous["topups_updated_at"])
        self.assertFalse(snapshot["topups_complete"])
        self.assertTrue(snapshot["features"]["topups"]["stale"])
        self.assertTrue(snapshot["features"]["wallet"]["status"] == "success")

    def test_previous_account_or_credential_binding_is_never_reused(self):
        previous = account_snapshot(self.router(), self.site)
        previous["binding_ref"] = "another-account"
        self.remote.fail_topups = True
        snapshot = account_snapshot(self.router(), self.site, previous=previous)
        self.assertIsNone(snapshot["recharge_usd"])
        self.assertIsNone(snapshot["topups_updated_at"])

    def test_create_reveal_export_exact_key_delete_no_full_account_refresh(self):
        router = self.router()
        token = create_token(router, self.site, name="new", group="default")
        self.assertEqual(token["key"], "sk-synthetic-full-model-key")
        exported = resolve_export(router, self.site, group="default", token_id=token["id"], create=False, model="m")
        self.assertEqual(exported["key"], token["key"])
        self.assertTrue(router.fetch(Feature.KEYS_DELETE, id=token["id"]).ok)
        self.assertEqual(self.remote.writes, 2)
        self.assertFalse(any(path == "/api/user/topup/self" for _, path, _, _ in self.remote.calls))
        journal = read_json(self.folder / "data" / "write_operations.json")
        self.assertTrue(all(e["status"] == "confirmed" for e in journal["operations"]))
        self.assertNotIn("synthetic", json.dumps(journal))

    def test_confirmed_create_with_reveal_denied_stays_created_and_does_not_repeat(self):
        self.remote.reveal_denied = True
        token = create_token(self.router(), self.site, name="new", group="default")
        self.assertTrue(token["created"])
        self.assertTrue(token["needs_reveal"])
        self.assertEqual(token["key"], "")
        self.assertEqual(self.remote.writes, 1)
        self.assertEqual(read_json(self.folder / "data" / "write_operations.json")["operations"][0]["status"], "confirmed")

    def test_unknown_create_blocks_same_request_after_restart(self):
        self.remote.fail_after_write = True
        first = self.router().fetch(Feature.KEYS_CREATE, name="new", group="default")
        self.assertEqual(first.error_category, ErrorCategory.OPERATION_UNKNOWN)
        restarted = IntegrationRuntime(self.config, self.settings, self.folder / "data", transport_factory=lambda *args: self.remote)
        second = restarted.router(self.site).fetch(Feature.KEYS_CREATE, name="new", group="default")
        self.assertEqual(second.error_category, ErrorCategory.OPERATION_UNKNOWN)
        self.assertEqual(self.remote.writes, 1)
        operation = read_json(self.folder / "data" / "write_operations.json")["operations"][0]
        self.assertTrue(restarted.router(self.site).journal.reconcile(operation["id"], account_id=7, rows=self.remote.rows))

    def test_unknown_delete_is_resolved_only_by_absence_on_complete_current_account_list(self):
        self.remote.rows = [{"id": 1}]
        self.remote.fail_after_write = True
        router = self.router()
        result = router.fetch(Feature.KEYS_DELETE, id=1)
        self.assertEqual(result.error_category, ErrorCategory.OPERATION_UNKNOWN)
        operation = result.data["operation_id"]
        with self.assertRaises(ProviderFailure):
            router.journal.reconcile(operation, account_id=99, rows=[])
        self.assertTrue(router.journal.reconcile(operation, account_id=7, rows=[]))

    def test_config_edit_during_response_discards_old_private_result(self):
        def mutate(method, path):
            if path == "/api/user/self":
                changed = copy.deepcopy(self.site)
                changed["auth"]["management_token"] = "replaced"
                write_json(self.config, {"sites": [changed]})
        self.remote.hook = mutate
        result = self.router().fetch(Feature.PROFILE)
        self.assertEqual(result.error_category, ErrorCategory.CONFIG_CHANGED)
        self.assertIsNone(result.data)

    def test_disabled_compat_is_not_imported_even_after_api_failure(self):
        source = '''import sys, tempfile
from pathlib import Path
from integrations.configuration import write_json
from integrations.runtime import IntegrationRuntime
from integrations.contracts import Feature
with tempfile.TemporaryDirectory() as folder:
 p = Path(folder); site = {"id":"s","base_url":"https://fixture.invalid","auth":{}}
 write_json(p/"sites.json", {"sites":[site]}); write_json(p/"settings.json", {"connection_policy":{"compat_enabled":False}})
 runtime = IntegrationRuntime(p/"sites.json", p/"settings.json", p/"data")
 runtime.router(site).fetch(Feature.PROFILE)
 assert "integrations.compat" not in sys.modules
'''
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pricing_uses_selected_feature_and_retains_offer_metadata(self):
        result = pricing_snapshot(self.router(), self.site)
        self.assertTrue(result["meta"]["ok"])
        self.assertTrue(result["offers"])
        self.assertEqual(result["offers"][0]["visibility_scope"], "account")
        self.assertIn("pricing.personal", result["meta"]["features"])

    def test_journal_failure_prevents_post(self):
        router = self.router()
        with patch.object(router.journal, "_save", side_effect=OSError("disk unavailable")):
            result = router.fetch(Feature.KEYS_CREATE, name="new", group="default")
        self.assertEqual(result.error_category, ErrorCategory.OPERATION_SAVE_FAILED)
        self.assertEqual(self.remote.writes, 0)


class CcsTests(unittest.TestCase):
    def test_newapi_verified_unit_template_excludes_new_api_user_and_refresh_token(self):
        usage = newapi_usage(base_url=SITE["base_url"], token="synthetic-management", quota_per_unit=1000, unit="CNY")
        link = build_ccswitch_link(app="codex", name="fixture", api_key="sk-model", usage=usage)
        import base64
        values = parse_qs(urlsplit(link).query)
        script = base64.b64decode(values["usageScript"][0]).decode()
        self.assertIn("/ 1000.0", script)
        self.assertIn('unit: "CNY"', script)
        self.assertNotIn("New-Api-User", script)
        self.assertNotIn("refresh", link)

    def test_unknown_wallet_unit_cannot_generate_usd_usage_script(self):
        self.assertIsNone(newapi_usage(base_url=SITE["base_url"], token="token", quota_per_unit=500000, unit="unknown"))

    def test_sub2_usage_uses_model_key_and_separates_balance_kinds(self):
        usage = sub2api_usage(base_url="https://fixture.invalid/prefix", key="sk-synthetic-model")
        self.assertIn("{{baseUrl}}/v1/usage", usage["script"])
        self.assertIn('"subscription"', usage["script"])
        self.assertIn('"key_quota"', usage["script"])
        self.assertNotIn("/api/user/self", usage["script"])


class SiteProxyTests(unittest.TestCase):
    """站点级代理：优先级、协议规整、保存校验，以及采集入口真的按站取代理。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sites.json"
        self.out = Path(self.temp.name) / "out"
        self.out.mkdir()

    def _store(self, sites):
        write_json(self.path, {"sites": sites, "integration_schema_version": 1})
        return ConfigurationStore(self.path)

    def test_site_proxy_prefers_site_then_global_then_direct(self):
        self.assertEqual(site_proxy({"proxy": "http://site.invalid:8080"}, "http://global.invalid:8080"),
                         "http://site.invalid:8080")
        self.assertEqual(site_proxy({"proxy": ""}, "http://global.invalid:8080"), "http://global.invalid:8080")
        self.assertEqual(site_proxy({}, "http://global.invalid:8080"), "http://global.invalid:8080")
        self.assertEqual(site_proxy({"proxy": "  "}, ""), "")
        self.assertEqual(site_proxy({}, None), "")

    def test_bare_host_port_gets_http_scheme_but_random_text_is_not_rewritten(self):
        self.assertEqual(normalize_proxy("127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(normalize_proxy(" proxy.local:1080 "), "http://proxy.local:1080")
        self.assertEqual(normalize_proxy("http://127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(normalize_proxy("socks5://127.0.0.1:1080"), "socks5://127.0.0.1:1080")
        self.assertEqual(normalize_proxy("随便写的"), "随便写的")
        self.assertEqual(normalize_proxy(None), "")
        # 规整过的主机:端口写法在取用时也带协议
        self.assertEqual(site_proxy({"proxy": "127.0.0.1:7890"}, "http://global.invalid:8080"),
                         "http://127.0.0.1:7890")

    def test_saving_keeps_normalized_proxy_and_rejects_unusable_values(self):
        store = self._store([{**copy.deepcopy(SITE), "auth": {}}])
        site = public_site(store.sites()[0])
        site["proxy"] = "127.0.0.1:7890"
        cleaned, errors = store.save([site])
        self.assertFalse(errors)
        self.assertEqual(cleaned[0]["proxy"], "http://127.0.0.1:7890")

        for bad in ("socks5://127.0.0.1:1080", "http://host.invalid", "http://a:1\nb", "x" * 201):
            with self.subTest(bad=bad):
                broken = public_site(store.sites()[0])
                broken["proxy"] = bad
                _, errors = store.save([broken])
                self.assertTrue(errors, bad)
        # 被拒的非法值不会污染已保存的配置
        self.assertEqual(store.sites()[0]["proxy"], "http://127.0.0.1:7890")

    def test_empty_proxy_is_not_persisted_and_keeps_revision(self):
        """留空 = 跟随全局。绝不能给每个站点凭空加 proxy 字段，否则 _revision 全变、账户快照全标脏。"""
        store = self._store([{**copy.deepcopy(SITE), "auth": {}}])
        # 先存一次，让 connection / _token_pair_ref 这些字段补齐，拿到稳定基线
        store.save([public_site(store.sites()[0])])
        baseline = store.sites()[0]["_revision"]

        draft = public_site(store.sites()[0])
        draft["proxy"] = ""
        cleaned, errors = store.save([draft])
        self.assertFalse(errors)
        self.assertNotIn("proxy", cleaned[0])
        self.assertEqual(cleaned[0]["_revision"], baseline)

        # 写了代理就落盘（并规整协议），清掉时代理字段一起消失
        draft = public_site(store.sites()[0])
        draft["proxy"] = "http://site.invalid:8080"
        store.save([draft])
        self.assertEqual(store.sites()[0]["proxy"], "http://site.invalid:8080")
        cleared = public_site(store.sites()[0])
        cleared["proxy"] = ""
        cleaned, _ = store.save([cleared])
        self.assertNotIn("proxy", cleaned[0])

    def test_collection_uses_site_proxy_before_global(self):
        """采集入口必须逐站取代理，不能用一份全局代理把站点自己的设置盖掉。"""
        import fetch_prices as fp
        sites = [
            {**copy.deepcopy(SITE), "id": "with-proxy", "auth": {}, "proxy": "http://site.invalid:8080"},
            {**copy.deepcopy(SITE), "id": "bare-proxy", "auth": {}, "proxy": "127.0.0.1:7890"},
            {**copy.deepcopy(SITE), "id": "inherit", "auth": {}},
        ]
        write_json(self.path, {"sites": sites, "integration_schema_version": 1})
        seen = {}

        def fake_collect(site, proxy, debug, **kwargs):
            seen[site["id"]] = proxy
            return {"meta": {"id": site["id"], "name": site["id"], "ok": True, "binding_ref": site["id"],
                             "model_count": 0, "offer_count": 0}, "offers": []}

        with patch.object(fp, "collect_site", fake_collect), \
             patch.object(fp, "load_settings", lambda: {"proxy": "http://global.invalid:8080", "throttle_sec": 0}):
            result = fp.run_collection(logger=lambda *_: None, config_path=self.path, out_dir=self.out)
        self.assertEqual(seen, {"with-proxy": "http://site.invalid:8080",
                                "bare-proxy": "http://127.0.0.1:7890",
                                "inherit": "http://global.invalid:8080"})
        # 没有报价时按既有约定不写盘
        self.assertFalse(result["written"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
