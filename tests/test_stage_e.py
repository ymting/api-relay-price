"""E 阶段验收：按 F01–F12 分组构造 fixture，核验本地文件内容 + 实际请求次数。

本文件只使用脱敏响应、临时 sites/settings 与脚本化远端替身；不读取真实凭据、
不访问外部站点、不写用户配置。与 A–D 的差别在于：这里对每个场景同时检查
「本地落盘内容」和「真实发生的请求次数」，避免出现"看起来过了"的假通过。

覆盖范围（与方案第十六节的分组对齐）：
  F01 配置迁移与策略默认值      F02/F03 鉴权分类与请求预算
  F11 任务入口/强停/健康无样本  F12 兼容剥离后的完整回归
  第十六节「成熟判断」         站点×功能证据矩阵、退出闸门、剥离验收
F04–F10 的关键断言已在 test_stage_a/b/c/d 中实现并有请求次数断言，本文件不重复造。
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from integrations.configuration import ConfigurationStore, public_site, read_json, write_json
from integrations.contracts import (ConnectionMode, ConnectionPolicy, ErrorCategory, FallbackReason,
                                    Feature, FeatureResult, FeatureStatus, ProviderKind, utc_now)
from integrations.providers import Credentials, CredentialSession, HttpResponse, RequestContext
from integrations.retirement import (CONTROLLED_ONLY, MIN_SCHEDULED_RUNS, EvidenceMatrix,
                                     build_report, readiness)
from integrations.router import Router, effective_mode
from integrations.runtime import IntegrationRuntime
from integrations import service as integration_service

ROOT = Path(__file__).resolve().parents[1]

SITE = {"id": "fixture", "name": "Fixture", "type": "newapi", "base_url": "https://fixture.invalid",
        "enabled": True, "auth": {"management_token": "synthetic-management", "account_id": 7}}


# ---------------------------------------------------------------------------
# 脚本化远端替身：记录每一次真实请求，便于按次数断言
# ---------------------------------------------------------------------------

class CountingRemote:
    def __init__(self, routes=None, responder=None):
        self.routes = dict(routes or {})
        self.responder = responder
        self.calls = []
        self.hook = None

    def request(self, method, url, *, headers, body, timeout):
        path = urlsplit(url).path
        self.calls.append((method, path))
        if self.hook:
            self.hook(method, path)
        if self.responder is not None:
            return self.responder(method, path, body)
        route = self.routes.get(path)
        if route is None:
            return HttpResponse(404, {"message": "no such route"})
        return route(method, body) if callable(route) else route

    def count(self, path=None):
        return sum(1 for _, p in self.calls if path is None or p == path)

    def paths(self):
        return [p for _, p in self.calls]


def newapi_remote(*, topups_ok=True, perf_models=(), price_ok=True, rows=None, error=None):
    """合成一个 New API 站点。error 非空时所有业务路径都返回该错误响应。"""
    rows = copy.deepcopy(rows if rows is not None else [])
    state = {"rows": rows, "writes": 0}

    def respond(method, path, body):
        if error is not None:
            return HttpResponse(error[0], error[1])
        if path == "/api/pricing":
            if not price_ok:
                return HttpResponse(500, {"message": "boom"})
            return HttpResponse(200, {"success": True, "data": [
                {"model_name": "m", "model_ratio": 1, "completion_ratio": 2, "enable_groups": ["default"]}],
                "group_ratio": {"default": 1}, "usable_group": {"default": "Default"}})
        if path == "/api/status":
            return HttpResponse(200, {"success": True, "data": {"quota_per_unit": 500000, "quota_currency": "USD"}})
        if path == "/api/user/self":
            return HttpResponse(200, {"success": True, "data": {
                "id": 7, "username": "fixture", "group": "default", "quota": 500000, "used_quota": 1000000}})
        if path == "/api/user/topup/self":
            if not topups_ok:
                return HttpResponse(403, {"message": "forbidden"})
            return HttpResponse(200, {"success": True, "data": {"items": [
                {"id": 4, "status": "success", "amount": 1500000}], "total": 1}})
        if path == "/api/user/self/groups":
            return HttpResponse(200, {"success": True, "data": {"default": {"ratio": 1}}})
        if path == "/api/perf-metrics/summary":
            return HttpResponse(200, {"success": True, "data": {"models": list(perf_models)}})
        if path == "/api/perf-metrics":
            return HttpResponse(200, {"success": True, "data": {
                "model_name": "m", "groups": [{"group": "default", "success_rate": 100}]}})
        if path == "/api/token/" and method == "GET":
            return HttpResponse(200, {"success": True, "data": {"items": copy.deepcopy(state["rows"]),
                                                               "total": len(state["rows"])}})
        if path == "/api/token/" and method == "POST":
            state["writes"] += 1
            state["rows"].append({"id": state["writes"], **(body or {})})
            return HttpResponse(200, {"success": True, "data": {"id": state["writes"]}})
        if path.endswith("/key"):
            return HttpResponse(200, {"success": True, "data": {"key": "synthetic-full-model-key"}})
        if path.startswith("/api/token/") and method == "DELETE":
            state["writes"] += 1
            state["rows"] = [r for r in state["rows"] if str(r["id"]) != path.rsplit("/", 1)[1]]
            return HttpResponse(200, {"success": True, "data": {}})
        return HttpResponse(404, {"message": "no such route"})

    return CountingRemote(responder=respond)


# ---------------------------------------------------------------------------
# F01 配置迁移
# ---------------------------------------------------------------------------

class ConfigurationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = self.folder / "sites.json"
        self.old = {**copy.deepcopy(SITE), "auth": {**SITE["auth"], "cookie": "synthetic-cookie",
            "password": "synthetic-password", "username": "old", "model_api_key": "sk-synthetic"}}
        write_json(self.config, {"sites": [self.old], "proxy": "old-proxy"})
        self.store = ConfigurationStore(self.config)

    def test_read_save_read_is_stable_and_never_touches_remote(self):
        remote = newapi_remote()
        runtime = IntegrationRuntime(self.config, self.folder / "settings.json",
                                     self.folder / "data", transport_factory=lambda *a: remote)
        public = public_site(self.store.sites()[0])
        self.assertTrue(all(public["has_secret"][k] for k in
                            ("cookie", "password", "management_token", "model_api_key")))
        public["connection"] = {"mode": "api_only", "features": {"wallet": "compat_only"}}
        with patch("integrations.providers.UrllibTransport.request",
                   side_effect=AssertionError("保存配置不该联网")):
            cleaned, errors = self.store.save([public])
        self.assertFalse(errors)
        again = public_site(ConfigurationStore(self.config).sites()[0])
        self.assertEqual(again["connection"], {"mode": "api_only", "features": {"wallet": "compat_only"}})
        self.assertEqual(again["has_secret"], public["has_secret"])
        self.assertEqual(remote.calls, [])
        # 站点策略覆盖优先于站点，站点优先于全局
        router = runtime.router(cleaned[0], settings={"connection_policy": {"mode": "compat_only"}})
        self.assertEqual(effective_mode(router.policy, router.site_policy, Feature.WALLET),
                         ConnectionMode.COMPAT_ONLY)
        self.assertEqual(effective_mode(router.policy, router.site_policy, Feature.PROFILE),
                         ConnectionMode.API_ONLY)

    def test_legacy_record_without_policy_defaults_to_api_preferred(self):
        policy = ConnectionPolicy.from_dict(None)
        self.assertEqual(policy.mode, ConnectionMode.API_PREFERRED)
        self.assertTrue(policy.compat_enabled)
        self.assertEqual(policy.to_dict()["mode"], "api_preferred")

    def test_two_accounts_are_never_merged_by_binding(self):
        other = {**copy.deepcopy(SITE), "id": "other", "auth": {"management_token": "t2", "account_id": 99}}
        self.assertNotEqual(integration_service.binding_of(self.old), integration_service.binding_of(other))


# ---------------------------------------------------------------------------
# F02 / F03 鉴权分类与请求预算
# ---------------------------------------------------------------------------

class AuthClassificationBudgetTests(unittest.TestCase):
    """F02 分类与预算、F03 权限目录：全部在契约/provider 层断言，不联网。"""

    def _provider(self, remote, *, read_retries=0, write_hooks=None):
        from integrations.providers import NewApiProvider
        session = CredentialSession(Credentials(management_token="synthetic", account_id=7,
                                                account_verified=True), "r")
        return NewApiProvider(SITE, session, config_revision="r", transport=remote,
                              read_retries=read_retries, write_hooks=write_hooks)

    def test_scope_dictionary_is_not_treated_as_current_authorization(self):
        from integrations.contracts import AuthKind, PermissionStatus, ScopeSource, check_newapi_permission
        # 访问令牌管理路由要求浏览器会话：管理令牌不可调用，/scopes 字典也解锁不了
        session = check_newapi_permission("GET", "/api/user/access_tokens", auth_kind=AuthKind.MANAGEMENT_TOKEN)
        self.assertEqual(session.status, PermissionStatus.SESSION_REQUIRED)
        self.assertEqual(session.error_category, ErrorCategory.AUTH_SESSION_REQUIRED)
        # 自报/字典里的 scope 一律待验证，不能当成已授权（否则等于用目录冒充令牌权限）
        self.assertEqual(check_newapi_permission("POST", "/api/token/1/key", scopes=["api_key:reveal"],
                          scope_source=ScopeSource.UNVERIFIED).status, PermissionStatus.UNVERIFIED)
        # 只有明确验证过的授权集合才可以判「缺权限」，且与令牌过期分开
        denied = check_newapi_permission("POST", "/api/token/1/key", scopes=["api_key:read"],
                                         scope_source=ScopeSource.VERIFIED_ASSIGNMENT)
        self.assertEqual(denied.error_category, ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED)

    def test_scope_denied_is_not_expiry_and_sends_no_refresh(self):
        import tempfile as _tempfile
        from integrations.journal import WriteJournal
        with _tempfile.TemporaryDirectory() as folder:
            journal = WriteJournal(Path(folder) / "writes.json", "fixture", "binding")
            remote = CountingRemote(responder=lambda m, p, b: HttpResponse(
                403, {"code": "ACCESS_TOKEN_SCOPE_DENIED"}))
            provider = self._provider(remote, write_hooks=journal)
            result = provider.fetch(Feature.KEYS_REVEAL, id=1)
        self.assertEqual(result.error_category, ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED)
        self.assertEqual(remote.count(), 1)                       # 权限不足不是过期：不续期、不重试
        self.assertNotIn("/auth/refresh", remote.paths())

    def test_rate_limit_obeys_bounded_budget_and_stops(self):
        remote = CountingRemote(responder=lambda m, p, b: HttpResponse(429, {"message": "slow down"}))
        provider = self._provider(remote, read_retries=1)
        result = provider.fetch(Feature.PROFILE, context=RequestContext(seconds=5))
        self.assertEqual(result.error_category, ErrorCategory.RATE_LIMITED)
        self.assertLessEqual(remote.count(), 2)      # 只允许一次有上限的重试
        self.assertNotIn("/api/user/topup/self", remote.paths())

    def test_html_challenge_is_blocked_without_blind_retry(self):
        remote = CountingRemote(responder=lambda m, p, b: HttpResponse(
            403, None, {"content-type": "text/html"}, "<html>Just a moment...</html>"))
        provider = self._provider(remote, read_retries=2)
        result = provider.fetch(Feature.PROFILE, context=RequestContext(seconds=5))
        self.assertEqual(result.error_category, ErrorCategory.ACCESS_BLOCKED)
        self.assertEqual(remote.count(), 1)          # 挑战页不盲重试

    def test_blocking_errors_never_open_compat_channel(self):
        from integrations.contracts import FeatureResult, ProviderKind
        for category in (ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED, ErrorCategory.AUTH_SESSION_REQUIRED,
                         ErrorCategory.PERMISSION_DENIED, ErrorCategory.RATE_LIMITED,
                         ErrorCategory.ACCESS_BLOCKED):
            with self.subTest(category=category):
                api = type("Api", (), {})()
                api.config_revision = "r"
                api.session = CredentialSession(Credentials(management_token="t"), "r")
                api.fetch = lambda feature, **kw: FeatureResult(
                    feature, FeatureStatus.ERROR, ProviderKind.API,
                    error_category=category, config_revision="r")
                factory_calls = []
                router = Router(api, compat_factory=lambda: factory_calls.append(1))
                router.fetch(Feature.WALLET)
                self.assertEqual(factory_calls, [], category)


# ---------------------------------------------------------------------------
# F11 任务入口 / 强停 / 健康无样本
# ---------------------------------------------------------------------------

class TaskEntryAndHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.config = self.folder / "sites.json"
        self.settings = self.folder / "settings.json"
        self.data = self.folder / "data"
        write_json(self.config, {"sites": [copy.deepcopy(SITE), {**copy.deepcopy(SITE), "id": "second"}]})
        write_json(self.settings, {"connection_policy": {"mode": "api_only", "compat_enabled": False}})

    def _runtime(self, remote):
        return IntegrationRuntime(self.config, self.settings, self.data,
                                  transport_factory=lambda *a: remote)

    def test_collector_task_uses_one_entrypoint_for_manual_and_schedule(self):
        import fetch_prices as fp
        import server
        task = server.CollectorTask()
        seen = []

        def fake_run(**kwargs):
            seen.append(kwargs)
            return {"stopped": False, "sites": [{"ok": True}], "model_count": 0, "offer_count": 0,
                    "generated_at": "t", "stale_sites": []}

        with patch.object(server.collector, "run_collection", side_effect=fake_run), \
             patch.object(server, "ensure_settings", return_value={"throttle_sec": 0}), \
             patch.object(server, "log_line"):
            self.assertTrue(task.start(trigger="schedule"))
            for _ in range(200):
                if not task.running:
                    break
                time.sleep(.01)
            self.assertTrue(task.start(trigger="manual", only="fixture", models=["m"], merge=True, scope="custom"))
            for _ in range(200):
                if not task.running:
                    break
                time.sleep(.01)
        self.assertEqual(len(seen), 2)
        # 两个入口共用同一个 run_collection，只是参数不同
        self.assertEqual(seen[0]["only"], "")
        self.assertEqual((seen[1]["only"], seen[1]["models"], seen[1]["merge"]), ("fixture", ["m"], True))
        for call in seen:
            self.assertIn("should_stop", call)

    def test_scheduled_collection_refreshes_cache_and_counts_only_fresh_sites(self):
        import server
        self.data.mkdir()
        write_json(self.data / "prices.json", {"generated_at": "old", "models": []})
        write_json(self.data / "index.json", {"generated_at": "old", "models": []})
        task = server.CollectorTask()

        def fake_run(**kwargs):
            write_json(self.data / "prices.json", {"generated_at": "new", "models": []})
            write_json(self.data / "index.json", {"generated_at": "new", "models": []})
            return {"stopped": False, "sites": [{"ok": True}, {"ok": True, "stale": True}],
                    "model_count": 1, "offer_count": 1, "generated_at": "new", "stale_sites": ["second"]}

        with patch.object(server, "DATA_DIR", self.data), \
             patch.object(server.collector, "run_collection", side_effect=fake_run), \
             patch.object(server, "ensure_settings", return_value={}), \
             patch.object(server, "log_line"):
            cache = server.DataCache()
            with patch.object(server, "CACHE", cache):
                self.assertTrue(task.start(trigger="schedule"))
                for _ in range(200):
                    if not task.snapshot()["running"]:
                        break
                    time.sleep(.01)
                snapshot = task.snapshot()
                self.assertFalse(snapshot["running"])
                self.assertIsNone(snapshot["error"])
                self.assertEqual(cache.get_index()["generated_at"], "new")
                self.assertEqual(snapshot["summary"]["ok_sites"], 1)
                self.assertEqual(snapshot["summary"]["stale_sites"], ["second"])

    def test_stopped_collection_does_not_refresh_cache_or_replace_summary(self):
        import server
        task = server.CollectorTask()
        task.last_summary = {"generated_at": "old"}
        cache = Mock()
        with patch.object(server, "CACHE", cache), \
             patch.object(server.collector, "run_collection", return_value={"stopped": True, "written": False}), \
             patch.object(server, "ensure_settings", return_value={}), patch.object(server, "log_line"):
            self.assertTrue(task.start(trigger="schedule"))
            for _ in range(200):
                if not task.snapshot()["running"]:
                    break
                time.sleep(.01)
            self.assertTrue(task.snapshot()["stopped"])
            self.assertEqual(task.snapshot()["summary"], {"generated_at": "old"})
            cache.reload.assert_not_called()

    def test_cli_uses_the_same_entrypoint(self):
        import fetch_prices as fp
        calls = []
        # 必须用临时状态目录：本机服务运行时会持有真实 .lock，
        # 否则这条单测会拿到退出码 2，结果随环境漂移。
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "sites.json"
            with patch.object(fp, "DEFAULT_CONFIG", config), \
                 patch.object(fp, "OUT_DIR", Path(temp) / "data"), \
                 patch.object(fp, "run_collection", side_effect=lambda **kw: (calls.append(kw) or
                              {"sites": [{"ok": True}], "model_count": 0, "offer_count": 0})), \
                 patch.object(sys, "argv", ["fetch_prices.py", "--only", "fixture", "--merge"]):
                self.assertEqual(fp.main(), 0)
        self.assertEqual(calls[0]["only"], "fixture")
        self.assertTrue(calls[0]["merge"])

    def test_strong_stop_stops_requests_and_writes_nothing(self):
        import fetch_prices as fp
        stop = threading.Event()
        remote = newapi_remote()

        def hook(method, path):
            if path == "/api/pricing":
                stop.set()          # 第一站刚发出请求就下令停止

        remote.hook = hook
        runtime = self._runtime(remote)
        with patch.object(fp, "integration_runtime", return_value=runtime), \
             patch.object(fp, "load_settings", return_value={}):
            payload = fp.run_collection(only="", throttle=0, config_path=self.config, out_dir=self.data,
                                        should_stop=stop.is_set, logger=lambda *a: None)
        self.assertTrue(payload["stopped"])
        self.assertFalse(payload["written"])
        self.assertEqual(remote.count("/api/pricing"), 1)       # 第二站一次都没发
        self.assertNotIn("/api/status", remote.paths())
        self.assertFalse((self.data / "prices.json").exists())

    def test_health_without_samples_is_not_fabricated(self):
        remote = newapi_remote(perf_models=[])
        runtime = self._runtime(remote)
        site = copy.deepcopy(SITE)
        snapshot = integration_service.pricing_snapshot(runtime.router(site), site)
        self.assertTrue(snapshot["meta"]["ok"])
        for offer in snapshot["offers"]:
            self.assertNotIn("health", offer)                   # 无样本不编造健康
        record = integration_service.health_snapshot(runtime.router(site), "m")
        self.assertEqual(record["groups"][0]["success_rate"], 100)

    def test_single_site_collect_only_requests_that_site(self):
        import fetch_prices as fp
        remote = newapi_remote()
        runtime = self._runtime(remote)
        with patch.object(fp, "integration_runtime", return_value=runtime), \
             patch.object(fp, "load_settings", return_value={}):
            payload = fp.run_collection(only="fixture", throttle=0, config_path=self.config,
                                        out_dir=self.data, logger=lambda *a: None)
        self.assertEqual(remote.count("/api/pricing"), 1)
        self.assertEqual([s["id"] for s in payload["sites"]], ["fixture"])

    def test_debug_flag_reports_per_feature_evidence_and_is_silent_otherwise(self):
        # --debug 在 D 重构后一度变成空操作（旧适配器的内部 print 已废弃），这里钉住它仍有输出。
        import fetch_prices as fp

        def collect(debug):
            lines = []
            with patch.object(fp, "integration_runtime", return_value=self._runtime(newapi_remote())), \
                 patch.object(fp, "load_settings", return_value={}):
                fp.run_collection(only="fixture", throttle=0, config_path=self.config, out_dir=self.data,
                                  debug=debug, logger=lines.append)
            return "".join(lines)

        quiet = collect(False)
        self.assertNotIn("provider=", quiet)
        loud = collect(True)
        self.assertIn("pricing.personal", loud)
        self.assertIn("provider=api", loud)
        self.assertIn("endpoint=/api/pricing", loud)
        self.assertIn("status=success", loud)
        # 诊断只走控制台，不能落进 prices.json 快照
        snapshot = read_json(self.data / "prices.json")
        self.assertNotIn("diagnostics", snapshot["sites"][0])


# ---------------------------------------------------------------------------
# F12 兼容剥离后的完整回归
# ---------------------------------------------------------------------------

class CompatRetirementTests(unittest.TestCase):
    SCRIPT = r'''
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, PROJECT_ROOT)
from integrations.configuration import write_json
from integrations.providers import HttpResponse
from integrations.runtime import IntegrationRuntime
from integrations import service

SITE = {"id": "fixture", "name": "Fixture", "type": "newapi", "base_url": "https://fixture.invalid",
        "enabled": True, "auth": {"management_token": "synthetic", "account_id": 7}}

class Remote:
    def __init__(self):
        self.calls = []
        self.rows = []
    def request(self, method, url, *, headers, body, timeout):
        from urllib.parse import urlsplit
        path = urlsplit(url).path
        self.calls.append(path)
        if path == "/api/pricing":
            return HttpResponse(200, {"success": True, "data": [
                {"model_name": "m", "model_ratio": 1, "completion_ratio": 2, "enable_groups": ["default"]}],
                "group_ratio": {"default": 1}, "usable_group": {"default": "Default"}})
        if path == "/api/status":
            return HttpResponse(200, {"success": True, "data": {"quota_per_unit": 500000, "quota_currency": "USD"}})
        if path == "/api/user/self":
            return HttpResponse(200, {"success": True, "data": {"id": 7, "username": "f", "group": "default", "quota": 500000, "used_quota": 1000000}})
        if path == "/api/user/topup/self":
            return HttpResponse(200, {"success": True, "data": {"items": [], "total": 0}})
        if path == "/api/user/self/groups":
            return HttpResponse(200, {"success": True, "data": {"default": {"ratio": 1}}})
        if path == "/api/perf-metrics/summary":
            return HttpResponse(200, {"success": True, "data": {"models": []}})
        if path == "/api/perf-metrics":
            return HttpResponse(200, {"success": True, "data": {"model_name": "m", "groups": []}})
        if path == "/api/token/" and method == "GET":
            return HttpResponse(200, {"success": True, "data": {"items": list(self.rows), "total": len(self.rows)}})
        if path == "/api/token/" and method == "POST":
            self.rows.append({"id": 1, "name": (body or {}).get("name"), "group": (body or {}).get("group"), "status": 1})
            return HttpResponse(200, {"success": True, "data": {"id": 1, "key": "sk-synthetic"}})
        if path.endswith("/key"):
            return HttpResponse(200, {"success": True, "data": {"key": "sk-synthetic"}})
        return HttpResponse(404, {"message": "no such route"})

with tempfile.TemporaryDirectory() as folder:
    base = Path(folder)
    write_json(base / "sites.json", {"sites": [SITE]})
    write_json(base / "settings.json", {"connection_policy": {"mode": "api_only", "compat_enabled": False}})
    remote = Remote()
    runtime = IntegrationRuntime(base / "sites.json", base / "settings.json", base / "data",
                                 transport_factory=lambda *a: remote)
    # 离线读取：本地快照路径不产生任何远端请求
    offline_requests = 0
    price = service.pricing_snapshot(runtime.router(SITE), SITE)          # 采集
    account = service.account_snapshot(runtime.router(SITE), SITE)        # 账户
    token = service.create_token(runtime.router(SITE), SITE, name="new", group="default")  # Key 创建 + 明文
    exported = service.resolve_export(runtime.router(SITE), SITE, group="default",
                                      token_id=token["id"], create=False, model="m")       # CCS 衔接
    health = service.health_snapshot(runtime.router(SITE), "m")           # 健康
    legacy = [p for p in remote.calls if p == "/api/user/self/topup/self" or p == "/api/legacy"]
    print(json.dumps({
        "compat_imported": "integrations.compat" in sys.modules,
        "offline_requests": offline_requests,
        "price_ok": price["meta"]["ok"],
        "offers": len(price["offers"]),
        "account_ok": account["ok"],
        "token_key": token["key"],
        "export_key": exported["key"],
        "health_model": health["model"],
        "legacy_paths": legacy,
        "requests": len(remote.calls),
    }))
'''

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.site = copy.deepcopy(SITE)
        self.config = self.folder / "sites.json"
        self.settings = self.folder / "settings.json"
        write_json(self.config, {"sites": [self.site]})
        write_json(self.settings, {"connection_policy": {"mode": "compat_only", "compat_enabled": False}})

    def test_full_regression_without_compat_module(self):
        script = self.SCRIPT.replace("PROJECT_ROOT", repr(str(ROOT)))
        result = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data["compat_imported"])            # 全局未加载 compat
        self.assertTrue(data["price_ok"])                    # 采集
        self.assertEqual(data["offers"], 1)
        self.assertTrue(data["account_ok"])                  # 账户
        self.assertEqual(data["token_key"], "sk-synthetic")  # Key 创建 + 明文
        self.assertEqual(data["export_key"], "sk-synthetic") # CCS 衔接
        self.assertEqual(data["health_model"], "m")          # 健康
        self.assertEqual(data["legacy_paths"], [])           # 没有回落到旧路径
        self.assertEqual(data["offline_requests"], 0)
        self.assertGreater(data["requests"], 0)

    def test_global_disable_with_legacy_compat_only_site_gives_readable_error(self):
        # 站点保留旧的 compat_only，全局已停用兼容：必须给出可理解的错误，且一个请求都不发
        remote = newapi_remote()
        runtime = IntegrationRuntime(self.config, self.settings, self.folder / "data",
                                     transport_factory=lambda *a: remote)
        result = runtime.router(self.site).fetch(Feature.PROFILE)
        self.assertEqual(result.error_category, ErrorCategory.COMPAT_DISABLED)
        from integrations.contracts import ErrorInfo
        self.assertIn("停用", ErrorInfo(result.error_category).message)
        self.assertEqual(remote.calls, [])

    def test_existing_local_endpoints_are_preserved(self):
        import server
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        for route in ("/api/state", "/api/accounts", "/api/task/start", "/api/task/stop",
                      "/api/token/create", "/api/token/delete", "/api/token/key",
                      "/api/export/ccswitch", "/api/sites", "/api/settings", "/api/health/refresh"):
            with self.subTest(route=route):
                self.assertIn(route, source)
        # 旧入口在边界适配统一结果，函数名保留
        self.assertTrue(hasattr(server, "token_session"))
        self.assertTrue(hasattr(server, "validate_sites"))


# ---------------------------------------------------------------------------
# 第十六节：站点 × 功能证据矩阵
#   记录版本／验证日期、覆盖的读写／错误场景、API 成功率、P95 耗时、
#   兼容次数与原因、未确认写操作。以成功语义和业务口径为准，HTTP 200 不代表验收成功。
# ---------------------------------------------------------------------------

class EvidenceMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "evidence_matrix.jsonl"
        self.matrix = EvidenceMatrix(self.path)

    def _api(self, category=None, *, site_id="fixture"):
        api = Mock(config_revision="r")
        api.site_id = site_id
        api.session = CredentialSession(Credentials(management_token="synthetic-management",
                                                   account_id=7), "r")
        api.fetch.side_effect = lambda feature, **kw: FeatureResult(
            feature, FeatureStatus.SUCCESS if category is None else FeatureStatus.ERROR,
            ProviderKind.API, data={"id": 7}, account_id=7,
            last_success_at=utc_now() if category is None else None,
            error_category=category, config_revision="r")
        return api

    def test_records_api_attempt_with_mode_trigger_and_latency(self):
        Router(self._api(), matrix=self.matrix, trigger="schedule").fetch(Feature.WALLET)
        rows = self.matrix.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["site"], row["feature"]), ("fixture", "wallet"))
        self.assertEqual(row["provider"], "api")
        self.assertEqual(row["mode"], "api_preferred")
        self.assertEqual(row["trigger"], "schedule")
        self.assertTrue(row["ok"])
        # 只有 api 提供者且未回退，才算接口路径真的被走通
        self.assertEqual(row["annotation"], "api_verified")
        self.assertIsInstance(row["elapsed_ms"], int)
        self.assertGreaterEqual(row["elapsed_ms"], 0)

    def test_compat_fallback_records_reason_and_stays_deployment_unverified(self):
        api = self._api(ErrorCategory.MISSING_CREDENTIAL)
        alternate = Mock()
        alternate.fetch.return_value = FeatureResult(Feature.WALLET, FeatureStatus.SUCCESS,
                                                     ProviderKind.COMPAT, account_id=7,
                                                     last_success_at=utc_now())
        Router(api, compat_factory=lambda: alternate, matrix=self.matrix).fetch(Feature.WALLET)
        row = self.matrix.rows()[0]
        self.assertEqual(row["provider"], "compat")
        self.assertEqual(row["fallback_reason"], "missing_credential")
        self.assertEqual(row["annotation"], "deployment_unverified")

    def test_failure_keeps_success_evidence_and_no_secret_is_stored(self):
        Router(self._api(), matrix=self.matrix).fetch(Feature.PROFILE)
        Router(self._api(ErrorCategory.NETWORK_ERROR), matrix=self.matrix).fetch(Feature.PROFILE)
        cell = self.matrix.summary()[("fixture", "profile")]
        self.assertEqual((cell["attempts"], cell["ok"], cell["success_rate"]), (2, 1, 0.5))
        self.assertEqual(cell["errors"], {"NETWORK_ERROR": 1})
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-management", raw)     # 凭据绝不进证据库
        self.assertNotIn("Authorization", raw)

    def test_p95_and_recent_window_bound_stale_evidence(self):
        for ms in (10, 20, 30, 40, 100):
            self.matrix.record(site_id="s", feature=Feature.WALLET, provider=ProviderKind.API, ok=True,
                               mode=ConnectionMode.API_ONLY, trigger="schedule", elapsed_ms=ms)
        cell = self.matrix.summary()[("s", "wallet")]
        self.assertEqual((cell["attempts"], cell["p95_ms"]), (5, 100))
        # 只保留最近 N 条，避免陈旧证据永久生效
        windowed = self.matrix.summary(window=2)[("s", "wallet")]
        self.assertEqual((windowed["attempts"], windowed["p95_ms"]), (2, 100))

    def test_malformed_lines_are_skipped_and_rotation_keeps_tail(self):
        self.matrix.record(site_id="s", feature=Feature.WALLET, provider=ProviderKind.API, ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("{ not json }\n\n")
        self.assertEqual(len(self.matrix.rows()), 1)
        with patch("integrations.retirement.MAX_BYTES", 1):
            self.matrix.record(site_id="s", feature=Feature.PROFILE, provider=ProviderKind.API, ok=True)
        rows = self.matrix.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(isinstance(r, dict) for r in rows))

    def test_pending_writes_reads_only_unresolved_operations(self):
        journal = Path(self.temp.name) / "write_operations.json"
        write_json(journal, {"schema_version": 1, "operations": [
            {"id": "a", "site": "s", "feature": "keys.create", "status": "unknown"},
            {"id": "b", "site": "s", "feature": "keys.delete", "status": "confirmed"},
            {"id": "c", "site": "s", "feature": "keys.update", "status": "sent"}]})
        self.assertEqual([e["id"] for e in EvidenceMatrix.pending_writes(journal)], ["a", "c"])
        self.assertEqual(EvidenceMatrix.pending_writes(Path(self.temp.name) / "missing.json"), [])


class RetirementGateTests(unittest.TestCase):
    """第十六节「成熟判断」：四条件必须同时满足，且两次定时任务不是充分条件。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.matrix = EvidenceMatrix(Path(self.temp.name) / "evidence_matrix.jsonl")

    def _row(self, feature, *, ok=True, provider=ProviderKind.API, mode=ConnectionMode.API_ONLY,
             trigger="schedule", reason=None):
        self.matrix.record(site_id="fixture", feature=feature, provider=provider, ok=ok, mode=mode,
                           trigger=trigger, fallback_reason=reason, elapsed_ms=5)

    def _wallet(self, **kwargs):
        return build_report(self.matrix, **kwargs)["sites"]["fixture"]["features"]["wallet"]

    def test_gate_requires_all_four_conditions(self):
        # 一次失败的手动调用：既没在 api_only 下成功过，也谈不上计划任务覆盖
        self._row(Feature.WALLET, ok=False, trigger="manual")
        wallet = self._wallet()
        self.assertFalse(wallet["ready"])
        self.assertIn("该功能还没有在 api_only 模式下成功过", wallet["blockers"])
        self.assertTrue(any("计划任务覆盖不足（0/2）" in b for b in wallet["blockers"]), wallet["blockers"])

        self._row(Feature.WALLET)                       # 1 次计划任务覆盖
        wallet = self._wallet()
        self.assertFalse(wallet["ready"])
        self.assertTrue(any("计划任务覆盖不足（1/2）" in b for b in wallet["blockers"]), wallet["blockers"])

        self._row(Feature.WALLET)                       # 第 2 次计划任务覆盖
        wallet = self._wallet()
        self.assertTrue(wallet["ready"], wallet["blockers"])
        self.assertEqual(wallet["evidence"]["scheduled_runs"], MIN_SCHEDULED_RUNS)

        self._row(Feature.WALLET, provider=ProviderKind.COMPAT, reason=FallbackReason.NOT_MIGRATED)
        wallet = self._wallet()
        self.assertFalse(wallet["ready"])
        self.assertTrue(any("近期仍发生过兼容回退" in b for b in wallet["blockers"]), wallet["blockers"])

    def test_controlled_only_features_cannot_be_cleared_by_scheduled_runs(self):
        for _ in range(3):
            self._row(Feature.KEYS_DELETE)              # 计划任务根本覆盖不到写操作
        item = readiness(self.matrix)["fixture"]["keys.delete"]
        self.assertFalse(item.ready)
        self.assertTrue(any("缺少受控验证" in b for b in item.blockers), item.blockers)
        self.assertEqual(item.evidence["requires"], "controlled")

        self._row(Feature.KEYS_DELETE, trigger="manual")   # 受控验证：api_only 下真实调用一次
        item = readiness(self.matrix)["fixture"]["keys.delete"]
        self.assertTrue(item.ready, item.blockers)

    def test_site_without_schedule_uses_equivalent_manual_runs(self):
        for _ in range(MIN_SCHEDULED_RUNS):
            self._row(Feature.WALLET, trigger="manual")
        self.assertTrue(readiness(self.matrix, scheduled_available=False)["fixture"]["wallet"].ready)
        self.assertEqual(readiness(self.matrix, scheduled_available=False)["fixture"]["wallet"]
                         .evidence["requires"], "manual_equivalent")
        # 开着定时的站点则仍需真正的计划任务覆盖（不为验收改动用户的定时设置）
        item = readiness(self.matrix, scheduled_available=True)["fixture"]["wallet"]
        self.assertFalse(item.ready)
        self.assertTrue(any("计划任务覆盖不足" in b for b in item.blockers), item.blockers)

    def test_pending_write_blocks_only_its_own_feature(self):
        for _ in range(MIN_SCHEDULED_RUNS):
            self._row(Feature.WALLET)
        self._row(Feature.KEYS_CREATE, trigger="manual")
        pending = [{"site": "fixture", "feature": "keys.create", "status": "unknown"}]
        report = readiness(self.matrix, pending=pending)
        self.assertTrue(report["fixture"]["wallet"].ready)
        self.assertFalse(report["fixture"]["keys.create"].ready)
        self.assertTrue(any("未确认的写操作" in b for b in report["fixture"]["keys.create"].blockers))

    def test_controlled_only_set_matches_the_documented_exclusions(self):
        self.assertEqual(sorted(f.value for f in CONTROLLED_ONLY),
                         ["ccs.usage", "health.detail", "keys.create", "keys.delete", "keys.update"])
        self.assertNotIn(Feature.PRICING_PERSONAL, CONTROLLED_ONLY)

    def test_server_endpoint_returns_the_report_without_remote_requests(self):
        import server
        data = Path(self.temp.name) / "data"
        data.mkdir()
        matrix = EvidenceMatrix(data / "evidence_matrix.jsonl")
        for _ in range(MIN_SCHEDULED_RUNS):
            matrix.record(site_id="fixture", feature=Feature.WALLET, provider=ProviderKind.API, ok=True,
                          mode=ConnectionMode.API_ONLY, trigger="schedule", elapsed_ms=12)
        write_json(data / "write_operations.json", {"schema_version": 1, "operations": []})
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/api/compat/retirement"
        handler.send_json = lambda body, status=200: (body, status)
        with patch.object(server, "DATA_DIR", data), \
             patch.object(server, "ensure_settings", return_value={"schedule": {"enabled": True}}):
            body, status = handler.handle_api_get("/api/compat/retirement")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["sites"]["fixture"]["features"]["wallet"]["ready"])
        self.assertEqual(body["rules"]["min_scheduled_runs"], MIN_SCHEDULED_RUNS)
        self.assertEqual(body["pending_writes"], [])

    def test_operations_and_retirement_share_one_pending_definition(self):
        # 两处若各写一套"未确认"判定就会漂移，这里钉住它们读的是同一份来源
        import server
        data = Path(self.temp.name) / "data"
        data.mkdir()
        write_json(data / "write_operations.json", {"schema_version": 1, "operations": [
            {"id": "op1", "site": "fixture", "feature": "keys.delete", "status": "unknown"},
            {"id": "op2", "site": "fixture", "feature": "keys.create", "status": "confirmed"}]})
        handler = server.Handler.__new__(server.Handler)
        handler.send_json = lambda body, status=200: (body, status)
        with patch.object(server, "DATA_DIR", data), \
             patch.object(server, "ensure_settings", return_value={"schedule": {"enabled": True}}):
            handler.path = "/api/operations"
            operations, _ = handler.handle_api_get("/api/operations")
            handler.path = "/api/compat/retirement"
            report, _ = handler.handle_api_get("/api/compat/retirement")
        self.assertEqual([e["id"] for e in operations["operations"]], ["op1"])
        self.assertEqual([e["id"] for e in report["pending_writes"]], ["op1"])


