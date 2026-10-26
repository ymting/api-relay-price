"""面向本机应用的投影：保留旧快照字段，同时追加功能级证据。"""
from __future__ import annotations

from .configuration import digest
from .contracts import ErrorCategory, ErrorInfo, Feature, FeatureStatus, utc_now
from .providers import ProviderFailure, RequestContext, positive_id


def require(result):
    if not result.ok:
        raise ProviderFailure(result.error_category, {"feature": result.to_dict()})
    return result.data


def binding_of(site):
    # This opaque reference isolates old private snapshots after any credential change.
    auth = {k: v for k, v in (site.get("auth") or {}).items() if k not in {"access_token", "refresh_token", "expires_at"}}
    pair = [(site.get("auth") or {}).get("access_token", ""), (site.get("auth") or {}).get("refresh_token", "")]
    manual_pair = None if site.get("_token_pair_ref") == digest(pair) else digest(pair)
    return digest([site.get("id"), site.get("base_url"), site.get("type"), site.get("_revision"), auth, manual_pair])


def configuration_binding(config):
    import copy
    value = copy.deepcopy(config)
    for site in value.get("sites", []):
        site.pop("credential_version", None)
        site.pop("_token_pair_ref", None)
        for key in ("access_token", "refresh_token", "expires_at"):
            (site.get("auth") or {}).pop(key, None)
    return digest(value)


def token_projection(row, site, *, group="default", wallet=None):
    row = dict(row)
    is_sub2 = site.get("type") == "sub2api"
    raw_group = row.get("group")
    group = str((raw_group.get("name") if isinstance(raw_group, dict) else raw_group) or group or row.get("group_id") or "default")
    status = row.get("status")
    enabled = status in (None, 1, "active", "enabled")
    scale = (wallet or {}).get("quota_per_unit")
    confirmed = (wallet or {}).get("unit") == "USD" and scale and scale > 0
    row.update(group=group, group_explicit=bool(row.get("group_id") if is_sub2 else row.get("group")),
               enabled=enabled, unlimited=row.get("quota") == 0 if is_sub2 else bool(row.get("unlimited_quota", True)),
               remain_usd=None, used_usd=None,
               expired_time=row.get("expired_time", row.get("expires_at", -1)),
               model_limited=bool(row.get("model_limits_enabled")))
    if confirmed and not is_sub2:
        row["remain_usd"] = row.get("remain_quota") / scale if isinstance(row.get("remain_quota"), (int, float)) else None
        row["used_usd"] = row.get("used_quota") / scale if isinstance(row.get("used_quota"), (int, float)) else None
    if "key" in row:
        key = str(row.pop("key") or "")
        row["key_masked"] = key[:6] + "***" + key[-4:] if key else ""
    return row


def groups_of(tokens):
    groups = {}
    for token in tokens:
        cell = groups.setdefault(token["group"], {"count": 0, "enabled": 0, "tokens": []})
        cell["count"] += 1
        cell["enabled"] += int(bool(token.get("enabled")))
        cell["tokens"].append({k: token.get(k) for k in ("id", "name", "key_masked", "enabled")})
    return groups


def _failure_message(result):
    message = ErrorInfo(result.error_category).message
    details = []
    if result.error_http_status is not None:
        details.append(f"HTTP {result.error_http_status}")
    if result.error_response_format is not None:
        details.append(result.error_response_format.upper())
    return message + ("（" + " / ".join(details) + "）" if details else "")


