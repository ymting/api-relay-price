"""一次 API 尝试，外加至多一次按需触发的兼容尝试。"""
from __future__ import annotations

from dataclasses import replace
import time

from .contracts import (ConnectionPolicy, SiteConnectionPolicy, ConnectionMode, ErrorCategory,
                        Feature, FeatureResult, FeatureStatus, ProviderKind, FallbackReason,
                        WRITE_FEATURES)
from .providers import RequestContext

_FALLBACK = {
    ErrorCategory.MISSING_CREDENTIAL: FallbackReason.MISSING_CREDENTIAL,
    ErrorCategory.NOT_MIGRATED: FallbackReason.NOT_MIGRATED,
    ErrorCategory.UNSUPPORTED: FallbackReason.UNSUPPORTED_ENDPOINT,
    ErrorCategory.AUTH_EXPIRED: FallbackReason.AUTH_EXPIRED,
    ErrorCategory.AUTH_REQUIRED: FallbackReason.AUTH_EXPIRED,
}


def effective_mode(global_policy, site_policy, feature):
    return site_policy.features.get(Feature(feature), site_policy.mode or global_policy.mode)


class Router:
    def __init__(self, api, *, policy=None, site_policy=None, compat_factory=None, is_current=None,
                 matrix=None, trigger="manual"):
        self.api = api
        self.policy = policy or ConnectionPolicy()
        self.site_policy = site_policy or SiteConnectionPolicy()
        self.compat_factory = compat_factory if self.policy.compat_enabled else None
        self.is_current = is_current or (lambda: True)
        # 证据矩阵（可选）：只做本地记录，供「站点 × 功能」退出判据使用，不发任何请求。
        self.matrix = matrix
        self.trigger = trigger
        self.site_id = getattr(api, "site_id", None)
        self.attempts = []
        self._verified_account = None

    def _error(self, feature, category):
        return FeatureResult(feature, FeatureStatus.ERROR, ProviderKind.API,
                             error_category=category, config_revision=self.api.config_revision)

    def _record(self, feature, result, mode, started):
        """记录一次功能调用的证据：提供者、生效模式、触发源、成败、回退原因、耗时。

        记录失败绝不影响业务结果——EvidenceMatrix 内部已经吞掉所有异常。
        """
        if not result.ok and hasattr(self, "interactive_profile_cache"):
            # 短会话出现认证、配置或操作错误后，下一次交互重新核实身份。
            self.interactive_profile_cache.clear()
        if (not result.ok or feature in {Feature.KEYS_UPDATE, Feature.KEYS_DELETE}) and hasattr(self, "interactive_token_cache"):
            # 失败或修改/撤销后，创建时的名称、权限和明文均不得继续走快速导入。
            self.interactive_token_cache.clear()
        if self.matrix is not None and isinstance(self.site_id, (str, int)) \
                and not isinstance(self.site_id, bool):
            self.matrix.record(site_id=self.site_id, feature=feature, provider=result.provider,
                               ok=result.ok, mode=mode, trigger=self.trigger,
                               error_category=result.error_category,
                               error_http_status=result.error_http_status,
                               error_response_format=result.error_response_format,
                               fallback_reason=result.fallback_reason,
                               source_endpoint=result.source_endpoint, auth_kind=result.auth_kind,
                               elapsed_ms=(time.monotonic() - started) * 1000)
        return result

    def fetch(self, feature, *, context=None, **params):
        feature = Feature(feature)
        context = context or RequestContext(
            seconds=12 if feature in WRITE_FEATURES or hasattr(self, "interactive_profile_cache") else 25,
            profile_cache=getattr(self, "interactive_profile_cache", None))
        if self._verified_account is not None:
            context.verified_account = self._verified_account
        mode = effective_mode(self.policy, self.site_policy, feature)
        self.attempts = []
        started = time.monotonic()
        if not self.is_current():
            return self._record(feature, self._error(feature, ErrorCategory.CONFIG_CHANGED), mode, started)
        if mode == ConnectionMode.COMPAT_ONLY:
            if self.compat_factory is None:
                return self._record(feature, self._error(feature, ErrorCategory.COMPAT_DISABLED), mode, started)
            self.attempts.append("compat")
            result = self.compat_factory().fetch(feature, context=context, **params)
        else:
            self.attempts.append("api")
            credentials = self.api.session.snapshot(self.api.config_revision).credentials
            if (feature in WRITE_FEATURES and not credentials.account_verified
                    and (credentials.management_token or credentials.access_token or credentials.refresh_token)):
                identity = self.api.fetch(Feature.PROFILE, context=context)
                if not identity.ok:
                    return self._record(feature, identity.__class__(feature, identity.status, identity.provider,
                        error_category=identity.error_category, config_revision=identity.config_revision),
                        mode, started)
                context.verified_account = identity.account_id
                self._verified_account = identity.account_id
            result = self.api.fetch(feature, context=context, **params)
            if feature == Feature.PROFILE and result.ok:
                self._verified_account = result.account_id
            reason = _FALLBACK.get(result.error_category)
            if (mode == ConnectionMode.API_PREFERRED and self.compat_factory is not None and reason
                    and (feature not in WRITE_FEATURES or result.error_category in {
                        ErrorCategory.MISSING_CREDENTIAL, ErrorCategory.NOT_MIGRATED}) and not context.write_sent
                    and result.status != FeatureStatus.SUCCESS and self.is_current()):
                # A used private API credential must have an expected, verified identity.
                creds = self.api.session.snapshot(self.api.config_revision).credentials
                has_api_identity = bool(creds.management_token or creds.access_token or creds.refresh_token or creds.model_api_key)
                private = feature not in {Feature.PRICING_PUBLIC, Feature.HEALTH_SUMMARY, Feature.HEALTH_DETAIL}
                expected = result.account_id or (creds.account_id if creds.account_verified else None)
                if not private or not has_api_identity or expected is not None:
                    self.attempts.append("compat")
                    alternate = self.compat_factory().fetch(feature, context=context, expected_account=expected, **params)
                    if private and expected is not None and alternate.status == FeatureStatus.SUCCESS and str(alternate.account_id) != str(expected):
                        result = self._error(feature, ErrorCategory.ACCOUNT_MISMATCH)
                    else:
                        result = replace(alternate, fallback_reason=reason)
        if not self.is_current() and feature not in WRITE_FEATURES:
            return self._record(feature, self._error(feature, ErrorCategory.CONFIG_CHANGED), mode, started)
        return self._record(feature, result, mode, started)
