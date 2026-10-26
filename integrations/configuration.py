"""本地配置、凭据动作与原子轮换。

普通接口响应永远不带出已保存的秘密。表单留空表示"保留"，
只有显式的清空动作才会删除已保存的凭据。
"""
from __future__ import annotations

from dataclasses import fields
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import urllib.request
import uuid
from urllib.parse import urlsplit

from .contracts import SiteConnectionPolicy, ScopeSource
from .providers import Credentials

SECRET_FIELDS = ("cookie", "password", "management_token", "access_token", "refresh_token", "model_api_key")
# 代理只支持 HTTP(S)：urllib 的 ProxyHandler 不认 socks，写进来也只会连不上，不如直接拦在保存前
PROXY_SCHEMES = ("http://", "https://")
PROXY_MAX_LENGTH = 200
# 全局代理的三种取用方式。站点级代理（站点卡片里的「本站代理」）永远优先于这里的全局设置：
#   direct —— 完全直连，连系统代理也不用（排查"是不是代理的锅"时最有用）
#   system —— 跟随操作系统当前的代理设置，端口换了会自动跟着走
#   manual —— 用下面 proxy 字段里手填的地址
PROXY_MODES = ("direct", "system", "manual")
DEFAULT_PROXY_MODE = "system"
# 只把"看着就像 主机:端口"的值补协议；别的乱七八糟文本原样留着，交给校验去报错
_PROXY_BARE_RE = re.compile(r"^(?:[A-Za-z0-9._-]+|\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}$")
_LOCKS = {}
_LOCK = threading.Lock()


def normalize_proxy(value):
    """把用户写的代理规整成带协议的地址；空值原样返回空串。

    `127.0.0.1:7890` 这种漏写协议的写法太常见，只有确实像"主机:端口"时才补 http://，
    免得把随手打错的文本硬套上一个协议前缀。
    """
    text = str(value or "").strip()
    if not text or "://" in text:
        return text
    return "http://" + text if _PROXY_BARE_RE.match(text) else text


def site_proxy(site, fallback=""):
    """站点级代理优先，其次回退到调用方给的全局代理；都为空表示直连。

    采集、账户刷新、Key 操作全都走这里，避免各处各写一套优先级而漂移。
    """
    return normalize_proxy((site or {}).get("proxy")) or normalize_proxy(fallback)


def system_proxies() -> dict:
    """读取操作系统当前的代理设置，返回 urllib 风格的 {协议: 地址}。

    Windows 上是注册表里那个「系统代理」开关（Clash 一类工具的开关控制的就是它），
    没开时 urllib 会退回环境变量；Linux/macOS 只有环境变量。
    返回空字典表示系统当前没启用代理，应当按直连处理。
    """
    try:
        return dict(urllib.request.getproxies() or {})
    except Exception:
        return {}


def system_proxy_address() -> str:
    """把系统代理收敛成一个地址字符串，没启用代理就返回空串。

    优先取 https 的：采集的目标站点全是 https，它才有意义。
    """
    proxies = system_proxies()
    if not proxies:
        return ""
    for scheme in ("https", "http"):
        value = normalize_proxy(proxies.get(scheme) or "")
        if value:
            return value
    # 少数系统只给了别的键（比如统一写成一个），取第一个能用的
    for value in proxies.values():
        normalized = normalize_proxy(value or "")
        if normalized:
            return normalized
    return ""


def normalize_proxy_mode(value, proxy="") -> str:
    """规整代理模式，非法值按规则回落。

    旧配置里没有 proxy_mode 这个字段，此时按 proxy 有没有填来推断：
    填了就是 manual（保住已配好的代理，别升级完悄悄失效），没填才是 system。
    """
    mode = str(value or "").strip().lower()
    if mode in PROXY_MODES:
        return mode
    return "manual" if normalize_proxy(proxy) else DEFAULT_PROXY_MODE


