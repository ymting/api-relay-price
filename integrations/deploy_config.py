"""部署环境变量解析。

所有路径、监听地址与来源白名单只在这里解析一次，避免 server / collector /
测试各自读取环境变量后出现两套口径。local 模式严格保持 Windows 双击兼容，
server 模式缺少必要配置时直接报错，不会悄悄退回代码目录。
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit


class DeployConfigError(ValueError):
    """部署配置不合法；消息可直接展示给用户，且不包含秘密。"""


def _parse_bool(raw: str | None, *, default: bool, name: str) -> bool:
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise DeployConfigError(f"{name} 只能是 0 或 1")


def _parse_int(raw: str | None, *, default: int, name: str, minimum: int = 0,
               maximum: int | None = None) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise DeployConfigError(f"{name} 必须是整数") from exc
    if value < minimum or (maximum is not None and value > maximum):
        upper = f"~{maximum}" if maximum is not None else " 以上"
        raise DeployConfigError(f"{name} 必须在 {minimum}{upper} 范围内")
    return value


def _split_csv(raw: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (raw or "").split(",") if item.strip())


def _is_loopback_host(host: str) -> bool:
    return host.lower() in {"127.0.0.1", "localhost", "::1", "[::1]"}


def _validate_origin(value: str) -> str:
    """Origin 必须是完整的 scheme://host[:port]，不接受路径、查询或通配符。"""
    if value == "null" or "*" in value:
        raise DeployConfigError("RELAY_ALLOWED_ORIGINS 不接受 null 或通配符")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DeployConfigError(f"来源格式不合法：{value}；应为 http(s)://主机[:端口]")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise DeployConfigError(f"来源格式不合法：{value}；不能包含路径、查询或账号信息")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise DeployConfigError(f"来源端口不合法：{value}") from exc
    return value.rstrip("/")


def _validate_host(value: str) -> str:
    """Host 白名单精确到端口；只允许 host[:port]，不允许协议或路径。"""
    if any(ch in value for ch in "/?#") or not value:
        raise DeployConfigError(f"Host 格式不合法：{value}")
    try:
        parsed = urlsplit("//" + value)
        port = parsed.port
    except ValueError as exc:
        raise DeployConfigError(f"Host 端口不合法：{value}") from exc
    if not parsed.hostname or parsed.username or parsed.password:
        raise DeployConfigError(f"Host 格式不合法：{value}")
    if port is not None and not 1 <= port <= 65535:
        raise DeployConfigError(f"Host 端口不合法：{value}")
    return value.lower()


@dataclass(frozen=True)
class DeployConfig:
    """部署相关配置；local 模式下所有严格项取兼容值。"""

    mode: str
    state_dir: Path
    host: str
    port: int | None
    strict_port: bool
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    trusted_proxies: tuple[str, ...]
    allow_null_origin: bool
    version_keep: int
    version_keep_days: int

    @property
    def secure_cookies(self) -> bool:
        """仅当全部来源都是 https 时才给会话 Cookie 加 Secure。"""
        return bool(self.allowed_origins) and all(
            origin.startswith("https://") for origin in self.allowed_origins
        )

    @property
    def data_dir(self) -> Path:
        return self.state_dir / "data"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"


def load_deploy_config(env=os.environ, root: str | Path | None = None) -> DeployConfig:
    """解析并校验部署配置；不合法时抛 DeployConfigError。"""
    base = Path(root or Path(__file__).resolve().parents[1]).resolve()
    mode = (env.get("RELAY_MODE") or "local").strip().lower()
    if mode not in {"local", "server"}:
        raise DeployConfigError("RELAY_MODE 只能是 local 或 server")

    host = (env.get("RELAY_HOST") or "127.0.0.1").strip()
    if not host or any(ch in host for ch in "/?#"):
        raise DeployConfigError("RELAY_HOST 格式不合法")

    raw_port = env.get("RELAY_PORT")
    port = None
    if raw_port is not None and raw_port.strip() != "":
        port = _parse_int(raw_port, default=0, name="RELAY_PORT", minimum=1, maximum=65535)

    origins = tuple(_validate_origin(item) for item in _split_csv(env.get("RELAY_ALLOWED_ORIGINS")))
    hosts = tuple(_validate_host(item) for item in _split_csv(env.get("RELAY_ALLOWED_HOSTS")))
    proxies = tuple(_split_csv(env.get("RELAY_TRUSTED_PROXIES")))

    if mode == "server":
        if not (env.get("RELAY_STATE_DIR") or "").strip():
            raise DeployConfigError("server 模式必须设置 RELAY_STATE_DIR")
        if not origins:
            raise DeployConfigError("server 模式必须设置 RELAY_ALLOWED_ORIGINS")
        if not hosts:
            raise DeployConfigError("server 模式必须设置 RELAY_ALLOWED_HOSTS")
        state_dir = Path(env["RELAY_STATE_DIR"]).expanduser()
        if not state_dir.is_absolute():
            state_dir = (base / state_dir).resolve()
        strict_port = _parse_bool(env.get("RELAY_STRICT_PORT"), default=True, name="RELAY_STRICT_PORT")
        allow_null = False
        # 混用 https 与非回环 http 会让 Cookie 的 Secure 判定失去意义，直接拒绝启动。
        has_https = any(o.startswith("https://") for o in origins)
        if has_https:
            for origin in origins:
                parsed = urlsplit(origin)
                if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname or ""):
                    raise DeployConfigError("RELAY_ALLOWED_ORIGINS 不能混用 https 与非回环 http")
    else:
        for name in ("RELAY_ALLOWED_ORIGINS", "RELAY_ALLOWED_HOSTS", "RELAY_STRICT_PORT"):
            if (env.get(name) or "").strip():
                raise DeployConfigError(f"local 模式不接受 {name}；请先设置 RELAY_MODE=server")
        state_dir = base
        strict_port = False
        allow_null = _parse_bool(env.get("RELAY_LOCAL_ALLOW_NULL_ORIGIN"),
                                 default=False, name="RELAY_LOCAL_ALLOW_NULL_ORIGIN")
        origins = ("http://127.0.0.1", "http://localhost") if allow_null else ()
        hosts = ()

    keep = _parse_int(env.get("RELAY_VERSION_KEEP"), default=30,
                      name="RELAY_VERSION_KEEP", minimum=1)
    keep_days = _parse_int(env.get("RELAY_VERSION_KEEP_DAYS"), default=30,
                           name="RELAY_VERSION_KEEP_DAYS", minimum=1)

    return DeployConfig(
        mode=mode,
        state_dir=state_dir.resolve(),
        host=host,
        port=port,
        strict_port=strict_port,
        allowed_origins=origins,
        allowed_hosts=hosts,
        trusted_proxies=proxies,
        allow_null_origin=allow_null,
        version_keep=keep,
        version_keep_days=keep_days,
    )


def auth_default_enabled(env=os.environ, mode: str | None = None) -> bool:
    """首次没有 auth.json 时的默认开关：服务器默认开，本地默认关。"""
    resolved = (mode or (env.get("RELAY_MODE") or "local")).strip().lower()
    return _parse_bool(env.get("RELAY_AUTH_DEFAULT_ENABLED"),
                       default=resolved == "server", name="RELAY_AUTH_DEFAULT_ENABLED")
