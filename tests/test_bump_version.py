"""版本号递增规则：dev 用 X.Y.Z-n，发版定版去掉 -n。"""
import unittest

from bump_version import next_dev, parse, release_of


class ParseTests(unittest.TestCase):
    def test_parses_release_version(self):
        self.assertEqual(parse("1.2.3"), (1, 2, 3, None))

    def test_parses_dev_version(self):
        self.assertEqual(parse("1.2.3-7"), (1, 2, 3, 7))

    def test_rejects_prefixed_or_short_forms(self):
        for bad in ("v1.2.3", "1.2", "1.2.3.4", "1.2.3-", "", "1.2.3-a"):
            with self.subTest(bad=bad):
                self.assertRaises(ValueError, parse, bad)


class NextDevTests(unittest.TestCase):
    def test_increments_existing_counter(self):
        self.assertEqual(next_dev("1.0.1-2"), "1.0.1-3")

    def test_starts_from_patch_bump_after_release(self):
        self.assertEqual(next_dev("1.0.0"), "1.0.1-1")

    def test_keeps_major_and_minor(self):
        self.assertEqual(next_dev("1.1.9"), "1.1.10-1")


class ReleaseTests(unittest.TestCase):
    def test_strips_dev_suffix(self):
        self.assertEqual(release_of("1.1.10-3"), "1.1.10")

    def test_refuses_on_release_version(self):
        self.assertRaises(ValueError, release_of, "1.1.10")


if __name__ == "__main__":
    unittest.main()
