"""优雅停机：预算内退出、任务被请求停止、账户刷新不再启动。"""
import dataclasses
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch


class ShutdownTests(unittest.TestCase):
    def test_wait_task_stop_returns_after_task_finishes(self):
        import server
        fake = Mock(running=True)
        with patch.object(server, "TASK", fake):
            def finish():
                time.sleep(0.3)
                fake.running = False
            threading.Thread(target=finish, daemon=True).start()
            started = time.monotonic()
            server._wait_task_stop(time.monotonic() + 5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(fake.running)

    def test_wait_task_stop_respects_deadline(self):
        import server
        fake = Mock(running=True)
        with patch.object(server, "TASK", fake):
            started = time.monotonic()
            server._wait_task_stop(time.monotonic() + 0.3)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 2)

    def test_scheduler_wait_thread_exits_when_stopped(self):
        import server
        scheduler = server.Scheduler()
        scheduler.stop()
        with patch.object(server, "start_account_refresh") as refresh:
            scheduler._wait_and_refresh_accounts()
            time.sleep(0.3)
        refresh.assert_not_called()

    def test_stop_handlers_only_set_event(self):
        import server
        stop = threading.Event()
        server.install_stop_handlers(stop)
        self.assertFalse(stop.is_set())


class HealthProbeTests(unittest.TestCase):
    def test_unwritable_state_dir_reports_failure(self):
        import server
        # DEPLOY 是冻结数据类，只能整体替换，不能单改字段。
        broken = dataclasses.replace(server.DEPLOY, state_dir=Path("/definitely/not/writable"))
        with patch.object(server, "DEPLOY", broken), \
             patch("tempfile.NamedTemporaryFile", side_effect=OSError("denied")):
            self.assertFalse(server.probe_state_dir())


if __name__ == "__main__":
    unittest.main()
