"""静态快照路径：只放行直属白名单文件，拒绝穿越与编码变体。"""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch


def handler(server, path):
    instance = server.Handler.__new__(server.Handler)
    instance.path = path
    instance.headers = {"Host": "127.0.0.1:8765"}
    instance.requestline = path
    instance.request_version = "HTTP/1.1"
    instance.log_request = lambda *args, **kwargs: None
    instance.send_response = Mock()
    instance.send_header = Mock()
    instance.end_headers = Mock()
    instance.wfile = Mock()
    instance.send_file = Mock()
    instance.send_json = Mock()
    instance.write_response = Mock()
    return instance


class StaticPathTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir()
        (self.data / "prices.js").write_text("window.__X__=1;", encoding="utf-8")
        (Path(self.temp.name) / "sites.json").write_text('{"sites": []}', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def get(self, path):
        instance = handler(self.server, path)
        with patch.object(self.server, "DATA_DIR", self.data):
            instance.do_GET()
        return instance

    def test_serves_allowlisted_snapshot(self):
        instance = self.get("/data/prices.js")
        instance.send_file.assert_called_once()
        self.assertEqual(instance.send_file.call_args.args[0].name, "prices.js")

    def test_rejects_traversal_and_unknown_files(self):
        for path in ("/data/../sites.json", "/data/..%2f..%2fsites.json",
                     "/data/%2e%2e/sites.json", "/data/sites.json",
                     "/data/prices.js/../../sites.json", "/data/", "/data/health.json.bak"):
            with self.subTest(path=path):
                instance = self.get(path)
                instance.send_file.assert_not_called()
                status = instance.send_json.call_args.args[1]
                self.assertIn(status, (403, 404))

    def test_missing_allowed_file_is_404(self):
        instance = self.get("/data/index.json")
        instance.send_file.assert_not_called()
        self.assertEqual(instance.send_json.call_args.args[1], 404)


if __name__ == "__main__":
    unittest.main()
