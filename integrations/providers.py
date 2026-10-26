"""New API / Sub2API 提供者。只依赖契约、纯解析和标准库，不导入旧业务。

阶段 C 不注册到生产入口。D 阶段须提供共享 CredentialSession、原子凭据
轮换回调和持久化 WriteHooks，再由 router 调用。所有写请求只发送一次。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .contracts import (AuthKind, ErrorCategory, ErrorInfo, Feature, FeatureResult, FeatureStatus,
                        OperationStatus, PermissionStatus, ProviderKind, ScopeSource, VisibilityScope,
                        WRITE_FEATURES, check_newapi_permission, classify_response,
                        looks_like_secret_mask, utc_now)
from .pricing import FetchError, parse_newapi_pricing, parse_sub2api_pricing


UPSTREAM_REVISIONS = {
    "newapi": "19d435e7fdfdaa2b9a734a76393f112cf980b815",
    "sub2api": "857a76d4a0e7b2389926c340555a2f8f5b24a0ae",
}
SUB2API_VIEWS_REVISION = "b0a02a3304a0a78cdd003ddda1dd23ba02c03084"

# 与原客户端保持一致。部分站点在到达账户认证前就拒绝默认 Python 请求头。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"


class ProviderFailure(Exception):
    def __init__(self, error: ErrorInfo | ErrorCategory, data=None):
        self.error = error if isinstance(error, ErrorInfo) else ErrorInfo(error)
        self.data = data
        super().__init__(self.error.message)


def number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR) from None
    if not math.isfinite(value):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    return value


def positive_id(value):
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or int(value) <= 0:
        raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
    return int(value)


# ---- 单日用量：窗口切分、兜底上限与取值工具 ----------------------------------
# 单日窗口按北京时间切分，与本机定时任务口径一致。
DAILY_TZ = timezone(timedelta(hours=8))
# 消耗明细单页最多 100 条，最多翻 10 页；翻满仍没到底就只给下界，不假装是全天。
DAILY_LOG_PAGES, DAILY_LOG_PAGE_SIZE = 10, 100
# 单日用量是增强项：每次请求前给账户刷新的其余部分留出这么多秒，不能把共享预算耗光。
DAILY_RESERVE = 4.0


def daily_window(now=None):
    """返回 (日期, 起始秒, 结束秒)：北京时间今日 00:00 到现在。"""
    moment = now or datetime.now(DAILY_TZ)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.strftime("%Y-%m-%d"), int(start.timestamp()), int(moment.timestamp())


def _count(value):
    """把回包里的计数字段安全地转成非负整数；缺失或类型不对一律算 0，不抛异常。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if math.isfinite(value) and value > 0 else 0


def _daily_reason(error):
    """单日用量的失败原因只记错误分类，不回显任何响应正文。"""
    return ":" + error.error.category.value if isinstance(error, ProviderFailure) else ":响应格式不符"


def _secret(value):
    if not isinstance(value, str) or any(ch in value for ch in "\r\n"):
        raise ValueError("凭据必须为不含换行的字符串")
    value = value.strip()
    # 只有真正的掩码形态才拒绝；单个 '*' 属于合法密码字符
    if value and looks_like_secret_mask(value):
        raise ValueError("不能把掩码作为凭据")
    return value


@dataclass(frozen=True, slots=True)
class Credentials:
    management_token: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    model_api_key: str = field(default="", repr=False)
    expires_at: float | None = None
    account_id: str | int | None = None
    account_verified: bool = False
    model_key_id: str | int | None = None
    model_group_id: str | int | None = None
    model_account_id: str | int | None = None
    model_binding_verified: bool = False
    scopes: tuple[str, ...] | None = None
    scope_source: ScopeSource = ScopeSource.UNVERIFIED

    def __post_init__(self):
        for name in ("management_token", "access_token", "refresh_token", "model_api_key"):
            object.__setattr__(self, name, _secret(getattr(self, name)))
        if self.expires_at is not None:
            value = number(self.expires_at)
            if value < 0:
                raise ValueError("expires_at 无效")
            object.__setattr__(self, "expires_at", value)
        for name in ("account_verified", "model_binding_verified"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} 必须为布尔值")
        for name in ("account_id", "model_key_id", "model_group_id", "model_account_id"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int)) or value == ""):
                raise ValueError(f"{name} 必须是 ID 或 null")
        if self.model_binding_verified and (not self.model_api_key or self.model_account_id is None
                or self.model_key_id is None or self.model_group_id is None):
            raise ValueError("已验证的模型 Key 绑定必须包含完整身份关联")
        if self.scopes is not None:
            if not isinstance(self.scopes, (list, tuple)) or not all(isinstance(s, str) for s in self.scopes):
                raise ValueError("scopes 必须为字符串列表")
            object.__setattr__(self, "scopes", tuple(self.scopes))
        object.__setattr__(self, "scope_source", ScopeSource(self.scope_source))

    @property
    def key_ref(self):
        return hashlib.sha256(self.model_api_key.encode()).hexdigest() if self.model_api_key else None


@dataclass(frozen=True, slots=True)
class CredentialSnapshot:
    credentials: Credentials = field(repr=False)
    config_revision: str | int
    token_version: int


class CredentialSession:
    """同站点/账户/地址/代理共享一个实例；D 的凭据管理器负责按这些维度隔离。

    save_rotation(expected, replacement) 必须在持久化层比较配置/凭据版本，
    原子保存 access/refresh/expiry 后返回 True。没有回调时不发续期请求。
    """
    def __init__(self, credentials: Credentials, config_revision=0, save_rotation=None):
        self._condition = threading.Condition(threading.RLock())
        self._credentials = credentials
        self._revision = config_revision
        self._version = 0
        self._refreshing = False
        self._refresh_failure = None
        self._save_rotation = save_rotation
        self._binding = None

    def bind(self, identity):
        with self._condition:
            if self._binding is not None and self._binding != identity:
                raise ValueError("凭据会话不能跨站点、地址、代理或平台复用")
            self._binding = identity

    def snapshot(self, expected_revision):
        with self._condition:
            if self._revision != expected_revision:
                raise ProviderFailure(ErrorCategory.CONFIG_CHANGED)
            return CredentialSnapshot(self._credentials, self._revision, self._version)

    def replace_credentials(self, credentials: Credentials, config_revision, save_rotation=None):
        with self._condition:
            if config_revision == self._revision:
                raise ValueError("修改凭据必须同时更新配置版本")
            # 连同配置版本替换，旧请求不能发布结果；不会等待远端续期结束。
            self._credentials, self._revision = credentials, config_revision
            self._version += 1
            self._refresh_failure = None
            if save_rotation is not None:
                self._save_rotation = save_rotation
            self._condition.notify_all()

    def assert_current(self, snapshot):
        with self._condition:
            if snapshot.config_revision != self._revision:
                raise ProviderFailure(ErrorCategory.CONFIG_CHANGED)

    def refresh(self, expected: CredentialSnapshot, context, perform):
        with self._condition:
            while True:
                context.check()
                self.assert_current(expected)
                if self._version != expected.token_version:
                    return self.snapshot(expected.config_revision)
                if self._refresh_failure and self._refresh_failure[0] == self._version:
                    raise ProviderFailure(self._refresh_failure[1])
                if not self._refreshing:
                    break
                self._condition.wait(timeout=min(.05, context.remaining()))
            if not expected.credentials.refresh_token:
                raise ProviderFailure(ErrorCategory.AUTH_REQUIRED)
            if self._save_rotation is None:
                raise ProviderFailure(ErrorCategory.CREDENTIAL_SAVE_FAILED)
            self._refreshing = True
        failure = None
        before_requests = context.requests
        try:
            rotated = perform(expected.credentials)
            with self._condition:
                # 远端已轮换时必须保全收到的新令牌对；取消只禁止后续读请求/业务快照。
                self.assert_current(expected)
                if self._version != expected.token_version:
                    raise ProviderFailure(ErrorCategory.CONFIG_CHANGED)
                replacement = CredentialSnapshot(rotated, self._revision, self._version + 1)
                try:
                    saved = self._save_rotation(expected, replacement)
                except Exception:
                    raise ProviderFailure(ErrorCategory.CREDENTIAL_SAVE_FAILED) from None
                if saved is not True:
                    raise ProviderFailure(ErrorCategory.CREDENTIAL_SAVE_FAILED)
                self._credentials, self._version = rotated, replacement.token_version
                self._refresh_failure = None
                return replacement
        except ProviderFailure as exc:
            failure = exc.error
            raise
        finally:
            with self._condition:
                if (failure and context.requests > before_requests and self._version == expected.token_version
                        and self._revision == expected.config_revision):
                    self._refresh_failure = (self._version, failure)
                self._refreshing = False
                self._condition.notify_all()


