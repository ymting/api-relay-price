#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中转站模型价格采集器
====================

从多家 API 中转站抓取「模型 / 分组 / 价格」，归一化成统一结构，供前端比价页使用。

支持两类站点程序：
  1. new-api 及其二开（绝大多数中转站）
     公共接口 GET /api/pricing，返回 模型列表 + 分组倍率 + 厂商 + 可用分组。
  2. sub2api
     公共接口 GET /api/v1/model-plaza，返回「分组为中心」的价目表。

价格换算规则（对齐 new-api 前端 price.ts / dynamic-price.ts，已核对源码）：
  · 按量计费（quota_type = 0）
       input  = model_ratio × 2 × group_ratio      （USD / 1M tokens）
       output = input × completion_ratio
       cache_read  = input × cache_ratio
       cache_write = input × create_cache_ratio
  · 动态表达式计费（billing_mode = tiered_expr）
       表达式里 p/c/cr/cc 的系数本身就是 USD / 1M tokens，再乘分组倍率。
       按 len=0 解析低区/标准档；无法解释的表达式保留原文，不猜价格。
  · 按次计费（quota_type = 1）
       per_call = model_price × group_ratio        （USD / 次）

用法：
    python fetch_prices.py                      # 采集 sites.json 里所有 enabled 站点
    python fetch_prices.py --only novaai,luna   # 只采集指定站点
    python fetch_prices.py --debug              # 打印每个站点的诊断信息
