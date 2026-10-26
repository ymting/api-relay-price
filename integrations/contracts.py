"""统一接入契约（阶段 B），仅依赖标准库，无网络、配置读写或提供者导入。

枚举值是持久化格式，不使用中文展示文案作为存储值。响应分类只判断
传输/业务信封；具体字段、单位及分页完整性由平台提供者另行验证。
权限表依据项目接入方案 v1.2，表示所需权限，不代表目标部署已支持。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
NEWAPI_PERMISSION_SPEC = "project-plan-v1.2-2026-10-15"


class Feature(str, Enum):
    PRICING_PUBLIC = "pricing.public"
    PRICING_PERSONAL = "pricing.personal"
    WALLET = "wallet"
    PROFILE = "profile"
    USAGE = "usage"
    TOPUPS = "topups"
    GROUPS = "groups"
    KEYS_LIST = "keys.list"
    KEYS_REVEAL = "keys.reveal"
    KEYS_CREATE = "keys.create"
    KEYS_UPDATE = "keys.update"
    KEYS_DELETE = "keys.delete"
    HEALTH_SUMMARY = "health.summary"
    HEALTH_DETAIL = "health.detail"
    CCS_USAGE = "ccs.usage"


# 按业务副作用区分，取 Key 明文虽为 POST，仍不属于创建/修改/删除。
WRITE_FEATURES = frozenset({Feature.KEYS_CREATE, Feature.KEYS_UPDATE, Feature.KEYS_DELETE})


class ProviderKind(str, Enum):
    API = "api"
    COMPAT = "compat"


class AuthKind(str, Enum):
    NONE = "none"
    MANAGEMENT_TOKEN = "management_token"
    PANEL_TOKEN = "panel_token"
    MODEL_KEY = "model_key"
    BROWSER_SESSION = "browser_session"
    COOKIE = "cookie"
    PASSWORD = "password"


class VisibilityScope(str, Enum):
    PUBLIC = "public"
    ACCOUNT = "account"
    KEY = "key"
    UNKNOWN = "unknown"


class FeatureStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"
    NOT_MIGRATED = "not_migrated"
    CANCELLED = "cancelled"


class CapabilityStatus(str, Enum):
    UNPROBED = "unprobed"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    AUTH_REQUIRED = "auth_required"
    BLOCKED = "blocked"
    TEMPORARY_FAILURE = "temporary_failure"
    NOT_MIGRATED = "not_migrated"


class MigrationStatus(str, Enum):
    NOT_MIGRATED = "not_migrated"
    MIGRATED = "migrated"


class OperationStatus(str, Enum):
    """写操作记录所处的阶段（方案第十六节的 prepared／sent／confirmed／unknown）。"""
    PREPARED = "prepared"
    SENT = "sent"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


# 仍未确认、必须先对账的阶段。读取/判定"未确认写操作"一律用这个集合，
# 不要在写操作记录、兼容退出闸门、服务端接口里各写一份。
UNRESOLVED_OPERATION_STATUSES = frozenset({OperationStatus.PREPARED.value,
                                           OperationStatus.SENT.value,
                                           OperationStatus.UNKNOWN.value})


class ConnectionMode(str, Enum):
    API_PREFERRED = "api_preferred"
    API_ONLY = "api_only"
    COMPAT_ONLY = "compat_only"


class FallbackReason(str, Enum):
    MISSING_CREDENTIAL = "missing_credential"
    NOT_MIGRATED = "not_migrated"
    UNSUPPORTED_ENDPOINT = "unsupported_endpoint"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    AUTH_EXPIRED = "auth_expired"
    SESSION_REQUIRED = "session_required"


class ErrorCategory(str, Enum):
    MISSING_CREDENTIAL = "MISSING_CREDENTIAL"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    ACCESS_TOKEN_SCOPE_DENIED = "ACCESS_TOKEN_SCOPE_DENIED"
    AUTH_SESSION_REQUIRED = "AUTH_SESSION_REQUIRED"
    AUTH_USER_HEADER_REQUIRED = "AUTH_USER_HEADER_REQUIRED"
    INVALID_CREDENTIAL = "INVALID_CREDENTIAL"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SECOND_FACTOR_REQUIRED = "SECOND_FACTOR_REQUIRED"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    ACCESS_BLOCKED = "ACCESS_BLOCKED"
    ENDPOINT_NOT_FOUND = "ENDPOINT_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    UNSUPPORTED = "UNSUPPORTED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    BUSINESS_ERROR = "BUSINESS_ERROR"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    PARTIAL_DATA = "PARTIAL_DATA"
    NOT_MIGRATED = "NOT_MIGRATED"
    OPERATION_UNKNOWN = "OPERATION_UNKNOWN"
    CANCELLED = "CANCELLED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    COMPAT_DISABLED = "COMPAT_DISABLED"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CREDENTIAL_SAVE_FAILED = "CREDENTIAL_SAVE_FAILED"
    OPERATION_SAVE_FAILED = "OPERATION_SAVE_FAILED"


_ERROR_MESSAGES = {
    ErrorCategory.MISSING_CREDENTIAL: "未配置此功能需要的凭据",
    ErrorCategory.AUTH_REQUIRED: "接口需要有效授权",
    ErrorCategory.AUTH_EXPIRED: "授权已过期",
    ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED: "管理令牌缺少此功能所需权限",
    ErrorCategory.AUTH_SESSION_REQUIRED: "此操作要求浏览器面板会话",
    ErrorCategory.AUTH_USER_HEADER_REQUIRED: "本站接口要求 New-Api-User 账户 ID 请求头，请填写预期账户 ID 并核对接口版本",
    ErrorCategory.INVALID_CREDENTIAL: "站点拒绝此令牌：访问令牌无效，请核对是否为该站的个人管理令牌",
    ErrorCategory.PERMISSION_DENIED: "当前凭据无权执行此功能",
    ErrorCategory.SECOND_FACTOR_REQUIRED: "此操作需要人工完成二次验证",
    ErrorCategory.ACCOUNT_DISABLED: "远端账户已停用或被封禁",
    ErrorCategory.ACCOUNT_MISMATCH: "凭据对应不同账户，已停止切换",
    ErrorCategory.ACCESS_BLOCKED: "请求被访问验证拦截，请人工验证",
    ErrorCategory.ENDPOINT_NOT_FOUND: "接口路径不存在或功能已关闭，部署支持待确认",
    ErrorCategory.METHOD_NOT_ALLOWED: "接口方法不受支持，版本适配待确认",
    ErrorCategory.UNSUPPORTED: "当前部署不支持此功能",
    ErrorCategory.RATE_LIMITED: "请求被限流，需遵循 Retry-After 和操作预算",
    ErrorCategory.TIMEOUT: "请求超时",
    ErrorCategory.NETWORK_ERROR: "网络请求失败",
    ErrorCategory.SERVER_ERROR: "远端服务暂时异常",
    ErrorCategory.BUSINESS_ERROR: "接口返回业务失败",
    ErrorCategory.PROTOCOL_ERROR: "接口响应结构不符合预期",
    ErrorCategory.PARTIAL_DATA: "读取结果不完整，保留上次成功数据",
    ErrorCategory.NOT_MIGRATED: "此功能尚未迁入统一提供者",
    ErrorCategory.OPERATION_UNKNOWN: "写操作结果不确定，需先对账，禁止自动重放",
    ErrorCategory.CANCELLED: "操作已取消",
    ErrorCategory.BUDGET_EXHAUSTED: "本次操作预算已用尽",
    ErrorCategory.COMPAT_DISABLED: "现有方式已停用",
    ErrorCategory.CONFIG_CHANGED: "配置已变化，已忽略旧操作结果",
    ErrorCategory.VALIDATION_ERROR: "请求参数不符合要求",
    ErrorCategory.CREDENTIAL_SAVE_FAILED: "续期凭据保存失败，请重新授权后再试",
    ErrorCategory.OPERATION_SAVE_FAILED: "写操作记录保存失败，未自动重放请求",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_copy(value: Any) -> Any:
    """快照只接受严格 JSON 值，拒绝 NaN/Infinity 和隐式字符串化。"""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _check_revision(value: str | int) -> None:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or value == "":
        raise ValueError("config_revision 必须为非空字符串或非负整数")
    if isinstance(value, int) and value < 0:
        raise ValueError("config_revision 不能为负数")


def _check_time(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError("时间必须是带时区的 ISO 字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")


def safe_endpoint(endpoint: str | None) -> str | None:
    """普通快照不携带查询参数、片段或 URL 内嵌凭据。"""
    if endpoint is None:
        return None
    if not isinstance(endpoint, str):
        raise ValueError("source_endpoint 必须为字符串或 null")
    parsed = urlsplit(endpoint)
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


_SECRET_FIELDS = frozenset({
    "key", "api_key", "apikey", "access_token", "accesstoken", "refresh_token", "refreshtoken",
    "management_token", "panel_token", "model_api_key", "password", "cookie", "authorization",
    "secret", "token", "session", "session_token", "set_cookie",
})


# 界面掩码的典型形态：连续三个及以上星号（sk-abc***wxyz）、省略号、整串星点、[REDACTED]。
# 不能一见星号就判死：真实密码里出现单个 '*' 完全合法（实测某站点密码就以 '*' 结尾），
# 早先用 `"*" in value` 判定会把真实凭据当成掩码，导致配置保存与整份备份还原都被拒。
_MASK_RUN = re.compile(r"\*{3,}")
_MASK_SYMBOLS = frozenset("*•·")


def looks_like_secret_mask(value: Any) -> bool:
    """判断凭据值是否只是界面上的掩码占位，而不是真实凭据。

    只认真正的掩码形态，业务代码里凡是"不许把掩码当凭据"的地方都调这里，
    避免各处各自写一份"含星号即掩码"的粗糙判断。
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    if text.upper() == "[REDACTED]" or "…" in text:
        return True
    if _MASK_RUN.search(text):
        return True
    return set(text) <= _MASK_SYMBOLS