class RequestContext:
    """可由 D 的 router 传入并跨尝试复用；不是每个分支重建的请求预算。"""
    def __init__(self, seconds=25, max_requests=128, should_stop=None, clock=time.monotonic,
                 profile_cache=None, key_list_cache=None):
        if not math.isfinite(seconds) or seconds <= 0 or max_requests <= 0:
            raise ValueError("操作预算必须为正数")
        self._clock, self._deadline = clock, clock() + seconds
        self.max_requests, self.requests = max_requests, 0
        self.should_stop = should_stop
        self.last_endpoint = None
        self.error_http_status = None
        self.error_response_format = None
        self.auth_kind = AuthKind.NONE
        self.visibility = VisibilityScope.UNKNOWN
        self.account_id = None
        self.write_sent = False
        self.operation_id = None
        # 按凭据会话及版本隔离；账户刷新独立使用，Key 交互可在短会话内复用身份。
        self.profile_cache = profile_cache if profile_cache is not None else {}
        # 完整 Key 列表只供同一次导出随后创建时作写前证据，不跨点击或写入复用。
        self.key_list_cache = key_list_cache if key_list_cache is not None else {}

    def remaining(self):
        return max(0., self._deadline - self._clock())

    def check(self):
        if self.should_stop and self.should_stop():
            raise ProviderFailure(ErrorCategory.CANCELLED)
        if self.remaining() <= 0:
            raise ProviderFailure(ErrorCategory.BUDGET_EXHAUSTED)

    def consume(self):
        self.check()
        if self.requests >= self.max_requests:
            raise ProviderFailure(ErrorCategory.BUDGET_EXHAUSTED)
        self.requests += 1

    def wait(self, seconds):
        if seconds >= self.remaining():
            raise ProviderFailure(ErrorCategory.BUDGET_EXHAUSTED)
        deadline = self._clock() + seconds
        while self._clock() < deadline:
            self.check()
            threading.Event().wait(min(.05, deadline - self._clock()))
        self.check()


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    payload: object = field(default=None, repr=False)
    headers: dict = field(default_factory=dict, repr=False)
    text: str = field(default="", repr=False)


class Transport(Protocol):
    def request(self, method, url, *, headers, body, timeout) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不自动转发任何重定向，包括 HTTPS 降级、跨域和写请求方法改写。
        return None


class UrllibTransport:
    def __init__(self, proxy=""):
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}), _NoRedirect())

    def request(self, method, url, *, headers, body=None, timeout=25):
        raw = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(url, raw, headers=headers, method=method)
        try:
            response = self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            text = raw.decode("utf-8", "replace")
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                payload = None
            return HttpResponse(response.code, payload, dict(response.headers), text)


class WriteHooks(Protocol):
    def prepare(self, *, feature, target_id, parameter_digest, config_revision, account_id) -> str: ...
    def sent(self, operation_id: str) -> None: ...
    def finish(self, operation_id: str, status: str, remote_id) -> None: ...


def _unwrap(payload):
    if isinstance(payload, dict) and ("code" in payload or "success" in payload) and "data" in payload:
        return payload["data"]
    return payload


def _object(payload):
    value = _unwrap(payload)
    if not isinstance(value, dict):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    return value


def parse_sub2api_usage(payload, *, site_id, credentials: Credentials):
    """日/周/月窗口独立保留；只有明确 balance 才可识别为钱包。"""
    value = _object(payload)
    mode = value.get("mode")
    if mode not in {"quota_limited", "unrestricted"}:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    kind = ("key_quota" if mode == "quota_limited" else
            "subscription" if isinstance(value.get("subscription"), dict) else
            "wallet" if value.get("balance") is not None else "unknown")
    # Key 限额字段缺失时不借用窗口 remaining，也不推算钱包。
    amount = number(value.get("remaining"))
    if kind == "wallet":
        balance = number(value.get("balance"))
        if amount is not None and amount != balance:
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        amount = balance
    if kind == "unknown":
        amount = None
    windows = []
    rate_limits = value.get("rate_limits")
    if rate_limits is not None and not isinstance(rate_limits, list):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    for row in rate_limits or []:
        if not isinstance(row, dict):
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        windows.append({"window": row.get("window"), "limit": number(row.get("limit")),
                        "used": number(row.get("used")), "remaining": number(row.get("remaining")),
                        "reset_at": row.get("reset_at"), "window_start": row.get("window_start")})
    subscription = value.get("subscription")
    if kind == "subscription":
        for window in ("daily", "weekly", "monthly"):
            windows.append({"window": window, "limit": number(subscription.get(window + "_limit_usd")),
                            "used": number(subscription.get(window + "_usage_usd")), "remaining": None})
    account = credentials.model_account_id if credentials.model_binding_verified else None
    scope_id = (f"{site_id}:account:{account}" if kind == "wallet" and account is not None else
                f"{site_id}:subscription-group:{credentials.model_group_id}:account:{account}"
                if kind == "subscription" and account is not None else f"{site_id}:key:{credentials.key_ref}")
    return {"balance_kind": kind, "scope_id": scope_id, "account_id": account,
            "amount": amount if amount is None or amount >= 0 else None, "raw_remaining": value.get("remaining"),
            "unlimited": amount == -1 if kind == "subscription" else False,
            "unit": value.get("unit") or "unknown", "windows": windows,
            "windows_complete": rate_limits is not None if kind == "key_quota" else kind == "subscription",
            "quota": value.get("quota"), "subscription": subscription, "mode": mode,
            "usage": value.get("usage"), "usage_scope": "key", "key_status": value.get("status"),
            "key_state_valid": value.get("isValid"), "key_ref": credentials.key_ref}


def parse_key_billing(payload, *, site_id, credentials: Credentials):
    value = _object(payload)
    if value.get("object") != "sub2api.key_billing" or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    effective = number(value.get("effective_rate_multiplier"))
    if effective is None or effective < 0:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    observed = value.get("observed_at")
    try:
        if not isinstance(observed, str) or datetime.fromisoformat(observed.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError()
    except ValueError:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR) from None
    result = {key: value.get(key) for key in ("billing_scope", "group_rate_multiplier", "user_rate_multiplier",
              "resolved_rate_multiplier", "peak_rate_enabled", "peak_start", "peak_end", "peak_rate_multiplier",
              "applied_peak_multiplier", "timezone", "observed_at")}
    result.update(kind="key_multiplier", effective_rate_multiplier=effective, site_id=site_id,
                  account_id=credentials.model_account_id if credentials.model_binding_verified else None,
                  group_id=credentials.model_group_id if credentials.model_binding_verified else None,
                  key_ref=credentials.key_ref, binding_verified=credentials.model_binding_verified)
    return result


def price_with_key_multiplier(base_price_per_token, billing, *, site_id, account_id, group_id, key_ref):
    """仅用于未乘倍率的基础 token 价；不能输入已处理的模型广场报价或图片价。"""
    if not billing.get("binding_verified") or any(str(billing.get(k)) != str(v) for k, v in (
            ("site_id", site_id), ("account_id", account_id), ("group_id", group_id), ("key_ref", key_ref))):
        raise ProviderFailure(ErrorCategory.ACCOUNT_MISMATCH)
    base, multiplier = number(base_price_per_token), number(billing.get("effective_rate_multiplier"))
    if base is None or multiplier is None or base < 0 or multiplier < 0:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    return number(base * 1000000 * multiplier)  # effective 已含峰时，不再乘 group/resolved/peak。


class BaseProvider:
    platform = ""
    def __init__(self, site, session: CredentialSession, *, config_revision=0, transport=None,
                 proxy="", read_retries=1, write_hooks: WriteHooks | None = None):
        self.site = json.loads(json.dumps(site, allow_nan=False))
        self.base = str(site.get("base_url") or "").rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or ".." in urllib.parse.unquote(parsed.path).split("/")):
            raise ValueError("站点地址无效")
        self.site_id = str(site["id"])
        self.session, self.config_revision = session, config_revision
        self.session.bind((self.site_id, self.base, proxy, self.platform))
        self.transport = transport if transport is not None else UrllibTransport(proxy)
        self.read_retries = max(0, min(2, int(read_retries)))
        self.write_hooks = write_hooks

    def fetch(self, feature, *, context=None, **parameters):
        feature = Feature(feature)
        context = context if context is not None else RequestContext(seconds=12 if feature in WRITE_FEATURES else 25)
        attempted = utc_now()
        try:
            context.check()
            self.session.snapshot(self.config_revision)
            if feature in WRITE_FEATURES and self.write_hooks is None:
                raise ProviderFailure(ErrorCategory.NOT_MIGRATED)
            data = self._dispatch(feature, context, parameters)
            self.session.snapshot(self.config_revision)
            context.check()
            return self._result(feature, context, attempted, data=data)
        except FetchError:
            return self._result(feature, context, attempted, error=ErrorCategory.PROTOCOL_ERROR)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return self._result(feature, context, attempted, error=(ErrorCategory.OPERATION_UNKNOWN
                if feature in WRITE_FEATURES and context.write_sent else ErrorCategory.PROTOCOL_ERROR))
        except ProviderFailure as error:
            category = error.error.category
            if feature in WRITE_FEATURES and context.write_sent and category in {
                    ErrorCategory.TIMEOUT, ErrorCategory.NETWORK_ERROR, ErrorCategory.SERVER_ERROR,
                    ErrorCategory.PROTOCOL_ERROR, ErrorCategory.CANCELLED, ErrorCategory.BUDGET_EXHAUSTED,
                    ErrorCategory.CONFIG_CHANGED, ErrorCategory.CREDENTIAL_SAVE_FAILED,
                    ErrorCategory.VALIDATION_ERROR, ErrorCategory.PARTIAL_DATA}:
                category = ErrorCategory.OPERATION_UNKNOWN
            data = error.data
            if category == ErrorCategory.OPERATION_UNKNOWN and context.operation_id is not None:
                data = {**(data or {}), "operation_id": context.operation_id}
            return self._result(feature, context, attempted, data=data, error=category)

    def _result(self, feature, context, attempted, *, data=None, error=None):
        statuses = {ErrorCategory.NOT_MIGRATED: FeatureStatus.NOT_MIGRATED,
                    ErrorCategory.UNSUPPORTED: FeatureStatus.UNSUPPORTED,
                    ErrorCategory.OPERATION_UNKNOWN: FeatureStatus.UNKNOWN,
                    ErrorCategory.CANCELLED: FeatureStatus.CANCELLED,
                    ErrorCategory.PARTIAL_DATA: FeatureStatus.PARTIAL}
        return FeatureResult(feature, FeatureStatus.SUCCESS if error is None else statuses.get(error, FeatureStatus.ERROR),
            ProviderKind.API, data=data, auth_kind=context.auth_kind, source_endpoint=context.last_endpoint,
            visibility_scope=context.visibility, account_id=context.account_id,
            last_success_at=utc_now() if error is None else None, attempted_at=attempted,
            error_category=error, config_revision=self.config_revision,
            error_http_status=context.error_http_status if error else None,
            error_response_format=context.error_response_format if error else None)

    def _dispatch(self, feature, context, params):
        raise ProviderFailure(ErrorCategory.NOT_MIGRATED)

    def _url(self, path):
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
        return self.base + path

    def _raw(self, method, path, headers, body, context, *, writing=False, auth_rotation=False):
        context.check()
        context.consume()
        context.last_endpoint = path
        context.error_http_status = None
        context.error_response_format = None
        if writing:
            context.write_sent = True
        try:
            response = self.transport.request(method, self._url(path), headers={
                "User-Agent": USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE,
                "Accept": "application/json", "Content-Type": "application/json", **headers},
                body=body, timeout=min(12 if writing else 25, context.remaining()))
        except ProviderFailure:
            raise
        except Exception as error:
            raise ProviderFailure(classify_response(exception=error)) from None
        if not auth_rotation:
            context.check()
        context.error_http_status = response.status
        content_type = next((str(value).lower() for key, value in response.headers.items()
                             if str(key).lower() == "content-type"), "")
        context.error_response_format = ("html" if "text/html" in content_type
            or response.text.lstrip().lower().startswith(("<!doctype html", "<html"))
            else "json" if isinstance(response.payload, (dict, list)) else "other")
        if 300 <= response.status < 400:
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        error = classify_response(response.status, response.payload, body=response.text,
                                  headers=response.headers, schema_valid=True if response.status == 204 else None)
        if response.status in {403, 404} and self._known_unsupported(path, response.payload):
            error = ErrorInfo(ErrorCategory.UNSUPPORTED, response.status)
        if error:
            message = response.payload.get("message") if isinstance(response.payload, dict) else None
            parameter_rejected = (self.platform == "newapi" and response.status in {400, 422}
                and isinstance(message, str) and re.search(r"page_size|\bps\b|\bsize\b", message, re.I))
            raise ProviderFailure(error, {"pagination_parameter_rejected": True} if parameter_rejected else None)
        return response.payload

    def _known_unsupported(self, path, payload):
        return False

    def _headers(self, kind, snapshot):
        credentials = snapshot.credentials
        token = {AuthKind.MANAGEMENT_TOKEN: credentials.management_token,
                 AuthKind.PANEL_TOKEN: credentials.access_token, AuthKind.MODEL_KEY: credentials.model_api_key}.get(kind)
        if kind == AuthKind.NONE:
            return {}
        if not token:
            raise ProviderFailure(ErrorCategory.MISSING_CREDENTIAL)
        return {"Authorization": "Bearer " + token}

    def _refresh(self, snapshot, context):
        raise ProviderFailure(ErrorCategory.AUTH_REQUIRED)

    def _call(self, path, context, *, kind, method="GET", body=None, writing=False):
        snapshot = self.session.snapshot(self.config_revision)
        context.auth_kind = kind
        context.visibility = VisibilityScope.PUBLIC if kind == AuthKind.NONE else VisibilityScope.KEY if kind == AuthKind.MODEL_KEY else VisibilityScope.ACCOUNT
        context.account_id = (snapshot.credentials.model_account_id if snapshot.credentials.model_binding_verified else None) if kind == AuthKind.MODEL_KEY else (
            snapshot.credentials.account_id if snapshot.credentials.account_verified else getattr(context, "verified_account", None))
        context.last_endpoint = path
        refreshed = False
        if kind == AuthKind.PANEL_TOKEN and snapshot.credentials.refresh_token and (
                not snapshot.credentials.access_token or snapshot.credentials.expires_at is not None
                and snapshot.credentials.expires_at <= time.time() + 30):
            snapshot = self._refresh(snapshot, context)
            refreshed = True
        if self.platform == "newapi" and kind == AuthKind.MANAGEMENT_TOKEN:
            check = check_newapi_permission(method, path, scopes=snapshot.credentials.scopes,
                scope_source=snapshot.credentials.scope_source)
            if check.status == PermissionStatus.DENIED:
                raise ProviderFailure(ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED, {"required_scope": check.required_scope})
            if check.status == PermissionStatus.SESSION_REQUIRED:
                raise ProviderFailure(ErrorCategory.AUTH_SESSION_REQUIRED)
        transient = 0
        while True:
            headers = self._headers(kind, snapshot)
            try:
                payload = self._raw(method, path, headers, body, context, writing=writing)
                self.session.assert_current(snapshot)
                return payload
            except ProviderFailure as error:
                self.session.assert_current(snapshot)
                if not writing and kind == AuthKind.PANEL_TOKEN and error.error.refresh_allowed and not refreshed:
                    snapshot = self._refresh(snapshot, context)
                    refreshed = True
                    continue
                if not writing and error.error.retryable_read and transient < self.read_retries:
                    context.wait(error.error.retry_after if error.error.retry_after is not None else .1 * (2 ** transient))
                    transient += 1
                    continue
                raise

    def _account(self, value, context):
        remote = value.get("id")
        if remote is None or isinstance(remote, bool) or not isinstance(remote, (str, int)):
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        credentials = self.session.snapshot(self.config_revision).credentials
        if credentials.account_id is not None and str(remote) != str(credentials.account_id):
            raise ProviderFailure(ErrorCategory.ACCOUNT_MISMATCH)
        context.account_id = remote
        return {key: value.get(key) for key in ("id", "username", "display_name", "email", "group", "status", "role")}

    def _pages(self, path, context, kind, *, sub2=False, require_total=False):
        items, seen, total = [], set(), None
        size_keys, size_index = ("page_size", "ps", "size"), 0
        for page in range(1, 101):
            while True:
                query = urllib.parse.urlencode({"page" if sub2 else "p": page, size_keys[size_index]: 100})
                try:
                    payload = self._call(path + "?" + query, context, kind=kind)
                    break
                except ProviderFailure as error:
                    if not sub2 and page == 1 and size_index < 2 and (error.data or {}).get("pagination_parameter_rejected"):
                        size_index += 1
                        continue
                    raise
            value = _unwrap(payload)
            rows = value if isinstance(value, list) else value.get("items") if isinstance(value, dict) else None
            if not isinstance(rows, list):
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            metadata = value if isinstance(value, dict) else {}
            reported = number(metadata.get("total"))
            if require_total and reported is None:
                raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
            if reported is not None:
                if reported < 0 or not reported.is_integer() or total is not None and total != reported:
                    raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
                total = reported
            page_size = number(metadata.get("page_size", metadata.get("ps", metadata.get("size")))) or 100
            added = 0
            for row in rows:
                if not isinstance(row, dict) or row.get("id") is None:
                    raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
                if str(row["id"]) not in seen:
                    seen.add(str(row["id"]))
                    items.append(row)
                    added += 1
            if total is not None and len(items) >= total:
                if len(items) != total:
                    raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
                return items
            if not rows or len(rows) < page_size:
                if total is not None:
                    raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
                return items
            if not added:
                raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
        raise ProviderFailure(ErrorCategory.PARTIAL_DATA)

    def _write(self, feature, path, context, *, kind, method, body=None, target_id=None, evidence=None):
        context.check()
        credentials = self.session.snapshot(self.config_revision).credentials
        if credentials.account_verified and credentials.account_id is not None:
            context.account_id = credentials.account_id
        if context.account_id is None:
            context.account_id = getattr(context, "verified_account", None)
        if context.account_id is None:
            raise ProviderFailure(ErrorCategory.AUTH_REQUIRED, {"reason": "account_identity_not_verified"})
        verified_account = context.account_id
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()
        try:
            operation = self.write_hooks.prepare(feature=feature.value, target_id=target_id,
                parameter_digest=digest, config_revision=self.config_revision, account_id=context.account_id)
            if not isinstance(operation, str) or not operation:
                raise ValueError()
            context.operation_id = operation
            if hasattr(self.write_hooks, "evidence"):
                self.write_hooks.evidence(operation, evidence or {"expected": body or {}})
            context.check()
            self.write_hooks.sent(operation)
        except ProviderFailure:
            raise
        except Exception:
            raise ProviderFailure(ErrorCategory.OPERATION_SAVE_FAILED) from None
        try:
            payload = self._call(path, context, kind=kind, method=method, body=body, writing=True)
            context.account_id = verified_account
        except ProviderFailure as error:
            try:
                # 已发出但结果不明 → unknown；连请求都没出去 → prepared。两者都要挡后续重放。
                self.write_hooks.finish(operation, OperationStatus.UNKNOWN.value if context.write_sent
                                        else OperationStatus.PREPARED.value, target_id)
            except Exception:
                pass
            raise ProviderFailure(error.error, {"operation_id": operation, "remote_id": target_id}) from None
        return payload, operation

    def _finish_write(self, operation, remote_id):
        try:
            self.write_hooks.finish(operation, OperationStatus.CONFIRMED.value, remote_id)
        except Exception:
            raise ProviderFailure(ErrorCategory.OPERATION_UNKNOWN, {"operation_id": operation, "remote_id": remote_id}) from None


_NEWAPI_EDIT_FIELDS = frozenset({"name", "status", "group", "remain_quota", "unlimited_quota", "expired_time",
    "model_limits_enabled", "model_limits", "allow_ips", "allow_user_quota_query", "cross_group_retry", "auto_groups"})
_SUB2_EDIT_FIELDS = frozenset({"name", "group_id", "status", "quota", "expires_at", "ip_whitelist", "ip_blacklist",
    "rate_limit_5h", "rate_limit_1d", "rate_limit_7d", "reset_quota", "reset_rate_limit_usage"})


def _public_key(row):
    record = {key: value for key, value in row.items() if key in _NEWAPI_EDIT_FIELDS | _SUB2_EDIT_FIELDS | {
        "id", "used_quota", "quota_used", "created_time", "created_at", "expires_at", "user_id", "model_limits"}}
    key = str(row.get("key") or "")
    record["key_masked"] = key[:6] + "***" + key[-4:] if key and "*" not in key else key
    return record


def _plain_key(row, *, newapi=False):
    key = row.get("key")
    try:
        key = _secret(key)
    except ValueError:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR) from None
    if not key:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    return "sk-" + key if newapi and not key.startswith("sk-") else key


class NewApiProvider(BaseProvider):
    platform = "newapi"
    _kind = AuthKind.MANAGEMENT_TOKEN

    def _headers(self, kind, snapshot):
        headers = super()._headers(kind, snapshot)
        account_id = getattr(self, "_user_header_id", None)
        if kind == self._kind and account_id is not None:
            headers["New-Api-User"] = account_id
        return headers

    def _profile(self, context):
        snapshot = self.session.snapshot(self.config_revision)
        cache_key = (self.session, snapshot.config_revision, snapshot.token_version)
        if cache_key in context.profile_cache:
            profile = dict(context.profile_cache[cache_key])
            context.auth_kind, context.visibility = self._kind, VisibilityScope.ACCOUNT
            context.last_endpoint, context.account_id = "/api/user/self", profile["id"]
            return profile
        previous_header = getattr(self, "_user_header_id", None)
        try:
            try:
                user = _object(self._call("/api/user/self", context, kind=self._kind))
            except ProviderFailure as error:
                if error.error.category != ErrorCategory.AUTH_USER_HEADER_REQUIRED or previous_header is not None:
                    raise
                # 仅在远端明确要求此头时协商；ID 必须由用户配置，不能猜测。
                try:
                    self._user_header_id = str(positive_id(snapshot.credentials.account_id))
                except ProviderFailure:
                    raise error
                user = _object(self._call("/api/user/self", context, kind=self._kind))
            profile = self._account(user, context)  # 回包身份必须与预期 ID 一致，才保留用户头。
        except Exception:
            self._user_header_id = previous_header
            raise
        profile.update({key: user.get(key) for key in ("quota", "used_quota", "request_count", "aff_quota")})
        context.profile_cache[cache_key] = dict(profile)
        return profile

    def _daily_usage(self, context):
        """单日用量（北京时间今日）：金额取区间汇总，Token 取按小时聚合。

        这是增强项，任何失败都只记进 daily.error，**绝不让累计用量跟着失败**；
        共享请求预算还要留给账户刷新的其余部分，所以每次请求前都先留够余量。
        """
        date, start, end = daily_window()
        daily = {"date": date, "raw_quota": None, "tokens": None, "requests": None,
                 "source": None, "complete": False, "error": None}
        errors = []
        # 1) 金额：区间消耗额度，仍是站点额度单位，换算成美元由 service 用 quota_per_unit 完成
        if context.remaining() <= DAILY_RESERVE:
            errors.append("金额:预算不足")
        else:
            try:
                stat = _object(self._call(
                    f"/api/log/self/stat?start_timestamp={start}&end_timestamp={end}",
                    context, kind=self._kind))
                daily["raw_quota"] = number(stat.get("quota"))
            except (ProviderFailure, ValueError, TypeError, KeyError, AttributeError) as error:
                errors.append("金额" + _daily_reason(error))
        # 2) Token：优先按小时聚合表，一个请求就能拿到全天（站点须开启「数据导出」）
        rows = None
        if context.remaining() <= DAILY_RESERVE:
            errors.append("Token:预算不足")
        else:
            try:
                value = _unwrap(self._call(
                    f"/api/data/self?start_timestamp={start}&end_timestamp={end}",
                    context, kind=self._kind))
                rows = value if isinstance(value, list) else None
                if rows is None:
                    raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            except (ProviderFailure, ValueError, TypeError, KeyError, AttributeError) as error:
                errors.append("Token" + _daily_reason(error))
        if rows:
            daily.update(tokens=sum(_count(row.get("token_used")) for row in rows if isinstance(row, dict)),
                         requests=sum(_count(row.get("count")) for row in rows if isinstance(row, dict)),
                         source="data.self", complete=True)
        elif rows is not None:
            # 空数组有两种可能：当天没用量，或站点没开「数据导出」导致该表恒空。
            # 只有确实有用量（金额 > 0）时才值得翻明细兜底；金额也没取到就保持未知。
            if daily["raw_quota"] is None:
                errors.append("Token:无法判断是没用量还是站点未开数据导出")
            elif daily["raw_quota"] > 0:
                self._daily_from_logs(context, start, end, daily, errors)
            else:
                daily.update(tokens=0, requests=0, source="data.self", complete=True)
        daily["error"] = "；".join(errors) or None
        return daily

    def _daily_from_logs(self, context, start, end, daily, errors):
        """聚合表拿不到时的兜底：翻消耗明细累加输入/输出 Token。

        单页上限 100 条、最多 DAILY_LOG_PAGES 页，且每页前都给账户刷新留出预算余量；
        一旦截断就把 complete 标成 False，绝不把部分和当成全天用量。
        """
        tokens, quota, total = 0, 0, None
        for page in range(1, DAILY_LOG_PAGES + 1):
            if context.remaining() <= DAILY_RESERVE:
                errors.append("Token:预算不足")
                break
            try:
                value = _unwrap(self._call(
                    f"/api/log/self?p={page}&page_size={DAILY_LOG_PAGE_SIZE}&type=2"
                    f"&start_timestamp={start}&end_timestamp={end}",
                    context, kind=self._kind))
            except (ProviderFailure, ValueError, TypeError, KeyError, AttributeError) as error:
                errors.append("Token" + _daily_reason(error))
                return
            rows = value.get("items") if isinstance(value, dict) else None
            if not isinstance(rows, list):
                errors.append("Token:明细格式不符")
                return
            if total is None:
                total = _count(value.get("total")) or None
            if not rows:
                daily.update(tokens=tokens, requests=total if total is not None else 0,
                             source="log.self", complete=True)
                if daily.get("raw_quota") is None:
                    daily["raw_quota"] = quota
                return
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tokens += _count(row.get("prompt_tokens")) + _count(row.get("completion_tokens"))
                quota += _count(row.get("quota"))
            if len(rows) < DAILY_LOG_PAGE_SIZE:
                daily.update(tokens=tokens, requests=total if total is not None else len(rows),
                             source="log.self", complete=True)
                if daily.get("raw_quota") is None:
                    daily["raw_quota"] = quota
                return
        # 翻满上限仍有后续页：只给下界
        daily.update(tokens=tokens, requests=total, source="log.self", complete=False)
        if daily.get("raw_quota") is None:
            daily["raw_quota"] = quota
        errors.append(f"Token:超过{DAILY_LOG_PAGES * DAILY_LOG_PAGE_SIZE}条，只统计到部分")

    def _dispatch(self, feature, context, params):
        if feature == Feature.CCS_USAGE:
            from .ccs import newapi_usage
            wallet = self._dispatch(Feature.WALLET, context, {})
            token = self.session.snapshot(self.config_revision).credentials.management_token
            # 复用本轮已核验的身份，导入 CCS 后仍能访问要求用户头的 New API 分叉。
            usage = newapi_usage(base_url=self.base, token=token,
                                 quota_per_unit=wallet.get("quota_per_unit"), unit=wallet.get("unit"),
                                 user_id=getattr(self, "_user_header_id", None) or wallet.get("account_id"))
            if usage is None:
                raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
            return usage
        if feature in {Feature.PRICING_PUBLIC, Feature.PRICING_PERSONAL}:
            kind = AuthKind.NONE if feature == Feature.PRICING_PUBLIC else self._kind
            payload = self._call("/api/pricing", context, kind=kind)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            data = parse_newapi_pricing(self.site, payload)
            data.update(visibility_scope=context.visibility.value, raw=payload)
            return data
        if feature == Feature.PROFILE:
            return self._profile(context)
        if feature in {Feature.WALLET, Feature.USAGE}:
            profile = self._profile(context)
            if feature == Feature.USAGE:
                return {"usage_scope": "account", "raw_used_quota": profile.get("used_quota"),
                        "request_count": profile.get("request_count"),
                        "daily": self._daily_usage(context)}
            quota = number(profile.get("quota"))
            # 标准 New API 的 quota / quota_per_unit 是系统美元额度。
            # quota_display_type（USD/CNY/TOKENS/CUSTOM）只决定站点展示，不能用作额度币种。
            # 仍须读取本站的实际换算比例，不能套旧实现的固定 500000。
            try:
                status = _object(self._call("/api/status", context, kind=AuthKind.NONE))
            except ProviderFailure as error:
                if error.error.category in {ErrorCategory.CANCELLED, ErrorCategory.BUDGET_EXHAUSTED, ErrorCategory.CONFIG_CHANGED}:
                    raise
                status = {}
            scale = number(status.get("quota_per_unit"))
            declared_unit = status.get("quota_currency") or status.get("currency")
            if declared_unit is None:
                unit, unit_source = "USD", "newapi.quota_per_unit"
            else:
                unit = declared_unit.strip().upper() if isinstance(declared_unit, str) else "unknown"
                unit_source = "status.quota_currency" if status.get("quota_currency") else "status.currency"
            amount = quota / scale if quota is not None and scale is not None and scale > 0 and unit in {"USD", "CNY"} else None
            context.auth_kind, context.visibility = self._kind, VisibilityScope.ACCOUNT
            context.last_endpoint, context.account_id = "/api/user/self", profile["id"]
            return {"balance_kind": "wallet", "scope_id": f"{self.site_id}:account:{profile['id']}",
                    "account_id": profile["id"], "raw_amount": quota, "amount": amount,
                    "unit": unit if amount is not None else "unknown", "quota_per_unit": scale,
                    "unit_source": unit_source if amount is not None else None}
        if feature == Feature.TOPUPS:
            return {"items": self._pages("/api/user/topup/self", context, self._kind), "complete": True}
        if feature == Feature.GROUPS:
            return {"available": _unwrap(self._call("/api/user/self/groups", context, kind=self._kind))}
        if feature == Feature.KEYS_LIST:
            rows = self._pages("/api/token/", context, self._kind)
            snapshot = self.session.snapshot(self.config_revision)
            context.key_list_cache[(self.session, snapshot.config_revision, snapshot.token_version)] = rows
            return {"items": [_public_key(row) for row in rows], "complete": True, "total": len(rows)}
        if feature == Feature.KEYS_REVEAL:
            remote_id = positive_id(params.get("id"))
            row = _object(self._call(f"/api/token/{remote_id}/key", context, kind=self._kind, method="POST", body={}))
            return {"id": remote_id, "key": _plain_key(row, newapi=True)}
        if feature in WRITE_FEATURES:
            return self._key_write(feature, context, params)
        if feature in {Feature.HEALTH_SUMMARY, Feature.HEALTH_DETAIL}:
            path = "/api/perf-metrics/summary" if feature == Feature.HEALTH_SUMMARY else "/api/perf-metrics"
            query = {"hours": 24}
            if feature == Feature.HEALTH_SUMMARY and "recent_models" in params:
                names = params["recent_models"]
                if (not isinstance(names, list) or len(names) > 50
                        or any(not isinstance(name, str) or not name.strip() or len(name) > 512 for name in names)):
                    raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
                query["recent_model"] = list(dict.fromkeys(names))
            if feature == Feature.HEALTH_DETAIL:
                if not isinstance(params.get("model"), str) or not params["model"].strip():
                    raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
                query["model"] = params["model"]
            credentials = self.session.snapshot(self.config_revision).credentials
            kind = self._kind if credentials.management_token else AuthKind.NONE
            value = _object(self._call(path + "?" + urllib.parse.urlencode(query, doseq=True), context, kind=kind))
            if feature == Feature.HEALTH_DETAIL and (value.get("model_name", params["model"]) != params["model"]
                    or not isinstance(value.get("groups"), list)):
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            if feature == Feature.HEALTH_SUMMARY and not isinstance(value.get("models"), list):
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            return value
        raise ProviderFailure(ErrorCategory.NOT_MIGRATED)

    def _key_write(self, feature, context, params):
        if feature == Feature.KEYS_CREATE:
            if set(params) - {"name", "group"}:
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            name, group = params.get("name"), params.get("group", "")
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 50 or not isinstance(group, str):
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            # C 暂限安全默认创建；高级额度换算及界面输入由 D 按单位验证后再映射。
            body = {"name": name.strip(), "group": group, "remain_quota": 0, "unlimited_quota": True,
                    "expired_time": -1, "model_limits_enabled": False, "model_limits": "", "allow_ips": ""}
            snapshot = self.session.snapshot(self.config_revision)
            before = context.key_list_cache.pop((self.session, snapshot.config_revision, snapshot.token_version), None)
            if before is None:
                before = self._pages("/api/token/", context, self._kind)
            payload, operation = self._write(feature, "/api/token/", context, kind=self._kind, method="POST", body=body,
                evidence={"expected": body, "before_ids": [str(row["id"]) for row in before]})
            value = _unwrap(payload)
            remote_id = value.get("id") if isinstance(value, dict) else None
            if remote_id is None:
                after = self._pages("/api/token/", context, self._kind)
                previous = {str(row["id"]) for row in before}
                # 已确认创建成功后，必须是新 ID 且所有创建参数吻合，不能仅按名称/最大 ID 猜。
                matches = [row for row in after if str(row["id"]) not in previous
                           and all(row.get(key) == value for key, value in body.items())]
                if len(matches) != 1:
                    raise ProviderFailure(ErrorCategory.OPERATION_UNKNOWN, {"operation_id": operation})
                remote_id = matches[0]["id"]
            remote_id = positive_id(remote_id)
            self._finish_write(operation, remote_id)
            # 有确认 ID 的创建回包可直接交付有效明文；掩码或缺失仍单独 reveal，绝不重复创建。
            record = {"id": remote_id, "name": name.strip(), "group": group, "created": True}
            if isinstance(value, dict) and str(value.get("id")) == str(remote_id):
                record["name"] = value.get("name") if isinstance(value.get("name"), str) else name.strip()
                try:
                    record["key"] = _plain_key(value, newapi=True)
                except ProviderFailure:
                    record["needs_reveal"] = True
            else:
                record["needs_reveal"] = True
            return record
        remote_id = positive_id(params.get("id"))
        if feature == Feature.KEYS_DELETE:
            _, operation = self._write(feature, f"/api/token/{remote_id}", context, kind=self._kind,
                                        method="DELETE", target_id=remote_id)
            rows = self._pages("/api/token/", context, self._kind)
            if any(str(row["id"]) == str(remote_id) for row in rows):
                raise ProviderFailure(ErrorCategory.OPERATION_UNKNOWN, {"operation_id": operation, "remote_id": remote_id})
        else:
            changes = params.get("changes")
            if not isinstance(changes, dict) or not changes or set(changes) - _NEWAPI_EDIT_FIELDS:
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            # status_only 与完整更新是上游两条语义，禁止用完整 PUT 把未编辑字段清零。
            current = _object(self._call(f"/api/token/{remote_id}", context, kind=self._kind))
            if str(current.get("id")) != str(remote_id):
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            if "status" in changes and len(changes) > 1:
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            status_only = set(changes) == {"status"}
            required = {"name", "group", "remain_quota", "unlimited_quota", "expired_time", "model_limits_enabled", "model_limits", "allow_ips", "cross_group_retry"}
            if not status_only and not required <= current.keys():
                raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
            body = {"id": remote_id, **{key: value for key, value in current.items() if key in _NEWAPI_EDIT_FIELDS}, **changes}
            _, operation = self._write(feature, "/api/token/" + ("?status_only=true" if status_only else ""), context,
                                       kind=self._kind, method="PUT", body=body, target_id=remote_id)
            confirmed = _object(self._call(f"/api/token/{remote_id}", context, kind=self._kind))
            if str(confirmed.get("id")) != str(remote_id) or any(confirmed.get(key) != value for key, value in changes.items()):
                raise ProviderFailure(ErrorCategory.OPERATION_UNKNOWN, {"operation_id": operation, "remote_id": remote_id})
        self._finish_write(operation, remote_id)
        return {"id": remote_id, "confirmed": True}


