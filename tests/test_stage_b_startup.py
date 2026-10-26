"""独立进程启动旧服务，合成配置及回环 HTTP，不触发远端请求。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class StartupTests(unittest.TestCase):
    def test_service_starts_and_reads_offline_state_without_compat_or_requests(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, PROJECT_ROOT)
import fetch_prices as fp
folder = Path.cwd()
fp.SETTINGS_FILE = folder / "settings.json"
fp.DEFAULT_CONFIG = folder / "sites.json"  # 文件不创建，load_sites 返回空列表。
fp.write_json_atomic(fp.SETTINGS_FILE, {"schedule":{"enabled":False,"time":"04:30","refresh_accounts":False}})
import integrations.contracts
import server
server.DATA_DIR = folder / "data"
server.ACCOUNTS_FILE = folder / "accounts.json"
server.HEALTH_FILE = folder / "health.json"
server.LOG_DIR = folder / "logs"
server.CACHE = server.DataCache()
server.Handler.log_message = lambda *args: None

with patch.object(fp.HttpClient, "request", side_effect=AssertionError("unexpected remote request")) as remote, \
     patch.object(server, "ccswitch_info", return_value={"installed":False}):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        client.request("GET", "/api/state")
        response = client.getresponse()
        result = json.loads(response.read())
        assert response.status == 200 and result["ok"]
        assert result["sites"] == []
        assert result["accounts"]["accounts"] == []
        assert result["schedule"]["time"] == "04:30"
        assert result["schedule"]["enabled"] is False
        assert result["task"]["running"] is False
        assert "integrations.compat" not in sys.modules
        client.close()
        remote.assert_not_called()
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=3)
print(json.dumps({"offline_state":True,"remote_requests":remote.call_count}))
'''.replace("PROJECT_ROOT", repr(str(root)))
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, "-c", script], cwd=temp,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"offline_state": True, "remote_requests": 0})

    def test_site_proxy_saves_over_http_and_invalid_value_is_rejected(self):
        """走真实 HTTP 存一次本站代理：合法的要规整后落盘，非法的要整条拒绝、不污染已有配置。"""
        root = Path(__file__).resolve().parents[1]
        script = r'''
import http.client
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, PROJECT_ROOT)
import fetch_prices as fp
folder = Path.cwd()
fp.SETTINGS_FILE = folder / "settings.json"
fp.DEFAULT_CONFIG = folder / "sites.json"
fp.write_json_atomic(fp.SETTINGS_FILE, {"schedule":{"enabled":False,"time":"04:30","refresh_accounts":False},
                                        "proxy":"global.invalid:3128"})
fp.write_json_atomic(fp.DEFAULT_CONFIG, {"sites":[{"id":"alpha","name":"Alpha","type":"newapi",
    "base_url":"https://alpha.invalid","enabled":True,"auth":{}}]})
import server
server.DATA_DIR = folder / "data"
server.ACCOUNTS_FILE = folder / "accounts.json"
server.HEALTH_FILE = folder / "health.json"
server.LOG_DIR = folder / "logs"
server.CACHE = server.DataCache()
server.Handler.log_message = lambda *args: None

def call(client, method, path, payload=None):
    body = json.dumps(payload) if payload is not None else None
    # 写请求现在要求精确 Origin；端口是运行时分配的，必须带真实端口。
    client.request(method, path, body=body,
                   headers={"Content-Type":"application/json", "Origin": ORIGIN})
    response = client.getresponse()
    return response.status, json.loads(response.read())

httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
ORIGIN = f"http://127.0.0.1:{httpd.server_port}"
worker = threading.Thread(target=httpd.serve_forever, daemon=True)
worker.start()
try:
    client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    status, state = call(client, "GET", "/api/state")
    assert status == 200 and state["sites"][0].get("proxy", "") == "", state
    site = state["sites"][0]

    site["proxy"] = "127.0.0.1:7890"          # 漏写协议头，服务端应补成 http://
    status, saved = call(client, "POST", "/api/sites", {"sites":[site]})
    assert status == 200 and saved["ok"] and not saved["errors"], saved
    on_disk = json.loads((folder / "sites.json").read_text(encoding="utf-8"))
    assert on_disk["sites"][0]["proxy"] == "http://127.0.0.1:7890", on_disk

    # 全局代理同样会规整协议头，并作为兜底继续保留
    status, general = call(client, "POST", "/api/settings",
                           {"settings":{"schedule":{"enabled":False,"time":"04:30","refresh_accounts":False},
                                        "proxy":"global.invalid:3128"}})
    assert status == 200 and general["ok"], general
    assert general["settings"]["proxy"] == "http://global.invalid:3128", general
    assert json.loads((folder / "settings.json").read_text(encoding="utf-8"))["proxy"] == "http://global.invalid:3128"

    bad = dict(on_disk["sites"][0], proxy="socks5://127.0.0.1:1080")
    status, rejected = call(client, "POST", "/api/sites", {"sites":[bad]})
    assert status == 400 and not rejected["ok"] and rejected["errors"], rejected
    assert "代理" in rejected["error"], rejected
    still = json.loads((folder / "sites.json").read_text(encoding="utf-8"))
    assert still["sites"][0]["proxy"] == "http://127.0.0.1:7890", still

    # 清空 = 跟随全局：字段连同一起消失，不会给站点留一个空壳键
    cleared = dict(still["sites"][0], proxy="")
    status, done = call(client, "POST", "/api/sites", {"sites":[cleared]})
    assert status == 200 and done["ok"] and not done["errors"], done
    final = json.loads((folder / "sites.json").read_text(encoding="utf-8"))
    assert "proxy" not in final["sites"][0], final
    client.close()
finally:
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=3)
print(json.dumps({"proxy_saved":True}))
'''.replace("PROJECT_ROOT", repr(str(root)))
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, "-c", script], cwd=temp,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"proxy_saved": True})


class InitAuthStdinTests(unittest.TestCase):
    """无 TTY 环境（容器 run -T / CI / 面板）只能用 --auth-stdin 初始化。"""

    def _run(self, state_dir, stdin_text):
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "RELAY_MODE": "server", "RELAY_STATE_DIR": str(state_dir),
               "RELAY_ALLOWED_ORIGINS": "http://127.0.0.1:8795",
               "RELAY_ALLOWED_HOSTS": "127.0.0.1:8795",
               "RELAY_STRICT_PORT": "1", "PYTHONUTF8": "1"}
        return subprocess.run([sys.executable, "-X", "utf8", "server.py",
                               "--init-auth", "--auth-stdin"],
                              cwd=root, env=env, input=stdin_text,
                              capture_output=True, text=True, timeout=90)

    def test_writes_enabled_auth_from_two_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self._run(temp, "admin\npw-123456\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            raw = json.loads((Path(temp) / "auth.json").read_text(encoding="utf-8"))
            self.assertTrue(raw["enabled"])
            self.assertEqual(raw["username"], "admin")
            self.assertTrue(raw["password_hash"].startswith("scrypt$v1$"))

    def test_rejects_short_password_without_writing(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self._run(temp, "admin\nshort\n")
            self.assertEqual(result.returncode, 1)
            self.assertFalse((Path(temp) / "auth.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