def resolve_proxy(mode, manual_proxy="", site=None) -> str:
    """按「站点级 > 全局模式」解析出最终要用的代理地址。

    返回空串表示直连，调用方（HttpClient / UrllibTransport）据此显式禁用代理。
    注意别把空串当成「跟随系统」：系统模式必须在这里就把地址读出来，
    否则会和直连混淆（urllib 默认行为就是空值跟随系统代理）。
    """
    own = normalize_proxy((site or {}).get("proxy"))
    if own:
        return own
    resolved_mode = normalize_proxy_mode(mode, manual_proxy)
    if resolved_mode == "direct":
        return ""
    if resolved_mode == "manual":
        return normalize_proxy(manual_proxy)
    return system_proxy_address()


def file_lock(path):
    key = str(Path(path).resolve())
    with _LOCK:
        return _LOCKS.setdefault(key, threading.RLock())


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return copy.deepcopy(default)


# 同一进程只提示一次：配置回退是偶发事件，别让每请求都刷一行
_FALLBACK_WARNED: set[str] = set()


def read_json_with_backup(path, default=None):
    """读配置文件，主文件解析失败时回退 .bak 与 ``*.versions/`` 里的最新几份。

    站点清单里是凭据，重配成本最高。本机出现过配置文件被写成全零的情况，
    所以这里宁可读一份旧版本，也不要让一次坏写入把整份配置悄悄变成空列表。
    返回 ``(内容, 实际来源路径)``；来源与主文件不同说明发生了回退。
    """
    path = Path(path)
    candidates = [path, path.with_suffix(path.suffix + ".bak")]
    archive = path.parent / (path.name + ".versions")
    try:
        archived = sorted((item for item in archive.iterdir()
                           if item.is_file() and item.suffix == ".json"),
                          key=lambda item: item.stat().st_mtime, reverse=True)[:5]
    except OSError:
        archived = []
    candidates.extend(archived)
    for candidate in candidates:
        try:
            return json.loads(candidate.read_text(encoding="utf-8-sig")), candidate
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
    return copy.deepcopy(default), path


def _version_policy() -> tuple[int, int]:
    """读取版本归档保留策略；环境变量非法时回落到 30 / 30，绝不因清理失败而拒绝写入。"""
    try:
        keep = max(1, int(os.environ.get("RELAY_VERSION_KEEP") or 30))
    except ValueError:
        keep = 30
    try:
        keep_days = max(1, int(os.environ.get("RELAY_VERSION_KEEP_DAYS") or 30))
    except ValueError:
        keep_days = 30
    return keep, keep_days