def _snapshot_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if key.lower().replace("-", "_") in _SECRET_FIELDS
                      else _snapshot_data(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_snapshot_data(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class FeatureResult:
    feature: Feature
    status: FeatureStatus
    provider: ProviderKind
    data: Any = field(default=None, repr=False)
    auth_kind: AuthKind = AuthKind.NONE
    source_endpoint: str | None = None
    visibility_scope: VisibilityScope = VisibilityScope.UNKNOWN
    account_id: str | int | None = None
    last_success_at: str | None = None
    attempted_at: str = field(default_factory=utc_now)
    stale: bool = False
    error_category: ErrorCategory | None = None
    fallback_reason: FallbackReason | None = None
    config_revision: str | int = 0
    served_from_cache: bool = False
    error_http_status: int | None = None
    error_response_format: str | None = None

    def __post_init__(self):
        for name, enum in (("feature", Feature), ("status", FeatureStatus), ("provider", ProviderKind),
                           ("auth_kind", AuthKind), ("visibility_scope", VisibilityScope)):
            object.__setattr__(self, name, enum(getattr(self, name)))
        if self.error_category is not None:
            object.__setattr__(self, "error_category", ErrorCategory(self.error_category))
        if self.fallback_reason is not None:
            object.__setattr__(self, "fallback_reason", FallbackReason(self.fallback_reason))
        _check_revision(self.config_revision)
        _check_time(self.attempted_at)
        _check_time(self.last_success_at)
        if self.error_http_status is not None and (type(self.error_http_status) is not int
                or not 100 <= self.error_http_status <= 599):
            raise ValueError("error_http_status 必须为 HTTP 状态码或 null")
        if self.error_response_format not in (None, "html", "json", "other"):
            raise ValueError("error_response_format 无效")
        if self.attempted_at is None:
            raise ValueError("attempted_at 不能为空")
        if self.account_id is not None and (isinstance(self.account_id, bool)
                                            or not isinstance(self.account_id, (str, int))):
            raise ValueError("account_id 必须为字符串、整数或 null")
        for name in ("stale", "served_from_cache"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} 必须为布尔值")
        if self.status == FeatureStatus.SUCCESS:
            if self.error_category is not None or self.last_success_at is None:
                raise ValueError("成功结果需要成功时间，且不能包含失败类别")
        elif self.error_category is None:
            raise ValueError("非成功结果必须有失败类别")
        specific_errors = {FeatureStatus.NOT_MIGRATED: ErrorCategory.NOT_MIGRATED,
                           FeatureStatus.UNSUPPORTED: ErrorCategory.UNSUPPORTED,
                           FeatureStatus.CANCELLED: ErrorCategory.CANCELLED}
        if self.status in specific_errors and self.error_category != specific_errors[self.status]:
            raise ValueError("功能状态与失败类别不一致")
        if (self.stale or self.served_from_cache) and self.last_success_at is None:
            raise ValueError("旧数据/缓存结果必须保留真实成功时间")
        object.__setattr__(self, "source_endpoint", safe_endpoint(self.source_endpoint))
        object.__setattr__(self, "data", _json_copy(self.data))

    @property
    def ok(self) -> bool:
        return self.status == FeatureStatus.SUCCESS

    def to_dict(self) -> dict:
        """面向普通快照/诊断的安全格式。显式复制 Key 时由服务读取内存 data。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "feature": self.feature.value, "status": self.status.value,
            "data": _snapshot_data(_json_copy(self.data)), "provider": self.provider.value,
            "auth_kind": self.auth_kind.value, "source_endpoint": self.source_endpoint,
            "visibility_scope": self.visibility_scope.value, "account_id": self.account_id,
            "last_success_at": self.last_success_at, "attempted_at": self.attempted_at,
            "stale": self.stale,
            "error_category": self.error_category.value if self.error_category else None,
            "fallback_reason": self.fallback_reason.value if self.fallback_reason else None,
            "config_revision": self.config_revision,
            "served_from_cache": self.served_from_cache,
            "error_http_status": self.error_http_status,
            "error_response_format": self.error_response_format,
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> FeatureResult:
        if not isinstance(value, Mapping):
            raise ValueError("FeatureResult 必须为对象")
        version = value.get("schema_version", SCHEMA_VERSION)
        if type(version) is not int or version != SCHEMA_VERSION:
            raise ValueError("不支持的 FeatureResult schema_version")
        required = {"feature", "status", "provider", "attempted_at"}
        if not required <= value.keys():
            raise ValueError("FeatureResult 缺少 feature/status/provider/attempted_at")
        names = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in names if key in value})


@dataclass(frozen=True, slots=True)
class FeatureCapability:
    feature: Feature
    migration_status: MigrationStatus = MigrationStatus.NOT_MIGRATED
    remote_status: CapabilityStatus = CapabilityStatus.UNPROBED
    last_probe_at: str | None = None
    error_category: ErrorCategory | None = None
    config_revision: str | int = 0

    def __post_init__(self):
        object.__setattr__(self, "feature", Feature(self.feature))
        object.__setattr__(self, "migration_status", MigrationStatus(self.migration_status))
        object.__setattr__(self, "remote_status", CapabilityStatus(self.remote_status))
        if self.remote_status == CapabilityStatus.NOT_MIGRATED:
            raise ValueError("not_migrated 是本地迁移状态，不能写为远端能力")
        if self.error_category is not None:
            object.__setattr__(self, "error_category", ErrorCategory(self.error_category))
        _check_time(self.last_probe_at)
        _check_revision(self.config_revision)

    @property
    def status(self) -> CapabilityStatus:
        return (CapabilityStatus.NOT_MIGRATED if self.migration_status == MigrationStatus.NOT_MIGRATED
                else self.remote_status)

    def to_dict(self) -> dict:
        return {"feature": self.feature.value, "status": self.status.value,
                "migration_status": self.migration_status.value, "remote_status": self.remote_status.value,
                "last_probe_at": self.last_probe_at,
                "error_category": self.error_category.value if self.error_category else None,
                "config_revision": self.config_revision}

    @classmethod
    def from_dict(cls, value: Mapping) -> FeatureCapability:
        if not isinstance(value, Mapping) or "feature" not in value:
            raise ValueError("FeatureCapability 必须为包含 feature 的对象")
        names = cls.__dataclass_fields__.keys()
        result = cls(**{key: value[key] for key in names if key in value})
        if "status" in value and value["status"] != result.status.value:
            raise ValueError("能力展示状态与迁移/远端状态不一致")
        return result


@dataclass(frozen=True, slots=True)
class ConnectionPolicy:
    mode: ConnectionMode = ConnectionMode.API_PREFERRED
    compat_enabled: bool = True

    def __post_init__(self):
        object.__setattr__(self, "mode", ConnectionMode(self.mode))
        if not isinstance(self.compat_enabled, bool):
            raise ValueError("compat_enabled 必须为布尔值")

    @classmethod
    def from_dict(cls, value: Mapping | None) -> ConnectionPolicy:
        if value is None:
            return cls()
        if not isinstance(value, Mapping) or set(value) - {"mode", "compat_enabled"}:
            raise ValueError("connection_policy 格式或字段不正确")
        return cls(**value)

    def to_dict(self) -> dict:
        return {"mode": self.mode.value, "compat_enabled": self.compat_enabled}


@dataclass(frozen=True, slots=True)
class SiteConnectionPolicy:
    # None/空 features 表示继承，不能把当前全局模式复制进每个旧站点。
    mode: ConnectionMode | None = None
    features: Mapping[Feature, ConnectionMode] = field(default_factory=dict)

    def __post_init__(self):
        if self.mode is not None:
            object.__setattr__(self, "mode", ConnectionMode(self.mode))
        if not isinstance(self.features, Mapping):
            raise ValueError("connection.features 必须为对象")
        object.__setattr__(self, "features", MappingProxyType({Feature(k): ConnectionMode(v)
                                                               for k, v in self.features.items()}))

    @classmethod
    def from_dict(cls, value: Mapping | None) -> SiteConnectionPolicy:
        if value is None:
            return cls()
        if not isinstance(value, Mapping) or set(value) - {"mode", "features"}:
            raise ValueError("connection 格式或字段不正确")
        return cls(**value)

    def to_dict(self) -> dict:
        result = {}
        if self.mode is not None:
            result["mode"] = self.mode.value
        if self.features:
            result["features"] = {key.value: value.value for key, value in self.features.items()}
        return result


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    category: ErrorCategory
    http_status: int | None = None
    retry_after: float | None = None

    def __post_init__(self):
        object.__setattr__(self, "category", ErrorCategory(self.category))
        if self.http_status is not None and (isinstance(self.http_status, bool)
                or not isinstance(self.http_status, int) or not 100 <= self.http_status <= 599):
            raise ValueError("http_status 必须为有效 HTTP 状态码或 null")
        if self.retry_after is not None and (not isinstance(self.retry_after, (int, float))
                or isinstance(self.retry_after, bool) or not math.isfinite(self.retry_after) or self.retry_after < 0):
            raise ValueError("retry_after 必须为非负有限秒数")

    @property
    def message(self) -> str:
        # 不回显响应正文、任意远端错误码或异常文本，以免它们包含凭据。
        return _ERROR_MESSAGES[self.category]

    @property
    def refresh_allowed(self) -> bool:
        # 仍需 C 阶段判断凭据是否可续期；本属性不执行续期，也不是重试次数。
        return self.category in {ErrorCategory.AUTH_REQUIRED, ErrorCategory.AUTH_EXPIRED}

    @property
    def retryable_read(self) -> bool:
        return self.category in {ErrorCategory.RATE_LIMITED, ErrorCategory.TIMEOUT,
                                 ErrorCategory.NETWORK_ERROR, ErrorCategory.SERVER_ERROR}

    @property
    def capability_status(self) -> CapabilityStatus:
        if self.category == ErrorCategory.UNSUPPORTED:
            return CapabilityStatus.UNSUPPORTED
        if self.category == ErrorCategory.ACCESS_BLOCKED:
            return CapabilityStatus.BLOCKED
        if self.category in {ErrorCategory.MISSING_CREDENTIAL, ErrorCategory.AUTH_REQUIRED,
                             ErrorCategory.AUTH_EXPIRED, ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED,
                             ErrorCategory.AUTH_SESSION_REQUIRED, ErrorCategory.AUTH_USER_HEADER_REQUIRED,
                             ErrorCategory.INVALID_CREDENTIAL, ErrorCategory.PERMISSION_DENIED,
                             ErrorCategory.SECOND_FACTOR_REQUIRED, ErrorCategory.ACCOUNT_DISABLED}:
            return CapabilityStatus.AUTH_REQUIRED
        if self.category in {ErrorCategory.RATE_LIMITED, ErrorCategory.TIMEOUT, ErrorCategory.NETWORK_ERROR,
                             ErrorCategory.SERVER_ERROR, ErrorCategory.BUSINESS_ERROR,
                             ErrorCategory.PROTOCOL_ERROR, ErrorCategory.PARTIAL_DATA}:
            return CapabilityStatus.TEMPORARY_FAILURE
        # 本地未迁移、404/405 候选、取消等不得污染远端能力。
        return CapabilityStatus.UNPROBED

    def to_dict(self) -> dict:
        return {"category": self.category.value, "message": self.message, "http_status": self.http_status,
                "retry_after": self.retry_after, "retryable_read": self.retryable_read,
                "refresh_allowed": self.refresh_allowed, "capability_status": self.capability_status.value}


def parse_retry_after(value: str | int | float | None, *, now: datetime | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            deadline = parsedate_to_datetime(str(value))
            reference = now if now is not None else datetime.now(timezone.utc)
            if deadline.tzinfo is None or reference.tzinfo is None:
                return None
            delay = (deadline - reference).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0.0, delay) if math.isfinite(delay) else None
    return delay if math.isfinite(delay) and delay >= 0 else None


_REMOTE_CODES = {
    "ACCESS_TOKEN_SCOPE_DENIED": ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED,
    "AUTH_SESSION_REQUIRED": ErrorCategory.AUTH_SESSION_REQUIRED,
    "AUTH_EXPIRED": ErrorCategory.AUTH_EXPIRED,
    "ACCESS_TOKEN_EXPIRED": ErrorCategory.AUTH_EXPIRED,
    "TOKEN_EXPIRED": ErrorCategory.AUTH_EXPIRED,
    "JWT_EXPIRED": ErrorCategory.AUTH_EXPIRED,
    "UNAUTHORIZED": ErrorCategory.AUTH_REQUIRED,
    "INVALID_TOKEN": ErrorCategory.AUTH_REQUIRED,
    "FORBIDDEN": ErrorCategory.PERMISSION_DENIED,
    "PERMISSION_DENIED": ErrorCategory.PERMISSION_DENIED,
    "MFA_REQUIRED": ErrorCategory.SECOND_FACTOR_REQUIRED,
    "TWO_FACTOR_REQUIRED": ErrorCategory.SECOND_FACTOR_REQUIRED,
    "VERIFICATION_REQUIRED": ErrorCategory.SECOND_FACTOR_REQUIRED,
    "ACCOUNT_DISABLED": ErrorCategory.ACCOUNT_DISABLED,
    "USER_BANNED": ErrorCategory.ACCOUNT_DISABLED,
}


def classify_response(http_status: int | None = None, payload: Any = None, *, body: str = "",
                      headers: Mapping[str, str] | None = None, schema_valid: bool | None = None,
                      exception: Exception | None = None, cancelled: bool = False,
                      endpoint_confirmed_unsupported: bool = False,
                      now: datetime | None = None) -> ErrorInfo | None:
    """纯分类，不发请求、不睡眠、不切通道。None 不是业务字段完整性证明。

    404/405 只有版本适配器确认前缀/版本后才可置 unsupported。
    retryable_read 仅供后续读取重试决策；写操作不能依据它自动重放。
    """
    if cancelled:
        return ErrorInfo(ErrorCategory.CANCELLED)
    header = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    html = ("text/html" in header.get("content-type", "").lower()
            or body.lstrip().lower().startswith(("<!doctype html", "<html")))
    low = body[:16384].lower()
    challenge = any(marker in low for marker in (
        "cf-chl-", "challenge-platform", "just a moment", "verify you are human",
        "checking your browser", "error code: 1020", 'cf-error-code">1020',
    ))
    if header.get("cf-mitigated", "").lower() == "challenge" or (html and challenge):
        return ErrorInfo(ErrorCategory.ACCESS_BLOCKED, http_status)
    if http_status == 429:
        return ErrorInfo(ErrorCategory.RATE_LIMITED, http_status,
                         parse_retry_after(header.get("retry-after"), now=now))
    if http_status is not None and 500 <= http_status <= 599:
        return ErrorInfo(ErrorCategory.SERVER_ERROR, http_status)
    if http_status in (404, 405):
        category = (ErrorCategory.UNSUPPORTED if endpoint_confirmed_unsupported else
                    ErrorCategory.ENDPOINT_NOT_FOUND if http_status == 404 else ErrorCategory.METHOD_NOT_ALLOWED)
        return ErrorInfo(category, http_status)
    if exception is not None:
        category = (ErrorCategory.TIMEOUT if isinstance(exception, TimeoutError)
                    or isinstance(getattr(exception, "reason", None), TimeoutError)
                    else ErrorCategory.NETWORK_ERROR if isinstance(exception, OSError)
                    else ErrorCategory.PROTOCOL_ERROR)
        return ErrorInfo(category, http_status)
    if payload is None and body and not html:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            pass
    envelope = payload if isinstance(payload, dict) else {}
    raw_error = envelope.get("error")
    error = raw_error if isinstance(raw_error, dict) else {}
    codes = (envelope.get("code"), envelope.get("error_code"), error.get("code"), error.get("type"))
    failed = ((http_status is not None and http_status >= 400) or envelope.get("success") is False
              or bool(raw_error) or any(isinstance(code, str) and code.upper() in _REMOTE_CODES for code in codes))
    if failed:
        for code in codes:
            if isinstance(code, str) and code.upper() in _REMOTE_CODES:
                return ErrorInfo(_REMOTE_CODES[code.upper()], http_status)
        # 旧版 New API 用固定文案表达鉴权错误。只识别已知含义，绝不回显任意正文。
        message = envelope.get("message")
        if not html and isinstance(message, str) and http_status in (200, 401, 403):
            if re.search(r"(?:未提供|缺少)\s*New-Api-User|New-Api-User\s+(?:header\s+)?is required", message, re.I):
                return ErrorInfo(ErrorCategory.AUTH_USER_HEADER_REQUIRED, http_status)
            if re.search(r"无权进行此操作[，,：:\s]*access\s*token\s*(?:无效|不合法)", message, re.I):
                return ErrorInfo(ErrorCategory.INVALID_CREDENTIAL, http_status)
    if http_status == 401:
        # 只有 JSON 接口认证失败才允许由凭据类型判断是否续期。
        return ErrorInfo(ErrorCategory.AUTH_REQUIRED if isinstance(payload, dict) and not html
                         else ErrorCategory.PROTOCOL_ERROR, http_status)
    if http_status == 403:
        # HTML 拒绝页不是账户 API 的权限结论，可能来自反代或站点访问控制。
        return ErrorInfo(ErrorCategory.ACCESS_BLOCKED if html else ErrorCategory.PERMISSION_DENIED, http_status)
    if http_status is None or not 200 <= http_status < 300:
        return ErrorInfo(ErrorCategory.BUSINESS_ERROR, http_status)
    if schema_valid is False or html:
        return ErrorInfo(ErrorCategory.PROTOCOL_ERROR, http_status)
    if "success" in envelope and not isinstance(envelope["success"], bool):
        return ErrorInfo(ErrorCategory.PROTOCOL_ERROR, http_status)
    if envelope.get("success") is False or raw_error or envelope.get("code", 0) not in (0, 200, None):
        return ErrorInfo(ErrorCategory.BUSINESS_ERROR, http_status)
    if (not isinstance(payload, (dict, list)) or payload == {}) and schema_valid is not True:
        return ErrorInfo(ErrorCategory.PROTOCOL_ERROR, http_status)
    return None


class PermissionStatus(str, Enum):
    UNVERIFIED = "unverified"
    ALLOWED = "allowed"
    DENIED = "denied"
    AUTH_REQUIRED = "auth_required"
    SESSION_REQUIRED = "session_required"
    UNKNOWN_ROUTE = "unknown_route"


class ScopeSource(str, Enum):
    UNVERIFIED = "unverified"
    USER_DECLARED = "user_declared"
    DICTIONARY = "dictionary"
    CATALOG = "catalog"
    VERIFIED_ASSIGNMENT = "verified_assignment"


@dataclass(frozen=True, slots=True)
class RoutePermission:
    known: bool = True
    required_scope: str | None = None
    browser_session: bool = False


@dataclass(frozen=True, slots=True)
class PermissionCheck:
    status: PermissionStatus
    required_scope: str | None = None
    error_category: ErrorCategory | None = None


# 仅静态路由需求，不读取 /scopes、/catalog，不推断当前令牌的实际授权。
_NEWAPI_RULES = (
    ("GET", r"/api/(?:pricing|perf-metrics(?:/summary)?)", None, False),
    ("GET", r"/api/user/(?:self(?:/groups)?|models)", "profile:read", False),
    ("GET", r"/api/user/topup/self", "wallet:read", False),
    ("GET", r"/api/token(?:/search|/auto-groups|/[1-9]\d*)?", "api_key:read", False),
    ("POST", r"/api/token/(?:[1-9]\d*/key|batch/keys)", "api_key:reveal", False),
    ("POST", r"/api/token(?:/batch)?", "api_key:write", False),
    ("PUT", r"/api/token", "api_key:write", False),
    ("DELETE", r"/api/token/[1-9]\d*", "api_key:write", False),
    ("GET", r"/api/user/access_tokens(?:/catalog|/scopes)?", None, True),
    ("POST", r"/api/user/access_tokens", None, True),
    ("PATCH", r"/api/user/access_tokens/[1-9]\d*", None, True),
    ("DELETE", r"/api/user/access_tokens/(?:[1-9]\d*|legacy)", None, True),
)


def newapi_route_permission(method: str, path: str) -> RoutePermission:
    path = urlsplit(path).path.rstrip("/") or "/"
    for verb, pattern, scope, session in _NEWAPI_RULES:
        if method.upper() == verb and re.fullmatch(pattern, path):
            return RoutePermission(required_scope=scope, browser_session=session)
    return RoutePermission(known=False)


def check_newapi_permission(method: str, path: str, *, auth_kind: AuthKind = AuthKind.MANAGEMENT_TOKEN,
                            scopes: tuple[str, ...] | list[str] | None = None,
                            scope_source: ScopeSource = ScopeSource.UNVERIFIED) -> PermissionCheck:
    """权限预检不证明认证成功；仅 verified_assignment 可用于判断已授予的 scope。

    用户输入、字典和目录都保持待验证。单次请求成功只能证明该路由可用，
    不能反推该令牌具有其他路由/写操作所需的完整权限。
    """
    auth_kind, scope_source = AuthKind(auth_kind), ScopeSource(scope_source)
    rule = newapi_route_permission(method, path)
    if not rule.known:
        return PermissionCheck(PermissionStatus.UNKNOWN_ROUTE)
    if rule.browser_session:
        if auth_kind not in {AuthKind.COOKIE, AuthKind.BROWSER_SESSION}:
            return PermissionCheck(PermissionStatus.SESSION_REQUIRED, error_category=ErrorCategory.AUTH_SESSION_REQUIRED)
        return PermissionCheck(PermissionStatus.UNVERIFIED)
    if auth_kind == AuthKind.NONE and rule.required_scope is None:
        # pricing/健康是否允许匿名取决于站点开关，权限表不能替站点作出拒绝判断。
        return PermissionCheck(PermissionStatus.UNVERIFIED)
    if auth_kind in {AuthKind.NONE, AuthKind.MODEL_KEY}:
        return PermissionCheck(PermissionStatus.AUTH_REQUIRED, rule.required_scope, ErrorCategory.AUTH_REQUIRED)
    if auth_kind != AuthKind.MANAGEMENT_TOKEN:
        return PermissionCheck(PermissionStatus.UNVERIFIED, rule.required_scope)
    if rule.required_scope is None or scope_source != ScopeSource.VERIFIED_ASSIGNMENT or scopes is None:
        return PermissionCheck(PermissionStatus.UNVERIFIED, rule.required_scope)
    if not isinstance(scopes, (tuple, list)) or not all(isinstance(scope, str) for scope in scopes):
        raise ValueError("scopes 必须为字符串列表，不能是权限字典或目录")
    if rule.required_scope in scopes:
        return PermissionCheck(PermissionStatus.ALLOWED, rule.required_scope)
    return PermissionCheck(PermissionStatus.DENIED, rule.required_scope, ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED)