"""

from __future__ import annotations

# 这几个是从接入层转出口的：本地服务用 collector.CC_APPS / collector.build_ccswitch_link，
# 回归脚本用 fp.CC_USAGE_SCRIPT / fp.CC_API_FORMAT_MIN_VERSION，故在本模块内看起来"未使用"。
from integrations.ccs import CC_APPS, CC_API_FORMAT_MIN_VERSION, CC_USAGE_SCRIPT, build_ccswitch_link

import argparse
import difflib
import http.cookiejar
from collections import Counter
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

# parse_tier_coefficients 同样是转出口：tests/test_stage_a.py 按 fp.parse_tier_coefficients 调用。
from integrations.pricing import (FetchError, to_float, canonical_model_name,
                                  parse_tier_coefficients, parse_newapi_pricing,
                                  parse_sub2api_pricing, BRACKET_PREFIX)
from integrations.providers import USER_AGENT, ACCEPT_LANGUAGE

if TYPE_CHECKING:      # 仅供类型注解；运行时按需导入，避免与新接入层互相循环导入
    from integrations.runtime import IntegrationRuntime

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
EXAMPLE_CONFIG = ROOT / "sites.example.json"
try:
    from integrations.deploy_config import DeployConfigError, load_deploy_config

    DEPLOY = load_deploy_config(root=ROOT)
    DEPLOY_ERROR = None
except DeployConfigError as exc:
    DEPLOY = None
    DEPLOY_ERROR = exc

_STATE_DIR = DEPLOY.state_dir if DEPLOY else ROOT
DEFAULT_CONFIG = _STATE_DIR / "sites.json"
SETTINGS_FILE = _STATE_DIR / "settings.json"
OUT_DIR = _STATE_DIR / "data"

# 额度单位换算：源码 common/constants.go 的 `QuotaPerUnit = 500 * 1000`
# 站点数据库里存的 quota / used_quota 都是「额度单位」，除以它就是美元。
QUOTA_PER_UNIT = 500 * 1000.0

# 充值记录里这些状态表示钱还没真正到账，统计「已充值」时要排除
TOPUP_FAIL_STATUSES = {
    "pending", "failed", "expired", "closed", "cancelled", "canceled",
    "refunded", "unpaid", "timeout",
}

class AuthError(FetchError):
    """账号密码登录失败或需要二次验证。

    kind 用来告诉前端「这属于哪一类过不去」，好给出对应的解决办法：
      turnstile      人机验证（滑块 / 验证码）
      password_off   站点禁用了密码登录
      password_enc   站点启用了密码加密登录
      need_verify    需要邮箱 / 2FA 二次验证
      bad_cred       账号或密码不对
      banned         账号被封
      auth           其他凭据问题
    """

    def __init__(self, message: str, kind: str = "auth"):
        super().__init__(message)
        self.kind = kind


# 这几类站点脚本一律过不去，只能靠人在浏览器里登录一次再拿 Cookie。
# 与其让用户看到一句含糊的「登录失败」，不如直接说明卡在哪、该干嘛。
COOKIE_ADVICE = "请在浏览器里登录一次（验证由你自己过），再把 cookie 整行复制进「站点配置」"
LOGIN_HINTS = {
    "turnstile": "站点开了人机验证（滑块 / 验证码），脚本过不去",
    "password_off": "站点禁用了密码登录",
    "password_enc": "站点启用了密码加密登录，脚本没法完成加密",
    "need_verify": "站点要求二次验证（邮箱验证码 / 2FA），脚本收不到验证码",
    "banned": "账号已被封禁",
    "bad_cred": "账号或密码不对",
}
# 这几类都属于「必须由人在浏览器里过一遍」，前端据此提示去拿 Cookie
NEEDS_HUMAN_LOGIN = {"turnstile", "password_off", "password_enc", "need_verify"}


# ---------------------------------------------------------------------------
# HTTP 客户端：带 Cookie 会话、可选代理、重试
# ---------------------------------------------------------------------------


class HttpClient:
    def __init__(self, proxy: str = "", timeout: int = 25, tries: int = 3, cookie: str = "",
                 stop_check: Callable[[], bool] | None = None):
        self.timeout = timeout
        self.tries = tries
        # 停止信号：设为 callable 后，每次重试前都会问一次，命中就不再发下一个请求。
        # 有了它，遇到超时重试的站点也能几秒内退出来，不必等整个站点跑完。
        self.stop_check = stop_check
        self.cookie_jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.cookie_jar)]
        # 代理为空时也要显式挂一个空 ProxyHandler：build_opener 默认会补一个读系统代理的
        # handler，不显式禁用的话「留空」就等价于跟随系统代理，直连模式会失效。
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
        self.opener = urllib.request.build_opener(*handlers)
        self.auth_header: dict[str, str] = {}
        # 用户直接贴的 Cookie 字符串优先级最高：登录会话失效时用它兜底
        if cookie:
            self.auth_header["Cookie"] = cookie.strip()

    def session_cookie(self) -> str:
        """把登录成功后 jar 里的 Cookie 拼成请求头格式，方便回写进配置复用。"""
        parts = []
        for c in self.cookie_jar:
            if c.value:
                parts.append(f"{c.name}={c.value}")
        return "; ".join(parts)

    def request(self, url: str, method: str = "GET", body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, str]:
        """返回 (HTTP 状态码, 响应文本)。4xx 直接返回不重试，5xx/网络错误重试。"""
        hdr = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "identity",  # 避免 gzip，省一层解压依赖
            "Accept-Language": ACCEPT_LANGUAGE,
            **self.auth_header,
            **(headers or {}),
        }
        last_err: Exception | None = None
        for attempt in range(self.tries):
            if self.stop_check and self.stop_check():
                raise FetchError("已手动停止")
            req = urllib.request.Request(url, data=body, headers=hdr, method=method)
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    return resp.status, resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                raw = ""
                try:
                    raw = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                # 401/403/404/400 属于确定性结果，直接返回给上层判断
                if exc.code < 500:
                    return exc.code, raw
                last_err = exc
            except Exception as exc:  # 超时 / DNS / 连接重置
                last_err = exc
            if attempt < self.tries - 1:
                time.sleep(1.2 * (attempt + 1))
        raise FetchError(f"请求失败 {url}：{last_err}")

    def get_json(self, url: str) -> tuple[int, dict]:
        code, text = self.request(url)
        try:
            return code, json.loads(text)
        except Exception:
            snippet = text[:160].replace("\n", " ")
            raise FetchError(f"响应不是 JSON（HTTP {code}）：{snippet}")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def interruptible_sleep(seconds: float, should_stop=None, step: float = 0.25) -> bool:
    """可打断的等待，用于站点之间的节流间隔。收到停止指令立刻返回 True。"""
    if not seconds or seconds <= 0:
        return bool(should_stop and should_stop())
    deadline = time.monotonic() + seconds
    while True:
        if should_stop and should_stop():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(step, remaining))


def extract_token(obj, acc: list[str] | None = None) -> str | None:
    """深度遍历登录响应，找出可能的 access_token / session token。"""
    if acc is None:
        acc = []
    token_keys = {"access_token", "accessToken", "token", "jwt", "session_token", "session"}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in token_keys and isinstance(value, str) and len(value) > 12:
                acc.append(value)
            else:
                extract_token(value, acc)
    elif isinstance(obj, list):
        for item in obj:
            extract_token(item, acc)
    return acc[0] if acc else None


def prompt_field(obj: dict, *keys, default=""):
    for key in keys:
        if isinstance(obj, dict) and obj.get(key):
            return obj[key]
    return default


# ---------------------------------------------------------------------------
# new-api 适配器
# ---------------------------------------------------------------------------


class NewApiAdapter:
    """适配 new-api / one-api 及其二开分支。"""

    type = "newapi"

    def __init__(self, site: dict, client: HttpClient, debug: bool = False):
        self.site = site
        self.base = (site.get("base_url") or "").rstrip("/")
        self.client = client
        self.debug = debug
        self.groups: dict[str, dict] = {}   # 分组 → {ratio, desc, usable}，parse 时填充

    # -- 登录 ---------------------------------------------------------------
    def login_if_needed(self, auth: dict) -> str:
        """返回鉴权方式描述；未提供账号时返回 anonymous（匿名采集）。"""
        username = (auth or {}).get("username", "").strip()
        password = (auth or {}).get("password", "").strip()
        if not username or not password:
            return "anonymous"
        return newapi_login(self.client, self.base, username, password)

    # -- 站点信息 -----------------------------------------------------------
    def load_profile(self) -> dict:
        try:
            code, payload = self.client.get_json(f"{self.base}/api/status")
            return payload.get("data") or {}
        except Exception:
            return {}

    # -- 采集 ---------------------------------------------------------------
    def fetch(self) -> dict:
        auth = self.site.get("auth") or {}
        auth_state = "cookie" if auth.get("cookie") else "anonymous"
        code, payload = self.client.get_json(f"{self.base}/api/pricing")
        if code == 401:
            if auth.get("username"):
                auth_state = self.login_if_needed(auth)
                code, payload = self.client.get_json(f"{self.base}/api/pricing")
            else:
                raise AuthError("该站点定价接口要求登录，请在站点配置里填 Cookie 或账号密码")
        if code != 200 or not payload.get("success", True):
            raise FetchError(f"定价接口返回异常（HTTP {code}）")

        profile = self.load_profile()
        offers = self.parse(payload)
        # 每站只补一次公开汇总；详情页仍只读快照，不因打开页面联网。
        health = fetch_model_health_summary(self.client, self.base)
        for offer in offers:
            if offer["model_raw"] in health:
                offer["health"] = health[offer["model_raw"]]
        return {
            "offers": offers,
            "profile": profile,
            "auth_state": auth_state,
            "raw_model_count": len(payload.get("data") or []),
            "groups": self.groups,
        }

    def parse(self, payload: dict) -> list[dict]:
        result = parse_newapi_pricing(self.site, payload)
        self.groups = result["groups"]
        return result["offers"]


# ---------------------------------------------------------------------------
# sub2api 适配器
# ---------------------------------------------------------------------------


class Sub2ApiAdapter:
    """适配 sub2api（Github: Wei-Shaw/sub2api）的公共模型广场。

    接口：GET /api/v1/model-plaza，返回 { description, groups: [ { name,
    rate_multiplier, models: [ { name, platform, pricing: {...} } ] } ] }
    token 价格从 USD/token 转为 USD/1M tokens；按次价格保持 USD/次。
    保留分时/生图元数据，不把尚未解析的价格当成已确认最低价。
    """

    type = "sub2api"

    def __init__(self, site: dict, client: HttpClient, debug: bool = False):
        self.site = site
        self.base = (site.get("base_url") or "").rstrip("/")
        self.client = client
        self.debug = debug

    def fetch(self) -> dict:
        code, payload = self.client.get_json(f"{self.base}/api/v1/model-plaza")
        if code == 401:
            raise AuthError("该站点模型广场要求登录/令牌")
        if code != 200:
            raise FetchError(f"模型广场返回异常（HTTP {code}）")

        return parse_sub2api_pricing(self.site, payload)


ADAPTERS = {"newapi": NewApiAdapter, "sub2api": Sub2ApiAdapter}


# ---------------------------------------------------------------------------
# 账号信息：余额 / 用量 / 充值 / 分组 / API Key
# ---------------------------------------------------------------------------
#
# 这一段只服务于「用户自己的账号」，和价格采集是两条独立的链路：
#   · 价格采集尽量匿名（口径统一、风险低）
#   · 账户信息必须登录，只在用户显式点「刷新账户」时才联网
# 凭据优先级：Cookie > 账号密码。Cookie 是首选，因为很多站点开了密码加密或 2FA，
# 脚本过不去，但用户从浏览器里复制出来的 Cookie 一定能用。


def login_switches(client: HttpClient, base: str) -> dict:
    """读 /api/status 上的登录开关（turnstile_check / password_login_enabled /
    password_login_encryption_enabled）。取不到就返回空 dict，不影响后续尝试。"""
    try:
        code, payload = client.get_json(f"{base}/api/status")
    except Exception:
        return {}
    if not isinstance(payload, dict) or code != 200:
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def newapi_login(client: HttpClient, base: str, username: str, password: str) -> str:
    """用账号密码登录 new-api，返回鉴权方式描述；失败抛 AuthError（带分类 kind）。"""
    # 先探一下站点开了哪些门槛，能提前判定的就不浪费一次登录尝试
    sw = login_switches(client, base)
    if sw.get("password_login_enabled") is False:
        raise AuthError(f"{LOGIN_HINTS['password_off']}；{COOKIE_ADVICE}", "password_off")
    if sw.get("password_login_encryption_enabled"):
        raise AuthError(f"{LOGIN_HINTS['password_enc']}；{COOKIE_ADVICE}", "password_enc")
    if sw.get("turnstile_check"):
        raise AuthError(f"{LOGIN_HINTS['turnstile']}；{COOKIE_ADVICE}", "turnstile")

    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    code, text = client.request(
        f"{base}/api/user/login", "POST", body,
        {"Content-Type": "application/json"},
    )
    try:
        payload = json.loads(text)
    except Exception:
        raise AuthError(f"登录响应无法解析（HTTP {code}）：{text[:120]}")
    if not payload.get("success"):
        msg = prompt_field(payload, "message", default="登录失败")
        low = str(msg).lower()
        # 有的站点把人机验证说成 turnstile，有的直接说 captcha / 验证码 / 滑动验证
        if "turnstile" in low or "captcha" in low or "验证码" in msg or "滑动" in msg:
            raise AuthError(f"{LOGIN_HINTS['turnstile']}（{msg}）；{COOKIE_ADVICE}", "turnstile")
        if "disabled" in low and "password" in low:
            raise AuthError(f"{LOGIN_HINTS['password_off']}；{COOKIE_ADVICE}", "password_off")
        # 官方文案是「Username or password is incorrect, or user has been banned」，
        # 里面同时带 incorrect 和 banned，所以必须先判 incorrect，否则一律被当成封号。
        if "incorrect" in low or "invalid" in low:
            raise AuthError(f"{LOGIN_HINTS['bad_cred']}（{msg}）", "bad_cred")
        if "banned" in low:
            raise AuthError(f"{LOGIN_HINTS['banned']}（{msg}）", "banned")
        raise AuthError(f"{msg}（HTTP {code}）", "bad_cred")

    # 站点要求二次验证时 success 仍是 true，但 data 里没有令牌，只有验证挑战
    data = payload.get("data")
    if isinstance(data, dict) and (data.get("require_verification") or data.get("verification")):
        raise AuthError(f"{LOGIN_HINTS['need_verify']}；{COOKIE_ADVICE}", "need_verify")

    # 旧版本 data 本身就是令牌字符串，新版是含 access_token 的对象，两种都要认
    token = extract_token(payload) or (data if isinstance(data, str) and len(data) > 12 else None)
    if token:
        client.auth_header["Authorization"] = f"Bearer {token}"
        return "account"
    raise AuthError("登录响应里没有拿到令牌（站点可能改版或需要二次验证）；" + COOKIE_ADVICE,
                    "need_verify")


def _self_user(client: HttpClient, base: str) -> tuple[dict | None, str, int]:
    """取 /api/user/self。返回 (用户信息, 错误说明, HTTP 码)。"""
    try:
        code, payload = client.get_json(f"{base}/api/user/self")
    except FetchError as exc:
        return None, str(exc), 0
    if code != 200 or not payload.get("success", True):
        return None, (payload.get("message") or f"HTTP {code}"), code
    data = payload.get("data") or {}
    if not data.get("id"):
        return None, "未登录或凭据无效", code
    return data, "", code


def auth_client(site: dict, proxy: str = "", *, timeout: int = 25,
                tries: int = 3) -> tuple[HttpClient, str, str, dict]:
    """建立已登录的会话。凭据按 Cookie → 账号密码 的顺序尝试。

    返回 (client, base_url, 凭据类型, 用户信息)。都失败时抛 AuthError，
    错误信息里会带上每一步的原因，方便用户判断是 Cookie 过期还是密码错了。
    """
    base = (site.get("base_url") or "").rstrip("/")
    auth = site.get("auth") or {}
    cookie = str(auth.get("cookie") or "").strip()
    username = str(auth.get("username") or "").strip()
    password = str(auth.get("password") or "").strip()

    reasons: list[str] = []
    kind = "auth"
    if cookie:
        client = HttpClient(proxy=proxy, cookie=cookie, timeout=timeout, tries=tries)
        user, err, _ = _self_user(client, base)
        if user:
            return client, base, "cookie", user
        reasons.append(f"Cookie 无效（{err}）")

    if username and password:
        client = HttpClient(proxy=proxy, timeout=timeout, tries=tries)
        try:
            newapi_login(client, base, username, password)
        except AuthError as exc:
            kind = getattr(exc, "kind", "auth") or "auth"
            reasons.append(f"账号密码登录失败（{exc}）")
        except FetchError as exc:
            reasons.append(f"账号密码登录失败（{exc}）")
        else:
            user, err, _ = _self_user(client, base)
            if user:
                return client, base, "account", user
            reasons.append(f"登录成功但取不到账号信息（{err}）")
    elif not cookie:
        reasons.append("未配置凭据（需要 Cookie 或账号密码）")

    raise AuthError("；".join(reasons) or "登录失败", kind)


def _page_items(payload) -> list:
    """new-api 的分页响应统一是 {success, data:{items,total,page,page_size}}，这里只取 items。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        return data.get("items") or []
    if isinstance(data, list):
        return data
    return []