def account_snapshot(router, site, *, previous=None, should_stop=None):
    out = {"schema_version": 2, "site": site["id"], "site_name": site.get("name") or site["id"],
           "fetched_at": utc_now(), "ok": False, "credential": "none", "error": None,
           "binding_ref": binding_of(site), "features": {}, "quota_usd": None, "used_quota_usd": None,
           "recharge_count": None, "recharge_usd": None, "recharge_money": None, "token_count": None,
           "tokens": None, "group_keys": None, "daily_usage": None}
    profile_cache = {}
    profile = router.fetch(Feature.PROFILE, context=RequestContext(should_stop=should_stop, profile_cache=profile_cache))
    results = {Feature.PROFILE: profile}
    if profile.ok:
        out.update(ok=True, user_id=profile.account_id, credential=profile.auth_kind.value,
                   **{k: v for k, v in profile.data.items() if k in {"username", "display_name", "email", "group", "status", "role"}})
    else:
        out.update(error=_failure_message(profile),
                   error_kind="none" if profile.error_category == ErrorCategory.MISSING_CREDENTIAL else "auth")
    for feature in (Feature.WALLET, Feature.USAGE, Feature.TOPUPS, Feature.GROUPS, Feature.KEYS_LIST):
        auth = site.get("auth") or {}
        key_usage = (feature == Feature.USAGE and site.get("type") == "sub2api" and bool(auth.get("model_api_key"))
                     and not (auth.get("access_token") or auth.get("refresh_token")))
        result = router.fetch(feature, context=RequestContext(should_stop=should_stop, profile_cache=profile_cache),
                              **({"scope": "key" if key_usage else "account"} if feature == Feature.USAGE else {}))
        # Never combine successful private sections belonging to another/unknown account.
        if (result.ok and profile.ok and str(result.account_id) != str(profile.account_id)
                and not (key_usage and result.account_id is None)):
            from dataclasses import replace
            result = replace(result, status=FeatureStatus.ERROR, data=None, last_success_at=None,
                             error_category=ErrorCategory.ACCOUNT_MISMATCH)
        results[feature] = result
    same = bool(previous and previous.get("binding_ref") == out["binding_ref"] and profile.ok
                and str(previous.get("user_id")) == str(profile.account_id))
    for feature, result in results.items():
        evidence = result.to_dict()
        old = (previous.get("features") or {}).get(feature.value) if same else None
        if not result.ok and old and old.get("last_success_at"):
            evidence.update(data=old.get("data"), last_success_at=old["last_success_at"], stale=True)
        out["features"][feature.value] = evidence
    wallet = out["features"][Feature.WALLET.value]
    wallet_data = wallet.get("data") or {}
    out["wallet"] = wallet_data
    if wallet_data.get("balance_kind") == "wallet" and wallet_data.get("unit") == "USD":
        out["quota_usd"] = wallet_data.get("amount")
    usage = out["features"][Feature.USAGE.value].get("data") or {}
    if usage.get("usage_scope") == "key":
        out["key_usage"] = usage
        if results[Feature.USAGE].ok and not profile.ok:
            out.update(ok=True, key_only=True, credential="model_key", error=None, error_kind=None)
    scale = wallet_data.get("quota_per_unit")
    if scale and scale > 0 and wallet_data.get("unit") == "USD":
        raw = usage.get("raw_used_quota")
        out["used_quota_usd"] = raw / scale if isinstance(raw, (int, float)) else None
    out["request_count"] = usage.get("request_count")
    if usage.get("usage_scope") == "account" and usage.get("unit") == "USD" and usage.get("actual_cost") is not None:
        out["used_quota_usd"] = usage["actual_cost"]
    # 单日用量（站点口径的「今日」）：金额与「已使用」同源换算，Token 统一为「输入 + 输出」、不含缓存
    daily = usage.get("daily")
    if isinstance(daily, dict):
        amount = daily.get("amount")
        # New API 拿到的是站点额度单位，复用钱包已有的换算比例折算美元，不再多发请求
        if amount is None and scale and scale > 0 and wallet_data.get("unit") == "USD" \
                and isinstance(daily.get("raw_quota"), (int, float)):
            amount = daily["raw_quota"] / scale
        out["daily_usage"] = {"date": daily.get("date"), "amount": amount,
                              "unit": daily.get("unit") or (wallet_data.get("unit") if amount is not None else None),
                              "tokens": daily.get("tokens"), "cache_tokens": daily.get("cache_tokens"),
                              "requests": daily.get("requests"), "source": daily.get("source"),
                              "complete": bool(daily.get("complete")), "error": daily.get("error")}
    for section, feature in (("usage", Feature.USAGE), ("wallet", Feature.WALLET), ("groups", Feature.GROUPS)):
        out[section + "_complete"] = results[feature].ok
        out[section + "_error"] = _failure_message(results[feature]) if not results[feature].ok else None
    for section, feature in (("topups", Feature.TOPUPS), ("keys", Feature.KEYS_LIST)):
        evidence = out["features"][feature.value]
        out.update({section + "_complete": results[feature].ok,
                    section + "_supported": results[feature].error_category not in {ErrorCategory.NOT_MIGRATED, ErrorCategory.UNSUPPORTED},
                    section + "_stale": evidence.get("stale", False),
                    section + "_error": _failure_message(results[feature]) if not results[feature].ok else None,
                    section + "_updated_at": evidence.get("last_success_at")})
    if not out["topups_supported"]:
        out["recharge_supported"] = False
    keys = out["features"][Feature.KEYS_LIST.value].get("data")
    if keys is not None:
        tokens = [token_projection(row, site, group=out.get("group") or "default", wallet=wallet_data) for row in keys.get("items", [])]
        if site.get("type") == "sub2api":
            available = (out["features"][Feature.GROUPS.value].get("data") or {}).get("available", [])
            names = {str(g["id"]): g.get("name") for g in available if isinstance(g, dict) and g.get("id") is not None}
            for token in tokens:
                token["group"] = names.get(str(token.get("group_id"))) or str(token.get("group_id") or "default")
        out.update(tokens=tokens, token_count=len(tokens), group_keys=groups_of(tokens))
    topups = out["features"][Feature.TOPUPS.value].get("data")
    if topups is not None:
        if topups.get("kind") == "sub2api_topups":
            out.update(topup_records=topups, recharge_supported=True,
                       recharge_count=topups.get("credit_count"), recharge_scope="balance_credits_gross",
                       recharge_note=topups.get("note"),
                       recharge_usd=topups.get("credit_amount") if topups.get("credit_unit") == "USD" else None)
            return out
        rows = [r for r in topups.get("items", []) if str(r.get("status", "")).lower() not in {"failed", "cancelled", "expired", "pending"}]
        out["recharge_count"] = len(rows)
        out["recharge_supported"] = True
        money = [row.get("money") for row in rows]
        out["recharge_money"] = sum(money) if all(isinstance(v, (int, float)) for v in money) else None
        # Raw topup quota has no independent unit evidence. Do not invent USD.
        if scale and scale > 0 and wallet_data.get("unit") == "USD":
            amounts = [row.get("amount") for row in rows]
            out["recharge_usd"] = sum(amounts) / scale if all(isinstance(v, (int, float)) for v in amounts) else None
    return out


