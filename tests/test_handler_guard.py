"""HTTP 守卫时序：Host / 认证 / 来源 / CSRF / JSON 全部在业务动作之前。"""
import json
from io import BytesIO
import unittest
from unittest.mock import Mock, patch

from integrations.auth import AuthState, SessionStore


def handler(server, path, body=None, *, host="127.0.0.1:8765",
            origin="http://127.0.0.1:8765", csrf=None, content_type="application/json",
            raw=None):
    instance = server.Handler.__new__(server.Handler)
    instance.path = path
    payload = raw if raw is not None else json.dumps(body or {}).encode("utf-8")
    instance.rfile = BytesIO(payload)
    instance.wfile = BytesIO()
    headers = {"Host": host, "Origin": origin, "Content-Type": content_type,
               "Content-Length": str(len(payload))}
    if csrf:
        headers["X-Relay-CSRF"] = csrf
    instance.headers = headers
    instance.requestline = path
    instance.request_version = "HTTP/1.1"
    instance.log_request = lambda *args, **kwargs: None
    instance.send_response = Mock()
    instance.send_header = Mock()
    instance.end_headers = Mock()
    instance.close_connection = False
    return instance


class GuardOrderTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.sessions = SessionStore()
        self.patches = [
            patch.object(server, "SESSIONS", self.sessions),
            patch.object(server, "AUTH", Mock(load=lambda: AuthState(False, "", "", 1))),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_bad_host_rejected_before_any_business_action(self):
        instance = handler(self.server, "/api/task/start", {"scope": "all"},
                           host="evil.example:8765")
        with patch.object(self.server.TASK, "start") as start:
            instance.do_POST()
        start.assert_not_called()
        instance.send_response.assert_called_with(403)

    def test_bad_origin_rejected_before_task_start(self):
        instance = handler(self.server, "/api/task/start", {"scope": "all"},
                           origin="https://evil.example")
        with patch.object(self.server.TASK, "start") as start:
            instance.do_POST()
        start.assert_not_called()
        instance.send_response.assert_called_with(403)

    def test_missing_origin_rejected_in_local_mode(self):
        instance = handler(self.server, "/api/task/start", {"scope": "all"})
        instance.headers.pop("Origin")
        with patch.object(self.server.TASK, "start") as start:
            instance.do_POST()
        start.assert_not_called()
        instance.send_response.assert_called_with(403)

    def test_wrong_content_type_rejected_before_task_start(self):
        instance = handler(self.server, "/api/task/start", {"scope": "all"},
                           content_type="text/plain")
        with patch.object(self.server.TASK, "start") as start:
            instance.do_POST()
        start.assert_not_called()
        instance.send_response.assert_called_with(415)

    def test_invalid_json_does_not_fall_back_to_default_scope(self):
        instance = handler(self.server, "/api/task/start", raw=b"{")
        with patch.object(self.server.TASK, "start") as start:
            instance.do_POST()
        start.assert_not_called()
        instance.send_response.assert_called_with(400)

    def test_authentication_runs_before_origin_and_business(self):
        self.server.AUTH.load = lambda: AuthState(True, "admin", "hash", 1)
        instance = handler(self.server, "/api/task/start", {"scope": "all"},
                           origin="https://evil.example")
        with patch.object(self.server.TASK, "start") as start:
            instance.do_POST()
        start.assert_not_called()
        instance.send_response.assert_called_with(401)

    def test_valid_session_requires_matching_csrf(self):
        self.server.AUTH.load = lambda: AuthState(True, "admin", "hash", 1)
        token, csrf = self.sessions.create()
        instance = handler(self.server, "/api/settings", {"settings": {}})
        instance.headers["Cookie"] = "relay_session=" + token
        with patch.object(self.server, "sanitize_settings", return_value={}) as sanitize:
            instance.do_POST()
        sanitize.assert_not_called()
        instance.send_response.assert_called_with(403)

    def test_root_redirects_to_login_when_authenticated(self):
        self.server.AUTH.load = lambda: AuthState(True, "admin", "hash", 1)
        instance = handler(self.server, "/")
        instance.do_GET()
        instance.send_response.assert_called_with(302)

    def test_reject_drains_body_and_keeps_connection_when_safe(self):
        """拒绝前先读掉 body：否则 Windows 会发 RST，客户端连 403 都收不到。"""
        instance = handler(self.server, "/api/settings", {"settings": {}},
                           origin="https://evil.example")
        instance.do_POST()
        self.assertEqual(instance.rfile.read(1), b"")   # body 已被读完
        self.assertFalse(instance.close_connection)
        headers = [call.args[0] for call in instance.send_header.call_args_list]
        self.assertNotIn("Connection", headers)

    def test_reject_closes_connection_when_body_cannot_be_drained(self):
        instance = handler(self.server, "/api/settings", raw=b"{}")
        instance.headers["Transfer-Encoding"] = "chunked"
        instance.do_POST()
        self.assertTrue(instance.close_connection)
        headers = [call.args[0] for call in instance.send_header.call_args_list]
        self.assertIn("Connection", headers)


if __name__ == "__main__":
    unittest.main()
