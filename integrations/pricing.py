"""新旧提供者共用的纯价格解析；不依赖登录、网络或兼容实现。"""


from __future__ import annotations


import math


import re


class FetchError(Exception):
    """共享采集/解析错误；fetch_prices 保留同名导出。"""


MODEL_RATIO_TO_USD_PER_M = 2.0


BRACKET_PREFIX = re.compile(r"^\s*[\[【(（][^\]】)）]*[\]】)）]\s*")


def to_float(value) -> float | None:
    """把可能为 None / 空串 / 非法值的数据转成 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def canonical_model_name(name: str) -> str:
    """模型名归一化：去掉前缀装饰标记、统一小写，用于跨站归类。

    只做「保守」清洗，不擅自切分厂商后缀，避免把 gpt-5.4-openai 和 gpt-5.4-azure
    这类确实不同的渠道混在一起（前端详情里仍会展示各站原始模型名）。
    """
    text = BRACKET_PREFIX.sub("", (name or "").strip())
    text = re.sub(r"\s+", " ", text).lower()
    return text.strip()


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """按不在括号/引号内的分隔符切分。"""
    parts, buf, depth, quote = [], [], 0, None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'" :
            quote = ch
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth -= 1
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _unwrap_expression(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for pos, ch in enumerate(text):
            depth += (ch == "(") - (ch == ")")
            if depth == 0:
                break
        if pos != len(text) - 1:
            break
        text = text[1:-1].strip()
    return text


def _default_service_tier_base(expr: str) -> str | None:
    """仅识别未指定 service_tier 时倍率为 1 的完整顶层乘积。"""
    factors = _split_top_level(_unwrap_expression(expr), "*")
    if len(factors) < 2:
        return None
    condition = re.compile(
        r'''param\s*\(\s*(["'])service_tier\1\s*\)\s*==\s*(["'])(?:priority|fast|ultrafast)\2\s*\?\s*'''
        r'''([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*:\s*1(?:\.0+)?\s*'''
    )
    for part in factors[1:]:
        match = condition.fullmatch(_unwrap_expression(part))
        if not match or not math.isfinite(float(match[3])):
            return None
    # 不支持的表达式不可截取局部；基础式也必须由调用方完整解析。
    return factors[0].strip()


def parse_tier_coefficients(expr: str) -> dict | None:
    """只解析可证明在短上下文（len=p=0）生效的线性档位，不执行远端表达式。"""
    if not isinstance(expr, str) or not expr or len(expr) > 100000:
        return None
    unwrap = _unwrap_expression
    expr = _default_service_tier_base(expr) or expr

    def select(text, level=0):
        if level > 32:
            return None
        text = unwrap(text)
        depth, quote, question, nested = 0, None, None, 0
        for pos, ch in enumerate(text):
            if quote:
                if ch == quote and (pos == 0 or text[pos - 1] != "\\"):
                    quote = None
                continue
            if ch in "\"'":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and ch == "?":
                if question is None:
                    question = pos
                else:
                    nested += 1
            elif depth == 0 and ch == ":" and question is not None:
                if nested:
                    nested -= 1
                    continue
                condition = unwrap(text[:question])
                match = re.fullmatch(r"(?:len|p)\s*(<=|>=|<|>|==|!=)\s*(\d+(?:\.\d+)?)", condition)
                reverse = False
                if not match:
                    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(<=|>=|<|>|==|!=)\s*(?:len|p)", condition)
                    reverse = True
                if not match:
                    return None
                op, value = (match[2], float(match[1])) if reverse else (match[1], float(match[2]))
                left, right = (value, 0) if reverse else (0, value)
                choice = {"<": left < right, "<=": left <= right, ">": left > right,
                          ">=": left >= right, "==": left == right, "!=": left != right}[op]
                return select(text[question + 1:pos] if choice else text[pos + 1:], level + 1)
        if question is not None:
            return None
        return text

    body = select(expr)
    if body is None:
        return None
    tier = re.fullmatch(r"tier\s*\((.*)\)", body, re.S)
    if tier:
        inner = _split_top_level(tier[1], ",")
        if len(inner) != 2 or not re.fullmatch(r'''\s*(?:"[^"\\]*"|'[^'\\]*')\s*''', inner[0]):
            return None
        body = inner[1]
    result: dict[str, float] = {}
    number = r"(?:[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)"
    variable = r"[a-z_][a-z0-9_]*"
    term = re.compile(rf"\s*(?:({variable})\s*\*\s*({number})|({number})\s*\*\s*({variable}))\s*")
    field_map = {"p": "input", "c": "output", "cr": "cache_read", "cc": "cache_write", "cc1h": "cache_write_1h"}
    # 逐项完整匹配，避免从非线性表达式中捡出部分系数并误报价格。
    for item in re.split(r"(?<![eE])\+", body):
        match = term.fullmatch(item)
        if not match:
            return None
        var, num = (match[1], float(match[2])) if match[1] else (match[4], float(match[3]))
        if not math.isfinite(num):
            return None
        field = field_map.get(var)
        if field is None:
            return None
        result[field] = result.get(field, 0.0) + num
    return result or None


def parse_newapi_pricing(site: dict, payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise FetchError("定价响应缺少模型列表")
    group_ratio: dict = payload.get("group_ratio") or {}
    usable_group: dict = payload.get("usable_group") or {}
    # 站点级的分组表：前端要按它判断「某个分组在这个站能不能用 / 倍率多少」
    groups_data = {
        name: {
            "ratio": to_float(ratio),
            "desc": (usable_group.get(name) if isinstance(usable_group, dict) else "") or "",
            "usable": True,
        }
        for name, ratio in (group_ratio or {}).items()
    }
    vendors = {
        v.get("id"): v
        for v in (payload.get("vendors") or [])
        if isinstance(v, dict) and v.get("id") is not None
    }
    site_id = site["id"]
    site_name = site.get("name") or site["id"]

    offers: list[dict] = []
    for raw in payload.get("data") or []:
        model_name = (raw.get("model_name") or "").strip()
        if not model_name:
            continue

        vendor = vendors.get(raw.get("vendor_id")) or {}
        groups = raw.get("enable_groups") or []
        if not groups:
            # 个别版本不返回可用分组，退化为默认分组
            groups = ["default"] if "default" in group_ratio else list(group_ratio.keys())

        for group in groups:
            ratio = to_float(group_ratio.get(group))
            ratio_known = ratio is not None
            if ratio is None:
                # 分组出现在模型的可用列表里、但倍率表没给出 —— 按 1.0 估算并打标记
                ratio = 1.0

            offer = {
                "site": site_id,
                "site_name": site_name,
                "model_raw": model_name,
                "model_key": canonical_model_name(model_name),
                "group": group,
                "group_desc": usable_group.get(group) if isinstance(usable_group, dict) else "",
                "group_ratio": ratio,
                "ratio_estimated": not ratio_known,
                "quota_type": raw.get("quota_type"),
                "vendor": vendor.get("name") or "",
                "tags": [t.strip() for t in (raw.get("tags") or "").split(",") if t.strip()],
                "endpoints": raw.get("supported_endpoint_types") or [],
                "description": raw.get("description") or "",
                "dynamic": False,
                "billing_expr": None,
                "input": None,
                "output": None,
                "cache_read": None,
                "cache_write": None,
                "per_call": None,
                "model_ratio": to_float(raw.get("model_ratio")),
                "completion_ratio": to_float(raw.get("completion_ratio")),
                "model_price": to_float(raw.get("model_price")),
            }

            if raw.get("quota_type") == 1:
                # 按次计费：单次价 × 分组倍率
                base_call = to_float(raw.get("model_price"))
                offer["per_call"] = base_call * ratio if base_call is not None else None
                offer["pricing_verified"] = base_call is not None
            else:
                expr = raw.get("billing_expr") if raw.get("billing_mode") == "tiered_expr" else None
                coeffs = parse_tier_coefficients(expr) if expr else None
                if expr:
                    # 动态计价：表达式系数即 USD/1M tokens，再乘分组倍率
                    offer["dynamic"] = True
                    offer["billing_expr"] = expr
                    offer["pricing_verified"] = bool(coeffs)
                    offer["pricing_basis"] = "len=0" if coeffs else "unknown_expression"
                    base_expr = _default_service_tier_base(expr) if coeffs else None
                    if base_expr:
                        # 默认报价不包含 priority / fast / ultrafast 的附加倍率。
                        offer["service_tier_basis"] = "unspecified"
                        offer["billing_base_expr"] = base_expr
                    coeffs = coeffs or {}
                    offer["input"] = coeffs.get("input")
                    offer["output"] = coeffs.get("output")
                    offer["cache_read"] = coeffs.get("cache_read")
                    offer["cache_write"] = coeffs.get("cache_write")
                    offer["cache_write_1h"] = coeffs.get("cache_write_1h")
                    for key in ("input", "output", "cache_read", "cache_write", "cache_write_1h"):
                        if offer[key] is not None:
                            offer[key] = offer[key] * ratio
                else:
                    model_ratio = to_float(raw.get("model_ratio"))
                    completion_ratio = to_float(raw.get("completion_ratio"))
                    unit = model_ratio * MODEL_RATIO_TO_USD_PER_M * ratio if model_ratio is not None else None
                    offer["input"] = unit
                    offer["output"] = unit * completion_ratio if unit is not None and completion_ratio is not None else None
                    offer["pricing_verified"] = unit is not None
                    cache_ratio = to_float(raw.get("cache_ratio"))
                    create_cache_ratio = to_float(raw.get("create_cache_ratio"))
                    offer["cache_read"] = unit * cache_ratio if unit is not None and cache_ratio is not None else None
                    offer["cache_write"] = unit * create_cache_ratio if unit is not None and create_cache_ratio is not None else None

            if not math.isfinite(ratio) or ratio < 0 or any(
                    offer.get(key) is not None and (not math.isfinite(offer[key]) or offer[key] < 0)
                    for key in ("input", "output", "per_call", "cache_read", "cache_write", "cache_write_1h")):
                raise FetchError("价格或倍率无效")
            offers.append(offer)
    return {"offers": offers, "groups": groups_data, "raw_model_count": len(payload.get("data") or [])}


def parse_sub2api_pricing(site: dict, payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("success") is False or payload.get("code", 0) not in (0, 200):
        raise FetchError("模型广场业务响应失败")
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if not isinstance(payload.get("groups"), list):
        raise FetchError("模型广场缺少 groups，未覆盖原有价格")
    site_id = site["id"]
    site_name = site.get("name") or site["id"]
    offers: list[dict] = []
    raw_model_count = 0

    for group in payload.get("groups") or []:
        group_name = group.get("name") or "default"
        multiplier = to_float(group.get("user_rate_multiplier"))
        if multiplier is None:
            multiplier = to_float(group.get("rate_multiplier"))
        if multiplier is None or not math.isfinite(multiplier) or multiplier < 0:
            raise FetchError(f"分组 {group_name} 的价格倍率缺失或无效")
        for model in group.get("models") or []:
            raw_model_count += 1
            name = (model.get("name") or "").strip()
            if not name:
                continue
            pricing = model.get("pricing") or {}
            def price(field, token_price=True):
                value = to_float(pricing.get(field))
                if value is None:
                    return None
                if not math.isfinite(value) or value < 0:
                    raise FetchError(f"模型 {name} 的 {field} 无效")
                converted = value * multiplier * (1000000 if token_price else 1)
                if not math.isfinite(converted):
                    raise FetchError(f"模型 {name} 的 {field} 换算后超出有效范围")
                return converted

            prices = {field: price(remote) for field, remote in (
                ("input", "input_price"), ("output", "output_price"),
                ("cache_read", "cache_read_price"), ("cache_write", "cache_write_price"))}
            if prices["input"] is not None and 0 < prices["input"] < 1e-4:
                raise FetchError(f"模型 {name} 换算后输入价格低于 1e-4 USD/1M，疑似单位不匹配，拒绝入库")
            time_pricing = model.get("time_pricing", group.get("time_pricing", pricing.get("time_pricing")))
            image_independent = model.get("image_rate_independent", group.get("image_rate_independent", pricing.get("image_rate_independent")))
            image_multiplier = model.get("image_rate_multiplier", group.get("image_rate_multiplier", pricing.get("image_rate_multiplier")))
            pending = bool(time_pricing or pricing.get("intervals") or image_independent
                           or group.get("peak_rate_enabled") or group.get("video_rate_independent"))
            offers.append({
                "site": site_id,
                "site_name": site_name,
                "model_raw": name,
                "model_key": canonical_model_name(name),
                "group": group_name,
                "group_id": group.get("id"),
                "group_desc": group.get("description") or "",
                "group_ratio": multiplier,
                "ratio_estimated": False,
                "quota_type": 1 if pricing.get("per_request_price") is not None else 0,
                "vendor": model.get("platform") or "",
                "tags": [],
                "endpoints": [],
                "description": "",
                "dynamic": pending,
                "billing_expr": None,
                **prices,
                "per_call": price("per_request_price", token_price=False),
                "price_unit": "USD/request" if pricing.get("per_request_price") is not None else "USD/1M tokens",
                "source_price_unit": "USD/request" if pricing.get("per_request_price") is not None else "USD/token",
                "pricing_verified": not pending,
                "pricing_basis": "base_rate_pending_dynamic" if pending else "base_rate",
                "time_pricing": time_pricing,
                "pricing_intervals": pricing.get("intervals"),
                "image_rate_independent": image_independent,
                "image_rate_multiplier": image_multiplier,
                "peak_rate_enabled": group.get("peak_rate_enabled"),
                "peak_start": group.get("peak_start"),
                "peak_end": group.get("peak_end"),
                "peak_rate_multiplier": group.get("peak_rate_multiplier"),
                "video_rate_independent": group.get("video_rate_independent"),
                "video_rate_multiplier": group.get("video_rate_multiplier"),
                "model_ratio": None,
                "completion_ratio": None,
                "model_price": None,
            })

    return {"offers": offers, "profile": {}, "auth_state": "anonymous",
            "raw_model_count": raw_model_count}