def _interaction_state(router, state=None):
    if state is not None:
        return state
    profile_cache = getattr(router, "interactive_profile_cache", None)
    # 短时交互会话会复用刚创建的 Key。这样创建后立刻导入 CCS 时，
    # 不必再次分页读取列表并重复 reveal；缓存仍绑定到该会话及其凭据版本。
    token_cache = getattr(router, "interactive_token_cache", None)
    return {"profile_cache": profile_cache if profile_cache is not None else {},
            "key_list_cache": {},
            "token_cache": token_cache if token_cache is not None else {}}


def _interaction_context(state):
    # 各阶段保留独立 12 秒预算，仅共享身份及本次操作的完整列表证据。
    return RequestContext(seconds=12, profile_cache=state["profile_cache"], key_list_cache=state["key_list_cache"])


def _token_cache_binding(router, site):
    """仅内存使用：创建明文必须绑定到同一会话、配置及当前凭据版本。"""
    api = getattr(router, "api", None)
    if api is None:
        return None
    snapshot = api.session.snapshot(api.config_revision)
    return (api.session, snapshot.config_revision, snapshot.token_version, binding_of(site))


def create_token(router, site, *, name, group="", unlimited=True, quota_usd=0, days=0,
                 group_id=None, _operation=None):
    if not unlimited or quota_usd or days:
        raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
    group_value = group
    operation = _interaction_state(router, _operation)
    if site.get("type") == "sub2api" and group_id is not None:
        group_value = positive_id(group_id)
        if str(group).isdigit() and str(group) != str(group_value):
            raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
    elif site.get("type") == "sub2api" and not str(group).isdigit():
        available = require(router.fetch(Feature.GROUPS, context=_interaction_context(operation))).get("available", [])
        matches = [g["id"] for g in available if str(g.get("name")) == str(group)]
        if len(matches) != 1:
            raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
        group_value = matches[0]
    params = {"name": name, "group_id" if site.get("type") == "sub2api" else "group": group_value}
    result = router.fetch(Feature.KEYS_CREATE, context=_interaction_context(operation), **params)
    raw = require(result)
    token = token_projection(raw, site, group=group or "default")
    token.update(site=site["id"], site_name=site.get("name") or site["id"], created=True, binding_ref=binding_of(site))
    key = raw.get("key")
    if not key:
        reveal = router.fetch(Feature.KEYS_REVEAL, context=_interaction_context(operation), id=raw["id"])
        if reveal.ok:
            key = reveal.data["key"]
        else:
            token.update(needs_reveal=True, reveal_error=ErrorInfo(reveal.error_category).message)
    token["key"] = key or ""
    # 只缓存已取得明文的成功结果；reveal 失败后不能重新填回刚被清空的缓存。
    cache_binding = _token_cache_binding(router, site)
    if key and cache_binding is not None:
        from copy import deepcopy
        operation["token_cache"][str(token["id"])] = {"token": deepcopy(token), "binding": cache_binding}
    return token


