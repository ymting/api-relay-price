"""真实 urllib + 本机合成 HTTP 服务；仅合成令牌，不访问外部站点。"""
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from integrations.contracts import Feature
from integrations.providers import Credentials, CredentialSession, Sub2ApiProvider, UrllibTransport


@contextmanager
def local_server(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(httpd.server_port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=3)


class QuietHandler(BaseHTTPRequestHandler):
    # 本机（火绒/本机过滤）对回环短连接很敏感：服务端抢先关闭会变成 WinError 10054
    # （实测偶发，约 1/5 概率）。显式声明 HTTP/1.1 并让客户端决定何时断开，可消除这个竞态。
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.close_connection = False
        super().end_headers()

    def reply(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class HttpTests(unittest.TestCase):
    def test_actual_redirect_does_not_forward_authorization_or_replay_post(self):
        reached = []
        class Sink(QuietHandler):
            def do_GET(self):
                reached.append(dict(self.headers))
                self.reply(200, {})
            do_POST = do_GET
        with local_server(Sink) as target:
            class Origin(QuietHandler):
                def do_GET(self):
                    if self.command == "POST":
                        self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    self.send_response(307)
                    self.send_header("Location", target + "/sink")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                do_POST = do_GET
            with local_server(Origin) as origin:
                transport = UrllibTransport()
                for method in ("GET", "POST"):
                    response = transport.request(method, origin + "/redirect", headers={"Authorization": "Bearer synthetic"},
                                                 body={} if method == "POST" else None, timeout=2)
                    self.assertEqual(response.status, 307)
        self.assertEqual(reached, [])

    def test_actual_401_refresh_retry_and_atomic_pair_save(self):
        calls = []
        class Api(QuietHandler):
            def do_GET(self):
                calls.append((self.command, self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") == "Bearer old":
                    self.reply(401, {"code": 401})
                else:
                    self.reply(200, {"code": 0, "data": {"id": 7, "balance": 12}})
            def do_POST(self):
                calls.append((self.command, self.path, self.headers.get("Authorization")))
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != "/api/v1/auth/refresh" or body != {"refresh_token": "refresh-old"}:
                    self.reply(400, {"code": 400})
                else:
                    self.reply(200, {"code": 0, "data": {"access_token": "new", "refresh_token": "refresh-new", "expires_in": 3600}})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pair.json"
            def save(expected, replacement):
                raw = {"access_token": replacement.credentials.access_token, "refresh_token": replacement.credentials.refresh_token,
                       "expires_at": replacement.credentials.expires_at}
                staging = path.with_suffix(".tmp")
                staging.write_text(json.dumps(raw), encoding="utf-8")
                staging.replace(path)
                return True
            session = CredentialSession(Credentials(access_token="old", refresh_token="refresh-old", account_id=7), save_rotation=save)
            with local_server(Api) as url:
                instance = Sub2ApiProvider({"id": "local", "base_url": url}, session)
                result = instance.fetch(Feature.PROFILE)
            self.assertTrue(result.ok)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["refresh_token"], "refresh-new")
        self.assertEqual([call[:2] for call in calls], [("GET", "/api/v1/user/profile"),
            ("POST", "/api/v1/auth/refresh"), ("GET", "/api/v1/user/profile")])
        self.assertIsNone(calls[1][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
