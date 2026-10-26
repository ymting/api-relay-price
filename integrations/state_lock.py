"""跨进程状态目录实例锁。

锁文件独立于 sites.json / settings.json，避免原子替换配置后锁随 inode 失效。
Windows 锁第 1 MiB 偏移，PID 写文件开头，避免读取 PID 时与锁区冲突。
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import threading

LOCK_OFFSET = 1 << 20


class LockConflict(RuntimeError):
    """状态目录已被其他进程占用。"""

    def __init__(self, pid: int | None):
        self.pid = pid
        super().__init__(f"状态目录已被进程 {pid or '未知'} 占用")


class StateLock:
    """同一 state_dir 只允许一个实例；获取失败时保留 PID 供提示。"""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / ".lock"
        self._stream = None
        self._locked = False
        self._thread_lock = threading.Lock()

    @property
    def acquired(self) -> bool:
        return self._locked

    def holder_pid(self) -> int | None:
        try:
            with self.path.open("rb") as stream:
                head = stream.read(32)
        except OSError:
            return None
        match = re.match(rb"\s*(\d+)", head)
        return int(match.group(1)) if match else None

    def acquire(self) -> bool:
        with self._thread_lock:
            if self._locked:
                return True
            self.state_dir.mkdir(parents=True, exist_ok=True)
            # 必须用随机写模式：append 模式下 seek 后写入仍会落到文件末尾，
            # PID 会跑到偏移 1，别处就读不到持有者进程。
            self.path.touch(exist_ok=True)
            stream = self.path.open("r+b")
            try:
                if os.name == "nt":
                    self._lock_windows(stream)
                else:
                    self._lock_posix(stream)
            except (OSError, ImportError):
                stream.close()
                return False
            stream.seek(0)
            # 只覆盖开头的 PID 区，不能截断文件，否则会削掉 Windows 的锁区。
            stream.write(f"{os.getpid()}\n".encode("ascii").ljust(32, b" "))
            stream.flush()
            os.fsync(stream.fileno())
            self._stream = stream
            self._locked = True
            return True

    @staticmethod
    def _lock_windows(stream) -> None:
        import msvcrt

        # 文件可能刚创建，先把锁区扩到固定偏移，避免锁到 EOF 之外行为不一致。
        stream.seek(0, os.SEEK_END)
        if stream.tell() <= LOCK_OFFSET:
            stream.seek(LOCK_OFFSET)
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(LOCK_OFFSET)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

    @staticmethod
    def _lock_posix(stream) -> None:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        with self._thread_lock:
            if not self._locked or self._stream is None:
                return
            try:
                if os.name == "nt":
                    import msvcrt

                    self._stream.seek(LOCK_OFFSET)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                try:
                    self._stream.close()
                finally:
                    self._stream = None
                    self._locked = False

    def __enter__(self) -> "StateLock":
        if not self.acquire():
            raise LockConflict(self.holder_pid())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass
