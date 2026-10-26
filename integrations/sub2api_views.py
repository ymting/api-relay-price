"""Sub2API 只读记录与监控投影；不保留兑换码、交易号或任意远端正文。"""
from datetime import datetime, timezone

from .contracts import ErrorCategory
from .providers import ProviderFailure, number


def nonnegative(value, *, required=False):
    value = number(value)
    if value is None and required:
        raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
    if value is not None and value < 0:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    return value


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR) from None
    raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)


def topup_records(sources, *, account_id, unit):
    """支付订单和兑换账本分开统计；支付到账本身也会产生 balance 兑换。"""
    items, payments = [], {}
    credits, credit_count, refunded_credits = 0., 0, 0.
    for source, result in sources.items():
        for row in result.get("rows", []):
            if row.get("user_id") is not None and str(row["user_id"]) != str(account_id):
                raise ProviderFailure(ErrorCategory.ACCOUNT_MISMATCH)
            if row.get("used_by") is not None and str(row["used_by"]) != str(account_id):
                raise ProviderFailure(ErrorCategory.ACCOUNT_MISMATCH)
            status = str(row.get("status") or "").lower()
            if source == "payments":
                kind = row.get("order_type")
                amount = nonnegative(row.get("amount"), required=True)
                paid = nonnegative(row.get("pay_amount"))
                refund = nonnegative(row.get("refund_amount"))
                if refund is not None and refund > amount:
                    raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
                currency = row.get("currency")
                currency = currency.upper() if isinstance(currency, str) and len(currency) == 3 and currency.isalpha() else "unknown"
                item = {"id": "payment:" + str(row["id"]), "source": source, "type": kind,
                        "status": status, "amount": amount, "unit": unit,
                        "pay_amount": paid, "currency": currency, "refund_amount": refund,
                        "at": row.get("completed_at") or row.get("paid_at") or row.get("created_at")}
                settled = status == "completed" or bool(row.get("completed_at")) and status.startswith(("refund", "partially_refunded"))
                if kind == "balance" and settled:
                    cell = payments.setdefault(currency, {"paid": 0., "count": 0})
                    cell["count"] += 1
                    cell["paid"] = None if cell["paid"] is None or paid is None else cell["paid"] + paid
                    if status in {"refunded", "partially_refunded"}:
                        refunded_credits = None if refunded_credits is None or refund is None else refunded_credits + refund
            else:
                kind = row.get("type")
                amount = number(row.get("value"))
                if amount is None:
                    raise ProviderFailure(ErrorCategory.PARTIAL_DATA)
                if kind == "balance" and status == "used":
                    if amount < 0:
                        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
                    credits += amount
                    credit_count += 1
                item = {"id": "redeem:" + str(row["id"]), "source": source, "type": kind,
                        "status": status, "amount": amount,
                        "unit": unit if kind in {"balance", "admin_balance"} else "other",
                        "at": row.get("used_at") or row.get("created_at")}
            items.append(item)
    ledger_complete = sources.get("redemptions", {}).get("complete") is True
    public_sources = {key: {k: v for k, v in value.items() if k != "rows"} for key, value in sources.items()}
    return {"kind": "sub2api_topups", "items": items, "sources": public_sources,
            "complete": all(value.get("complete") is True for value in sources.values()),
            "credit_count": credit_count if ledger_complete else None,
            "credit_amount": credits if ledger_complete else None, "credit_unit": unit,
            "payment_totals": payments,
            "refunded_credit_amount": refunded_credits if sources.get("payments", {}).get("complete") is True else None,
            "note": "累计入账取已使用的余额兑换记录，含在线充值及赠送，退款前；不重复加支付订单，不含管理员调整和订阅。实际付款按币种单列；退款字段为钱包额度，不推算现金退款或净付款。"}


def v2_metric(metrics, health):
    if not isinstance(metrics, dict) or not isinstance(health, dict):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    rate = nonnegative(metrics.get("success_rate"))
    if rate is not None and rate > 1:
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    duration = metrics.get("duration") or {}
    ttft = metrics.get("ttft") or {}
    if not isinstance(duration, dict) or not isinstance(ttft, dict):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    latency = nonnegative(duration.get("avg_ms"))
    first = nonnegative(ttft.get("avg_ms"))
    # 普通用户的计数及延迟样本数会被清零；不能把这些零当作无样本证明。
    sampled = (health.get("overall") in {"healthy", "warning", "critical"} or
               rate is not None and rate > 0 or latency is not None or first is not None or
               nonnegative(metrics.get("request_count")) not in (None, 0))
    return {"success_rate": rate * 100 if sampled and rate is not None else None,
            "avg_latency_ms": latency, "avg_tps": None, "ttft_ms": first,
            "traffic_rpm": nonnegative(metrics.get("rpm")) or None,
            "traffic_tpm": nonnegative(metrics.get("tpm")) or None,
            "has_samples": sampled, "source": "sub2api_monitor_v2",
            "window_label": "24h", "scope_label": "可见分组"}


def v2_snapshot(value, model):
    metric = v2_metric(value.get("metrics"), value.get("health"))
    coverage = value.get("coverage")
    if not isinstance(coverage, dict) or not isinstance(value.get("trend"), list):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    series = []
    for point in value["trend"]:
        if not isinstance(point, dict):
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        cell = v2_metric(point.get("metrics"), point.get("health"))
        series.append({"ts": timestamp(point.get("bucket_start")), "success_rate": cell["success_rate"]})
    metric.update(model_name=model, recent_success_series=series,
                  coverage_complete=coverage.get("coverage_complete") is True)
    return {"models": [metric], "window_start": timestamp(coverage.get("requested_start")),
            "window_end": timestamp(coverage.get("requested_end") or coverage.get("data_through")),
            "source": "sub2api_monitor_v2"}


def v2_details(value, model):
    if not isinstance(value.get("items"), list):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    groups = []
    for row in value["items"]:
        if not isinstance(row, dict):
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        if row.get("group_id") is None or not isinstance(row.get("group_name"), str):
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        metric = v2_metric(row.get("metrics"), row.get("health"))
        # 上游会为模型过滤交集为空的分组返回无模型空行，仅跳过可确认无数据的占位。
        # 有样本、流量或桶的无模型行仍拒绝，避免将聚合数据误绑定到当前模型。
        if ("model" not in row or row["model"] == "") and (
                not metric["has_samples"] and row["health"].get("overall") == "unknown"
                and metric["traffic_rpm"] is None and metric["traffic_tpm"] is None
                and row.get("buckets") == []):
            continue
        if row.get("model") != model:
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        metric.update(group=row["group_name"], group_id=row["group_id"], series=[])
        groups.append(metric)
    return {"model_name": model, "groups": groups, "series": [], "source": "sub2api_monitor_v2"}


def v1_views(value, model):
    if not isinstance(value.get("items"), list):
        raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
    groups = []
    for row in value["items"]:
        if not isinstance(row, dict):
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        # 附加模型没有自己的 7 天汇总，不能借主模型的可用率。
        if row.get("primary_model") != model:
            continue
        rate = nonnegative(row.get("availability_7d"))
        if rate is not None and rate > 100:
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        timeline = row.get("timeline") or []
        if not isinstance(timeline, list) or any(not isinstance(point, dict) for point in timeline):
            raise ProviderFailure(ErrorCategory.PROTOCOL_ERROR)
        now = datetime.now(timezone.utc).timestamp()
        times = [timestamp(point.get("checked_at")) for point in timeline]
        # 0% 也可能是七天内无记录的默认值；旧 latest 状态不足以证明近期有失败。
        sampled = bool(rate is not None and rate > 0 or any(at is not None and now - 7*86400 <= at <= now + 300 for at in times))
        name = row.get("group_name") or row.get("name") or "未命名监控"
        groups.append({"model_name": model, "group": str(name), "success_rate": rate if sampled else None,
                       "avg_latency_ms": nonnegative(row.get("primary_latency_ms")), "avg_tps": None,
                       "source": "sub2api_monitor_v1", "has_samples": sampled,
                       "scope_label": "监控「" + str(name) + "」", "window_label": "7天探测",
                       "series": [], "recent_success_series": []})
    # 多个监控缺少可合并的分母，绝不平均百分比后冒充模型全组统计。
    return {"models": [groups[0]] if len(groups) == 1 else [], "source": "sub2api_monitor_v1"}, {
        "model_name": model, "groups": groups, "series": [], "source": "sub2api_monitor_v1"}
