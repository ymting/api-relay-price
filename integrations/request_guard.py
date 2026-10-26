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


def _local_host_only(host: str) -> bool:
    """监听地址是否只对本机开放。拿不准一律按"可能对外"处理。"""
    raw = (host or "").strip().strip("[]").lower()
    if raw in {"", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False        # 网卡地址、0.0.0.0、:: 或主机名都算对外


# 开启访问保护的具体方法。这条提示通常出现在"用户刚把服务暴露出去"的时刻，
# 含糊其辞等于没有 —— 必须能直接照抄执行，而且不能给出当下根本走不通的路径。
def open_auth_hint(cfg: DeployConfig, *, page_available: bool = True) -> str:
    """按场景给出开启方法。

    page_available=False 时服务还没起来（启动被拦），这时说"打开浏览器进设置页"
    是废话，只能给命令行。
    """
    if cfg.mode == "server":
        command = ("docker compose run --rm -it app python server.py --init-auth"
                   "（源码/systemd 部署改为 python server.py --init-auth）")
    else:
        command = "python server.py --init-auth"
    if page_available:
        # 服务在跑：页面是最短路径；--init-auth 会抢实例锁，别让人走弯路
        return ("开启方法：浏览器打开本服务，进入「系统设置 → 访问保护」，"
                "设置用户名和至少 8 位密码后开启。")
    return f"开启方法：执行 {command}，设好用户名和至少 8 位密码后重启服务。"


def startup_auth_problem(cfg: DeployConfig, auth_enabled: bool) -> str | None:
    """启动前拦一道：没开访问保护却可能被外部访问，直接拒绝启动。

    两种会被拦的配置：
      · server 模式 —— 语义就是对外提供服务，不该有"裸奔"这个选项；
      · local 模式但监听地址不是回环 —— 有人手动绑了 0.0.0.0 做穿透。
    确实要在无认证下对外提供服务（例如反代已做鉴权），显式设置
    RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1。
    """
    if auth_enabled or cfg.allow_unauthenticated_remote:
        return None
    if cfg.mode == "server":
        reason = "当前是服务器模式（对外提供服务）"
    elif not _local_host_only(cfg.host):
        reason = f"当前监听地址 {cfg.host} 可被外部访问"
    else:
        return None
    return (f"{reason}，但访问保护未开启。\n"
            f"任何人拿到地址就能看到、修改站点配置和模型 Key。\n"
            f"{open_auth_hint(cfg, page_available=False)}\n"
            f"如果访问保护由前面的反向代理负责，请设置 "
            f"RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1 后再启动。")


def check_remote_without_auth(peer_ip: str, auth_enabled: bool, cfg: DeployConfig) -> None:
    """未开启访问保护时只接受回环来源。

    只看 TCP 对端地址，不看 X-Forwarded-For —— 那个头谁都能写，用它判断等于没判断。
    """
    if auth_enabled or cfg.allow_unauthenticated_remote:
        return
    raw = str(peer_ip or "").strip().strip("[]")
    if not raw:
        return              # 拿不到来源时放行，交给 Host 校验兜底
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return
    raise GuardError(403, "当前未开启访问保护，已拒绝来自外部地址的请求。" + open_auth_hint(cfg))


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