HEALTH_SCHEMA_VERSION = 2


def model_health_summary(data, model, fetched_at):
    """按站点原始模型名匹配卡片汇总；无 24h 汇总时，近期样本仍独立保留。"""
    row = next((row for row in data.get("models", [])
                if isinstance(row, dict) and row.get("model_name") == model), None)
    recent = data.get("recent_success_series")
    series = recent.get(model) if isinstance(recent, dict) else None
    if not isinstance(series, list):
        series = row.get("recent_success_series") if row else None
    if row is None and not series:
        return None
    return {**{key: row[key] for key in ("source", "window_label", "scope_label", "coverage_complete", "traffic_tpm",
                                       "traffic_rpm", "ttft_ms", "has_samples") if row and key in row},
            "model_name": model, "scope": "model", "success_rate": row.get("success_rate") if row else None,
            "avg_latency_ms": row.get("avg_latency_ms") if row else None,
            "avg_tps": row.get("avg_tps") if row else None,
            "recent_success_series": series if isinstance(series, list) else [],
            "window_start": data.get("window_start"), "window_end": data.get("window_end"),
            "fetched_at": fetched_at}


def health_snapshot(router, model, *, previous=None):
    # 卡片使用 summary + recent_model；分组明细只用于展开区，不能替代卡片口径。
    context = RequestContext()
    overview = router.fetch(Feature.HEALTH_SUMMARY, context=context, recent_models=[model])
    # 认证、限流或预算失败时不继续第二次远端读取。
    blocking = {ErrorCategory.ACCOUNT_MISMATCH, ErrorCategory.AUTH_REQUIRED, ErrorCategory.AUTH_EXPIRED,
                ErrorCategory.PERMISSION_DENIED, ErrorCategory.ACCESS_BLOCKED, ErrorCategory.RATE_LIMITED,
                ErrorCategory.CANCELLED, ErrorCategory.BUDGET_EXHAUSTED, ErrorCategory.CONFIG_CHANGED,
                ErrorCategory.MISSING_CREDENTIAL, ErrorCategory.CREDENTIAL_SAVE_FAILED}
    if not overview.ok and overview.error_category in blocking and getattr(router.api, "platform", "") == "sub2api":
        require(overview)
    result = router.fetch(Feature.HEALTH_DETAIL, context=context, model=model)
    if not overview.ok and not result.ok:
        require(overview)
    data = result.data if result.ok else {}
    out = {"health_schema_version": HEALTH_SCHEMA_VERSION, "model": model, "fetched_at": utc_now(),
           "source": data.get("source") or (overview.data or {}).get("source"),
           "window_start": data.get("window_start"), "window_end": data.get("window_end"),
           "summary": data.get("summary"), "series": data.get("series") or [],
           "groups": data.get("groups") or [], "feature": result.to_dict(),
           "detail_fetched_at": result.last_success_at,
           "model_summary": model_health_summary(overview.data, model, overview.last_success_at) if overview.ok else None,
           "summary_feature": overview.to_dict(),
           "model_summary_checked_at": overview.attempted_at,
           "model_summary_error": _failure_message(overview) if not overview.ok else None,
           "detail_error": _failure_message(result) if not result.ok else None,
           "model_summary_stale": False, "detail_stale": False}
    if previous:
        if not overview.ok and previous.get("model_summary"):
            out.update(model_summary=previous["model_summary"], model_summary_stale=True)
        if not result.ok:
            for key in ("summary", "series", "groups", "window_start", "window_end"):
                if key in previous:
                    out[key] = previous[key]
            out["detail_stale"] = bool(previous.get("feature", {}).get("last_success_at") or previous.get("groups"))
            out["detail_fetched_at"] = previous.get("detail_fetched_at") or previous.get("fetched_at")
    return out


