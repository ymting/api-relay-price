"""HTTP 请求前置校验。

这些函数不依赖 BaseHTTPRequestHandler，便于直接用合成输入做安全回归。
所有拒绝都发生在业务动作之前，错误消息不包含请求体或凭据。
"""
from __future__ import annotations

import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit

from .deploy_config import DeployConfig


class GuardError(Exception):
    """请求校验失败；status 为可直接返回的 HTTP 状态码。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_host_header(raw_host: str) -> tuple[str, int | None, str]:
    """返回 (hostname, port, 原始值)。拒绝 userinfo、逗号和空白注入。"""
    raw = (raw_host or "").strip()
    if not raw or any(ch in raw for ch in ",/"):
        raise GuardError(403, "Host 不合法")
    if "@" in raw:
        raise GuardError(403, "Host 不合法")
    try:
        parsed = urlsplit("//" + raw)
        port = parsed.port
    except ValueError as exc:
        raise GuardError(403, "Host 不合法") from exc
    hostname = (parsed.hostname or "").lower()
    if not hostname or parsed.username or parsed.password or parsed.path:
        raise GuardError(403, "Host 不合法")
    return hostname, port, raw


def check_host(raw_host: str, bound_port: int, cfg: DeployConfig) -> None:
    """校验 Host，阻止 DNS 重绑定与错误端口。"""
    if cfg.mode == "server":
        lowered = (raw_host or "").strip().lower()
        if not lowered or lowered not in {item.lower() for item in cfg.allowed_hosts}:
            raise GuardError(403, "Host 不在允许列表")
        return

    hostname, port, _ = _parse_host_header(raw_host)
    if hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise GuardError(403, "Host 不合法")
    if port != int(bound_port):
        raise GuardError(403, "Host 端口不匹配")


def _local_origins(bound_port: int) -> tuple[str, str]:
    return (f"http://127.0.0.1:{int(bound_port)}",
            f"http://localhost:{int(bound_port)}")


def check_write_origin(origin: str | None, bound_port: int, cfg: DeployConfig) -> None:
    """写请求必须来自精确 Origin；null 与缺失默认拒绝。"""
    if cfg.mode == "server":
        if not origin or origin == "null" or origin not in cfg.allowed_origins:
            raise GuardError(403, "请求来源不合法")
        return

    if origin == "null":
        if cfg.allow_null_origin:
            return
        raise GuardError(403, "请求来源不合法")
    allowed = set(_local_origins(bound_port))
    if cfg.allow_null_origin:
        allowed.update({"http://127.0.0.1", "http://localhost"})
    if not origin or origin not in allowed:
        raise GuardError(403, "请求来源不合法")


def cors_allow_origin(origin: str | None, bound_port: int, cfg: DeployConfig) -> str | None:
    """返回允许回显的 Origin；普通 GET 无 Origin 时返回 None（不添加 CORS 头）。"""
    if not origin or origin == "null":
        if origin == "null" and cfg.allow_null_origin and cfg.mode == "local":
            return "null"
        return None
    if cfg.mode == "server":
        return origin if origin in cfg.allowed_origins else None
    allowed = set(_local_origins(bound_port))
    if cfg.allow_null_origin:
        allowed.update({"http://127.0.0.1", "http://localhost"})
    return origin if origin in allowed else None


def _header(headers: Any, name: str) -> str | None:
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return None if value is None else str(value)


def parse_json_body(headers: Any, rfile: Any, max_body: int) -> dict:
    """严格解析 JSON 写请求；任何失败都抛 GuardError，绝不回退成空对象。"""
    if _header(headers, "Transfer-Encoding"):
        raise GuardError(400, "不支持 Transfer-Encoding 请求")

    content_type = (_header(headers, "Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise GuardError(415, "Content-Type 必须是 application/json")

    raw_length = _header(headers, "Content-Length")
    if raw_length is None or raw_length.strip() == "":
        raise GuardError(400, "缺少 Content-Length")
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise GuardError(400, "Content-Length 不合法") from exc
    if length < 0:
        raise GuardError(400, "Content-Length 不合法")
    if length > max_body:
        raise GuardError(413, "请求体过大")

    try:
        raw = rfile.read(length)
    except Exception as exc:
        raise GuardError(400, "请求体读取失败") from exc
    if len(raw) != length:
        raise GuardError(400, "请求体不完整")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError(400, "请求体不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise GuardError(400, "请求体顶层必须是对象")
    return value


def client_address(peer_ip: str, forwarded_for: str | None, cfg: DeployConfig) -> str:
    """仅在 TCP 对端属于可信代理时，取 X-Forwarded-For 最右一跳。"""
    peer = str(peer_ip or "").strip()
    trusted = {item.strip() for item in cfg.trusted_proxies}
    if not forwarded_for or peer not in trusted:
        return peer
    candidates = [item.strip() for item in forwarded_for.split(",") if item.strip()]
    if not candidates:
        return peer
    candidate = candidates[-1]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer
