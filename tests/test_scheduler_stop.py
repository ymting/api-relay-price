"""调度线程：_stop_evt 不再遮蔽 Thread._stop，join 在 3.10/3.12 不抛错。"""
import threading
import unittest
from unittest import mock


class SchedulerStopTests(unittest.TestCase):
    def test_stop_event_does_not_shadow_thread_api(self):
        import server
        scheduler = server.Scheduler()
        self.assertIsInstance(scheduler._stop_evt, threading.Event)
        # Python 3.13 已移除 Thread._stop；3.10/3.12 上它必须仍是可调用方法，
        # 否则 join() 会把 Event 当函数调用而抛 TypeError。
        legacy_stop = getattr(scheduler, "_stop", None)
        self.assertTrue(legacy_stop is None or callable(legacy_stop))
        self.assertFalse(hasattr(server.Scheduler, "_stop_evt") and
                         callable(getattr(server.Scheduler, "_stop_evt", None)))

    def test_join_after_stop_returns_promptly(self):
        import server
        scheduler = server.Scheduler()
        scheduler.daemon = True
        scheduler.start()
        scheduler.stop()
        scheduler.join(timeout=5)
        self.assertFalse(scheduler.is_alive())

    def test_stopped_scheduler_does_not_start_collection(self):
        import server
        scheduler = server.Scheduler()
        scheduler.stop()
        with mock.patch.object(server.TASK, "start") as start:
            scheduler.run()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