def pricing_snapshot(router, site, *, model_filter=None, should_stop=None):
    context = RequestContext(should_stop=should_stop)
    auth = site.get("auth") or {}
    personal = bool(auth.get("management_token") or auth.get("access_token") or auth.get("refresh_token")
                    or auth.get("cookie") or auth.get("username"))
    feature = Feature.PRICING_PERSONAL if personal else Feature.PRICING_PUBLIC
    result = router.fetch(feature, context=context)
    meta = {"id": site["id"], "name": site.get("name") or site["id"], "url": site["base_url"],
            "type": site.get("type", "newapi"), "ok": result.ok, "error": None,
            "auth_state": result.auth_kind.value, "requires_login": False, "site_title": site.get("name") or site["id"],
            "model_count": 0, "offer_count": 0, "groups": {}, "features": {feature.value: result.to_dict()},
            "binding_ref": binding_of(site), "config_revision": result.config_revision}
    # Price feature evidence needs metadata, not a duplicate full price table in every snapshot.
    meta["features"][feature.value]["data"] = None
    if not result.ok:
        meta["error"] = ErrorInfo(result.error_category).message
        meta["requires_login"] = result.error_category in {ErrorCategory.AUTH_REQUIRED, ErrorCategory.MISSING_CREDENTIAL}
        return {"meta": meta, "offers": []}
    data = result.data
    offers = [dict(o) for o in data["offers"] if model_filter is None or o["model_key"] in model_filter]
    summary = router.fetch(Feature.HEALTH_SUMMARY, context=context) if site.get("type", "newapi") == "newapi" else None
    if summary and summary.ok:
        health = {row.get("model_name"): row for row in summary.data.get("models", [])}
        for offer in offers:
            if offer.get("model_raw") in health:
                offer["health"] = model_health_summary(summary.data, offer["model_raw"], summary.last_success_at)
    for offer in offers:
        offer.update(provider=result.provider.value, visibility_scope=result.visibility_scope.value,
                     account_id=result.account_id, last_success_at=result.last_success_at)
    meta.update(model_count=data.get("raw_model_count", 0), offer_count=len(offers), groups=data.get("groups") or {})
    return {"meta": meta, "offers": offers}


