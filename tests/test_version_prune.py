"""配置归档清理：保留「最近 N 份 ∪ N 天内」，跳过临时文件。"""
import os
from pathlib import Path
import tempfile
import time
import unittest

from integrations.configuration import prune_versions, write_json


class PruneVersionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Path(self.temp.name) / "sites.json.versions"
        self.archive.mkdir()
        self.now = time.time()

    def tearDown(self):
        self.temp.cleanup()

    def make(self, name: str, age_days: float) -> Path:
        path = self.archive / name
        path.write_text("{}", encoding="utf-8")
        stamp = self.now - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_keeps_recent_count_and_recent_days_union(self):
        old = [self.make(f"old-{i}.json", 90) for i in range(4)]
        recent = [self.make(f"new-{i}.json", 1) for i in range(3)]
        fresh_beyond_count = self.make("fresh.json", 2)
        removed = prune_versions(self.archive, keep=3, keep_days=30, now=self.now)
        self.assertEqual(removed, 4)
        remaining = {item.name for item in self.archive.iterdir()}
        self.assertEqual(remaining, {item.name for item in recent + [fresh_beyond_count]})
        self.assertFalse(any(item.exists() for item in old))

    def test_skips_temporary_files_and_subdirectories(self):
        keep = self.make("keep.json", 1)
        temp_file = self.make("x.tmp", 100)
        nested = self.archive / "nested"
        nested.mkdir()
        prune_versions(self.archive, keep=1, keep_days=1, now=self.now)
        self.assertTrue(keep.exists())
        self.assertTrue(temp_file.exists())
        self.assertTrue(nested.exists())

    def test_missing_archive_is_not_an_error(self):
        self.assertEqual(prune_versions(self.archive / "missing", now=self.now), 0)

    def test_write_json_prunes_after_archiving(self):
        config = Path(self.temp.name) / "sites.json"
        config.write_text('{"sites": []}', encoding="utf-8")
        # 先放四份 90 天前的旧归档，再写一次配置触发归档 + 清理。
        archive = Path(self.temp.name) / "sites.json.versions"
        archive.mkdir(exist_ok=True)
        old_stamp = time.time() - 90 * 86400
        for index in range(4):
            item = archive / f"old-{index}.json"
            item.write_text("{}", encoding="utf-8")
            os.utime(item, (old_stamp, old_stamp))
        write_json(config, {"sites": [], "round": 1}, backup=True,
                   version_keep=2, version_keep_days=30)
        versions = list(archive.iterdir())
        self.assertEqual(len(versions), 2)
        self.assertEqual(config.exists(), True)


if __name__ == "__main__":
    unittest.main()