def read_newapi_pages(client: HttpClient, url: str, *, max_pages: int = 100,
                      page_size: int = 100) -> dict:
    """有界读取列表。仅 complete=True 的结果可代替完整快照或用于创建前对账。"""
    page_size = max(1, min(100, int(page_size)))
    parsed = urllib.parse.urlsplit(url)
    query = {k: v for k, v in urllib.parse.parse_qsl(parsed.query)
             if k not in {"p", "page_size", "ps", "size"}}
    rows, seen = [], set()
    expected_total, reported_total, requested_pages = None, None, 0
    size_key, fallback_index = "page_size", 0
    size_keys = ("page_size", "ps", "size")

    def result(complete, error=None):
        return {"items": rows, "complete": complete, "error": error,
                "total": reported_total, "pages": requested_pages}

    for page in range(1, max_pages + 1):
        while True:
            stop_check = getattr(client, "stop_check", None)
            if callable(stop_check) and stop_check():
                raise FetchError("已手动停止")
            page_url = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(
                {**query, "p": page, size_key: page_size})))
            try:
                code, payload = client.get_json(page_url)
            except FetchError:
                return result(False, "分页请求失败，结果不完整")
            requested_pages += 1
            # 只有明确拒绝分页参数时才试旧别名；业务失败/限流/权限错误不追打。
            message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
            if (page == 1 and code in (400, 422) and fallback_index < 2
                    and re.search(r"page_size|\bps\b|\bsize\b", message, re.I)):
                fallback_index += 1
                size_key = size_keys[fallback_index]
                continue
            break
        if code != 200 or not isinstance(payload, dict) or payload.get("success") is False:
            return result(False, f"列表读取失败（HTTP {code}），结果不完整")
        data = payload.get("data")
        if isinstance(data, list):
            items, metadata = data, {}
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            items, metadata = data["items"], data
        else:
            return result(False, "列表响应缺少 items，结果不完整")
        raw_total = metadata.get("total", payload.get("total"))
        total = to_float(raw_total)
        if raw_total is not None and total is None:
            return result(False, "分页 total 无效，结果不完整")
        if total is not None:
            if not math.isfinite(total) or total < 0 or not total.is_integer():
                return result(False, "分页 total 无效，结果不完整")
            reported_total = int(total)
            if expected_total is None:
                expected_total = reported_total
            elif reported_total != expected_total:
                return result(False, "分页期间 total 变化，请重新读取")
        reported_size = to_float(metadata.get("page_size", metadata.get("ps", metadata.get("size"))))
        actual_size = (int(reported_size) if reported_size is not None and math.isfinite(reported_size)
                       and reported_size > 0 and reported_size.is_integer() else page_size)
        added = 0
        for item in items:
            if not isinstance(item, dict):
                return result(False, "列表条目格式不正确，结果不完整")
            identity = next((str(item[k]) for k in ("id", "trade_no", "order_id")
                             if item.get(k) is not None), None)
            # 旧分叉缺 ID 时按完整内容去重；重复页仍无法冒充完整列表。
            identity = ("id", identity) if identity is not None else (
                "row", json.dumps(item, sort_keys=True, ensure_ascii=False))
            if identity not in seen:
                rows.append(item)
                seen.add(identity)
                added += 1
        if expected_total is not None and len(rows) >= expected_total:
            if len(rows) != expected_total:
                return result(False, "分页条数与 total 不符，结果不完整")
            return result(True)
        if not items or len(items) < actual_size:
            if expected_total is not None:
                return result(False, "分页提前结束，未读全 total 条记录")
            return result(True)
        if not added:
            return result(False, "分页返回重复页，结果不完整")
    return result(False, f"达到分页安全上限 {max_pages} 页，结果不完整")


def quota_to_usd(value) -> float | None:
    """额度单位 → 美元。站点存的是整数单位，1 USD = 500000 单位。"""
    num = to_float(value)
    return None if num is None else num / QUOTA_PER_UNIT


def _token_group(token_group: str, user_group: str) -> str:
    """令牌分组为空表示「跟随账号所属分组」，这里补成账号分组便于按分组聚合。"""
    group = (token_group or "").strip()
    return group or (user_group or "").strip() or "default"


