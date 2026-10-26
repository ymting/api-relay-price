"""单账号认证、会话、登录挑战与限速。

认证文件损坏时一律失败关闭；口令只保存 scrypt 派生值，会话只在内存中
保存 token 摘要。所有公开状态只返回 enabled / configured，不暴露哈希。
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time

from .deploy_config import auth_default_enabled


class AuthConfigError(RuntimeError):
    """认证配置损坏或不可读；调用方必须拒绝访问，不能当作关闭处理。"""


_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 3
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_HASH_PREFIX = "scrypt$v1"


def hash_password(password: str) -> str:
    """生成带算法参数的 scrypt 哈希；参数写进字符串以便未来迁移。"""
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("密码至少需要 8 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                            r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM,
                            dklen=32)
    return "$".join((_HASH_PREFIX, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
                     base64.b64encode(salt).decode("ascii"),
                     base64.b64encode(digest).decode("ascii")))


def _split_hash(encoded: str) -> tuple[int, int, int, bytes, bytes]:
    parts = (encoded or "").split("$")
    if len(parts) != 7 or "$".join(parts[:2]) != _HASH_PREFIX:
        raise AuthConfigError("密码哈希格式不正确")
    try:
        n, r, p = int(parts[2]), int(parts[3]), int(parts[4])
        salt = base64.b64decode(parts[5], validate=True)
        digest = base64.b64decode(parts[6], validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthConfigError("密码哈希格式不正确") from exc
    # 防止被篡改成超高成本参数拖垮进程；正常文件始终落在允许区间内。
    if not (2 ** 14 <= n <= 2 ** 20) or not (1 <= r <= 32) or not (1 <= p <= 16):
        raise AuthConfigError("密码哈希参数超出安全范围")
    if len(salt) < 16 or len(digest) < 32:
        raise AuthConfigError("密码哈希长度不足")
    return n, r, p, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    """常量时间校验口令；格式损坏返回 False，不抛给登录接口。"""
    try:
        n, r, p, salt, expected = _split_hash(encoded)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                                maxmem=max(_SCRYPT_MAXMEM, 128 * n * r * 2),
                                dklen=len(expected))
    except (AuthConfigError, ValueError, OSError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class AuthState:
    enabled: bool
    username: str
    password_hash: str
    auth_revision: int

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password_hash)


class AuthStore:
    """读写状态目录下的 auth.json。文件不存在时才应用首次默认开关。"""

    def __init__(self, state_dir: str | Path, *, default_enabled: bool | None = None,
                 env=os.environ, mode: str | None = None):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "auth.json"
        self.default_enabled = (auth_default_enabled(env, mode)
                                if default_enabled is None else bool(default_enabled))
        self._lock = threading.RLock()

    def load(self) -> AuthState:
        with self._lock:
            if not self.path.exists():
                return AuthState(self.default_enabled, "", "", 1)
            try:
                if os.name != "nt" and self.path.stat().st_mode & 0o077:
                    raise AuthConfigError("auth.json 权限过宽，请收紧为 0600")
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except AuthConfigError:
                raise
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise AuthConfigError("auth.json 读取失败或内容损坏") from exc
            if not isinstance(raw, dict):
                raise AuthConfigError("auth.json 顶层必须是对象")
            enabled = raw.get("enabled")
            username = raw.get("username", "")
            password_hash = raw.get("password_hash", "")
            revision = raw.get("auth_revision")
            if not isinstance(enabled, bool) or not isinstance(username, str) \
                    or not isinstance(password_hash, str) or not isinstance(revision, int) \
                    or isinstance(revision, bool) or revision < 1:
                raise AuthConfigError("auth.json 字段缺失或类型不正确")
            if password_hash:
                _split_hash(password_hash)
            if enabled and not (username and password_hash):
                raise AuthConfigError("认证已开启但用户名或密码未初始化")
            return AuthState(enabled, username, password_hash, revision)

    def save(self, state: AuthState) -> None:
        """原子写入并尽量收紧权限；失败时不留下半截 auth.json。"""
        if state.enabled and not state.configured:
            raise AuthConfigError("认证已开启但没有有效密码")
        if state.password_hash:
            _split_hash(state.password_hash)
        payload = {
            "enabled": bool(state.enabled),
            "username": state.username,
            "password_hash": state.password_hash,
            "auth_revision": int(state.auth_revision),
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def status(self) -> dict:
        state = self.load()
        return {"enabled": state.enabled, "configured": state.configured}

    def verify(self, username: str, password: str) -> bool:
        state = self.load()
        if not state.configured:
            return False
        return hmac.compare_digest(username, state.username) and verify_password(password, state.password_hash)

    def set_initial(self, username: str, password: str, *, enabled: bool = True) -> AuthState:
        username = (username or "").strip()
        if enabled and not username:
            raise AuthConfigError("用户名不能为空")
        state = AuthState(enabled, username, hash_password(password) if enabled else "",
                          1)
        self.save(state)
        return state

    def disable(self) -> AuthState:
        try:
            old = self.load()
        except AuthConfigError:
            old = AuthState(False, "", "", 1)
        state = AuthState(False, old.username, old.password_hash, old.auth_revision + 1)
        self.save(state)
        return state

    def change(self, current_password: str, *, enabled: bool | None = None,
               username: str | None = None, new_password: str | None = None) -> AuthState:
        """页面或 CLI 修改认证；已有密码时必须先验证当前密码。"""
        old = self.load()
        if old.configured and not verify_password(current_password, old.password_hash):
            raise AuthConfigError("当前密码不正确")
        next_enabled = old.enabled if enabled is None else bool(enabled)
        next_username = old.username if username is None else (username or "").strip()
        next_hash = old.password_hash
        if new_password:
            next_hash = hash_password(new_password)
        if next_enabled and not (next_username and next_hash):
            raise AuthConfigError("开启认证前必须设置用户名和密码")
        if not next_enabled:
            next_hash = next_hash  # 关闭时保留哈希，重新开启仍需当前密码或 CLI 重置
        state = AuthState(next_enabled, next_username, next_hash, old.auth_revision + 1)
        self.save(state)
        return state


@dataclass
class SessionRecord:
    csrf: str
    created: float
    last_seen: float


class SessionStore:
    """内存会话表；只保存 token 的 SHA-256 摘要。"""

    def __init__(self, *, idle_seconds: int = 30 * 60, absolute_seconds: int = 12 * 60 * 60):
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, SessionRecord] = {}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    def create(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._sessions[self._digest(token)] = SessionRecord(csrf, now, now)
        return token, csrf

    def get(self, token: str) -> SessionRecord | None:
        if not token:
            return None
        key = self._digest(token)
        now = time.monotonic()
        with self._lock:
            record = self._sessions.get(key)
            if record is None:
                return None
            if now - record.created > self.absolute_seconds or now - record.last_seen > self.idle_seconds:
                self._sessions.pop(key, None)
                return None
            record.last_seen = now
            return record

    def destroy(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(self._digest(token), None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


class ChallengeStore:
    """登录前的匿名挑战，数量有上限，5 分钟过期。"""

    def __init__(self, *, ttl_seconds: int = 300, capacity: int = 64):
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, float]] = {}

    def create(self) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            while len(self._items) >= self.capacity:
                self._items.pop(next(iter(self._items)))
            self._items[hashlib.sha256(token.encode()).hexdigest()] = (csrf, now)
        return token, csrf

    def verify(self, token: str, csrf: str) -> bool:
        if not token or not csrf:
            return False
        key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            self._prune(time.monotonic())
            item = self._items.get(key)
            return bool(item and hmac.compare_digest(item[0], csrf))

    def consume(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._items.pop(hashlib.sha256(token.encode("utf-8")).hexdigest(), None)

    def _prune(self, now: float) -> None:
        expired = [key for key, (_, created) in self._items.items()
                   if now - created > self.ttl_seconds]
        for key in expired:
            self._items.pop(key, None)


class LoginLimiter:
    """按来源、账号与进程总量三层限速；失败退避，不永久锁死。"""

    def __init__(self, *, max_failures: int = 5, window_seconds: int = 300,
                 cooldown_base: int = 1, cooldown_max: int = 60,
                 global_limit: int = 30, global_window_seconds: int = 60):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.cooldown_base = cooldown_base
        self.cooldown_max = cooldown_max
        self.global_limit = global_limit
        self.global_window_seconds = global_window_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._cooldowns: dict[str, tuple[int, float]] = {}
        self._global: list[float] = []

    @staticmethod
    def _keys(ip: str, username: str) -> tuple[str, str]:
        return f"ip:{ip or 'unknown'}", f"user:{(username or '').strip().lower()}"

    def _prune(self, now: float) -> None:
        horizon = now - self.window_seconds
        for key in list(self._failures):
            self._failures[key] = [stamp for stamp in self._failures[key] if stamp >= horizon]
            if not self._failures[key]:
                self._failures.pop(key, None)
        for key in list(self._cooldowns):
            _count, until = self._cooldowns[key]
            # 冷却到期就解除；失败记录仍留在窗口内，下次再失败会按更长的退避重算。
            # 若这里还要求“没有失败记录”，冷却会被窗口内的历史一直续命，等于永久封禁。
            if until <= now:
                self._cooldowns.pop(key, None)
        global_horizon = now - self.global_window_seconds
        self._global = [stamp for stamp in self._global if stamp >= global_horizon]

    def check(self, ip: str, username: str, *, now: float | None = None) -> int:
        """返回还需等待的秒数；0 表示允许尝试。"""
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._prune(current)
            if len(self._global) >= self.global_limit:
                wait = (self._global[0] + self.global_window_seconds) - current
                return max(1, int(wait + 0.999))
            for key in self._keys(ip, username):
                cooldown = self._cooldowns.get(key)
                if cooldown and cooldown[1] > current:
                    return max(1, int(cooldown[1] - current + 0.999))
            return 0

    def record_failure(self, ip: str, username: str, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._global.append(current)
            self._prune(current)
            retry = 0
            for key in self._keys(ip, username):
                stamps = self._failures.setdefault(key, [])
                stamps.append(current)
                if len(stamps) >= self.max_failures:
                    exponent = len(stamps) - self.max_failures
                    cooldown = min(self.cooldown_max, self.cooldown_base * (2 ** exponent))
                    self._cooldowns[key] = (len(stamps), current + cooldown)
                    retry = max(retry, cooldown)
            return retry

    def record_success(self, ip: str, username: str) -> None:
        with self._lock:
            for key in self._keys(ip, username):
                self._failures.pop(key, None)
                self._cooldowns.pop(key, None)


def parse_cookie(header: str | None, name: str) -> str | None:
    """从 Cookie 头里取指定名称，忽略格式错误的片段。"""
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def cookie_header(token: str, *, secure: bool, max_age: int) -> str:
    value = f"relay_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(max_age)}"
    return value + ("; Secure" if secure else "")


def expired_cookie_header(*, secure: bool) -> str:
    value = "relay_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    return value + ("; Secure" if secure else "")
