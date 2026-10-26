"""状态目录实例锁：冲突、PID 读取与释放后可重取。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from integrations.state_lock import StateLock

ROOT = Path(__file__).resolve().parents[1]


class StateLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_same_process_reacquire_is_idempotent(self):
        lock = StateLock(self.state)
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.acquire())
        self.assertEqual(lock.holder_pid(), os.getpid())
        lock.release()
        self.assertFalse(lock.acquired)

    def test_other_process_cannot_acquire_and_reads_pid(self):
        holder = subprocess.Popen([sys.executable, "-X", "utf8", "-c", (
            "import sys,time; sys.path.insert(0, r'" + str(ROOT) + "');"
            "from integrations.state_lock import StateLock;"
            "lock=StateLock(r'" + str(self.state) + "');"
            "assert lock.acquire(); print('locked', flush=True); time.sleep(30)")],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            second = StateLock(self.state)
            self.assertFalse(second.acquire())
            self.assertEqual(second.holder_pid(), holder.pid)
        finally:
            holder.kill()
            holder.wait(timeout=10)
            holder.stdout.close()

    def test_lock_is_reusable_after_holder_dies(self):
        holder = subprocess.Popen([sys.executable, "-X", "utf8", "-c", (
            "import sys,time; sys.path.insert(0, r'" + str(ROOT) + "');"
            "from integrations.state_lock import StateLock;"
            "lock=StateLock(r'" + str(self.state) + "');"
            "assert lock.acquire(); print('locked', flush=True); time.sleep(30)")],
            stdout=subprocess.PIPE, text=True)
        try:
            holder.stdout.readline()
        finally:
            holder.kill()
            holder.wait(timeout=10)
            holder.stdout.close()
        lock = StateLock(self.state)
        try:
            self.assertTrue(lock.acquire())
        finally:
            lock.release()

    def test_pid_survives_without_truncating_lock_region(self):
        lock = StateLock(self.state)
        self.assertTrue(lock.acquire())
        try:
            # 只有 Windows 使用 1 MiB 偏移锁区；POSIX 的 flock 不扩展文件。
            if os.name == "nt":
                self.assertGreater(lock.path.stat().st_size, 1 << 20)
            else:
                contender = StateLock(self.state)
                try:
                    self.assertFalse(contender.acquire())
                finally:
                    contender.release()
            self.assertEqual(lock.holder_pid(), os.getpid())
        finally:
            lock.release()


if __name__ == "__main__":
    unittest.main()
