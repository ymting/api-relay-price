"""站点 × 功能证据矩阵与兼容退出闸门（方案第十六节）。

职责：
  · EvidenceMatrix —— 每次功能调用追加一行**不含明文凭据**的证据（站点 / 功能 / 提供者 /
    生效模式 / 触发源 / 成败 / 错误类别 / 回退原因 / 端点 / 耗时），供事后统计
    API 成功率、P95 耗时、兼容次数与原因。
  · readiness() —— 按第十六节「成熟判断」的四条件判定某个「站点 × 功能」能否停用兼容。

判定口径严格照文档：
  1. 该功能在 api_only 模式下真实成功过；
  2. 至少两次实际计划任务覆盖其任务路径 —— 但**两次定时任务不构成充分条件**，
     计划任务覆盖不到 Key 增删改、CCS 指定 Key 导入与健康详情，这些功能改用
     「受控验证」计数（见 CONTROLLED_ONLY）；
  3. 无未确认的写操作（读持久化写操作记录里仍是 prepared / sent / unknown 的条目）；
  4. 无未解决的兼容需求 —— 以「近期窗口内没有兼容回退」为证。

存储用 JSONL：一行一个事件，追加写、无读改写竞争，超过上限后保留尾部。
本模块只做本地读写，不发起任何远端请求，也不保存任何凭据明文。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping

from .contracts import (ConnectionMode, Feature, FallbackReason, ProviderKind,
                        UNRESOLVED_OPERATION_STATUSES)

# 计划任务覆盖不到的功能（文档第十六节明确列出）：
# Key 创建／修改／删除、CCS 指定 Key 导入、健康详情读取。它们不能用"两次定时采集"作依据。
CONTROLLED_ONLY = frozenset({Feature.KEYS_CREATE, Feature.KEYS_UPDATE, Feature.KEYS_DELETE,
                             Feature.CCS_USAGE, Feature.HEALTH_DETAIL})

MIN_SCHEDULED_RUNS = 2          # 普通功能：至少两次计划任务覆盖其任务路径
MIN_CONTROLLED_RUNS = 1         # 低频写/明细功能：至少一次受控验证
RECENT_WINDOW = 50              # 每个「站点 × 功能」参与判定的最近事件数
MAX_LINES = 5000                # JSONL 上限，超出后保留尾部
MAX_BYTES = 4 * 1024 * 1024     # 文件体积上限，超过就轮转，避免证据库无限膨胀

TRIGGER_SCHEDULE = "schedule"

_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _enum_value(value: Any) -> str | None:
    return value.value if hasattr(value, "value") else (str(value) if value is not None else None)


def annotation_of(provider: Any, fallback_reason: Any) -> str:
    """方案的分层标记：只有 api 提供者且未发生回退，才算接口路径真的被走通。

    兼容记录一律是「部署支持待验证」，不能据此宣布某个站点已原生支持该接口。
    """
    if _enum_value(provider) == ProviderKind.API.value and not fallback_reason:
        return "api_verified"
    if _enum_value(fallback_reason) == FallbackReason.UNSUPPORTED_ENDPOINT.value:
        return "endpoint_unsupported"
    return "deployment_unverified"


class EvidenceMatrix:
    """追加式证据库。所有写入都吞掉异常，绝不让记录失败影响业务请求。"""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    # -- 写入 -----------------------------------------------------------
    def record(self, *, site_id: str | int, feature: Any, provider: Any, ok: bool,
               mode: Any = None, trigger: str = "manual", error_category: Any = None,
               fallback_reason: Any = None, source_endpoint: str | None = None,
               auth_kind: Any = None, elapsed_ms: float | None = None, at: str | None = None,
               error_http_status: int | None = None, error_response_format: str | None = None) -> None:
        # site_id 是站点标识字符串；项目里 site 一律指站点字典，这里刻意区分开。
        row = {
            "at": at or _now(),
            "site": str(site_id),
            "feature": _enum_value(feature),
            "provider": _enum_value(provider),
            "mode": _enum_value(mode),
            "trigger": str(trigger or "manual"),
            "ok": bool(ok),
            "error_category": _enum_value(error_category),
            "error_http_status": error_http_status if type(error_http_status) is int
                and 100 <= error_http_status <= 599 else None,
            "error_response_format": error_response_format if error_response_format in ("html", "json", "other") else None,
            "fallback_reason": _enum_value(fallback_reason),
            # 端点是路径（如 /api/user/self），不含查询串与凭据
            "endpoint": str(source_endpoint or "")[:200] or None,
            "auth_kind": _enum_value(auth_kind),
            "elapsed_ms": int(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else None,
            "annotation": annotation_of(provider, fallback_reason),
        }
        try:
            self._append(row)
        except Exception:
            pass

    def _append(self, row: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
        with _LOCK:
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
            self._rotate()

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size < MAX_BYTES:
                return
            rows = self.rows()
            temporary = self.path.with_name(self.path.name + ".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                for row in rows[-MAX_LINES:]:
                    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            os.replace(temporary, self.path)
        except Exception:
            pass

    # -- 读取 -----------------------------------------------------------
    def rows(self) -> list[dict]:
        try:
            with self.path.open("r", encoding="utf-8-sig") as stream:
                out = []
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict) and value.get("site") and value.get("feature"):
                        out.append(value)
                return out
        except FileNotFoundError:
            return []

    def recent(self, *, window: int = RECENT_WINDOW) -> list[dict]:
        """只保留每个「站点 × 功能」最近的 window 条，避免陈旧证据永久生效。"""
        buckets: dict[tuple[str, str], list[dict]] = {}
        for row in self.rows():
            buckets.setdefault((row["site"], row["feature"]), []).append(row)
        return [row for rows in buckets.values() for row in rows[-window:]]

    def summary(self, *, window: int = RECENT_WINDOW) -> dict[tuple[str, str], dict]:
        """聚合出方案要求的字段：成功率、P95 耗时、兼容次数与原因、错误分布、触发源分布。"""
        table: dict[tuple[str, str], dict] = {}
        for row in self.recent(window=window):
            cell = table.setdefault((row["site"], row["feature"]), {
                "site": row["site"], "feature": row["feature"], "attempts": 0, "ok": 0,
                "api_ok": 0, "api_only_ok": 0, "compat_calls": 0, "compat_reasons": {},
                "errors": {}, "triggers": {}, "last_at": None, "latencies": [], "_rows": []})
            cell["_rows"].append(row)
            cell["attempts"] += 1
            cell["ok"] += int(bool(row.get("ok")))
            if row.get("provider") == ProviderKind.API.value:
                cell["api_ok"] += int(bool(row.get("ok")))
                if row.get("ok") and row.get("mode") == ConnectionMode.API_ONLY.value:
                    cell["api_only_ok"] += 1
            elif row.get("provider") == ProviderKind.COMPAT.value:
                cell["compat_calls"] += 1
                reason = row.get("fallback_reason") or "unknown"
                cell["compat_reasons"][reason] = cell["compat_reasons"].get(reason, 0) + 1
            if row.get("error_category"):
                key = str(row["error_category"])
                cell["errors"][key] = cell["errors"].get(key, 0) + 1
            trigger = str(row.get("trigger") or "manual")
            cell["triggers"][trigger] = cell["triggers"].get(trigger, 0) + 1
            if isinstance(row.get("elapsed_ms"), int):
                cell["latencies"].append(row["elapsed_ms"])
            cell["last_at"] = row.get("at") or cell["last_at"]
        for cell in table.values():
            cell["success_rate"] = round(cell["ok"] / cell["attempts"], 4) if cell["attempts"] else None
            cell["p95_ms"] = _percentile(cell.pop("latencies"), 95)
            cell["scheduled_runs"] = cell["triggers"].get(TRIGGER_SCHEDULE, 0)
            # 受控验证 = api_only 模式下真实走通的非计划任务调用（含只读与低频写）。
            cell["controlled_runs"] = sum(
                1 for row in cell.pop("_rows", [])
                if row.get("ok") and row.get("provider") == ProviderKind.API.value
                and row.get("mode") == ConnectionMode.API_ONLY.value
                and row.get("trigger") != TRIGGER_SCHEDULE)
        return table

    # -- 配置 / 未确认写操作 ---------------------------------------------
    @staticmethod
    def pending_writes(journal_path: str | os.PathLike) -> list[dict]:
        """写操作记录里仍未确认的条目（prepared / sent / unknown）。不含明文。"""
        try:
            data = json.loads(Path(journal_path).read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, ValueError):
            return []
        out = []
        for entry in (data or {}).get("operations", []):
            if not isinstance(entry, dict):
                continue
            if entry.get("status") in UNRESOLVED_OPERATION_STATUSES:
                out.append({key: entry.get(key) for key in
                            ("id", "site", "binding", "account_id", "feature", "target_id",
                             "status", "created_at", "updated_at")})
        return out


def _percentile(values: list[int], percent: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, min(len(ordered), -(-percent * len(ordered) // 100)))
    return ordered[rank - 1]


@dataclass
class Readiness:
    site_id: str
    feature: str
    ready: bool
    blockers: list
    evidence: dict


def readiness(matrix: EvidenceMatrix, *, site_id: str | None = None,
              features: Iterable[Any] | None = None, pending: list[dict] | None = None,
              scheduled_available: bool = True) -> dict[str, dict[str, "Readiness"]]:
    """判定每个「站点 × 功能」是否满足停用兼容的四条件。

    matrix               EvidenceMatrix 实例
    site_id              只判定某个站点；None 表示判定证据里出现过的全部站点
    pending              未确认写操作列表（EvidenceMatrix.pending_writes 的结果）
    scheduled_available  站点是否真的会跑定时任务。为 False 时（文档：不为验收改动用户的
                         定时设置）普通功能改用「等价手动任务」计数，要求次数不变。
    """
    table = matrix.summary()
    sites = {site_id} if site_id else {key[0] for key in table}
    wanted = [Feature(f) for f in features] if features else list(Feature)
    # 未确认写操作按「站点 × 功能」精确阻断：一次结果不明的 keys.delete 不该拖住钱包退出。
    pending_features: dict[str, set] = {}
    for item in (pending or []):
        pending_features.setdefault(str(item.get("site")), set()).add(str(item.get("feature")))
    out = {}
    for sid in sorted(sites):
        out[sid] = {}
        for feature in wanted:
            cell = table.get((sid, feature.value))
            blockers = []
            if not cell:
                blockers.append("没有任何调用证据，无法判定该功能是否已迁移")
                out[sid][feature.value] = Readiness(sid, feature.value, False, blockers, {})
                continue
            if not cell["api_only_ok"]:
                blockers.append("该功能还没有在 api_only 模式下成功过")
            if feature in CONTROLLED_ONLY:
                # 计划任务覆盖不到这类功能（文档第十六节明确列出），只能用受控验证。
                if cell["controlled_runs"] < MIN_CONTROLLED_RUNS:
                    blockers.append(f"缺少受控验证（计划任务覆盖不到该功能，需 {MIN_CONTROLLED_RUNS} 次 "
                                    "api_only 下的真实调用）")
            elif scheduled_available:
                if cell["scheduled_runs"] < MIN_SCHEDULED_RUNS:
                    blockers.append(f"计划任务覆盖不足（{cell['scheduled_runs']}/{MIN_SCHEDULED_RUNS}）")
            elif cell["controlled_runs"] < MIN_SCHEDULED_RUNS:
                blockers.append(f"站点未开定时，等价手动任务覆盖不足"
                                f"（{cell['controlled_runs']}/{MIN_SCHEDULED_RUNS}）")
            if feature.value in pending_features.get(sid, ()):
                blockers.append("该功能存在未确认的写操作，必须先对账")
            if cell["compat_calls"]:
                blockers.append(f"近期仍发生过兼容回退（{cell['compat_calls']} 次："
                                + "、".join(f"{k}×{v}" for k, v in sorted(cell["compat_reasons"].items())) + "）")
            evidence = {key: cell[key] for key in
                        ("attempts", "ok", "success_rate", "p95_ms", "api_ok", "api_only_ok",
                         "compat_calls", "compat_reasons", "errors", "triggers", "scheduled_runs",
                         "controlled_runs", "last_at")}
            evidence["requires"] = ("controlled" if feature in CONTROLLED_ONLY
                                    else "scheduled" if scheduled_available else "manual_equivalent")
            out[sid][feature.value] = Readiness(sid, feature.value, not blockers, blockers, evidence)
    return out


def build_report(matrix: EvidenceMatrix, *, pending: list[dict] | None = None,
                 scheduled_available: bool = True) -> dict:
    """给服务端与 CLI 用的整体报告。"""
    table = readiness(matrix, pending=pending, scheduled_available=scheduled_available)
    sites = {}
    for sid, features in table.items():
        rows = {name: {"ready": item.ready, "blockers": item.blockers, "evidence": item.evidence}
                for name, item in features.items()}
        sites[sid] = {
            "ready_count": sum(1 for item in rows.values() if item["ready"]),
            "feature_count": len(rows),
            "features": rows,
        }
    return {
        "sites": sites,
        "pending_writes": list(pending or []),
        "rules": {
            "min_scheduled_runs": MIN_SCHEDULED_RUNS,
            "min_controlled_runs": MIN_CONTROLLED_RUNS,
            "recent_window": RECENT_WINDOW,
            "scheduled_available": bool(scheduled_available),
            "controlled_only": sorted(f.value for f in CONTROLLED_ONLY),
        },
        "generated_at": _now(),
    }