def fetch_model_health_summary(client: HttpClient, base: str) -> dict:
    """采集时每站一次，模型级汇总不能冒充某个分组的健康状态。"""
    timeout, tries = client.timeout, client.tries
    try:
        client.timeout, client.tries = min(timeout, 8), 1
        code, payload = client.get_json(f"{base}/api/perf-metrics/summary?hours=24")
        if code != 200 or not payload.get("success", False):
            return {}
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return {}
        return {
            row["model_name"]: {**{k: row.get(k) for k in ("success_rate", "avg_latency_ms", "avg_tps")},
                                "scope": "model", "window_end": data.get("window_end"),
                                "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            for row in data.get("models") or []
            if isinstance(row, dict) and row.get("model_name")
        }
    except Exception:
        return {}  # 不支持健康接口不能影响价格采集
    finally:
        client.timeout, client.tries = timeout, tries


def fetch_model_health(site: dict, model: str, proxy: str = "") -> dict:
    """显式点击更新时读取站点原生统计，不发推理请求、不消耗模型额度。"""
    if site.get("type", "newapi") != "newapi":
        raise FetchError("这个框架暂未适配模型健康统计")
    base = str(site.get("base_url") or "").rstrip("/")
    client = HttpClient(proxy=proxy, cookie=str((site.get("auth") or {}).get("cookie") or ""),
                        timeout=12, tries=1)
    url = f"{base}/api/perf-metrics?" + urllib.parse.urlencode({"model": model, "hours": 24})
    code, payload = client.get_json(url)
    if code in (401, 403):
        client, base, _, _ = auth_client(site, proxy)
        client.timeout, client.tries = 12, 1
        code, payload = client.get_json(url)
    if code != 200 or not payload.get("success", False):
        raise FetchError(f"站点未开放模型健康统计，或当前凭据无权读取（HTTP {code}）")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        raise FetchError("站点返回的模型健康数据格式不受支持")
    returned_model = str(data.get("model_name") or model)
    if returned_model != model:
        raise FetchError("站点返回了其他模型的统计，已忽略")
    return {"model": model, "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "window_start": data.get("window_start"), "window_end": data.get("window_end"),
            "summary": data.get("summary"),
            "groups": [{k: row.get(k) for k in ("group", "success_rate", "avg_latency_ms", "avg_ttft_ms", "avg_tps", "series")}
                       for row in data["groups"] if isinstance(row, dict) and row.get("group") is not None]}


def fetch_account_snapshot(site: dict, proxy: str = "") -> dict:
    """拉取单个站点的账号快照：余额 / 用量 / 充值 / 分组 / Key。

    返回结构里所有金额都换算成 USD（展示时再按用户选的币种折算），
    同时保留充值记录里的原始金额，便于核对。
    """
    site_id = site.get("id")
    out = {
        "site": site_id,
        "site_name": site.get("name") or site_id,
        "ok": False,
        "error": None,
        "error_kind": None,     # auth（凭据问题） | network（连不上）| none（没配凭据）
        "credential": "none",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    auth = site.get("auth") or {}
    if not (str(auth.get("cookie") or "").strip() or str(auth.get("username") or "").strip()):
        out["error"] = "未配置凭据"
        out["error_kind"] = "none"
        return out

    try:
        client, base, credential, user = auth_client(site, proxy)
    except AuthError as exc:
        out["error"] = str(exc)
        out["error_kind"] = getattr(exc, "kind", None) or "auth"
        # 需要人去浏览器里操作的那些类型，单独给一句可执行的建议
        out["advice"] = COOKIE_ADVICE if out["error_kind"] in NEEDS_HUMAN_LOGIN else ""
        return out
    except FetchError as exc:
        out["error"] = str(exc)
        out["error_kind"] = "network"
        return out

    user_group = str(user.get("group") or "").strip() or "default"
    out.update(
        ok=True,
        credential=credential,
        user_id=user.get("id"),
        username=user.get("username") or "",
        display_name=user.get("display_name") or "",
        email=user.get("email") or "",
        group=user_group,
        status=user.get("status"),
        role=user.get("role"),
        quota_usd=quota_to_usd(user.get("quota")),
        used_quota_usd=quota_to_usd(user.get("used_quota")),
        aff_quota_usd=quota_to_usd(user.get("aff_quota")),
        request_count=to_float(user.get("request_count")),
    )

    # —— 充值记录：有的站点关掉了这个模块，取不到也不影响余额展示 ——
    topup_page = read_newapi_pages(client, f"{base}/api/user/topup/self")
    topups = topup_page["items"] if topup_page["complete"] else []
    amount_sum = money_sum = 0.0
    amount_known = money_known = True
    recharge_count = 0
    for row in topups:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status in TOPUP_FAIL_STATUSES:
            continue
        amount = to_float(row.get("amount"))
        money = to_float(row.get("money"))
        amount_known = amount_known and amount is not None and math.isfinite(amount)
        money_known = money_known and money is not None and math.isfinite(money)
        amount_sum += amount if amount is not None and math.isfinite(amount) else 0
        money_sum += money if money is not None and math.isfinite(money) else 0
        recharge_count += 1
    out.update(
        recharge_count=recharge_count if topup_page["complete"] else None,
        recharge_usd=amount_sum / QUOTA_PER_UNIT if topup_page["complete"] and amount_known else None,
        recharge_money=money_sum if topup_page["complete"] and money_known else None,
        recharge_supported=topup_page["complete"],
        topups_complete=topup_page["complete"],
        topups_error=topup_page["error"],
        topups_updated_at=out["fetched_at"] if topup_page["complete"] else None,
    )

    # —— API Key 列表：站点只返回掩码，明文要单独取 ——
    token_page = read_newapi_pages(client, f"{base}/api/token/")
    rows = token_page["items"] if token_page["complete"] else []

    tokens: list[dict] = []
    group_keys: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = to_float(row.get("status"))
        group = _token_group(str(row.get("group") or ""), user_group)
        item = {
            "id": row.get("id"),
            "name": row.get("name") or "",
            "group": group,
            "group_explicit": bool(str(row.get("group") or "").strip()),
            "status": status,
            "enabled": True if status is None else int(status) == 1,
            "unlimited": bool(row.get("unlimited_quota")),
            "remain_usd": quota_to_usd(row.get("remain_quota")),
            "used_usd": quota_to_usd(row.get("used_quota")),
            "expired_time": row.get("expired_time"),
            "key_masked": row.get("key") or "",
            "model_limited": bool(row.get("model_limits_enabled")),
            "model_limits": str(row.get("model_limits") or ""),
        }
        tokens.append(item)
        cell = group_keys.setdefault(group, {"count": 0, "enabled": 0, "tokens": []})
        cell["count"] += 1
        if item["enabled"]:
            cell["enabled"] += 1
        if len(cell["tokens"]) < 20:
            cell["tokens"].append({
                "id": item["id"], "name": item["name"],
                "key_masked": item["key_masked"], "enabled": item["enabled"],
            })

    out.update(tokens=tokens if token_page["complete"] else None,
               group_keys=group_keys if token_page["complete"] else None,
               token_count=len(tokens) if token_page["complete"] else None,
               keys_complete=token_page["complete"], keys_error=token_page["error"],
               keys_updated_at=out["fetched_at"] if token_page["complete"] else None)
    return out


def get_newapi_token_key(site: dict, proxy: str = "", token_id: int | str | None = None,
                         client: HttpClient | None = None, base: str = "") -> dict:
    """取某个令牌的明文 Key（站点列表里只给掩码，复制前必须单独调一次）。"""
    if client is None:
        client, base, _credential, _user = auth_client(site, proxy)
    code, text = client.request(
        f"{base}/api/token/{int(token_id)}/key", "POST", b"{}",
        {"Content-Type": "application/json"},
    )
    try:
        payload = json.loads(text)
    except Exception:
        raise FetchError(f"取 Key 响应无法解析（HTTP {code}）：{text[:120]}")
    if code != 200 or not payload.get("success"):
        raise FetchError(payload.get("message") or f"取 Key 失败（HTTP {code}）")
    key = str((payload.get("data") or {}).get("key") or "").strip()
    if not key:
        raise FetchError("站点没有返回明文 Key")
    if "*" in key or "…" in key:
        raise FetchError("站点返回的是 Key 掩码，无法复制或导入；请到站点查看完整 Key")
    # NewAPI 的明文接口通常返回数据库中的原始令牌，站点前端复制时补 sk-。
    # 在共同出口补齐，创建、复制已有 Key 和 CC Switch 导入使用同一完整值。
    if not key.startswith("sk-"):
        key = "sk-" + key
    return {"id": token_id, "key": key}


def create_newapi_token(site: dict, proxy: str = "", name: str = "", group: str = "",
                        unlimited: bool = True, quota_usd: float = 0.0, days: int = 0,
                        session: tuple | None = None) -> dict:
    """在指定站点创建一个 API Key 并返回明文。

    站点的新建接口只回 {"success": true}，不给 id，所以要按名字回查列表拿到 id，
    再调一次取明文。整套流程只在用户点「创建 Key」时触发。
    """
    client, base, _credential, user = session or auth_client(site, proxy)

    remain = 0
    if not unlimited:
        remain = int(max(0.0, to_float(quota_usd) or 0.0) * QUOTA_PER_UNIT)
    # -1 表示永不过期（new-api 的约定）
    expired = int(time.time()) + int(days or 0) * 86400 if days else -1

    wanted = (name or "").strip()[:50]
    if not wanted:
        raise FetchError("请输入 Key 名称")
    wanted_group = (group or "").strip()
    # 新建接口不返回 id。先记录已有 id，避免列表延迟时把同名旧 Key 当成新 Key。
    listing = read_newapi_pages(client, f"{base}/api/token/")
    if not listing["complete"]:
        raise FetchError("无法读取 Key 列表，暂未创建；请检查站点凭据后重试")
    previous_ids = {str(row.get("id")) for row in listing["items"]
                    if isinstance(row, dict)}
    body = json.dumps({
        "name": wanted,
        "remain_quota": remain,
        "unlimited_quota": bool(unlimited),
        "expired_time": expired,
        "group": wanted_group,
        "model_limits_enabled": False,
        "model_limits": "",
        "allow_ips": "",
    }).encode("utf-8")
    old_tries = client.tries
    try:
        client.tries = 1
        code, text = client.request(f"{base}/api/token/", "POST", body,
                                    {"Content-Type": "application/json"})
    finally:
        client.tries = old_tries
    try:
        payload = json.loads(text)
    except Exception:
        raise FetchError(f"创建响应无法解析（HTTP {code}）：{text[:120]}")
    if code != 200 or not payload.get("success"):
        raise FetchError(payload.get("message") or f"创建失败（HTTP {code}）")

    user_group = str(user.get("group") or "").strip() or "default"
    # 列表可能有一两秒延迟，重试几次；命中同名时取 id 最大的那个（最新建的）
    for _ in range(3):
        listing = read_newapi_pages(client, f"{base}/api/token/")
        if not listing["complete"]:
            raise FetchError("Key 已创建成功，但列表读取不完整；请刷新列表确认，不要重复创建")
        rows = listing["items"]
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("id")) in previous_ids:
                continue
            if (str(row.get("name") or "").strip() == wanted
                    and _token_group(str(row.get("group") or ""), user_group)
                    == _token_group(wanted_group, user_group)):
                matches.append(row)
        if len(matches) > 1:
            raise FetchError("Key 已创建成功，但出现多个同名新 Key，无法唯一确认；请刷新列表，不要重复创建")
        hit = matches[0] if matches else None
        if hit:
            plain = get_newapi_token_key(site, proxy, hit.get("id"), client=client, base=base)
            plain.update(
                name=hit.get("name") or "",
                group=_token_group(str(hit.get("group") or ""), user_group),
                site=site.get("id"),
                site_name=site.get("name") or site.get("id"),
                status=1, enabled=True, expired_time=expired, unlimited=bool(unlimited),
                remain_usd=quota_to_usd(remain), used_usd=0.0, model_limited=False,
                group_explicit=bool(wanted_group), model_limits="",
            )
            return plain
        time.sleep(0.6)
    raise FetchError("Key 已创建成功，但回查列表时没找到它（站点可能未开放列表接口）")


def delete_newapi_token(site: dict, proxy: str = "", token_id: int | str | None = None,
                        session: tuple | None = None) -> dict:
    """只删除指定站点当前账号拥有的单个令牌，不自动重试写操作。"""
    text_id = str(token_id or "")
    if not text_id.isascii() or not text_id.isdigit() or int(text_id) <= 0:
        raise FetchError("Key ID 必须是正整数")
    client, base, _, _ = session or auth_client(site, proxy)
    old_tries = client.tries
    try:
        client.tries = 1
        code, text = client.request(f"{base}/api/token/{int(text_id)}", "DELETE")
    finally:
        client.tries = old_tries
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise FetchError(f"删除响应无法解析（HTTP {code}）；请刷新 Key 列表确认结果")
    if code != 200 or not payload.get("success"):
        raise FetchError(payload.get("message") or f"删除 Key 失败（HTTP {code}）")
    return {"id": int(text_id)}


# ---------------------------------------------------------------------------
# 导出到 CC Switch（ccswitch://v1/import 深链）
# ---------------------------------------------------------------------------

# CC Switch 支持的应用，导出时按这个白名单校验，避免拼出被客户端拒绝的链接
# 接口格式：留空表示跟随应用自己的默认格式
# apiFormat 参数要 CC Switch 4.0.10 起才认，老版本传了会被当成无效值

# 官方 NewAPI 用量模板：拿 /api/user/self 的额度，除以 500000 换成美元。
# {{baseUrl}} / {{accessToken}} / {{userId}} 由 CC Switch 在运行时替换。


def parse_version_tuple(text: str) -> tuple[int, ...] | None:
    """把 "4.0.9" 之类版本号拆成元组，用来比较高低；解析不出来返回 None。"""
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(p) for p in parts[:3]) if parts else None




