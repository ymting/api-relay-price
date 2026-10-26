import json
import unittest

from _diagnose_account_auth import probe_site, redact_message
from integrations.providers import HttpResponse
from tests.test_stage_c import FakeTransport


class AccountDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.site = {"id": "fixture", "base_url": "https://fixture.invalid", "auth": {
            "management_token": "synthetic-management-secret", "account_id": "7",
            "password": "private-password", "cookie": "session=private-session"}}

    def test_probe_compares_headers_and_stops_at_verified_success(self):
        transport = FakeTransport([HttpResponse(403, {"message": "账户请求头缺失"}),
                                   HttpResponse(200, {"success": True, "data": {"id": 7, "email": "private"}})])
        result = probe_site(self.site, [self.site], transport=transport)
        self.assertEqual(result["working_scenario"], "bearer_and_account_id")
        self.assertEqual(len(transport.calls), 2)
        self.assertNotIn("New-Api-User", transport.calls[0]["headers"])
        self.assertEqual(transport.calls[1]["headers"]["New-Api-User"], "7")
        self.assertTrue(all(call["method"] == "GET" and "Cookie" not in call["headers"] for call in transport.calls))
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn(self.site["auth"]["management_token"], json.dumps(result))

    def test_messages_are_scrubbed_before_truncation(self):
        message = " ".join([self.site["auth"]["management_token"], "private-password", "private-session",
                            "private%2Dpassword", "a" * 40])
        result = redact_message({"message": message}, [self.site])
        for secret in (self.site["auth"]["management_token"], "private-password", "private-session",
                       "private%2Dpassword", "a" * 40):
            self.assertNotIn(secret, result)

    def test_rate_limit_or_account_mismatch_stops_probe(self):
        for response in (HttpResponse(429, {"message": "slow down"}),
                         HttpResponse(200, {"success": True, "data": {"id": 8}})):
            with self.subTest(status=response.status):
                transport = FakeTransport([response])
                result = probe_site(self.site, [self.site], transport=transport)
                self.assertNotIn("working_scenario", result)
                self.assertEqual(len(transport.calls), 1)

    def test_missing_id_never_guesses_an_account(self):
        self.site["auth"].pop("account_id")
        transport = FakeTransport([])
        result = probe_site(self.site, [self.site], transport=transport)
        self.assertEqual(result["result"], "missing_token_or_account_id")
        self.assertFalse(transport.calls)


if __name__ == "__main__":
    unittest.main()
