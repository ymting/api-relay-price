"""认证 HTTP 行为：匿名白名单、401、登录与 CSRF、会话生命周期。"""
import json
from io import BytesIO
import unittest
from unittest.mock import Mock, patch

from integrations.auth import AuthState, ChallengeStore, LoginLimiter, SessionStore, hash_password


def handler(server, path, body=None, *, host="127.0.0.1:8765",
            origin="http://127.0.0.1:8765", cookie="", csrf="", method="GET"):
    instance = server.Handler.__new__(server.Handler)
    instance.path = path
    raw = json.dumps(body or {}).encode("utf-8")
    instance.rfile = BytesIO(raw)
    instance.wfile = BytesIO()
    headers = {"Host": host, "Origin": origin,
               "Content-Type": "application/json", "Content-Length": str(len(raw))}
    if cookie:
        headers["Cookie"] = cookie
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
    instance.write_response = Mock(side_effect=lambda body, ctype, status=200, **kwargs:
                                   instance.send_response(status))
    instance.send_json = Mock(side_effect=lambda obj, status=200, extra_headers=None:
                              instance.send_response(status))
    instance._method = method
    return instance


class AuthHttpTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.state = AuthState(True, "admin", hash_password("correct-horse"), 1)
        self.sessions = SessionStore()
        self.challenges = ChallengeStore()
        self.patches = [
            patch.object(server, "AUTH", Mock(load=lambda: self.state,
                                              verify=lambda u, p: u == "admin" and p == "correct-horse")),
            patch.object(server, "SESSIONS", self.sessions),
            patch.object(server, "CHALLENGES", self.challenges),
            patch.object(server, "LOGIN_LIMITER", LoginLimiter()),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def status(self, instance):
        return instance.send_response.call_args.args[0]

    def test_anonymous_resources_stay_available(self):
        for path in ("/login", "/login.js", "/healthz", "/api/ping"):
            with self.subTest(path=path):
                instance = handler(self.server, path)
                if path == "/healthz":
                    with patch.object(self.server, "probe_state_dir", return_value=True):
                        instance.do_GET()
                    self.assertEqual(self.status(instance), 200)
                elif path.startswith("/api"):
                    instance.do_GET()
                    self.assertEqual(self.status(instance), 200)
                else:
                    instance.do_GET()
                    self.assertEqual(self.status(instance), 200)

    def test_business_api_without_session_returns_401(self):
        instance = handler(self.server, "/api/state")
        instance.do_GET()
        self.assertEqual(self.status(instance), 401)

    def test_root_redirects_to_login(self):
        instance = handler(self.server, "/")
        instance.do_GET()
        self.assertEqual(self.status(instance), 302)

    def test_login_issues_cookie_and_csrf(self):
        token, csrf = self.challenges.create()
        instance = handler(self.server, "/api/auth/login",
                           {"challenge": token, "csrf": csrf,
                            "username": "admin", "password": "correct-horse"}, method="POST")
        instance.do_POST()
        self.assertEqual(self.status(instance), 200)
        extra = instance.send_json.call_args.kwargs.get("extra_headers") or {}
        self.assertIn("relay_session=", extra.get("Set-Cookie", ""))
        self.assertIn("HttpOnly", extra["Set-Cookie"])
        self.assertIn("SameSite=Strict", extra["Set-Cookie"])

    def test_wrong_password_is_generic_401(self):
        token, csrf = self.challenges.create()
        instance = handler(self.server, "/api/auth/login",
                           {"challenge": token, "csrf": csrf,
                            "username": "admin", "password": "wrong"}, method="POST")
        instance.do_POST()
        self.assertEqual(self.status(instance), 401)
        self.assertEqual(instance.send_json.call_args.args[0]["error"], "用户名或密码错误")

    def test_challenge_is_single_use(self):
        token, csrf = self.challenges.create()
        instance = handler(self.server, "/api/auth/login",
                           {"challenge": token, "csrf": csrf,
                            "username": "admin", "password": "correct-horse"}, method="POST")
        instance.do_POST()
        second = handler(self.server, "/api/auth/login",
                         {"challenge": token, "csrf": csrf,
                          "username": "admin", "password": "correct-horse"}, method="POST")
        second.do_POST()
        self.assertEqual(self.status(second), 403)

    def test_session_requires_csrf_header_for_writes(self):
        token, csrf = self.sessions.create()
        instance = handler(self.server, "/api/settings", {"settings": {}},
                           cookie="relay_session=" + token, method="POST")
        instance.do_POST()
        self.assertEqual(self.status(instance), 403)

    def test_session_with_valid_csrf_reaches_business(self):
        token, csrf = self.sessions.create()
        instance = handler(self.server, "/api/settings", {"settings": {}},
                           cookie="relay_session=" + token, csrf=csrf, method="POST")
        with patch.object(self.server, "sanitize_settings", return_value={}) as sanitize, \
             patch.object(self.server, "integration_write_json"), \
             patch.object(self.server, "ensure_settings", return_value={}), \
             patch.object(self.server.SCHEDULER, "apply_settings"):
            instance.do_POST()
        sanitize.assert_called_once()

    def test_logout_destroys_session(self):
        token, csrf = self.sessions.create()
        instance = handler(self.server, "/api/auth/logout", {},
                           cookie="relay_session=" + token, csrf=csrf, method="POST")
        instance.do_POST()
        self.assertIsNone(self.sessions.get(token))

    def test_public_state_never_exposes_hash(self):
        token, csrf = self.sessions.create()
        instance = handler(self.server, "/api/state", cookie="relay_session=" + token)
        with patch.object(self.server, "ensure_settings", return_value={}), \
             patch.object(self.server, "load_sites", return_value=[]), \
             patch.object(self.server, "load_accounts", return_value={"accounts": []}), \
             patch.object(self.server, "load_health", return_value={"sites": {}}), \
             patch.object(self.server, "ccswitch_info", return_value={}), \
             patch.object(self.server, "cindy_info", return_value={}), \
             patch.object(self.server, "CACHE", self.server.DataCache()):
            instance.handle_api_get("/api/state")
        payload = instance.send_json.call_args.args[0]
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("password_hash", text)
        self.assertNotIn(self.state.password_hash, text)


if __name__ == "__main__":
    unittest.main()