def resolve_export_key(site: dict, proxy: str = "", group: str = "", create: bool = True,
                       name: str = "", token_id: int | str | None = None, model: str = "",
                       session: tuple | None = None) -> dict:
    """拿到某个分组下可用的明文 API Key：已有就复用，没有就（允许时）新建。

    同时把用量查询需要的 access_token / user_id 一起带出来——站点只认登录令牌，
    实测 sk- 开头的 Key 调 /api/user/self 会被 401 拒掉。
    """
    session = session or auth_client(site, proxy)
    client, base, credential, user = session
    user_group = str(user.get("group") or "").strip() or "default"
    wanted = (group or "").strip() or user_group

    listing = read_newapi_pages(client, f"{base}/api/token/")
    if not listing["complete"]:
        raise FetchError("Key 列表读取不完整，暂不导出或新建；请刷新后重试")
    rows = listing["items"]

    hit = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if token_id is not None and str(row.get("id")) != str(token_id):
            continue
        if _token_group(str(row.get("group") or ""), user_group) != wanted:
            continue
        status = to_float(row.get("status"))
        if not (True if status is None else int(status) == 1):
            continue
        expires = to_float(row.get("expired_time"))
        if expires is not None and expires > 0 and expires <= time.time():
            continue
        if row.get("model_limits_enabled") and model:
            limits = {s.strip() for s in str(row.get("model_limits") or "").split(",") if s.strip()}
            if model not in limits:
                continue
        if hit is None or (row.get("id") or 0) > (hit.get("id") or 0):
            hit = row

    created = False
    token_name = ""
    if hit is not None:
        key = get_newapi_token_key(site, proxy, hit.get("id"), client=client, base=base)["key"]
        token_name = str(hit.get("name") or "")
    else:
        if token_id is not None:
            raise FetchError("所选 Key 不属于这个分组、已停用/过期，或不允许调用此模型；请换一个 Key")
        if not create:
            raise FetchError(f"分组「{wanted}」下没有可用的 Key；勾选「自动新建」或先在详情页建一个")
        token = create_newapi_token(site, proxy, name=(name or "cc-switch"), group=wanted,
                                   session=session)
        key, created, token_name = token["key"], True, str(token.get("name") or "")

    # 只有账号密码登录才拿得到 Bearer 令牌，Cookie 方式没有，用量查询就只能跳过
    authz = str(client.auth_header.get("Authorization") or "")
    access_token = authz[7:] if authz.startswith("Bearer ") else ""

    return {
        "key": key,
        "created": created,
        "token_name": token_name,
        "group": wanted,
        "user_group": user_group,
        "base_url": base,
        "credential": credential,
        "access_token": access_token,
        "user_id": user.get("id") or "",
    }


# ---------------------------------------------------------------------------
# 按模型聚类
# ---------------------------------------------------------------------------


def version_signature(name: str) -> list[str]:
    """提取模型名里的版本号序列，例如 claude-opus-4-5 → ['4','5']，gpt-5.4 → ['5.4']。

    用于阻断「版本号不同却被相似度合并」的误判（如 gemini-3 与 gemini-3.6）。
    """
    return re.findall(r"\d+(?:\.\d+)*", name)


def token_set(name: str) -> set[str]:
    """按连字符切分出词汇集合，用于判断两个模型名是否只是「后缀增减」关系。"""
    return {t for t in re.split(r"[-_/\s]+", name) if t}


