"""模型卡片与分组明细的口径回归；只使用合成响应。"""
import copy
import json
import time
import unittest
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from integrations.contracts import Feature, ErrorCategory
from integrations.providers import HttpResponse
from integrations.router import Router
from integrations.service import health_snapshot, model_health_summary
from tests.test_stage_c import provider, newapi


MODEL = "[Special]/原始 Model+name"


class ModelHealthTests(unittest.TestCase):
    def summary(self, **updates):
        value = {"summary": {"success_rate": 5}, "window_start": 1000, "window_end": 90000,
                 "models": [{"model_name": "other", "success_rate": 2},
                            {"model_name": MODEL, "success_rate": 100, "avg_latency_ms": 56190, "avg_tps": 17.4}],
                 "recent_success_series": {MODEL: [{"ts": 7200, "success_rate": 100}]}}
        value.update(updates)
        return value

    def test_card_uses_exact_model_row_and_preserves_samples_not_site_or_group_average(self):
        instance, transport = provider("newapi", [newapi(self.summary()), newapi({
            "model_name": MODEL, "summary": {"success_rate": 95}, "series": [],
            "groups": [{"group": "vip", "success_rate": 0, "series": [{"ts": 7200, "success_rate": 0}]}]})])
        record = health_snapshot(Router(instance), MODEL)
        self.assertEqual(record["health_schema_version"], 2)
        self.assertEqual(record["model_summary"]["success_rate"], 100)
        self.assertEqual(record["model_summary"]["avg_latency_ms"], 56190)
        self.assertEqual(record["model_summary"]["recent_success_series"][0]["success_rate"], 100)
        self.assertEqual(record["summary"]["success_rate"], 95)
        self.assertEqual(record["groups"][0]["success_rate"], 0)
        first, second = transport.calls
        self.assertEqual(urlsplit(first["url"]).path, "/api/perf-metrics/summary")
        self.assertEqual(parse_qs(urlsplit(first["url"]).query), {"hours": ["24"], "recent_model": [MODEL]})
        self.assertEqual(parse_qs(urlsplit(second["url"]).query), {"hours": ["24"], "model": [MODEL]})

    def test_recent_model_parameters_are_repeated_not_json_or_bracket_serialized(self):
        instance, transport = provider("newapi", [newapi({"models": []})])
        self.assertTrue(instance.fetch(Feature.HEALTH_SUMMARY, recent_models=[MODEL, "b", MODEL]).ok)
        query = parse_qs(urlsplit(transport.calls[0]["url"]).query)
        self.assertEqual(query["recent_model"], [MODEL, "b"])
        self.assertNotIn("recent_model[]", query)

    def test_invalid_recent_models_do_not_send_requests(self):
        for names in ("model", [""], [None], ["m"] * 51, ["m" * 513]):
            with self.subTest(names=type(names).__name__):
                instance, transport = provider("newapi", [])
                result = instance.fetch(Feature.HEALTH_SUMMARY, recent_models=names)
                self.assertEqual(result.error_category, ErrorCategory.VALIDATION_ERROR)
                self.assertFalse(transport.calls)

    def test_history_alone_does_not_fabricate_24h_rate_latency_or_tps(self):
        metric = model_health_summary(self.summary(models=[]), MODEL, "2026-10-16T00:00:00Z")
        self.assertIsNone(metric["success_rate"])
        self.assertIsNone(metric["avg_latency_ms"])
        self.assertIsNone(metric["avg_tps"])
        self.assertEqual(len(metric["recent_success_series"]), 1)
        self.assertIsNone(model_health_summary({"models": []}, MODEL, "now"))

    def test_failed_summary_preserves_previous_card_with_original_time_and_error(self):
        old_metric = model_health_summary(self.summary(), MODEL, "2026-10-15T00:00:00Z")
        previous = {"model_summary": old_metric}
        instance, _ = provider("newapi", [HttpResponse(403, {"message": "forbidden"}),
                                           newapi({"model_name": MODEL, "groups": []})])
        record = health_snapshot(Router(instance), MODEL, previous=previous)
        self.assertEqual(record["model_summary"], old_metric)
        self.assertTrue(record["model_summary_stale"])
        self.assertIn("HTTP 403", record["model_summary_error"])
        self.assertEqual(record["summary_feature"]["status"], "error")

    def test_successful_empty_summary_clears_old_card_instead_of_showing_stale_success(self):
        previous = {"model_summary": model_health_summary(self.summary(), MODEL, "2026-10-15T00:00:00Z")}
        before = copy.deepcopy(previous)
        instance, _ = provider("newapi", [newapi({"models": []}), newapi({"model_name": MODEL, "groups": []})])
        record = health_snapshot(Router(instance), MODEL, previous=previous)
        self.assertIsNone(record["model_summary"])
        self.assertFalse(record["model_summary_stale"])
        self.assertIsNone(record["model_summary_error"])
        self.assertEqual(previous, before)

    def test_failed_details_keep_groups_separate_without_marking_old_data_as_fresh(self):
        previous = {"fetched_at": "2026-10-15T00:00:00Z", "groups": [{"group": "vip", "success_rate": 0}]}
        instance, _ = provider("newapi", [newapi(self.summary()), HttpResponse(403, {"message": "forbidden"})])
        record = health_snapshot(Router(instance), MODEL, previous=previous)
        self.assertEqual(record["model_summary"]["success_rate"], 100)
        self.assertEqual(record["groups"], previous["groups"])
        self.assertTrue(record["detail_stale"])
        self.assertEqual(record["detail_fetched_at"], previous["fetched_at"])
        self.assertIn("HTTP 403", record["detail_error"])

    def test_server_does_not_reuse_detail_only_cache_after_schema_change(self):
        import server
        from integrations.service import binding_of, HEALTH_SCHEMA_VERSION
        site = {"id": "fixture", "enabled": True, "base_url": "https://fixture.invalid"}
        old = {"binding_ref": binding_of(site), "cached_at": time.time(), "groups": []}
        new = {"health_schema_version": HEALTH_SCHEMA_VERSION, "groups": [], "model_summary": None}
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/api/health/refresh"
        raw = json.dumps({"site": "fixture", "model": "m"}).encode("utf-8")
        handler.rfile = BytesIO(raw)
        # 写请求必须带齐 Host / Origin / JSON 头，避免绕过 _guard。
        handler.headers = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765",
                           "Content-Type": "application/json", "Content-Length": str(len(raw))}
        handler.send_json = lambda body, status=200: (body, status)
        with patch.object(server, "find_site", return_value=site), \
                patch.object(server, "ensure_settings", return_value={}), \
                patch.object(server, "load_health", return_value={"sites": {"fixture": {"m": old}}}), \
                patch.object(server.collector, "integration_runtime", return_value=Mock()), \
                patch.object(server.collector, "write_json_atomic"), \
                patch.object(server.integration_service, "health_snapshot", return_value=new) as fetch:
            body, status = handler.do_POST()
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertNotIn("cached", body)
        self.assertEqual(fetch.call_args.kwargs["previous"], old)


if __name__ == "__main__":
    unittest.main()