# ---------------------------------------------------------------------------
# 第十六节「剥离验收」：不注册 compat 后，API 路径不依赖旧编排与旧私有字段
# ---------------------------------------------------------------------------

class StrippingAcceptanceTests(unittest.TestCase):
    LEGACY_BUSINESS_FUNCTIONS = ("auth_client", "fetch_account_snapshot", "get_newapi_token_key",
                                 "create_newapi_token", "delete_newapi_token", "resolve_export_key")
    LEGACY_ADAPTERS = ("NewApiAdapter", "Sub2ApiAdapter")
    SNAPSHOT_FIELDS = ("schema_version", "quota_usd", "used_quota_usd", "recharge_count",
                       "recharge_usd", "recharge_money", "token_count", "tokens", "group_keys",
                       "binding_ref")

    def test_only_compat_bridges_the_legacy_module(self):
        for path in sorted((ROOT / "integrations").glob("*.py")):
            if path.name == "compat.py":
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn("import fetch_prices", text)
                for name in self.LEGACY_ADAPTERS:
                    self.assertNotIn(name, text)

    def test_no_integration_module_references_legacy_business_functions(self):
        for path in sorted((ROOT / "integrations").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for name in self.LEGACY_BUSINESS_FUNCTIONS:
                with self.subTest(module=path.name, name=name):
                    self.assertNotIn(name, text)

    def test_snapshot_fields_stay_backward_compatible(self):
        source = (ROOT / "integrations" / "service.py").read_text(encoding="utf-8")
        for key in self.SNAPSHOT_FIELDS:
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', source)


class CredentialRevealTests(unittest.TestCase):
    """显式查看已保存凭据：普通响应绝不回填明文，只有显式请求单个字段才返回。

    普通配置响应只给 has_secret／掩码，是 D 阶段定下的规矩（避免明文进页面、日志、链接）。
    用户要"能看见"时走这个显式入口：一次一个字段、只在本机、只记动作不记值。
    """

    COOKIE = "session=synthetic-cookie-value"
    PASSWORD = "synthetic-Pass*word"
    TOKEN = "syntheticTokenAbCdEf0123456789==x"

    def setUp(self):
        import server
        self.server = server
        self.site = {"id": "fixture", "name": "Fixture", "type": "newapi",
                     "base_url": "https://fixture.invalid", "enabled": True,
                     "auth": {"cookie": self.COOKIE, "username": "someone", "password": self.PASSWORD,
                              "management_token": self.TOKEN, "account_id": 277}}

    def _handler(self, path, body=None):
        import json as jsonlib
        from io import BytesIO
        handler = self.server.Handler.__new__(self.server.Handler)
        handler.path = path
        raw = jsonlib.dumps(body or {}).encode("utf-8")
        handler.rfile = BytesIO(raw)
        handler.wfile = BytesIO()
        # 写请求现在统一走 _guard：Host / Origin / JSON 头必须齐全，不能用空头绕过。
        handler.headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Content-Type": "application/json",
            "Content-Length": str(len(raw)),
        }
        # _reject 走 send_response → log_request，合成实例没有底层 socket，需要补空实现。
        handler.requestline = path
        handler.request_version = "HTTP/1.1"
        handler.log_request = lambda *args, **kwargs: None
        handler.send_response = lambda status, *args, **kwargs: None
        handler.send_header = lambda *args, **kwargs: None
        handler.end_headers = lambda: None
        handler.send_json = lambda data, status=200, **kwargs: (data, status)
        return handler

    def test_reveal_returns_exact_value_for_one_field(self):
        with patch.object(self.server, "find_site", return_value=self.site), \
             patch.object(self.server, "log_line") as log:
            for field, expected in (("management_token", self.TOKEN), ("cookie", self.COOKIE),
                                    ("password", self.PASSWORD)):
                handler = self._handler("/api/site/secret", {"site": "fixture", "field": field})
                body, status = handler.do_POST()
                self.assertEqual((status, body["value"]), (200, expected), field)
                self.assertEqual((body["site"], body["field"]), ("fixture", field))
            # 日志只记"谁看了哪个字段"，绝不记值
            self.assertNotIn(self.TOKEN, str(log.call_args_list))
            self.assertNotIn(self.PASSWORD, str(log.call_args_list))

    def test_reveal_rejects_unknown_field_site_and_empty_value(self):
        with patch.object(self.server, "find_site", return_value=self.site), \
             patch.object(self.server, "log_line"):
            for field in ("username", "nope", "", None):
                handler = self._handler("/api/site/secret", {"site": "fixture", "field": field})
                _body, status = handler.do_POST()
                self.assertEqual(status, 400, repr(field))
            # 站点里没存过值的字段不假装能看
            handler = self._handler("/api/site/secret", {"site": "fixture", "field": "refresh_token"})
            _body, status = handler.do_POST()
            self.assertEqual(status, 404)
        with patch.object(self.server, "find_site", return_value=None), \
             patch.object(self.server, "log_line"):
            handler = self._handler("/api/site/secret", {"site": "missing", "field": "cookie"})
            _body, status = handler.do_POST()
            self.assertEqual(status, 404)

    def test_normal_state_response_never_carries_secrets(self):
        handler = self._handler("/api/state")
        with patch.object(self.server, "load_sites", return_value=[self.site]), \
             patch.object(self.server, "ensure_settings", return_value={}), \
             patch.object(self.server, "load_accounts", return_value={"accounts": []}), \
             patch.object(self.server, "load_health", return_value={"sites": {}}), \
             patch.object(self.server, "ccswitch_info", return_value={}), \
             patch.object(self.server, "CACHE", self.server.DataCache()):
            body, status = handler.handle_api_get("/api/state")
        self.assertEqual(status, 200)
        text = json.dumps(body, ensure_ascii=False)
        for secret in (self.COOKIE, self.PASSWORD, self.TOKEN):
            self.assertNotIn(secret, text)
        site = body["sites"][0]
        self.assertTrue(site["has_secret"]["management_token"])
        self.assertTrue(site["has_secret"]["cookie"])
        self.assertFalse((site.get("auth") or {}).get("management_token"))
        self.assertFalse((site.get("auth") or {}).get("cookie"))

    def test_saving_sites_never_echoes_secrets_and_keeps_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "sites.json"
            write_json(config_path, {"sites": [self.site]})
            public = public_site(self.site)
            public["secret_actions"] = {key: "keep" for key in
                                        ("cookie", "password", "management_token")}
            handler = self._handler("/api/sites", {"sites": [public]})
            with patch.object(self.server.collector, "DEFAULT_CONFIG", config_path), \
                 patch.object(self.server.collector, "_INTEGRATION_RUNTIMES", {}), \
                 patch.object(self.server, "TOKEN_SESSIONS", {}):
                body, status = handler.do_POST()
            self.assertEqual(status, 200, body)
            text = json.dumps(body, ensure_ascii=False)
            for secret in (self.COOKIE, self.PASSWORD, self.TOKEN):
                self.assertNotIn(secret, text)          # 保存的回执也不能带明文
            saved = ConfigurationStore(config_path).sites()[0]
            self.assertEqual(saved["auth"]["management_token"], self.TOKEN)   # keep 生效
            self.assertEqual(saved["auth"]["cookie"], self.COOKIE)

    def test_reveal_is_origin_gated(self):
        captured = {}

        def fake_send_response(status, *args, **kwargs):
            captured["status"] = status

        def probe(origin):
            handler = self._handler("/api/site/secret",
                                    {"site": "fixture", "field": "management_token"})
            handler.headers["Origin"] = origin
            handler.send_response = fake_send_response
            handler.send_header = lambda *args, **kwargs: None
            handler.end_headers = lambda: None
            with patch.object(self.server, "find_site", return_value=self.site), \
                 patch.object(self.server, "log_line"):
                result = handler.do_POST()
            return result, captured.get("status"), handler.wfile

        # 本机页面必须带精确 Origin；file:// 的 null 自 v1.4 起不再授权。
        result, _status, _sink = probe("http://127.0.0.1:8765")
        body, status = result
        self.assertEqual((status, body["value"]), (200, self.TOKEN))
        for origin in ("https://evil.example", "null", None):
            result, status, sink = probe(origin)
            self.assertEqual(status, 403, repr(origin))
            self.assertIsNone(result)         # 拒绝分支不经过 send_json
            self.assertNotIn(self.TOKEN.encode("utf-8"), sink.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