def build_models(offers: list[dict], threshold: float) -> list[dict]:
    """把跨站报价按模型聚类，返回以模型为主体的列表。

    聚类策略：
      1. 先按归一化后的模型名精确分组（不同站点命名一致时直接合并）；
      2. 再对归一化名做相似度合并（默认 0.9），吸收带前后缀的命名差异，
         比如 claude-opus-4-5 / claude-opus-4-5-thinking；
      3. 合并前必须满足「版本号序列完全一致」，避免 gemini-3 与 gemini-3.6 被误并；
      4. 词汇集合必须是「一方包含另一方」的关系，即只允许后缀增减、不允许替换，
         这样 gemini-3.1-flash-image 不会被并进 gemini-3.1-flash-lite。
    每个模型内部保留所有报价并按低价排序，同时保留各站原始模型名。
    """
    exact: dict[str, list[dict]] = {}
    for offer in offers:
        exact.setdefault(offer["model_key"], []).append(offer)

    keys = sorted(exact)
    signatures = {key: version_signature(key) for key in keys}
    token_sets = {key: token_set(key) for key in keys}
    clusters: list[list[str]] = []
    for key in keys:
        placed = False
        for cluster in clusters:
            rep = cluster[0]
            # 首字母不同直接跳过，避免把不同家族的模型误合并
            if rep[0] != key[0]:
                continue
            # 版本号必须完全一致，否则是不同代次的模型
            if signatures[rep] != signatures[key]:
                continue
            # 词汇只能是「一方包含另一方」，即后缀增减；出现平行替换词则不合
            if not (token_sets[rep] <= token_sets[key] or token_sets[key] <= token_sets[rep]):
                continue
            if difflib.SequenceMatcher(None, rep, key).ratio() >= threshold:
                cluster.append(key)
                placed = True
                break
        if not placed:
            clusters.append([key])

    models: list[dict] = []
    for cluster in clusters:
        bucket: list[dict] = []
        for key in cluster:
            bucket.extend(exact[key])

        all_units: list[str] = []
        vendors: list[str] = []
        tags: list[str] = []
        endpoints: list[str] = []
        sites: list[str] = []
        for offer in bucket:
            all_units.append(offer["model_raw"])
            if offer["vendor"] and offer["vendor"] not in vendors:
                vendors.append(offer["vendor"])
            for tag in offer["tags"]:
                if tag not in tags:
                    tags.append(tag)
            for endpoint in offer["endpoints"]:
                if endpoint not in endpoints:
                    endpoints.append(endpoint)
            if offer["site"] not in sites:
                sites.append(offer["site"])

        # 展示名：优先挑没有中括号前缀的干净名字（如 [次]xxx 里的 xxx），
        # 同类里取出现次数最多的，次数相同取最短的，视觉上更干净
        counts: dict[str, int] = {}
        for name in all_units:
            counts[name] = counts.get(name, 0) + 1
        clean = [name for name in counts if not BRACKET_PREFIX.match(name)]
        pool = set(clean) if clean else set(counts)
        display = sorted(
            (item for item in counts.items() if item[0] in pool),
            key=lambda kv: (-kv[1], len(kv[0]), kv[0]),
        )[0][0]

        ranked = [o for o in bucket if price_is_rankable(o)]
        inputs = [o["input"] for o in ranked if o["input"] is not None]
        outputs = [o["output"] for o in ranked if o["output"] is not None]
        calls = [o["per_call"] for o in ranked if o["per_call"] is not None]

        models.append({
            "key": cluster[0],
            "display": display,
            "aliases": sorted(set(all_units)),
            "site_count": len(sites),
            "offer_count": len(bucket),
            "vendors": vendors,
            "tags": tags,
            "endpoints": endpoints,
            "has_token": bool(inputs),
            "has_call": bool(calls),
            "min_input": min(inputs) if inputs else None,
            "min_output": min(outputs) if outputs else None,
            "min_call": min(calls) if calls else None,
            "max_input": max(inputs) if inputs else None,
            # 报价按低价优先排序：按次模型优先用单次价，按量模型用输入价
            "offers": sorted(bucket, key=_offer_sort_key),
        })

    # 排序键去掉中括号标记，避免 "[次]xxx" 这类名字全部挤在最前面
    models.sort(key=lambda m: (BRACKET_PREFIX.sub("", m["display"]) or m["display"]).lower())
    return models


def price_is_rankable(offer: dict) -> bool:
    """是否参与比价：只要拿得到任一价格数字就参与。

    动态计费（底价）与倍率估算的报价同样进排名和最低价计算；
    价格可信度由 pricing_verified / ratio_estimated 字段承载，前端标为「待确认 · 参考价」。
    """
    return any(offer.get(k) is not None for k in ("input", "output", "per_call"))


def _offer_sort_key(offer: dict):
    """报价排序键：先按量（输入价），没有输入价再退回单次价，缺失的排最后。"""
    if not price_is_rankable(offer):
        return (3, 0.0)
    if offer["input"] is not None:
        return (0, round(offer["input"], 8))
    if offer["per_call"] is not None:
        return (1, round(offer["per_call"], 8))
    return (2, 0.0)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def load_json(path: Path, default=None):
    """读取 JSON 文件，不存在或解析失败时返回默认值，不让配置问题打断采集。"""
    try:
        with path.open(encoding="utf-8") as fp:
            return json.load(fp)
    except FileNotFoundError:
        return default if default is not None else {}
    except Exception as exc:
        print(f"  ! 配置文件 {path.name} 解析失败（{exc}），将使用默认值")
        return default if default is not None else {}