class Sub2ApiProvider(BaseProvider):
    platform = "sub2api"
    _kind = AuthKind.PANEL_TOKEN

    def __init__(self, *args, api_prefix="/api/v1", wallet_unit="USD", **kwargs):
        super().__init__(*args, **kwargs)
        if (not isinstance(api_prefix, str) or not api_prefix.startswith("/") or api_prefix.startswith("//")
                or any(ch in api_prefix for ch in "?#\\") or ".." in urllib.parse.unquote(api_prefix).split("/")):
            raise ValueError("api_prefix 无效")
        self.api_prefix = api_prefix.rstrip("/")
        if wallet_unit not in {"USD", "CNY", "unknown"}:
            raise ValueError("wallet_unit 无效")
        self.wallet_unit = wallet_unit

    def _known_unsupported(self, path, payload):
        if not isinstance(payload, dict):
            return False
        error = payload.get("error")
        code = payload.get("reason") or payload.get("error_code") or payload.get("code")
        return ((path.startswith(self.api_prefix + "/channel-monitor-v2/")
                 and code in {"CHANNEL_MONITOR_DISABLED", "CHANNEL_MONITOR_MODE_MISMATCH"})
                or (path == "/v1/sub2api/billing" and isinstance(error, dict)
                 and error.get("type") == "not_found_error"
                 and error.get("message") == "Billing information is not supported in simple mode")
                or (path == self.api_prefix + "/model-plaza" and payload.get("code") == 404
                    and payload.get("message") == "Model plaza is not enabled"))

    def _refresh(self, snapshot, context):
        def perform(credentials):
            payload = self._raw("POST", self.api_prefix + "/auth/refresh", {},
                                {"refresh_token": credentials.refresh_token}, context, auth_rotation=True)
            value = _object(payload)
            lifetime = number(value.get("expires_in"))
            try:
                access, refresh = _secret(value.get("access_token")), _secret(value.get("refresh_token"))
            except ValueError:
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR) from None
            if not access or not refresh or lifetime is None or lifetime <= 0:
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            return replace(credentials, access_token=access, refresh_token=refresh, expires_at=time.time() + lifetime)
        return self.session.refresh(snapshot, context, perform)

    def _profile(self, context):
        snapshot = self.session.snapshot(self.config_revision)
        cache_key = (self.session, snapshot.config_revision, snapshot.token_version)
        if cache_key in context.profile_cache:
            profile = dict(context.profile_cache[cache_key])
            context.auth_kind, context.visibility = self._kind, VisibilityScope.ACCOUNT
            context.last_endpoint, context.account_id = self.api_prefix + "/user/profile", profile["id"]
            return profile
        user = _object(self._call(self.api_prefix + "/user/profile", context, kind=self._kind))
        profile = self._account(user, context)
        profile.update(balance=number(user.get("balance")), frozen_balance=number(user.get("frozen_balance")))
        # 续期可能在 _call 内发生，用实际生效的凭据版本缓存。
        snapshot = self.session.snapshot(self.config_revision)
        context.profile_cache[(self.session, snapshot.config_revision, snapshot.token_version)] = dict(profile)
        return profile

    def _usage(self, context):
        payload = self._call("/v1/usage", context, kind=AuthKind.MODEL_KEY)
        return parse_sub2api_usage(payload, site_id=self.site_id,
            credentials=self.session.snapshot(self.config_revision).credentials)

    def _topups(self, context):
        from .sub2api_views import topup_records
        profile = self._profile(context)
        sources, failures = {}, []
        for name, path in (("payments", "/payment/orders/my"), ("redemptions", "/redeem/history")):
            try:
                rows = self._pages(self.api_prefix + path, context, self._kind, sub2=True, require_total=True)
                # 老版兑换历史忽略分页参数、只给 25 条数组，不能宣称完整。
                sources[name] = {"rows": rows, "complete": True, "error_category": None}
            except ProviderFailure as error:
                if error.error.category in {ErrorCategory.CANCELLED, ErrorCategory.BUDGET_EXHAUSTED,
                        ErrorCategory.ACCOUNT_MISMATCH, ErrorCategory.CONFIG_CHANGED,
                        ErrorCategory.AUTH_REQUIRED, ErrorCategory.AUTH_EXPIRED,
                        ErrorCategory.PERMISSION_DENIED, ErrorCategory.ACCESS_BLOCKED,
                        ErrorCategory.RATE_LIMITED, ErrorCategory.CREDENTIAL_SAVE_FAILED}:
                    raise
                sources[name] = {"rows": [], "complete": False, "error_category": error.error.category.value}
                failures.append((error.error.category, context.error_http_status, context.error_response_format))
        context.account_id = profile["id"]
        data = topup_records(sources, account_id=profile["id"], unit=self.wallet_unit)
        if failures:
            category, context.error_http_status, context.error_response_format = failures[0]
            raise ProviderFailure(category if len(failures) == 2 else ErrorCategory.PARTIAL_DATA, data)
        return data

    def _health(self, feature, context, params):
        from .sub2api_views import v1_views, v2_details, v2_snapshot
        model = params.get("model")
        if feature == Feature.HEALTH_SUMMARY:
            names = params.get("recent_models")
            if not isinstance(names, list) or len(names) != 1:
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            model = names[0]
        if not isinstance(model, str) or not model.strip() or len(model) > 512:
            raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
        profile = self._profile(context)
        # 同一次 health_snapshot 内共用模式探测结果；不把错误缓存到后续刷新。
        cache_key = ("sub2-monitor-v1", self.session, self.config_revision)
        fallback = context.profile_cache.get(cache_key)
        if fallback is None:
            query = {"range": "24h", "model": model}
            action = "/snapshot" if feature == Feature.HEALTH_SUMMARY else "/matrix"
            if feature == Feature.HEALTH_DETAIL:
                query["group_by"] = "platform_group_model"
            try:
                value = _object(self._call(self.api_prefix + "/channel-monitor-v2" + action + "?" +
                                          urllib.parse.urlencode(query), context, kind=self._kind))
                context.account_id = profile["id"]
                return v2_snapshot(value, model) if feature == Feature.HEALTH_SUMMARY else v2_details(value, model)
            except ProviderFailure as error:
                # 只对路径不存在/明确未启用尝试旧监控；认证、限流和格式错误不换接口掩盖。
                if error.error.category not in {ErrorCategory.ENDPOINT_NOT_FOUND, ErrorCategory.UNSUPPORTED}:
                    raise
                fallback = _object(self._call(self.api_prefix + "/channel-monitors", context, kind=self._kind))
                context.profile_cache[cache_key] = fallback
        context.account_id = profile["id"]
        overview, detail = v1_views(fallback, model)
        return overview if feature == Feature.HEALTH_SUMMARY else detail

    def billing(self, *, context=None):
        context = context if context is not None else RequestContext()
        attempted = utc_now()
        try:
            payload = self._call("/v1/sub2api/billing", context, kind=AuthKind.MODEL_KEY)
            data = parse_key_billing(payload, site_id=self.site_id,
                credentials=self.session.snapshot(self.config_revision).credentials)
            return self._result(Feature.PRICING_PERSONAL, context, attempted, data=data)
        except ProviderFailure as error:
            # 404 只影响倍率能力，不影响钱包/广场，不能拿倍率作为钱包。
            return self._result(Feature.PRICING_PERSONAL, context, attempted,
                data={"kind": "key_multiplier"}, error=error.error.category)

    def _daily_from_stats(self, value):
        """站点口径的「今日」：同一个 stats 回包里就带 today_*，不需要额外请求。

        Token 只算输入 + 输出，与 New API 的 token_used 对齐（两边都不含缓存）；
        缓存单独存一份留给展开区。「今日」由站点自行切分，本地不推定时区。
        """
        daily = {"date": None, "amount": None, "unit": self.wallet_unit, "tokens": None,
                 "cache_tokens": None, "requests": None, "source": "dashboard.stats",
                 "complete": False, "error": None}
        try:
            amount = number(value.get("today_actual_cost"))
            input_tokens = number(value.get("today_input_tokens"))
            output_tokens = number(value.get("today_output_tokens"))
            requests = number(value.get("today_requests"))
            cache_tokens = (number(value.get("today_cache_creation_tokens")) or 0) + (
                number(value.get("today_cache_read_tokens")) or 0)
        except (ProviderFailure, ValueError, TypeError, KeyError, AttributeError):
            daily["error"] = "站点未返回今日用量"
            return daily
        if amount is None and input_tokens is None and output_tokens is None:
            daily["error"] = "站点未返回今日用量"
            return daily
        daily["amount"] = amount
        if input_tokens is not None or output_tokens is not None:
            daily["tokens"] = int((input_tokens or 0) + (output_tokens or 0))
        daily["cache_tokens"] = int(cache_tokens) if cache_tokens else 0
        daily["requests"] = int(requests) if requests is not None and requests.is_integer() else None
        daily["complete"] = daily["tokens"] is not None
        return daily

    def _dispatch(self, feature, context, params):
        if feature == Feature.CCS_USAGE:
            from .ccs import sub2api_usage
            key = _secret(params.get("model_key") or self.session.snapshot(self.config_revision).credentials.model_api_key)
            if not key:
                raise ProviderFailure(ErrorCategory.MISSING_CREDENTIAL)
            context.auth_kind, context.visibility = AuthKind.MODEL_KEY, VisibilityScope.KEY
            return sub2api_usage(base_url=self.base, key=key)
        if feature in {Feature.PRICING_PUBLIC, Feature.PRICING_PERSONAL}:
            kind = AuthKind.NONE if feature == Feature.PRICING_PUBLIC else self._kind
            payload = self._call(self.api_prefix + "/model-plaza", context, kind=kind)
            data = parse_sub2api_pricing(self.site, payload)
            data.update(visibility_scope=context.visibility.value, raw=_unwrap(payload))
            return data
        if feature == Feature.PROFILE:
            return self._profile(context)
        if feature == Feature.TOPUPS:
            return self._topups(context)
        if feature in {Feature.HEALTH_SUMMARY, Feature.HEALTH_DETAIL}:
            return self._health(feature, context, params)
        if feature == Feature.USAGE:
            scope = params.get("scope", "key")
            if scope == "key":
                return self._usage(context)
            if scope != "account":
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            profile = self._profile(context)
            value = _object(self._call(self.api_prefix + "/usage/dashboard/stats", context, kind=self._kind))
            amount = number(value.get("total_actual_cost"))
            requests = number(value.get("total_requests"))
            if amount is None or requests is None:
                raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
            if amount < 0 or requests < 0 or not requests.is_integer():
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            context.account_id = profile["id"]
            return {"usage_scope": "account", "period": "all_time", "account_id": profile["id"],
                    "actual_cost": amount, "unit": self.wallet_unit, "request_count": int(requests),
                    "amount_source": "dashboard.total_actual_cost",
                    "daily": self._daily_from_stats(value)}
        if feature == Feature.WALLET:
            credentials = self.session.snapshot(self.config_revision).credentials
            if (credentials.model_binding_verified and credentials.account_id is not None
                    and str(credentials.model_account_id) != str(credentials.account_id)):
                raise ProviderFailure(ErrorCategory.ACCOUNT_MISMATCH)
            if credentials.model_api_key:
                try:
                    usage = self._usage(context)
                except ProviderFailure as error:
                    if error.error.category not in {ErrorCategory.AUTH_REQUIRED, ErrorCategory.AUTH_EXPIRED,
                            ErrorCategory.ENDPOINT_NOT_FOUND, ErrorCategory.UNSUPPORTED} or not (
                            credentials.access_token or credentials.refresh_token):
                        raise
                    usage = {"balance_kind": "unknown", "amount": None, "account_id": None}
                if usage["balance_kind"] == "wallet" and usage["amount"] is not None and usage["account_id"] is not None:
                    if credentials.account_id is not None and str(credentials.account_id) != str(usage["account_id"]):
                        raise ProviderFailure(ErrorCategory.ACCOUNT_MISMATCH)
                    return usage
                if not credentials.access_token and not credentials.refresh_token:
                    raise ProviderFailure(ErrorCategory.PARTIAL_DATA, {"key_usage": usage})
            profile = self._profile(context)
            # 官方 profile.balance 是美元钱包；分叉未确认时可配置为 unknown，不能与 Key 额度相加。
            return {"balance_kind": "wallet", "scope_id": f"{self.site_id}:account:{profile['id']}",
                    "account_id": profile["id"], "amount": profile["balance"] if self.wallet_unit != "unknown" else None,
                    "raw_amount": profile["balance"], "unit": self.wallet_unit,
                    "frozen_balance": profile["frozen_balance"]}
        if feature == Feature.GROUPS:
            available = _unwrap(self._call(self.api_prefix + "/groups/available", context, kind=self._kind))
            rates = _unwrap(self._call(self.api_prefix + "/groups/rates", context, kind=self._kind))
            if not isinstance(available, list) or not isinstance(rates, dict):
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            return {"available": available, "personal_rates": rates}
        if feature == Feature.KEYS_LIST:
            rows = self._pages(self.api_prefix + "/keys", context, self._kind, sub2=True)
            return {"items": [_public_key(row) for row in rows], "complete": True, "total": len(rows)}
        if feature == Feature.KEYS_REVEAL:
            remote_id = positive_id(params.get("id"))
            value = _object(self._call(self.api_prefix + f"/keys/{remote_id}", context, kind=self._kind))
            if str(value.get("id")) != str(remote_id):
                raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
            return {"id": remote_id, "key": _plain_key(value)}
        if feature in WRITE_FEATURES:
            return self._key_write(feature, context, params)
        # 不把未知功能转调 New API。
        raise ProviderFailure(ErrorCategory.NOT_MIGRATED)

    def _key_write(self, feature, context, params):
        if feature == Feature.KEYS_CREATE:
            if set(params) - {"name", "group_id"}:
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            name = params.get("name")
            group_id = positive_id(params.get("group_id"))
            if not isinstance(name, str) or not name.strip():
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            body = {"name": name.strip(), "group_id": group_id, "quota": 0}
            payload, operation = self._write(feature, self.api_prefix + "/keys", context,
                kind=self._kind, method="POST", body=body)
            value = _object(payload)
            remote_id = positive_id(value.get("id"))
            self._finish_write(operation, remote_id)
            record = _public_key(value)
            record.update(created=True)
            try:
                record["key"] = _plain_key(value)
            except ProviderFailure:
                record["needs_reveal"] = True
            return record
        remote_id = positive_id(params.get("id"))
        if feature == Feature.KEYS_DELETE:
            _, operation = self._write(feature, self.api_prefix + f"/keys/{remote_id}", context,
                kind=self._kind, method="DELETE", target_id=remote_id)
            rows = self._pages(self.api_prefix + "/keys", context, self._kind, sub2=True)
            if any(str(row["id"]) == str(remote_id) for row in rows):
                raise ProviderFailure(ErrorCategory.OPERATION_UNKNOWN, {"operation_id": operation, "remote_id": remote_id})
        else:
            changes = params.get("changes")
            if not isinstance(changes, dict) or not changes or set(changes) - _SUB2_EDIT_FIELDS:
                raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
            payload, operation = self._write(feature, self.api_prefix + f"/keys/{remote_id}", context,
                kind=self._kind, method="PUT", body=changes, target_id=remote_id)
            value = _object(payload)
            if str(value.get("id")) != str(remote_id) or any(value.get(key) != val for key, val in changes.items()):
                raise ProviderFailure(ErrorCategory.OPERATION_UNKNOWN, {"operation_id": operation, "remote_id": remote_id})
        self._finish_write(operation, remote_id)
        return {"id": remote_id, "confirmed": True}