def resolve_export(router, site, *, group="", create=True, name="cc-switch", token_id=None, model="",
                   group_id=None, _operation=None):
    operation = _interaction_state(router, _operation)
    profile = require(router.fetch(Feature.PROFILE, context=_interaction_context(operation)))
    wanted = str(group or profile.get("group") or "default")
    wanted_id = positive_id(group_id) if site.get("type") == "sub2api" and group_id is not None else None
    if wanted_id is not None and wanted.isdigit() and wanted != str(wanted_id):
        raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
    import time
    # 明确指定 token_id 且来自本次短会话创建时，服务端已有完整的真实记录。
    # 复用它可省掉一次列表分页和一次明文 reveal；其它 Key 仍走完整远端列表校验。
    token_cache = operation.get("token_cache", {})
    entry = token_cache.get(str(token_id)) if token_id is not None else None
    cached = None
    if entry is not None:
        cache_binding = _token_cache_binding(router, site)
        if (cache_binding is not None and entry.get("binding") == cache_binding
                and isinstance(entry.get("token"), dict)
                and entry["token"].get("binding_ref") == cache_binding[-1]):
            cached = entry["token"]
        else:
            # 续期、配置或会话已变更，走远端完整列表及 reveal，不能复用旧明文。
            token_cache.pop(str(token_id), None)
    if cached is not None:
        rows = [cached]
    else:
        rows = require(router.fetch(Feature.KEYS_LIST, context=_interaction_context(operation)))["items"]
    hits = []
    for raw in rows:
        # 缓存项已经是 token_projection 结果，保留其中仅存在于内存的明文 key；
        # 远端列表项仍按原规则投影并移除明文。
        token = dict(raw) if raw is cached else token_projection(raw, site, group=profile.get("group") or "default")
        if token_id is not None and str(token["id"]) != str(token_id):
            continue
        # Sub2API 用稳定 ID 核对分组；名称仅展示，不能让同名或改名导致误选 Key。
        matches_group = (str(token.get("group_id")) == str(wanted_id) if wanted_id is not None else
                         token["group"] == wanted or str(token.get("group_id")) == wanted)
        if not matches_group or not token["enabled"]:
            continue
        expiry = token.get("expired_time")
        if isinstance(expiry, str):
            try:
                from datetime import datetime
                parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    continue
                expiry = parsed.timestamp()
            except (ValueError, OverflowError, OSError):
                continue
        if isinstance(expiry, (int, float)) and 0 < expiry <= time.time():
            continue
        if token.get("model_limited") and model not in str(token.get("model_limits") or "").split(","):
            continue
        hits.append(token)
    if hits:
        hit = max(hits, key=lambda t: int(t["id"]))
        key = hit.get("key") or require(
            router.fetch(Feature.KEYS_REVEAL, context=_interaction_context(operation), id=hit["id"]))["key"]
        created = False
    else:
        if token_id is not None or not create:
            raise ProviderFailure(ErrorCategory.VALIDATION_ERROR)
        hit = create_token(router, site, name=name, group=wanted, group_id=group_id, _operation=operation)
        key, created = hit["key"], True
        if not key:
            raise ProviderFailure(ErrorCategory.ACCESS_TOKEN_SCOPE_DENIED, {"created_id": hit["id"]})
    return {"key": key, "created": created, "token_name": hit.get("name", ""), "group": wanted,
            "user_group": profile.get("group"), "base_url": site["base_url"].rstrip("/"),
            "credential": "api", "access_token": "", "user_id": profile.get("id"), "token_id": hit["id"],
            "created_token": hit if created else None}