def write_json_atomic(path: Path, data) -> None:
    """原子写文件：先写临时文件、读回校验通过后再替换，并留一份 .bak。

    本机实测出现过 settings.json 被写成 341 字节全零的情况（怀疑安全软件扫描干扰），
    所以这里加了三道保险：
      1. 写临时文件时 flush + fsync，确保内容真的落盘；
      2. 替换前把临时文件读回来重新解析，空内容或坏数据绝不覆盖原文件；
      3. 替换成功后把上一版留成 .bak，真出问题还能手工找回。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
        fp.flush()
        os.fsync(fp.fileno())

    # 替换前自检：读回来必须能解析，且不能是空对象以外的一无所有
    with tmp.open("r", encoding="utf-8") as fp:
        parsed = json.load(fp)
    if parsed is None:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"写入内容为空，已放弃覆盖 {path.name}")

    if path.exists():
        try:
            shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        except Exception:
            pass  # 留不下备份也不能阻止这次写入
    os.replace(tmp, path)


def load_settings() -> dict:
    """读取 settings.json（定时、节流、常用模型、手动采集目标等）。

    文件损坏（例如被写成全零）时先试同目录的 .bak，再退回空字典 ——
    绝不让一次坏写入把用户配置悄悄清空。
    """
    for candidate in (SETTINGS_FILE, SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".bak")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            if candidate != SETTINGS_FILE:
                print(f"  ! {SETTINGS_FILE.name} 读取失败，已改用 {candidate.name}")
            return data
    return {}


def load_config(path: Path | None = None) -> dict:
    """读取站点清单；缺失时给出可操作的提示。"""
    path = Path(path) if path is not None else DEFAULT_CONFIG
    if not path.exists():
        raise SystemExit(
            f"找不到配置文件 {path}\n请复制 {EXAMPLE_CONFIG.name} 为 {path.name} 并填入站点信息。"
        )
    return load_json(path, default={"sites": []})


def get_target_sites(config: dict, only: str = "") -> list[dict]:
    """按 enabled 与 --only 过滤出本次要采集的站点列表。"""
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()}
    sites = [s for s in config.get("sites", []) if s.get("enabled", True)]
    if wanted:
        sites = [s for s in sites if s.get("id") in wanted]
    return sites


_INTEGRATION_RUNTIMES = {}
_INTEGRATION_RUNTIME_LOCK = __import__('threading').RLock()


def integration_runtime(config_path=None, data_dir=None):
    from integrations.runtime import IntegrationRuntime
    config_path, data_dir = Path(config_path or DEFAULT_CONFIG), Path(data_dir or OUT_DIR)
    identity = tuple(str(p.resolve()) for p in (config_path, SETTINGS_FILE, data_dir))
    with _INTEGRATION_RUNTIME_LOCK:
        if identity not in _INTEGRATION_RUNTIMES:
            _INTEGRATION_RUNTIMES[identity] = IntegrationRuntime(config_path, SETTINGS_FILE, data_dir)
        return _INTEGRATION_RUNTIMES[identity]


def _site_diagnostics(meta: dict) -> list[str]:
    """把某站点各功能的证据摊平成可读行，供 --debug 排查。

    D 阶段起取数统一走 router，旧适配器内部那套 print 调试已废弃；
    这里直接读统一结果里的证据（提供者/端点/错误类别），对 api 与 compat 两条路都有效。
    """
    lines: list[str] = []
    for name, evidence in sorted((meta.get("features") or {}).items()):
        if not isinstance(evidence, dict):
            continue
        lines.append("      {:<18} provider={:<6} status={:<11} endpoint={} error={}".format(
            name, evidence.get("provider") or "-", evidence.get("status") or "-",
            evidence.get("source_endpoint") or "-", evidence.get("error_category") or "-"))
    return lines


def collect_site(site: dict, proxy: str, debug: bool, model_filter: set[str] | None = None,
                 should_stop: Callable[[], bool] | None = None,
                 runtime: IntegrationRuntime | None = None, trigger: str = "manual") -> dict:
    from integrations.service import pricing_snapshot
    from integrations.providers import ProviderFailure
    try:
        runtime = runtime or integration_runtime()
        # trigger 只用于「站点 × 功能」证据记录（manual/schedule/cli），不改变任何请求行为。
        result = pricing_snapshot(runtime.router(site, proxy, trigger=trigger), site,
                                  model_filter=model_filter, should_stop=should_stop)
        if debug:
            # 诊断信息挂在 meta 上由调用方输出，避免库函数直接写 stdout。
            result["meta"]["diagnostics"] = _site_diagnostics(result["meta"])
        return result
    except (ProviderFailure, ValueError, TypeError, KeyError) as error:
        meta = {"id": site.get("id"), "name": site.get("name") or site.get("id"),
                "type": site.get("type"), "ok": False, "model_count": 0, "offer_count": 0,
                "error": str(error) if isinstance(error, ProviderFailure) else "站点配置格式不正确"}
        if debug:
            meta["diagnostics"] = []
        return {"meta": meta, "offers": []}


def expand_model_keys(keys: list[str] | None, out_dir: Path | None = None) -> set[str] | None:
    """把前端选中的「模型（聚类 key）」展开成可匹配的归一化名集合。

    前端选的是聚类后的模型（一个条目可能对应多个站点的不同命名），
    所以要借助现有索引把它的全部别名一起展开，才能和原始报价对上。
    传空列表或 None 表示不筛选。
    """
    out_dir = Path(out_dir) if out_dir is not None else OUT_DIR
    if not keys:
        return None
    wanted = {canonical_model_name(k) for k in keys if k}
    index_path = out_dir / "index.json"
    if index_path.exists():
        index = load_json(index_path, default=None)
        for model in (index or {}).get("models", []):
            if model.get("key") in wanted:
                names = model.get("alias_keys") or model.get("aliases") or []
                for alias in list(names) + [model.get("key") or ""]:
                    wanted.add(canonical_model_name(alias))
    return wanted


def _fallback_from_old(old_payload: dict | None, site_id: str) -> tuple[dict | None, list[dict]]:
    """从上一版数据里把某个站点的 meta 和报价原样取回来。

    用途：本次采集失败的站点，沿用上一次的数据，别让一次网络抖动把它的报价洗成 0。
    上一版里没有这个站（首次采集）就返回 (None, [])，保持原样。
    """
    if not old_payload:
        return None, []
    old_meta = next((m for m in (old_payload.get("sites") or [])
                     if isinstance(m, dict) and m.get("id") == site_id), None)
    if not old_meta:
        return None, []
    offers = [o for m in (old_payload.get("models") or [])
              for o in (m.get("offers") or []) if o.get("site") == site_id]
    return old_meta, offers


def run_collection(only: str = "", logger: Callable[[str], None] = print, throttle: float | None = None,
                   config_path: Path | None = None, out_dir: Path | None = None,
                   debug: bool = False, models: list[str] | None = None,
                   merge: bool = False, should_stop: Callable[[], bool] | None = None,
                   on_progress: Callable[[int, int, str, bool], None] | None = None,
                   trigger: str = "manual") -> dict:
    """执行一次采集并落盘，返回结果 payload。

    这是脚本与本地服务共用的唯一入口：
      · 服务工作进程 —— 定时任务与前端「手动采集」都调它
      · 命令行 —— main() 也调它，保持一致行为

    三个控制参数：
      throttle    站点之间的间隔秒数。串行 + 延时是为了降低被判定为爬虫而封 IP 的概率。
      models      只保留这些模型的报价；其余数据原样保留（配合 merge 使用）。
      merge       把本次结果按「站点 × 模型」合并进已有数据，而不是整份覆盖。
                  手动挑站点/挑模型时必须用它，否则会把没采的部分洗掉。
      should_stop 外部传进来的停止信号（callable），每次采站点前检查一次，
                  命中就中止本次任务并保留上一次的数据。
    """
    from integrations.configuration import ConfigurationStore, resolve_proxy, site_proxy
    # 默认值在函数体内解析，保证测试或部署切换模块属性后仍然生效。
    config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG
    out_dir = Path(out_dir) if out_dir is not None else OUT_DIR
    config = ConfigurationStore(config_path).migrate()
    if not Path(config_path).exists():
        config = load_config(config_path)
    settings = load_settings()

    # 全局代理按「直连 / 跟随系统 / 手填」三模式解析。
    # sites.json 里的 proxy 是老配置可能写在那里的值，作为手填地址的兜底。
    proxy = resolve_proxy(settings.get("proxy_mode"),
                          settings.get("proxy") or config.get("proxy") or "")
    threshold = float(config.get("cluster_threshold") or settings.get("cluster_threshold") or 0.9)
    if throttle is None:
        throttle = float(settings.get("throttle_sec") or config.get("throttle_sec") or 0)

    sites = get_target_sites(config, only)
    if not sites:
        raise SystemExit("没有可采集的站点，请检查 sites.json 里是否有 enabled 的站点。")

    model_filter = expand_model_keys(models, out_dir) if models else None
    # 历史数据不再只给 merge 用：全量采集时某个站点失败了，也要靠它把该站的旧数据搬回来
    old_payload = load_json(out_dir / "prices.json", default=None)
    if merge and not old_payload:
        logger("  ! 没有历史数据可合并，本次按全量处理\n")

    scope_bits = []
    if only:
        scope_bits.append(f"站点 {len(sites)} 个")
    if model_filter:
        scope_bits.append(f"模型 {len(models)} 个")
    logger(f"开始采集 {len(sites)} 个站点"
           + (f"（{'，'.join(scope_bits)}）" if scope_bits else "（全量）")
           + (f"，站点间隔 {throttle:g} 秒" if throttle else "") + "\n")

    all_offers: list[dict] = []
    site_meta: list[dict] = []
    stopped = False
    for index, site in enumerate(sites):
        if should_stop and should_stop():
            stopped = True
            logger("  已收到停止指令，正在结束本次采集（上一次的数据不会被改动）")
            break
        # 站点之间留出间隔，避免短时间高频访问触发风控；等待期间也能响应停止
        if index and throttle > 0 and interruptible_sleep(throttle, should_stop):
            stopped = True
            logger("  已收到停止指令，正在结束本次采集（上一次的数据不会被改动）")
            break
        started = time.time()
        # 站点级代理优先：写在这一站的 proxy 用这一站的，没写才回落到全局代理
        result = collect_site(site, site_proxy(site, proxy), debug, model_filter=model_filter, should_stop=should_stop,
                              runtime=integration_runtime(config_path, out_dir), trigger=trigger)
        meta = result["meta"]
        # --debug 的诊断只用于控制台排查，取出来后从 meta 里摘掉，
        # 免得这类一次性调试信息落进 prices.json 快照。
        diagnostics = list(meta.pop("diagnostics", None) or []) if debug else []
        fallback_note = ""
        if not meta["ok"]:
            # 本次失败的站点：把上一版的数据原样搬回来并标记 stale，
            # 避免「一个站超时 → 整份覆盖 → 该站报价归零」。
            fb_meta, fb_offers = _fallback_from_old(old_payload, site.get("id"))
            if fb_meta and (not meta.get("binding_ref") or fb_meta.get("binding_ref") == meta.get("binding_ref")):
                meta = {**fb_meta, "stale": True, "stale_error": meta.get("error")}
                result = {"meta": meta, "offers": fb_offers}
                fallback_note = f"，沿用上一次的 {len(fb_offers)} 条报价"
        site_meta.append(meta)
        all_offers.extend(result["offers"])
        cost = time.time() - started
        # 回退时 meta 来自上一版数据，字段未必齐全，这里统一用 get 兜底
        if meta.get("stale"):
            # 本次失败、只沿用旧数据的站点必须标成 ⚠ 并带上真实原因：
            # 旧 meta 里 ok 仍是 True，沿用旧写法会打 ✓，日志看着全绿其实一条都没抓到。
            logger(f"  ⚠ {meta.get('name') or site.get('id'):<16} 本次失败：{meta.get('stale_error') or '未知错误'}，"
                   f"沿用上一次的 {len(result.get('offers') or [])} 条报价，用时 {cost:.1f}s")
        elif meta.get("ok"):
            auth = meta.get("auth_state") or "anonymous"
            flag = f" [{auth}]" if auth != "anonymous" else ""
            logger(f"  ✓ {meta.get('name') or site.get('id'):<16} 模型 {meta.get('model_count') or 0:>4} 个，"
                   f"报价 {meta.get('offer_count') or 0:>5} 条，用时 {cost:.1f}s{flag}{fallback_note}")
        else:
            logger(f"  ✗ {meta.get('name') or site.get('id'):<16} 失败：{meta.get('error')}")
        for line in diagnostics:
            logger(line + "\n")
        if on_progress:
            on_progress(index + 1, len(sites), meta.get("name") or site.get("id"), bool(meta["ok"]))

    # 一个都没采到（失败站点即使沿用回旧数据也不算「本次成功」）时同样不写盘：
    # 否则只会把 generated_at 刷成当下，让首页「数据更新于」谎报新鲜，
    # 而内容其实全是上一次的。写盘必须至少有一站是这次真抓到的。
    fresh_count = sum(1 for m in site_meta if m.get("ok") and not m.get("stale"))
    if stopped or fresh_count == 0 or not all_offers:
        reason = ("被手动停止" if stopped
                  else "所有站点都失败了" if not all_offers and site_meta
                  else "没有一个站点这次抓到报价")
        logger(f"\n本次未写入数据：{reason}，本地仍保留上一次的采集结果。")
        # 所有站点一起挂通常是环境级故障（代理端口失效、断网），把错误汇总出来才能一眼看出来
        failed = [m for m in site_meta if m.get("stale") or not m.get("ok")]
        if site_meta and len(failed) == len(site_meta):
            tally = Counter((m.get("stale_error") or m.get("error") or "未知错误") for m in failed)
            logger("  " + f"{len(failed)}/{len(site_meta)} 个站点全部失败，错误分布："
                   + "；".join(f"{err} × {n}" for err, n in tally.most_common(3)))
        return {"stopped": True, "written": False, "reason": reason,
                "sites": site_meta, "model_count": 0, "offer_count": 0,
                "models": [], "generated_at": None}

    # —— 合并：只替换「本次采集到的 站点×模型」，其余原样保留 ——
    if merge and old_payload:
        touched = {(o["site"], o["model_key"]) for o in all_offers}
        old_offers = [o for m in (old_payload.get("models") or []) for o in (m.get("offers") or [])]
        kept = [o for o in old_offers if (o.get("site"), o.get("model_key")) not in touched]
        merged_offers = kept + all_offers
        collected_ids = {m["id"] for m in site_meta}
        site_meta = [m for m in (old_payload.get("sites") or []) if m.get("id") not in collected_ids] + site_meta
        logger(f"\n已合并进现有数据：更新 {len(all_offers)} 条，保留 {len(kept)} 条")
    else:
        merged_offers = all_offers

    models_built = build_models(merged_offers, threshold)

    stale_ids = [m["id"] for m in site_meta if m.get("stale")]
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cluster_threshold": threshold,
        "base_currency": "USD",
        "note": "价格为 USD / 1M tokens（按次模型为 USD / 次），已按各自分组倍率折算",
        "site_count": sum(1 for m in site_meta if m["ok"]),
        "model_count": len(models_built),
        "offer_count": len(merged_offers),
        "sites": site_meta,
        "models": models_built,
        "merged": bool(merge and old_payload),
        # 本次没采到、沿用上一版数据的站点，前端好提示「这几站不是最新的」
        "stale_sites": stale_ids,
        "scope": {
            "sites": [s for s in (only or "").split(",") if s] or [m["id"] for m in site_meta],
            "models": list(models or []),
            "full": not (only or model_filter),
        },
    }

    # Configuration edits and strong stop invalidate the whole pending business snapshot.
    from integrations.service import configuration_binding
    if ((should_stop and should_stop()) or configuration_binding(load_config(config_path)) != configuration_binding(config)):
        return {"stopped": True, "written": False, "reason": "采集已停止或站点配置已变化"}
    write_outputs(payload, out_dir)

    ok_count = sum(1 for m in site_meta if m["ok"])
    logger(f"\n完成：{ok_count}/{len(site_meta)} 个站点成功，"
           f"共 {len(models_built)} 个模型、{len(merged_offers)} 条报价"
           + (f"（其中 {len(stale_ids)} 个站点沿用了上一次的数据："
              + "、".join((m.get("name") or m.get("id")) for m in site_meta if m.get("stale")) + "）"
              if stale_ids else ""))
    for meta in site_meta:
        if not meta.get("ok"):
            logger(f"  ! {meta.get('name') or meta.get('id')}：{meta.get('error')}")
    return payload


def write_outputs(payload: dict, out_dir: Path) -> dict:
    """把采集结果写成三份本地文件（数据全部留在本地，前端从本地读取，不实时请求站点）。

      prices.json  全量数据（含每条报价），供本地服务与排障使用
      prices.js    同内容的 JS 版本，双击 HTML（file://）时兜底加载
      index.json   只有模型摘要的轻量索引，前端首屏加载它，速度快一个量级
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "prices.json"
    write_json_atomic(json_path, payload)

    # 同步输出一份 JS，方便直接双击 HTML（file:// 下 fetch 本地 json 会被浏览器拦掉）
    js_path = out_dir / "prices.js"
    tmp = js_path.with_suffix(js_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        fp.write("window.__RELAY_PRICE_DATA__ = ")
        json.dump(payload, fp, ensure_ascii=False)
        fp.write(";\n")
    os.replace(tmp, js_path)

    index_path = out_dir / "index.json"
    write_json_atomic(index_path, build_index(payload))

    return {"json": json_path, "js": js_path, "index": index_path}


def build_index(payload: dict) -> dict:
    """从全量数据里抽出模型摘要，供前端首屏快速渲染（点击明细再按需请求）。

    除了最低价，还带上每个模型在**各站点**的最低输入/输出/单次价（by_site），
    让前端筛选站点、排序都能在本地完成，不必反复向服务要数据。
    """
    models = []
    for m in payload.get("models", []):
        by_site: dict[str, dict] = {}
        for offer in m["offers"]:
            sid = offer["site"]
            cell = by_site.setdefault(sid, {"input": None, "output": None, "call": None})
            if not price_is_rankable(offer):
                continue
            for src, dst in (("input", "input"), ("output", "output"), ("per_call", "call")):
                value = offer.get(src)
                if value is None:
                    continue
                # 同站点不同分组可能都是有效报价，这里只保留该站的最低价
                if cell[dst] is None or value < cell[dst]:
                    cell[dst] = value
        models.append({
            "key": m["key"],
            "display": m["display"],
            "aliases": m["aliases"],
            # 归一化后的别名集合：手动采集挑模型时用它把选中的聚类展开成可匹配的名字
            "alias_keys": sorted({canonical_model_name(a) for a in m["aliases"]}),
            "site_count": m["site_count"],
            "offer_count": m["offer_count"],
            "vendors": m["vendors"],
            "tags": m["tags"],
            "endpoints": m["endpoints"],
            "has_token": m["has_token"],
            "has_call": m["has_call"],
            "min_input": m["min_input"],
            "min_output": m["min_output"],
            "min_call": m["min_call"],
            "max_input": m["max_input"],
            "by_site": by_site,
        })

    return {
        "generated_at": payload.get("generated_at"),
        "base_currency": payload.get("base_currency"),
        "note": payload.get("note"),
        "site_count": payload.get("site_count"),
        "model_count": payload.get("model_count"),
        "offer_count": payload.get("offer_count"),
        "cluster_threshold": payload.get("cluster_threshold"),
        "stale_sites": payload.get("stale_sites") or [],
        "sites": payload.get("sites", []),
        "models": models,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="采集中转站模型价格")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--only", default="", help="只采集指定站点 id，逗号分隔")
    parser.add_argument("--models", default="", help="只采集指定模型（聚类 key，逗号分隔），需配合 --merge")
    parser.add_argument("--merge", action="store_true", help="把结果合并进现有数据，而不是整份覆盖")
    parser.add_argument("--throttle", type=float, default=None,
                        help="站点之间的间隔秒数，默认取 settings.json 的 throttle_sec")
    parser.add_argument("--debug", action="store_true", help="输出诊断信息")
    args = parser.parse_args()

    if DEPLOY_ERROR is not None:
        print(f"部署配置错误：{DEPLOY_ERROR}")
        return 1

    # CLI 与服务写同一批文件，必须共用状态目录锁；冲突时只提示、不写快照。
    from integrations.state_lock import StateLock

    lock = StateLock(DEFAULT_CONFIG.parent)
    if not lock.acquire():
        print(f"服务正在运行（PID {lock.holder_pid() or '未知'}），请改用页面右上角的“手动采集”，"
              "或先关闭服务窗口。")
        return 2
    try:
        return _run_cli(args)
    finally:
        lock.release()


def _run_cli(args) -> int:
    config_path = args.config if args.config is not None else DEFAULT_CONFIG
    out_dir = args.out_dir if args.out_dir is not None else OUT_DIR
    payload = run_collection(only=args.only, throttle=args.throttle,
                             config_path=config_path, out_dir=out_dir,
                             debug=args.debug,
                             models=[x for x in args.models.split(",") if x.strip()],
                             merge=args.merge, trigger="cli")
    ok = sum(1 for s in payload["sites"] if s["ok"])
    print(f"\n输出：{out_dir / 'prices.json'}"
          f"\n      {out_dir / 'prices.js'}"
          f"\n      {out_dir / 'index.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
