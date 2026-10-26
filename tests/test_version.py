"""版本号读取：只认 VERSION，异常时给可识别的占位值。"""
import tempfile
import unittest
from pathlib import Path

from integrations.version import FALLBACK_VERSION, read_version


class VersionTests(unittest.TestCase):
    def test_reads_trimmed_version(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "VERSION").write_text(" 1.2.3-4\n", encoding="utf-8")
            self.assertEqual(read_version(temp), "1.2.3-4")

    def test_missing_file_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(read_version(temp), FALLBACK_VERSION)

    def test_invalid_format_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "VERSION").write_text("v1.2.3\n", encoding="utf-8")
            self.assertEqual(read_version(temp), FALLBACK_VERSION)


if __name__ == "__main__":
    unittest.main()