def prune_versions(archive: Path, keep: int = 30, keep_days: int = 30,
                   *, now: float | None = None) -> int:
    """清理配置归档：保留「最近 keep 份」与「keep_days 天内」的并集。

    只处理目录下的普通文件，跳过临时文件与子目录；单个文件删除失败不影响其它文件，
    清理永远发生在正式配置替换之后，所以清理异常不会让用户保存失败。
    """
    current = time.time() if now is None else float(now)
    horizon = current - max(1, int(keep_days)) * 86400
    try:
        files = [item for item in Path(archive).iterdir()
                 if item.is_file() and not item.name.endswith(".tmp")]
    except OSError:
        return 0
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    removed = 0
    for index, item in enumerate(files):
        try:
            recent = index < max(1, int(keep))
            fresh = item.stat().st_mtime >= horizon
            if not recent and not fresh:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def write_json(path, value, *, backup=False, version_keep: int | None = None,
               version_keep_days: int | None = None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        if backup and path.exists():
            archive = path.parent / (path.name + ".versions")
            archive.mkdir(exist_ok=True)
            # Exclusive creation; previous versions are never replaced.
            with (archive / (uuid.uuid4().hex + ".json")).open("xb") as stream:
                stream.write(path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            # 归档成功后再清理；保留策略取「最近 N 份 ∪ N 天内」，两项都可由环境变量覆盖。
            keep, keep_days = _version_policy()
            prune_versions(archive,
                           keep if version_keep is None else version_keep,
                           keep_days if version_keep_days is None else version_keep_days)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def credentials_of(site):
    auth = site.get("auth") or {}
    allowed = {field.name for field in fields(Credentials)}
    return Credentials(**{key: value for key, value in auth.items() if key in allowed})


def public_site(site):
    result = copy.deepcopy(site)
    auth = result.setdefault("auth", {})
    result["has_secret"] = {key: bool(auth.get(key)) for key in SECRET_FIELDS}
    for key in SECRET_FIELDS:
        auth.pop(key, None)
    result["credential_expires_at"] = auth.pop("expires_at", None)
    result.pop("_token_pair_ref", None)
    result.pop("secret_actions", None)
    return result


def merge_sites(raw_sites, previous, site_types=("newapi", "sub2api")):
    if not isinstance(raw_sites, list):
        return [], ["站点列表应为数组"]
    old = {s.get("id"): s for s in previous}
    cleaned, errors, seen = [], [], set()
    for index, raw in enumerate(raw_sites):
        try:
            if not isinstance(raw, dict):
                raise ValueError("站点必须为对象")
            sid = str(raw.get("id", "")).strip()
            import re
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", sid) or sid in seen:
                raise ValueError("站点标识非法或重复")
            existing = old.get(sid, {})
            if existing and raw.get("_revision") is not None and raw["_revision"] != existing.get("_revision"):
                raise ValueError("配置已变化，请重新读取后保存；当前输入尚未写入")
            site = copy.deepcopy(existing)
            # Preserve extension fields, but never persist UI secret metadata/actions.
            site.update({k: copy.deepcopy(v) for k, v in raw.items()
                         if k not in {"auth", "has_secret", "secret_actions", "_revision", "credential_version", "_token_pair_ref", "credential_expires_at"}})
            base = str(site.get("base_url") or "").strip().rstrip("/")
            parsed = urlsplit(base)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("站点地址必须是无凭据、查询串的 HTTP(S) 地址")
            site.update(id=sid, base_url=base, type=site.get("type", "newapi"), enabled=bool(site.get("enabled", True)))
            if site["type"] not in site_types:
                raise ValueError("不支持的站点框架")
            site["connection"] = SiteConnectionPolicy.from_dict(site.get("connection") or {}).to_dict()
            incoming = raw.get("auth", {})
            actions = raw.get("secret_actions", {})
            if not isinstance(incoming, dict) or not isinstance(actions, dict):
                raise ValueError("凭据及凭据动作必须为对象")
            if set(actions) - set(SECRET_FIELDS) or any(v not in {"keep", "clear", "replace"} for v in actions.values()):
                raise ValueError("凭据动作非法")
            auth = copy.deepcopy(existing.get("auth") or {})
            auth.update({k: copy.deepcopy(v) for k, v in incoming.items() if k not in SECRET_FIELDS
                         and k not in {"account_verified", "model_binding_verified", "scope_source"}})
            for key in ("account_id", "model_account_id", "model_key_id", "model_group_id", "expires_at"):
                if auth.get(key) == "":
                    auth[key] = None
            changed = False
            for key in SECRET_FIELDS:
                action, value = actions.get(key), incoming.get(key)
                if action == "clear":
                    changed |= bool(auth.get(key))
                    auth[key] = ""
                elif action != "keep" and value:
                    if not isinstance(value, str) or "*" in value or "…" in value or value == "[REDACTED]":
                        raise ValueError("不能保存凭据掩码")
                    value = value.strip()
                    if key != "password" and any(c in value for c in "\r\n"):
                        raise ValueError("凭据不能包含换行")
                    changed |= value != auth.get(key, "")
                    auth[key] = value
                elif action == "replace":
                    raise ValueError("替换凭据需要非空值")
            identity_changed = any(site.get(k) != existing.get(k) for k in ("base_url", "type"))
            binding_changed = any(auth.get(k) != (existing.get("auth") or {}).get(k)
                                  for k in ("account_id", "model_account_id", "model_key_id", "model_group_id"))
            if changed or identity_changed or binding_changed:
                auth.update(account_verified=False, model_binding_verified=False, scope_source=ScopeSource.UNVERIFIED.value)
            site["auth"] = auth
            site["_token_pair_ref"] = digest([auth.get("access_token", ""), auth.get("refresh_token", "")])
            credentials_of(site)  # Validate types and API credential masks before saving.
            if site.get("api_prefix"):
                prefix = site["api_prefix"]
                if not isinstance(prefix, str) or not prefix.startswith("/") or prefix.startswith("//") or any(c in prefix for c in "?#\\") or ".." in prefix.split("/"):
                    raise ValueError("接口前缀非法")
            # 站点级代理：留空表示跟随全局设置；写了就必须是能用的 HTTP(S) 代理
            raw_proxy = site.get("proxy")
            if raw_proxy in (None, ""):
                # 空值不落盘：否则给每个站点凭空加一个字段，会让所有站点的 _revision 变化，
                # 连带把已经核对过的账户快照全部标成"需刷新"。
                site.pop("proxy", None)
            else:
                if not isinstance(raw_proxy, str):
                    raise ValueError("站点代理必须为文本")
                proxy = normalize_proxy(raw_proxy)
                if len(proxy) > PROXY_MAX_LENGTH or any(c in proxy for c in " \t\r\n"):
                    raise ValueError("站点代理格式非法")
                if not proxy.lower().startswith(PROXY_SCHEMES):
                    raise ValueError("站点代理只支持 http:// 或 https:// 开头的地址")
                parsed_proxy = urlsplit(proxy)
                if not parsed_proxy.hostname or not parsed_proxy.port:
                    raise ValueError("站点代理要写成 http://主机:端口")
                site["proxy"] = proxy
            if site.get("wallet_unit", "USD") not in {"USD", "CNY", "unknown"}:
                raise ValueError("钱包单位非法")
            comparable = {k: v for k, v in site.items() if k not in {"_revision", "credential_version"}}
            old_comparable = {k: v for k, v in existing.items() if k not in {"_revision", "credential_version"}}
            site["_revision"] = existing.get("_revision", "legacy") if comparable == old_comparable else uuid.uuid4().hex
            cleaned.append(site)
            seen.add(sid)
        except (ValueError, TypeError, AttributeError) as error:
            errors.append(f"第 {index + 1} 个站点：{error}")
    return cleaned, errors


class ConfigurationStore:
    def __init__(self, path):
        self.path = Path(path)

    def _read(self) -> dict:
        """读配置；主文件坏了就用 .bak / 归档里的最新可用版本。

        回退只提示一次（每进程），避免把日志刷满；真正修复要等下一次保存把内容写回去。
        """
        value, source = read_json_with_backup(self.path, {})
        if source != self.path:
            key = str(source)
            if key not in _FALLBACK_WARNED:
                _FALLBACK_WARNED.add(key)
                print(f"  ! {self.path.name} 读取失败，已回退到 {source.name}",
                      file=sys.stderr)
        return value if isinstance(value, dict) else {}

    def sites(self):
        return self._read().get("sites", [])

    def migrate(self):
        with file_lock(self.path):
            config = self._read()
            if self.path.exists() and config.get("integration_schema_version") != 1:
                for site in config.get("sites", []):
                    site.setdefault("_revision", uuid.uuid4().hex)
                    auth = site.get("auth") or {}
                    site["_token_pair_ref"] = digest([auth.get("access_token", ""), auth.get("refresh_token", "")])
                config["integration_schema_version"] = 1
                write_json(self.path, config, backup=True)
            return config

    def save(self, raw_sites):
        with file_lock(self.path):
            config = self._read()
            sites, errors = merge_sites(raw_sites, config.get("sites", []))
            # A malformed card must never delete another saved card or its credentials.
            if errors:
                return [], errors
            config["sites"] = sites
            write_json(self.path, config, backup=True)
            return sites, []

    def rotate(self, site, expected, replacement):
        with file_lock(self.path):
            config = self._read()
            current = next((s for s in config.get("sites", []) if s.get("id") == site.get("id")), None)
            if current is None or current.get("_revision") != site.get("_revision"):
                return False
            if any(current.get(k) != site.get(k) for k in ("base_url", "type")) or credentials_of(current) != expected.credentials:
                return False
            for key in ("access_token", "refresh_token", "expires_at"):
                current.setdefault("auth", {})[key] = getattr(replacement.credentials, key)
            current["credential_version"] = replacement.token_version
            current["_token_pair_ref"] = digest([replacement.credentials.access_token, replacement.credentials.refresh_token])
            write_json(self.path, config, backup=True)
            return True
