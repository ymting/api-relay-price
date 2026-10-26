"""可选的旧 Cookie／账号密码入口：只在策略允许时才被导入和启用。

旧登录与价格解析器仍然可用。Key 写操作复用与 API 提供者相同的
"只发一次 + 持久化钩子"机制，但要求一个已核实的旧会话身份。
"""
from __future__ import annotations

from dataclasses import replace
import json
import threading
import time
import urllib.request

from .contracts import (AuthKind, ErrorCategory, Feature, FeatureResult, FeatureStatus,
                        ProviderKind, VisibilityScope, classify_response, utc_now)
from .providers import (CredentialSession, Credentials, HttpResponse, NewApiProvider,
                        ProviderFailure, _NoRedirect)


class CompatProvider:
    def __init__(self, site, *, proxy="", config_revision=0, write_hooks=None, legacy=None):
        if legacy is None:
            import fetch_prices as legacy
        self.legacy, self.site, self.proxy = legacy, site, proxy
        self.config_revision, self.write_hooks = config_revision, write_hooks
        self._lock, self._session, self._expires = threading.RLock(), None, 0

    def _client(self, context, cookie=""):
        legacy = self.legacy
        class Client(legacy.HttpClient):
            fatal = None

            def request(inner, url, method="GET", body=None, headers=None):
                if inner.fatal:
                    raise ProviderFailure(inner.fatal)
                if not getattr(inner, "_precharged", False):
                    context.consume()
                inner._precharged = False
                inner.timeout, inner.tries = min(12, context.remaining()), 1
                try:
                    code, raw = super(Client, inner).request(url, method, body, headers)
                except legacy.FetchError:
                    raise ProviderFailure(ErrorCategory.NETWORK_ERROR) from None
                context.check()
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = None
                error = classify_response(code, payload, body=raw)
                if error and error.category not in {ErrorCategory.AUTH_REQUIRED, ErrorCategory.AUTH_EXPIRED,
                                                    ErrorCategory.ENDPOINT_NOT_FOUND, ErrorCategory.METHOD_NOT_ALLOWED}:
                    inner.fatal = error
                    raise ProviderFailure(error)
                return code, raw
        client = Client(proxy=self.proxy, cookie=cookie, timeout=12, tries=1)
        client.opener.add_handler(_NoRedirect())
        return client

    def _login(self, context):
        if self._session is not None and self._expires > time.monotonic():
            self._session[0]._context = context
            return self._session
        auth = self.site.get("auth") or {}
        base = self.site["base_url"].rstrip("/")
        if auth.get("cookie"):
            client = self._client(context, auth["cookie"])
            user, _, _ = self.legacy._self_user(client, base)
            if user:
                self._session = client, base, AuthKind.COOKIE, user
        if self._session is None and auth.get("username") and auth.get("password"):
            client = self._client(context)
            self.legacy.newapi_login(client, base, auth["username"], auth["password"])
            user, _, _ = self.legacy._self_user(client, base)
            if user:
                self._session = client, base, AuthKind.PASSWORD, user
        if self._session is None:
            raise ProviderFailure(ErrorCategory.AUTH_REQUIRED if auth.get("cookie") or auth.get("username")
                                  else ErrorCategory.MISSING_CREDENTIAL)
        self._expires = time.monotonic() + 120
        return self._session

    def fetch(self, feature, *, context, expected_account=None, **params):
        feature, attempted = Feature(feature), utc_now()
        account_id, auth_kind = None, AuthKind.NONE
        try:
            with self._lock:
                context.check()
                if self.site.get("type", "newapi") != "newapi":
                    if feature != Feature.PRICING_PUBLIC:
                        raise ProviderFailure(ErrorCategory.NOT_MIGRATED)
                    data = self.legacy.Sub2ApiAdapter(self.site, self._client(context)).fetch()
                    return FeatureResult(feature, FeatureStatus.SUCCESS, ProviderKind.COMPAT, data=data,
                                         visibility_scope=VisibilityScope.PUBLIC, last_success_at=utc_now(),
                                         config_revision=self.config_revision)
                if feature == Feature.PRICING_PUBLIC and not (self.site.get("auth") or {}).get("cookie") and not (self.site.get("auth") or {}).get("username"):
                    data = self.legacy.NewApiAdapter(self.site, self._client(context)).fetch()
                    return FeatureResult(feature, FeatureStatus.SUCCESS, ProviderKind.COMPAT, data=data,
                                         visibility_scope=VisibilityScope.PUBLIC, last_success_at=utc_now(),
                                         config_revision=self.config_revision)
                # Budget is per operation; cached clients must bind the new context.
                # Rebuild only the transport wrapper; preserve cookie jar and auth headers.
                if self._session and self._expires > time.monotonic():
                    old, base, auth_kind, user = self._session
                    client = self._client(context)
                    client.auth_header = dict(old.auth_header)
                    client.cookie_jar = old.cookie_jar
                    client.opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy} if self.proxy else {}),
                        urllib.request.HTTPCookieProcessor(client.cookie_jar), _NoRedirect())
                    self._session = client, base, auth_kind, user
                else:
                    self._session = None
                client, base, auth_kind, user = self._login(context)
                account_id = user.get("id")
                declared = (self.site.get("auth") or {}).get("account_id")
                if account_id is None or any(v is not None and str(v) != str(account_id) for v in (expected_account, declared)):
                    raise ProviderFailure(ErrorCategory.ACCOUNT_MISMATCH)

                class Transport:
                    def request(inner, method, url, *, headers, body, timeout):
                        client._precharged = True  # BaseProvider._raw already charged this network call.
                        code, raw = client.request(url, method, None if body is None else json.dumps(body).encode(),
                                                   {"Content-Type": "application/json"})
                        try:
                            payload = json.loads(raw)
                        except ValueError:
                            payload = None
                        return HttpResponse(code, payload, {}, raw)

                credentials = Credentials(management_token="legacy-session", account_id=account_id, account_verified=True)
                provider = NewApiProvider(self.site, CredentialSession(credentials, self.config_revision),
                                          config_revision=self.config_revision, transport=Transport(),
                                          write_hooks=self.write_hooks, read_retries=0)
                result = provider.fetch(feature, context=context, **params)
                if feature == Feature.CCS_USAGE and result.ok:
                    authorization = client.auth_header.get("Authorization", "")
                    token = authorization[7:] if authorization.startswith("Bearer ") else ""
                    if token:
                        result = replace(result, data={**result.data, "access_token": token})
                    else:
                        result = replace(result, status=FeatureStatus.PARTIAL, data=None,
                                         error_category=ErrorCategory.PARTIAL_DATA, last_success_at=None)
                if not result.ok:
                    self._session = None
                return replace(result, provider=ProviderKind.COMPAT, auth_kind=auth_kind, account_id=account_id,
                    visibility_scope=VisibilityScope.ACCOUNT if feature in {Feature.PRICING_PUBLIC, Feature.PRICING_PERSONAL}
                    else result.visibility_scope)
        except ProviderFailure as error:
            category = error.error.category
        except self.legacy.AuthError as error:
            category = {"turnstile": ErrorCategory.ACCESS_BLOCKED, "need_verify": ErrorCategory.SECOND_FACTOR_REQUIRED,
                        "banned": ErrorCategory.ACCOUNT_DISABLED}.get(getattr(error, "kind", ""), ErrorCategory.AUTH_REQUIRED)
        except self.legacy.FetchError:
            category = ErrorCategory.PROTOCOL_ERROR
        self._session = None
        status = FeatureStatus.NOT_MIGRATED if category == ErrorCategory.NOT_MIGRATED else FeatureStatus.ERROR
        if category == ErrorCategory.CANCELLED:
            status = FeatureStatus.CANCELLED
        return FeatureResult(feature, status, ProviderKind.COMPAT, error_category=category,
                             attempted_at=attempted, account_id=account_id, auth_kind=auth_kind,
                             config_revision=self.config_revision)
