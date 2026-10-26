#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地服务（仅监听 127.0.0.1）
============================

给前端页面提供三类能力，页面本身不发起任何对中转站的请求：

  1. 配置读写  —— 站点清单 sites.json、偏好设置 settings.json
  2. 手动采集  —— 前端点按钮触发全量采集，带进度与日志
  3. 每日定时  —— 默认每天早上 08:00 自动采集一次

设计上刻意遵守两条：
  · 服务启动时不自动采集，避免「打开就抓」把 IP 搞封；
  · 采集串行执行，站点之间按 settings.json 的 throttle_sec 主动延时。

数据全部落在本地 data/ 目录，前端读的是本地快照，不实时打站点。

启动：
    python server.py            # 默认端口 8765
    python server.py --port 9000 --open
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import fetch_prices as collector
from integrations.configuration import (ConfigurationStore, DEFAULT_PROXY_MODE, SECRET_FIELDS,
                                        public_site, merge_sites, normalize_proxy, normalize_proxy_mode,
                                        resolve_proxy, site_proxy, system_proxy_address,
                                        write_json as integration_write_json)
from integrations.contracts import ConnectionMode, ConnectionPolicy, ErrorCategory, Feature
from integrations import service as integration_service
from integrations import cindy as cindy_link
from integrations import config_backup
from integrations.retirement import EvidenceMatrix, build_report as build_retirement_report
from integrations.auth import (AuthConfigError, AuthState, AuthStore, ChallengeStore, LoginLimiter,
                               SessionStore, cookie_header, expired_cookie_header, parse_cookie)
from integrations.deploy_config import DeployConfigError, load_deploy_config
from integrations.request_guard import (GuardError, check_host, check_write_origin,
                                        client_address, cors_allow_origin, parse_json_body)
from integrations.state_lock import LockConflict, StateLock
from integrations.version import read_version

ROOT = Path(__file__).resolve().parent
try:
    DEPLOY = load_deploy_config(root=ROOT)
    DEPLOY_ERROR: DeployConfigError | None = None
except DeployConfigError as exc:
    # 保留可导入的本地默认值，真正启动时 main() 会拒绝运行并显示原因。
    DEPLOY = load_deploy_config(env={}, root=ROOT)
    DEPLOY_ERROR = exc

DATA_DIR = DEPLOY.state_dir / "data"
LOG_DIR = DEPLOY.state_dir / "logs"
HTML_FILE = ROOT / "中转站比价.html"
# 账号快照（余额 / 用量 / 分组 Key）落地位置，和价格数据一样只放本地
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
HEALTH_FILE = DATA_DIR / "health.json"
VERSION = read_version(ROOT)

# 部署模式下的运行期单例。测试可分别 patch 这些对象，避免读真实 auth.json。
AUTH = AuthStore(DEPLOY.state_dir, mode=DEPLOY.mode)
SESSIONS = SessionStore()
CHALLENGES = ChallengeStore()
LOGIN_LIMITER = LoginLimiter()
SESSION_COOKIE = "relay_session"

# 并发上限的允许区间：配置填得再大也不超过这个数，免得把站点打挂
CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 16


def concurrency_of(value, fallback: int) -> int:
    """把「并发数」配置收敛到合法区间；缺失或填了非数字就退回 fallback。"""
    try:
        return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, int(value)))
    except (TypeError, ValueError):
        return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, int(fallback)))


class DynamicGate:
    """按当前配置动态限流的并发闸门。

    和 threading.Semaphore 的差别：名额上限每次 acquire 时重新读配置，
    所以在「系统设置」里改完并发数，下一次请求立刻生效，不用重启服务。
    名额满了是「排队等待（可带超时）」，不是直接失败。
    """

    def __init__(self, limit_getter, name: str = "gate"):
        self._limit_getter = limit_getter
        self._name = name
        self._cond = threading.Condition()
        self._active = 0

    def limit(self) -> int:
        try:
            return max(CONCURRENCY_MIN, int(self._limit_getter()))
        except (TypeError, ValueError):
            return CONCURRENCY_MIN

    def acquire(self, timeout: float | None = None) -> bool:
        """占用一个名额；满了就等，等过 timeout 秒仍未轮到则返回 False。"""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while self._active >= self.limit():
                if deadline is None:
                    self._cond.wait()
                    continue
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return False
                self._cond.wait(remain)
            self._active += 1
            return True

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify()


# 模型健康：多个模型可以同时在读，用闸门限流替代原来的全局互斥，
# 上限取设置里的 health_concurrency（默认 5，同样收敛到 1–16）。
HEALTH_GATE = DynamicGate(lambda: concurrency_of(ensure_settings().get("health_concurrency"), 5), "health")
# 排队上限：等太久就如实报「并发已满」，不让前端干等着
HEALTH_GATE_TIMEOUT = 45.0
# health.json 是「读—改—写」：多个模型的记录同时落盘会互相覆盖，这段必须原子
HEALTH_WRITE_LOCK = threading.Lock()


def load_health() -> dict:
    data = collector.load_json(HEALTH_FILE, default={})
    return data if isinstance(data, dict) and isinstance(data.get("sites", {}), dict) else {}

# 允许的站点框架，与采集器支持的适配器保持一致
SITE_TYPES = tuple(collector.ADAPTERS.keys())
SITE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 默认放在凌晨：定时任务走全量采集，夜里跑对中转站的压力最小
DEFAULT_SETTINGS = {
    # refresh_accounts：定时任务跑完价目后，顺带把账户（余额/用量/Key）也刷一遍
    "schedule": {"enabled": True, "time": "03:00", "refresh_accounts": True},
    "throttle_sec": 2,
    "currency": "USD",
    "rate": 7.2,
    "cluster_threshold": 0.9,
    "proxy": "",
    # 全局代理的取用方式：direct 直连 / system 跟随系统代理 / manual 手填上面的 proxy。
    # 全新用户默认跟随系统，装了代理工具就能直接用。
    "proxy_mode": DEFAULT_PROXY_MODE,
    "favorite_models": [],
    "auto_refresh_model_health": False,
    # 并发上限，都可在「系统设置 → 通用设置」里改（1–16，默认 5）：
    #   health_concurrency —— 打开模型详情时同时在读的站点数
    #   refresh_concurrency —— 刷新账户（余额/用量/Key）时同时在读的站点数
    "health_concurrency": 5,
    "refresh_concurrency": 5,
    # 展开报价行时分档价格表是否默认打开（「系统设置 → 通用设置」可改）
    "tier_expand_default": False,
    # 手动采集的目标：前端选过一次就记住，下次点开直接带上
    "manual_target": {"scope": "all", "sites": [], "models": []},
    "connection_policy": {"mode": ConnectionMode.API_PREFERRED.value, "compat_enabled": True},
}

# 手动采集允许的时间点建议值，前端做成快捷按钮
SCHEDULE_PRESETS = ["01:00", "02:00", "03:00", "04:00", "08:00"]

MAX_BODY = 4 * 1024 * 1024  # 配置体积上限，防止误提交超大数据


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------


def ensure_settings() -> dict:
    """读取 settings.json，缺失时写入一份默认配置。"""
    path = collector.SETTINGS_FILE
    if not path.exists():
        collector.write_json_atomic(path, DEFAULT_SETTINGS)
        return json.loads(json.dumps(DEFAULT_SETTINGS))
    settings = collector.load_settings()
    changed = False
    for key, value in DEFAULT_SETTINGS.items():
        if key not in settings:
            # 代理模式是后加的字段，老配置里没有：按 proxy 有没有填推断，
            # 否则一个已配好代理的配置升级后会掉回「跟随系统」，用户会莫名其妙采不到。
            settings[key] = (normalize_proxy_mode(None, settings.get("proxy"))
                             if key == "proxy_mode" else json.loads(json.dumps(value)))
            changed = True
    if changed:
        integration_write_json(path, settings, backup=True)
    return settings


def settings_proxy(settings, site=None) -> str:
    """按「直连 / 跟随系统 / 手填」三模式解析出要用的代理地址。

    传 site 时站点自己的「本站代理」优先于全局设置；返回空串表示直连。
    Key 操作、健康检查、采集全都走这里，避免各处各写一套优先级。
    """
    return resolve_proxy(settings.get("proxy_mode"), settings.get("proxy") or "", site)


def load_sites() -> list[dict]:
    """读取站点清单；文件不存在时返回空列表（由前端引导用户新增站点）。"""
    if not collector.DEFAULT_CONFIG.exists():
        return []
    return ConfigurationStore(collector.DEFAULT_CONFIG).migrate().get("sites", [])


def validate_sites(sites) -> tuple[list[dict], list[str]]:
    return merge_sites(sites, load_sites(), SITE_TYPES)


def sanitize_settings(settings) -> dict:
    """清洗偏好设置，保证字段类型正确且不越界。"""
    clean = json.loads(json.dumps(DEFAULT_SETTINGS))
    if not isinstance(settings, dict):
        return clean

    schedule = settings.get("schedule") or {}
    if isinstance(schedule, dict):
        time_str = str(schedule.get("time", DEFAULT_SETTINGS["schedule"]["time"]))
        if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", time_str):
            clean["schedule"]["time"] = time_str
        clean["schedule"]["enabled"] = bool(schedule.get("enabled", True))
        # 定时任务跑完价目后，是否顺带刷新一次账户（余额 / 用量 / Key）
        clean["schedule"]["refresh_accounts"] = bool(schedule.get(
            "refresh_accounts", DEFAULT_SETTINGS["schedule"]["refresh_accounts"]))

    try:
        clean["throttle_sec"] = max(0.0, min(60.0, float(settings.get("throttle_sec", 2))))
    except (TypeError, ValueError):
        pass

    clean["currency"] = "CNY" if settings.get("currency") == "CNY" else "USD"
    try:
        clean["rate"] = max(0.1, min(100.0, float(settings.get("rate", 7.2))))
    except (TypeError, ValueError):
        pass
    try:
        clean["cluster_threshold"] = max(0.5, min(1.0, float(settings.get("cluster_threshold", 0.9))))
    except (TypeError, ValueError):
        pass
    # 全局代理是每个站点的兜底值：站点自己写了 proxy 就优先用站点的
    clean["proxy"] = normalize_proxy(settings.get("proxy"))
    # 代理取用方式；非法值交给 normalize_proxy_mode 回落（缺失时按 proxy 有没有填推断）
    clean["proxy_mode"] = normalize_proxy_mode(settings.get("proxy_mode"), clean["proxy"])
    # 只有显式启用才会在打开模型详情时联网读取统计。
    clean["auto_refresh_model_health"] = settings.get("auto_refresh_model_health") is True
    # 并发上限统一收敛到 1–16，非法值（空、0、负数、超界、非数字）回落默认 5
    for key in ("health_concurrency", "refresh_concurrency"):
        clean[key] = concurrency_of(settings.get(key), DEFAULT_SETTINGS[key])
    # 分档价格表默认展开：只有显式 true 才开，避免旧配置里的脏值误开
    clean["tier_expand_default"] = settings.get("tier_expand_default") is True

    favs = settings.get("favorite_models")
    clean["favorite_models"] = [str(x) for x in favs][:200] if isinstance(favs, list) else []
    clean["manual_target"] = sanitize_manual_target(settings.get("manual_target"))
    clean["connection_policy"] = ConnectionPolicy.from_dict(settings.get("connection_policy")).to_dict()
    return clean


def sanitize_manual_target(raw) -> dict:
    """清洗手动采集目标。sites / models 只保留字符串，数量设上限防止配置膨胀。"""
    clean = {"scope": "all", "sites": [], "models": []}
    if not isinstance(raw, dict):
        return clean
    scope = str(raw.get("scope", "all")).strip().lower()
    clean["scope"] = "custom" if scope == "custom" else "all"
    for key in ("sites", "models"):
        values = raw.get(key)
        if isinstance(values, list):
            clean[key] = [str(v).strip() for v in values if str(v).strip()][:500]
    return clean


# ---------------------------------------------------------------------------
# 采集任务：单实例 + 日志收集
# ---------------------------------------------------------------------------


class CollectorTask:
    """后台采集任务。同一时刻只允许一个任务在跑，且随时可以被强制停止。

    停止的实现是「协作式」的：前端点停止只是置一个事件，采集器在**每个站点之间**
    检查一次。之所以不直接杀线程，是因为那样会在写文件写一半的时候中断，
    留下坏数据；让它自己走到安全点再退出，本地上一次的完整数据才保得住。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.started_at = None
        self.finished_at = None
        self.logs: list[str] = []
        self.error = None
        self.trigger = "manual"   # manual | schedule
        self.scope = "all"        # all | custom
        self.stopped = False      # 上一次任务是否被手动停止
        self.stop_reason = ""
        self.task_id = ""
        self.last_summary = None
        self.progress = {"done": 0, "total": 0, "current": "", "ok": 0}
        self._stop_evt = threading.Event()

    def start(self, only: str = "", trigger: str = "manual", throttle: float | None = None,
              models: list[str] | None = None, merge: bool = False, scope: str = "all") -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.finished_at = None
            self.logs = []
            self.error = None
            self.trigger = trigger
            self.scope = scope
            self.stopped = False
            self.stop_reason = ""
            self.task_id = uuid.uuid4().hex
            self.progress = {"done": 0, "total": len(only.split(",")) if only else 0,
                             "current": "", "ok": 0}
            self._stop_evt.clear()

        def run():
            settings = ensure_settings()
            try:
                payload = collector.run_collection(
                    only=only,
                    logger=self._log,
                    throttle=throttle if throttle is not None else settings.get("throttle_sec", 0),
                    models=models or None,
                    merge=merge,
                    should_stop=self._stop_evt.is_set,
                    on_progress=self._on_progress,
                    trigger=self.trigger,
                )
                # 被停止时采集器不会落盘，这里也就不更新摘要，页面上仍显示上一次的结果
                if payload.get("stopped"):
                    with self.lock:
                        self.stopped = True
                        self.stop_reason = str(payload.get("reason") or "被手动停止")
                    return
                ok = sum(1 for s in payload["sites"] if s["ok"])
                with self.lock:
                    self.last_summary = {
                        "ok_sites": ok,
                        "total_sites": len(payload["sites"]),
                        "model_count": payload["model_count"],
                        "offer_count": payload["offer_count"],
                        "generated_at": payload["generated_at"],
                        "merged": bool(payload.get("merged")),
                        # 本次失败、沿用上一版数据的站点（前端会提示「不是最新的」）
                        "stale_sites": payload.get("stale_sites") or [],
                    }
            except SystemExit as exc:
                self.error = str(exc)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                with self.lock:
                    self.running = False
                    self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")

        threading.Thread(target=run, daemon=True).start()
        return True

    def request_stop(self, task_id: str | None = None) -> bool:
        """请求停止当前任务。返回是否确实有一个任务在被停。"""
        with self.lock:
            if task_id and task_id != self.task_id:
                raise ValueError("原采集任务已结束，不能停止其它任务")
            if not self.running:
                return False
            self._stop_evt.set()
        self._log("收到停止指令，正在等当前站点请求结束后退出…")
        return True

    def _on_progress(self, done: int, total: int, current: str, ok: bool):
        """采集每跑完一个站点回调一次，供前端显示「第几个 / 共几个」。"""
        with self.lock:
            self.progress = {
                "done": done,
                "total": total,
                "current": current or "",
                "ok": self.progress.get("ok", 0) + (1 if ok else 0),
            }

    def _log(self, message: str):
        line = str(message)
        if line.strip():
            with self.lock:
                self.logs.append(line.rstrip())
            log_line("collect", line.rstrip())     # 同时落地，关了页面也留痕
        try:
            sys.stderr.write(line + "\n")
        except Exception:
            pass

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "started_at": self.started_at,
                "task_id": self.task_id,
                "finished_at": self.finished_at,
                "trigger": self.trigger,
                "scope": self.scope,
                "stopped": self.stopped,
                "stop_reason": self.stop_reason,
                "stopping": self._stop_evt.is_set() and self.running,
                "progress": dict(self.progress),
                "logs": list(self.logs),
                "error": self.error,
                "summary": self.last_summary,
            }


TASK = CollectorTask()


def next_run_at(time_str: str, now: datetime | None = None) -> datetime:
    """算出下一次定时采集的时刻。已过今天的点则顺延到明天，不做补跑。"""
    now = now or datetime.now()
    try:
        hour, minute = (int(x) for x in time_str.split(":"))
    except Exception:
        hour, minute = 8, 0
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


class Scheduler(threading.Thread):
    """每日定时采集。服务启动时只排好下一次时间，不立即补跑。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        settings = ensure_settings()
        sch = settings.get("schedule", {})
        self.enabled = bool(sch.get("enabled", True))
        self.time_str = str(sch.get("time", DEFAULT_SETTINGS["schedule"]["time"]))
        with self.lock:
            self.next_run = next_run_at(self.time_str)
        # 不能叫 _stop：Thread._stop() 是父类方法，3.10/3.12 的 join() 会调用它。
        self._stop_evt = threading.Event()

    def apply_settings(self, settings: dict):
        sch = settings.get("schedule", {})
        self.enabled = bool(sch.get("enabled", True))
        new_time = str(sch.get("time", DEFAULT_SETTINGS["schedule"]["time"]))
        if new_time != self.time_str:
            self.time_str = new_time
            with self.lock:
                self.next_run = next_run_at(self.time_str)

    def get_next_run(self) -> str:
        with self.lock:
            return self.next_run.isoformat(timespec="seconds")

    def run(self):
        while not self._stop_evt.wait(20):
            now = datetime.now()
            with self.lock:
                due = self.enabled and now >= self.next_run
            if not due:
                continue
            started = TASK.start(trigger="schedule")
            if started:
                log_line("schedule", f"触发每日采集 {now:%Y-%m-%d %H:%M}")
                # 采完价目顺带刷一次账户，余额/用量才不会一直停在手动刷的那次
                if bool(ensure_settings().get("schedule", {}).get("refresh_accounts", True)):
                    self._wait_and_refresh_accounts()
                else:
                    log_line("schedule", "已设置在定时任务中不刷新账户，跳过")
            with self.lock:
                self.next_run = next_run_at(self.time_str, now)

    def _wait_and_refresh_accounts(self):
        """等本次采集跑完，再串行刷账户（两者都要联网，别同时打站点）。"""
        def wait():
            for _ in range(3600):          # 最多等 1 小时
                if self._stop_evt.is_set() or not TASK.running:
                    break
                time.sleep(1)
            if self._stop_evt.is_set():
                return
            if TASK.running:
                log_line("schedule", "采集迟迟未结束，本次跳过账户刷新")
                return
            try:
                start_account_refresh(trigger="schedule")
            except Exception as exc:
                log_line("schedule", f"定时刷新账户失败：{type(exc).__name__}: {exc}")
        threading.Thread(target=wait, daemon=True).start()

    def stop(self):
        self._stop_evt.set()


SCHEDULER = Scheduler()


# ---------------------------------------------------------------------------
# 数据缓存：全量数据常驻内存，避免每次请求都读盘
# ---------------------------------------------------------------------------


class DataCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.full = None      # 全量（含 offers）
        self.index = None     # 轻量索引
        self.mtime = 0
        self.reload()

    def reload(self):
        """重新加载本地数据文件。"""
        index_path = DATA_DIR / "index.json"
        json_path = DATA_DIR / "prices.json"
        full, index = None, None
        if json_path.exists():
            full = collector.load_json(json_path, default=None)
        if index_path.exists():
            index = collector.load_json(index_path, default=None)
        elif full:
            index = collector.build_index(full)
        with self.lock:
            self.full = full
            self.index = index
            self.mtime = max(p.stat().st_mtime for p in (index_path, json_path) if p.exists()) \
                if (index_path.exists() or json_path.exists()) else 0

    def get_index(self):
        with self.lock:
            return self.index

    def get_models(self, keys: list[str] | None = None):
        """按 key 取模型明细（含全部报价）；不传 key 时返回索引里的全部摘要。"""
        with self.lock:
            full = self.full
            index = self.index
        if not full:
            return []
        models = full.get("models", [])
        if not keys:
            return models
        wanted = set(keys)
        return [m for m in models if m["key"] in wanted]


CACHE = DataCache()


# ---------------------------------------------------------------------------
# 日志落地：任务日志除了留在内存里给轮询，还要写进 logs/YYYY-MM-DD.log，
# 这样关掉页面、甚至重启服务之后，之前跑过什么、哪一站失败过都还能翻到。
# ---------------------------------------------------------------------------

LOG_LOCK = threading.Lock()
MAX_LOG_DAYS = 14          # 只留最近两周，避免日志无限长
MAX_LOG_LINES = 4000       # 单日行数上限，超了就砍掉最早的


def _log_file(date_str: str | None = None) -> Path:
    name = date_str or datetime.now().astimezone().strftime("%Y-%m-%d")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{name}.log"


def log_line(kind: str, message: str, date_str: str | None = None) -> str:
    """写一行日志并返回这一行的原文。kind 用于前端着色：collect / account / schedule / system。"""
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    line = f"{ts} [{kind}] {message}"
    try:
        with LOG_LOCK:
            path = _log_file(date_str)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass          # 日志写不进去不能影响主流程
    return line


def read_logs(date_str: str, since: int = 0) -> dict:
    """读某一天的日志。since 是已读行数，只返回新增部分，前端据此增量追加。"""
    path = _log_file(date_str)
    lines: list[str] = []
    if path.exists():
        with LOG_LOCK:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"date": date_str, "total": len(lines), "lines": lines[since:]}


def list_log_dates() -> list[str]:
    """列出有日志的日期（倒序，最新在前）。"""
    if not LOG_DIR.exists():
        return []
    names = sorted((p.stem for p in LOG_DIR.glob("*.log")), reverse=True)
    return names[:MAX_LOG_DAYS]


def prune_logs():
    """顺手清掉过期日志；单日超长就截断，保证文件不会无限增长。"""
    try:
        if not LOG_DIR.exists():
            return
        files = sorted(LOG_DIR.glob("*.log"), reverse=True)
        for old in files[MAX_LOG_DAYS:]:
            old.unlink(missing_ok=True)
        with LOG_LOCK:
            for path in files[:MAX_LOG_DAYS]:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                if len(lines) > MAX_LOG_LINES:
                    path.write_text("\n".join(lines[-MAX_LOG_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 账号快照：余额 / 用量 / 充值 / 分组 Key
# ---------------------------------------------------------------------------


def load_accounts() -> dict:
    """读取本地账号快照；没刷过就返回空结构，页面据此引导用户去配凭据。"""
    data = collector.load_json(ACCOUNTS_FILE, default=None)
    if not isinstance(data, dict):
        return {"generated_at": None, "accounts": []}
    data.setdefault("generated_at", None)
    data.setdefault("accounts", [])
    configured = {s.get("id"): s for s in load_sites()}
    for account in data["accounts"]:
        site = configured.get(account.get("site"))
        if site and account.get("binding_ref") and account["binding_ref"] != integration_service.binding_of(site):
            account.clear()
            account.update(site=site["id"], site_name=site.get("name") or site["id"], ok=False,
                           error="站点或凭据已变更，请刷新账户", error_kind="pending", tokens=None,
                           group_keys=None, token_count=None)
    return data


ACCOUNT_LOCK = threading.Lock()
ACCOUNT_DATA_LOCK = threading.RLock()
TOKEN_REVISIONS: dict[str, int] = {}
TOKEN_SESSION_LOCK = threading.Lock()
TOKEN_SESSIONS: dict[str, dict] = {}


@contextmanager
def token_session(site: dict, proxy: str = "", trigger: str = "manual"):
    """Key 操作短时复用登录会话；配置变化立即隔离，客户端按会话串行使用。

    trigger 只影响「站点 × 功能」证据记录（manual / schedule / cli），不改变请求行为；
    复用的会话对象每次都会刷新 trigger，避免把上一次任务的来源记到这一次上。
    """
    identity = json.dumps([site, proxy, ensure_settings().get("connection_policy")],
                          sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    with TOKEN_SESSION_LOCK:
        now = time.monotonic()
        for key, value in list(TOKEN_SESSIONS.items()):
            if value["expires"] and value["expires"] <= now:
                TOKEN_SESSIONS.pop(key, None)
        if fingerprint not in TOKEN_SESSIONS:
            if len(TOKEN_SESSIONS) >= 64:
                TOKEN_SESSIONS.pop(next(iter(TOKEN_SESSIONS)))
            TOKEN_SESSIONS[fingerprint] = {"lock": threading.RLock(), "session": None, "expires": 0}
        entry = TOKEN_SESSIONS[fingerprint]
    with entry["lock"]:
        try:
            if entry["session"] is None or entry["expires"] <= time.monotonic():
                entry["session"] = collector.integration_runtime(data_dir=DATA_DIR).router(site, proxy)
                # 仅 Key 交互短会话共享已验证身份；账户刷新仍显式使用自己的缓存。
                entry["session"].interactive_profile_cache = {}
                # 创建 Key 后紧接着导入 CCS 时复用本次已取得的明文和真实名称，
                # 缓存随短会话过期，不落盘、不跨站点共享。
                entry["session"].interactive_token_cache = {}
                entry["session"].api.read_retries = 0
                entry["expires"] = time.monotonic() + 120
            entry["session"].trigger = trigger
            yield entry["session"]
        except Exception:
            # 凭据过期或操作失败时，下次点击重新鉴权；不自动重放创建/删除。
            if entry["session"] is not None:
                entry["session"].interactive_profile_cache.clear()
                entry["session"].interactive_token_cache.clear()
            entry["session"] = None
            raise


def update_token_snapshot(site_id: str, token: dict | None = None, deleted_id=None) -> dict | None:
    """只更新本地掩码列表，不为创建或删除重新抓取余额和充值记录。"""
    with ACCOUNT_DATA_LOCK:
        expected_binding = (token or {}).get("binding_ref")
        if expected_binding:
            current_site = find_site(site_id)
            if (not current_site or current_site.get("enabled") is False
                    or integration_service.binding_of(current_site) != expected_binding):
                return None
        data = load_accounts()
        account = next((a for a in data["accounts"] if a.get("site") == site_id), None)
        if account is None:
            return None
        if expected_binding and account.get("binding_ref") != expected_binding:
            return None
        tokens = list(account.get("tokens") or [])
        affected_id = deleted_id if deleted_id is not None else (token or {}).get("id")
        tokens = [t for t in tokens if str(t.get("id")) != str(affected_id)]
        if token is not None:
            item = {k: token.get(k) for k in ("id", "name", "group", "group_explicit", "status", "enabled",
                    "unlimited", "remain_usd", "used_usd", "expired_time", "model_limited", "model_limits")}
            key = str(token.get("key") or "")
            item["key_masked"] = key[:6] + "***" + key[-4:] if key else ""
            item["group_id"] = token.get("group_id")
            tokens.append(item)
        groups: dict[str, dict] = {}
        for item in tokens:
            group = groups.setdefault(item.get("group") or account.get("group") or "default",
                                      {"count": 0, "enabled": 0, "tokens": []})
            group["count"] += 1
            group["enabled"] += int(bool(item.get("enabled")))
            if len(group["tokens"]) < 20:
                group["tokens"].append({k: item.get(k) for k in ("id", "name", "key_masked", "enabled")})
        account.update(tokens=tokens, group_keys=groups, token_count=len(tokens))
        account["keys_updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        collector.write_json_atomic(ACCOUNTS_FILE, data)
        TOKEN_REVISIONS[site_id] = TOKEN_REVISIONS.get(site_id, 0) + 1
        return account
# 刷新是后台跑的：POST 只负责起任务，进度由 /api/accounts 轮询，页面刷新或重开也能看到
ACCOUNT_TASK = {"running": False, "done": 0, "total": 0, "current": "", "ok": 0,
                "started_at": None, "finished_at": None, "error": None, "sites": []}
# 站点（账户）刷新的兜底并发：正常取 settings.json 的 refresh_concurrency（默认 5），
# 只有配置缺失/非法时才会落到这个值。
ACCOUNT_REFRESH_WORKERS = 5


def _account_progress(done: int, total: int, current: str, ok: bool):
    with ACCOUNT_LOCK:
        ACCOUNT_TASK["done"] = done
        ACCOUNT_TASK["total"] = total
        ACCOUNT_TASK["current"] = current or ""
        ACCOUNT_TASK["ok"] = ACCOUNT_TASK.get("ok", 0) + (1 if ok else 0)


def preserve_account_sections(snapshot: dict, previous: dict | None) -> dict:
    """分页失败仅保留同一账户上次成功的子快照，成功时间不随本次刷新前移。"""
    same_user = (previous and previous.get("user_id") is not None
                 and str(previous.get("user_id")) == str(snapshot.get("user_id")))
    for section, fields in (
        ("topups", ("recharge_count", "recharge_usd", "recharge_money", "recharge_supported")),
        ("keys", ("tokens", "token_count", "group_keys")),
    ):
        if snapshot.get(section + "_complete") is not False:
            continue
        snapshot[section + "_stale"] = True
        snapshot[section + "_attempted_at"] = snapshot.get("fetched_at")
        has_previous = previous and (isinstance(previous.get("tokens"), list) if section == "keys" else (
            previous.get("topups_complete") is True or previous.get("recharge_supported") is True
            or bool(previous.get("recharge_count"))))
        if same_user and has_previous:
            for field in fields:
                if field in previous:
                    snapshot[field] = previous[field]
            snapshot[section + "_updated_at"] = previous.get(section + "_updated_at")
            if snapshot[section + "_updated_at"] is None and previous.get(section + "_complete") is not False:
                snapshot[section + "_updated_at"] = previous.get("fetched_at")
    return snapshot


def refresh_accounts(site_ids: list[str] | None = None, on_progress=None, trigger: str = "manual") -> dict:
    """账户跨站并发刷新（并发数取设置里的 refresh_concurrency，默认 5）；
    完成一站就发布快照，站内仍按功能顺序只读。"""
    settings = ensure_settings()
    # 全局代理只作为兜底：每个站点自己的 proxy 优先，站点没写才用全局，都没写就是直连
    proxy = settings_proxy(settings)
    sites = load_sites()
    wanted = {str(s).strip() for s in (site_ids or []) if str(s).strip()}
    targets = [s for s in sites
               if s.get("enabled", True) and (not wanted or s.get("id") in wanted)]
    original_sites = {s["id"]: s for s in sites}
    with ACCOUNT_DATA_LOCK:
        old = {a.get("site"): a for a in load_accounts().get("accounts", [])}
        revisions = dict(TOKEN_REVISIONS)
    results: list[dict] = []

    def fetch_site(site):
        try:
            return integration_service.account_snapshot(
                collector.integration_runtime(data_dir=DATA_DIR).router(site, site_proxy(site, proxy), trigger=trigger),
                site, previous=old.get(site.get("id")))
        except Exception as exc:
            return {"site": site.get("id"), "site_name": site.get("name") or site.get("id"),
                    "ok": False, "error": f"{type(exc).__name__}: {exc}", "error_kind": "network"}

    def publish():
        # 发布和 Key 本地修改共用锁。未完成/未选中的站点保留最新本地快照。
        with ACCOUNT_DATA_LOCK:
            configured = load_sites()
            current = {s["id"]: s for s in configured if s.get("enabled", True)}
            latest = {a.get("site"): a for a in load_accounts().get("accounts", [])}
            for result in results:
                sid = result.get("site")
                if sid not in current or integration_service.binding_of(current[sid]) != integration_service.binding_of(original_sites[sid]):
                    continue  # 请求期间改了凭据、停用或删除站点，旧结果不回写。
                account = dict(result)
                if TOKEN_REVISIONS.get(sid, 0) != revisions.get(sid, 0) and sid in latest:
                    # 并发创建/删除后，不能让旧列表复活 Key 或冒充完整列表。
                    for field in ("tokens", "token_count", "group_keys", "keys_updated_at"):
                        if field in latest[sid]:
                            account[field] = latest[sid][field]
                    if latest[sid].get("keys_complete") is False:
                        account.update(keys_complete=False, keys_stale=True,
                                       keys_error=latest[sid].get("keys_error") or "Key 列表待完整刷新")
                latest[sid] = account
            accounts = [latest.get(sid) or {"site": sid, "site_name": site.get("name") or sid,
                        "ok": False, "error": "未配置凭据", "error_kind": "none",
                        "tokens": [], "group_keys": {}, "token_count": 0}
                        for sid, site in current.items()]
            payload = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                       "accounts": accounts}
            collector.write_json_atomic(ACCOUNTS_FILE, payload)
            return payload

    # 站点并发数走设置（默认 5），改完下次刷新即生效
    workers = concurrency_of(settings.get("refresh_concurrency"), ACCOUNT_REFRESH_WORKERS)
    with ThreadPoolExecutor(max_workers=min(workers, len(targets)) or 1,
                            thread_name_prefix="account-refresh") as pool:
        pending = {pool.submit(fetch_site, site): site for site in targets}
        for index, future in enumerate(as_completed(pending)):
            site, snap = pending[future], future.result()
            if snap.get("ok"):
                snap = preserve_account_sections(snap, old.get(site.get("id")))
            log_line("account", f"{'✓' if snap.get('ok') else '✗'} {site.get('name') or site.get('id')}"
                               + (f"：{snap.get('error')}" if not snap.get("ok") else ""))
            if (not snap.get("ok") and old.get(site.get("id"))
                    and old[site["id"]].get("binding_ref") == integration_service.binding_of(site)
                    and not any(f.get("error_category") == ErrorCategory.ACCOUNT_MISMATCH.value
                                for f in snap.get("features", {}).values())):
                # 这次取不到，保留上一次成功的数据并打上过期标记，页面不至于整块变空
                keep = dict(old[site.get("id")])
                keep["stale"] = True
                keep["stale_error"] = snap.get("error")
                # 失败原因要换成这一次的原因：否则上次留下的「未配置凭据」
                # 会把这次的「Cookie 过期 / 需要验证码」盖掉，把人往错误的方向带
                if snap.get("error_kind") and snap["error_kind"] != "none":
                    keep["error"] = snap.get("error")
                    keep["error_kind"] = snap["error_kind"]
                    keep["advice"] = snap.get("advice") or ""
                results.append(keep)
            else:
                results.append(snap)
            payload = publish()
            if on_progress:
                on_progress(index + 1, len(targets),
                            site.get("name") or site.get("id") or "", bool(snap.get("ok")))

    return {"ok": True, **(payload if targets else publish())}


def start_account_refresh(site_ids: list[str] | None = None, trigger: str = "manual") -> dict:
    """在后台线程里刷新账号快照，立刻返回，前端轮询 /api/accounts 看进度。
    trigger 区分是手动点的还是定时任务带的（schedule）。"""
    with ACCOUNT_LOCK:
        if ACCOUNT_TASK["running"]:
            return {"ok": False, "error": "已有刷新任务在运行", "busy": True}
        wanted = {str(s).strip() for s in (site_ids or []) if str(s).strip()}
        configured = load_sites()
        available = {s.get("id") for s in configured if s.get("enabled", True)}
        if wanted - available:
            return {"ok": False, "error": "所选站点不存在或已停用"}
        targets = [s for s in configured
                   if s.get("enabled", True) and (not wanted or s.get("id") in wanted)]
        if not targets:
            return {"ok": False, "error": "没有可刷新的已启用站点"}
        ACCOUNT_TASK.update({"running": True, "done": 0, "total": len(targets), "current": "",
                             "sites": [s["id"] for s in targets],
                             "ok": 0, "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "finished_at": None, "error": None})

    log_line("account", f"开始刷新账户（{trigger}），共 {len(targets)} 个站点")

    def run():
        try:
            refresh_accounts(site_ids, on_progress=_account_progress, trigger=trigger)
        except Exception as exc:   # 刷新失败不能让服务挂掉，状态要如实反映
            with ACCOUNT_LOCK:
                ACCOUNT_TASK["error"] = f"{type(exc).__name__}: {exc}"
            log_line("account", f"刷新出错：{type(exc).__name__}: {exc}")
        finally:
            with ACCOUNT_LOCK:
                ACCOUNT_TASK["running"] = False
                ACCOUNT_TASK["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            log_line("account", "刷新账户结束")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": len(targets), "sites": [s["id"] for s in targets]}


def find_site(site_id: str) -> dict | None:
    for site in load_sites():
        if site.get("id") == site_id:
            return site
    return None


# ---------------------------------------------------------------------------
# 配置备份与还原
# ---------------------------------------------------------------------------

# 导出/导入都走 POST：导出内含明文凭据，不该被 GET 链接、浏览器历史或反代日志顺手带走；
# 导入本身就是写操作，天然需要 CSRF 与 Origin 校验。

def export_config(body: dict) -> tuple[dict, int]:
    """打包当前站点与偏好设置，返回给前端另存为 JSON。

    凭据默认带上——不含凭据的备份还原后还得重配一遍，等于没备份。
    """
    include_secrets = body.get("include_secrets") is not False
    include_auth = body.get("include_auth") is True
    sites = load_sites()
    settings = ensure_settings()

    auth_payload = None
    if include_auth:
        state = AUTH.load()
        auth_payload = {"enabled": state.enabled, "username": state.username,
                        "password_hash": state.password_hash,
                        "auth_revision": state.auth_revision}

    payload = config_backup.build_export(sites, settings, app_version=VERSION,
                                        include_secrets=include_secrets,
                                        auth=auth_payload)
    log_line("config", f"导出配置：{len(sites)} 个站点，凭据={'含' if include_secrets else '不含'}"
                       f"，访问保护={'含' if include_auth else '不含'}")
    return {"ok": True, "payload": payload,
            "filename": config_backup.export_filename()}, 200


def import_config(body: dict) -> tuple[dict, int]:
    """还原配置：整体替换站点与偏好，可选连访问保护一起还原。

    dry_run 只做校验并回摘要，供前端在真正写入前让用户确认。
    """
    payload = body.get("payload")
    options = body.get("options") or {}
    if not isinstance(options, dict):
        return {"ok": False, "error": "导入选项格式不正确"}, 400
    dry_run = options.get("dry_run") is True
    restore_auth = options.get("restore_auth") is True

    try:
        plan = config_backup.plan_restore(payload, sanitize_settings=sanitize_settings,
                                          restore_auth=restore_auth)
    except config_backup.BackupError as exc:
        return {"ok": False, "error": str(exc)}, 400

    summary = plan.summary()
    if dry_run:
        return {"ok": True, "dry_run": True, **summary,
                "message": f"备份可用，将还原 {summary['site_count']} 个站点"}, 200

    # 访问保护先落盘再还原站点：哈希不合法时整体失败，不会出现「站点换了、密码没换」。
    # 落盘前先快照旧字节，站点写入失败时按原样写回。
    auth_state = None
    previous_auth: bytes | None = None
    if plan.restore_auth and plan.auth is not None:
        candidate = AuthState(bool(plan.auth.get("enabled")),
                              str(plan.auth.get("username") or ""),
                              str(plan.auth.get("password_hash") or ""),
                              int(plan.auth.get("auth_revision") or 1))
        if candidate.enabled and not candidate.configured:
            return {"ok": False, "error": "备份里的访问保护已开启但没有有效密码，配置未改动"}, 400
        if candidate.password_hash:
            try:
                previous_auth = AUTH.path.read_bytes()
            except FileNotFoundError:
                previous_auth = None
            except OSError as exc:
                return {"ok": False, "error": f"读取现有访问保护配置失败：{exc}"}, 400
            try:
                AUTH.save(candidate)   # save 内部会校验哈希格式
            except (AuthConfigError, ValueError, OSError) as exc:
                return {"ok": False, "error": f"访问保护还原失败，配置未改动：{exc}"}, 400
            auth_state = candidate

    try:
        result = config_backup.apply_restore(plan, sites_path=collector.DEFAULT_CONFIG,
                                             settings_path=collector.SETTINGS_FILE)
    except config_backup.BackupError as exc:
        # 站点/设置没写成时把刚写的访问保护也撤回去，避免只还原了密码
        if auth_state is not None:
            try:
                if previous_auth is None:
                    AUTH.path.unlink(missing_ok=True)
                else:
                    AUTH.path.write_bytes(previous_auth)
            except OSError:
                log_line("config", "访问保护回滚失败，请手工检查 auth.json")
        return {"ok": False, "error": str(exc)}, 400

    # 配置换了一整套：运行时缓存、令牌会话、定时任务全部按新配置重建
    for runtime in collector._INTEGRATION_RUNTIMES.values():
        runtime.invalidate()
    with TOKEN_SESSION_LOCK:
        TOKEN_SESSIONS.clear()
    if auth_state is not None:
        # 密码哈希与 revision 都换了：旧会话一律作废，谁也不能拿老 cookie 继续用
        SESSIONS.clear()
    settings = ensure_settings()
    SCHEDULER.apply_settings(settings)
    CACHE.reload()

    message = f"已还原 {result['sites']} 个站点与偏好设置"
    if auth_state is not None:
        message += "；访问保护密码已一并还原，请重新登录"
    log_line("config", f"还原配置：{result['sites']} 个站点，"
                       f"访问保护={'已还原' if auth_state is not None else '未改动'}")
    return {"ok": True, "dry_run": False, **summary, "warnings": result["warnings"],
            "auth_restored": auth_state is not None,
            "requires_login": auth_state is not None and auth_state.enabled,
            "message": message}, 200


# ---------------------------------------------------------------------------
# CC Switch 导出
# ---------------------------------------------------------------------------

# CC Switch 把配置放在用户目录下，这里只做「装没装 / 什么版本」的判断，
# 不读也不改它的数据——导出走的是深链，最终由用户在那边点确认。
CC_HOME_CANDIDATES = (
    Path.home() / ".cc-switch",
    Path.home() / "AppData" / "Roaming" / "cc-switch",
    Path.home() / ".config" / "cc-switch",
)


def ccswitch_info() -> dict:
    """探测本机是否装了 CC Switch，以及大致版本（用来决定能不能带 apiFormat）。"""
    info = {"installed": False, "version": "", "path": "", "api_format_ok": True}
    for folder in CC_HOME_CANDIDATES:
        if not folder.is_dir():
            continue
        info["installed"] = True
        info["path"] = str(folder)
        # whatsNewSeenVersion 是「上次看到的版本」，够用来判断新旧
        for name in ("settings.json", "config.json"):
            candidate = folder / name
            if not candidate.is_file():
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            version = str(data.get("whatsNewSeenVersion") or data.get("version") or "")
            if version:
                info["version"] = version
                break
        break
    if info["version"]:
        seen = collector.parse_version_tuple(info["version"])
        # 版本明确低于 4.0.10 时，apiFormat 传了会被拒，直接不带
        if seen and seen < collector.CC_API_FORMAT_MIN_VERSION:
            info["api_format_ok"] = False
    return info


def export_ccswitch(body: dict) -> tuple[dict, int]:
    """拼一条 CC Switch 导入深链：站点端点 + 明文 Key + 用户选的模型。"""
    site = find_site(str(body.get("site", "")))
    if not site:
        return {"ok": False, "error": "站点不存在"}, 404

    app = str(body.get("app") or "").strip()
    if app not in collector.CC_APPS:
        return {"ok": False, "error": f"不支持的应用：{app or '(空)'}"}, 400

    model = str(body.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "没有指定模型，导过去也不知道用哪个"}, 400

    settings = ensure_settings()
    proxy = settings_proxy(settings, site)
    info = ccswitch_info()
    wanted_format = str(body.get("api_format") or "").strip()
    api_format = wanted_format
    if api_format and not info.get("api_format_ok"):
        api_format = ""  # 老版本不认这个参数，降级而不是生成一条被拒的链接

    try:
        with token_session(site, proxy) as session:
            operation = integration_service._interaction_state(session)
            resolved = integration_service.resolve_export(
                session, site,
                group=str(body.get("group") or "").strip(),
                create=bool(body.get("create", True)),
                name=str(body.get("key_name") or "").strip() or "cc-switch",
                token_id=body.get("token_id"),
                model=model,
                group_id=body.get("group_id"),
                _operation=operation,
            )
            usage = None
            if body.get("with_usage", True):
                # 用量脚本仍核实真实单位，复用本轮身份，且与共享会话在同一串行区内使用。
                usage_result = session.fetch(Feature.CCS_USAGE, model_key=resolved["key"],
                                            context=integration_service._interaction_context(operation))
                if usage_result.ok:
                    usage = usage_result.data
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400

    endpoint = str(body.get("endpoint") or "").strip() or (resolved["base_url"] + "/v1")
    site_name = site.get("name") or site.get("id")
    link = collector.build_ccswitch_link(
        app=app,
        name=f"{site_name}-{resolved.get('token_name') if str(resolved.get('token_name') or '').strip() else '未命名 Key'}-{model}",
        endpoint=endpoint,
        api_key=resolved["key"],
        model=model,
        homepage=site.get("url") or resolved["base_url"],
        notes=str(body.get("notes") or "").strip(),
        api_format=api_format,
        usage=usage,
        icon=site.get("icon") or "",
        icon_url=site.get("icon_url") or "",
    )

    warn = []
    if resolved["created"]:
        warn.append(f"已在分组「{resolved['group']}」新建了一个 Key（{resolved['token_name'] or 'cc-switch'}）")
    if body.get("with_usage", True) and not usage:
        warn.append("未取得可验证单位与授权的用量查询，已跳过；模型 Key 导入仍可使用")
    if wanted_format and not api_format:
        warn.append(f"当前 CC Switch（{info.get('version') or '版本未知'}）不支持 apiFormat，"
                    "已改用应用默认格式；想指定格式请升到 4.0.10 以上")
    log_line("export", f"生成 CC Switch 导入链接：{site.get('id')} / {app} / {model}")

    account = None
    if resolved["created"] and resolved.get("created_token"):
        try:
            account = update_token_snapshot(site["id"], token=resolved["created_token"])
            if account is None:
                warn.append("Key 已创建，本地账户绑定已变更或尚无快照，请刷新账户列表")
        except Exception:
            warn.append("Key 已创建，本地列表更新失败，请刷新账户列表")

    return {
        "ok": True,
        "link": link,
        "endpoint": endpoint,
        "model": model,
        "app": app,
        "app_name": collector.CC_APPS[app],
        "group": resolved["group"],
        "created": resolved["created"],
        "token_id": resolved["token_id"],
        "token_name": resolved.get("token_name") or "",
        "account": account,
        "usage": bool(usage),
        "ccswitch": info,
        "warn": warn,
    }, 200


# ---------------------------------------------------------------------------
# Cindy 导出
# ---------------------------------------------------------------------------

# Cindy 桌面端在用户目录留下的痕迹。只用来提示「装没装」，**不阻塞导出**——
# 链接在没装 Cindy 的机器上点了没反应，但那不是错误，不该因此禁用按钮。
CINDY_HOME_CANDIDATES = (
    Path.home() / "AppData" / "Roaming" / "Cindy",
    Path.home() / "AppData" / "Local" / "Cindy",
    Path.home() / "AppData" / "Local" / "Programs" / "cindy",
    Path.home() / "AppData" / "Local" / "Programs" / "Cindy",
    Path.home() / "Library" / "Application Support" / "Cindy",
    Path.home() / ".config" / "Cindy",
    Path.home() / ".cindy",
)


def _cindy_protocol_registered() -> bool:
    """Windows：看 cindy:// 协议有没有注册（HKCU / HKLM 的 Software\\Classes\\cindy）。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"Software\Classes\cindy"):
                return True
        except OSError:
            continue
    return False


def _cindy_running() -> bool:
    """看 Cindy 桌面端进程在不在。

    v0.1.114（2026-10-23 自动更新）起，**Cindy 只要在运行，深链就一定落空**：
    Windows 协议激活会新起一个 Cindy.exe，它拿不到单实例锁 —— 新版这段分支直接
    `app.quit()`，不再像旧版那样把 argv 转给主实例；而 quit() 之前同步执行的
    `findDeepLinkInArgv()` 已经把导入草稿缓冲进这个将死进程的内存（日志里
    source 恒为 cold-start-argv，紧接着就是 previous run exited cleanly）。
    结论：「Cindy 正在运行」= 这次导入必然收不到，前端据此提示先退出 Cindy。
    """
    if sys.platform != "win32":
        return False
    try:
        done = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Cindy.exe", "/NH"],
                              capture_output=True, timeout=3, text=True)
    except Exception:
        return False
    return "Cindy.exe" in (done.stdout or "")


def _cindy_pids() -> list:
    """当前所有 Cindy.exe 的 PID（主进程 + 渲染/工具子进程都算）。

    CSV 输出形如 ``"Cindy.exe","31556","RDP-Tcp#0","2","200,652 K"``，
    只取前两列；解析失败就当没有，调用方会走「没在运行」分支。
    """
    if sys.platform != "win32":
        return []
    try:
        done = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Cindy.exe", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=3, text=True)
    except Exception:
        return []
    pids = []
    for line in (done.stdout or "").splitlines():
        parts = [cell.strip().strip('"') for cell in line.split('","')]
        if len(parts) >= 2 and parts[0].lower().endswith("cindy.exe"):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                continue
    return pids


def close_cindy(*, force: bool = False, wait_seconds: float = 5.0) -> tuple[dict, int]:
    """关掉正在运行的 Cindy，为导入深链腾出单实例锁。

    v0.1.114 起「Cindy 在运行」= 深链必被一个马上退出的小进程吃掉（见 `_cindy_running`），
    所以自动导入只剩一条路：先把 Cindy 关干净，再唤起链接让它冷启动。

    默认**不强杀**：`taskkill` 不带 `/F` 只是发关闭请求（等同点窗口的 ×），
    Cindy 若是「关闭即最小化到托盘」的设置就会关不掉 —— 此时返回 ``needs_force``，
    由前端再确认一次才走 `/F`，避免在用户不知情时丢未保存内容。
    """
    if not _cindy_running():
        return {"ok": True, "closed": False, "already_closed": True,
                "message": "Cindy 本来就没在运行"}, 200

    pids = _cindy_pids()
    command = ["taskkill", "/IM", "Cindy.exe"]
    if force:
        command.append("/F")
    try:
        subprocess.run(command, capture_output=True, timeout=10, text=True)
    except Exception as exc:
        return {"ok": False, "error": f"关闭 Cindy 时出错：{type(exc).__name__}: {exc}"}, 500

    deadline = time.monotonic() + max(0.5, wait_seconds)
    while time.monotonic() < deadline:
        if not _cindy_running():
            log_line("export", f"已关闭 Cindy{'（强制）' if force else ''}，PID {pids}；准备重新唤起导入")
            return {"ok": True, "closed": True, "already_closed": False,
                    "force": force, "pids": pids}, 200
        time.sleep(0.3)

    if not force:
        return {"ok": False, "needs_force": True, "pids": pids,
                "error": "Cindy 没退出，可能设成了「关闭即最小化到托盘」"}, 409
    return {"ok": False, "error": "强制结束后仍有 Cindy 进程存活，请手动退出", "pids": pids}, 409


def cindy_info() -> dict:
    """探测本机是否装了 Cindy 桌面端；纯提示用，不影响导出能否使用。"""
    info = {"installed": False, "path": "", "protocol_registered": _cindy_protocol_registered()}
    if info["protocol_registered"]:
        info["installed"] = True
    for folder in CINDY_HOME_CANDIDATES:
        if folder.is_dir():
            info["installed"] = True
            info["path"] = str(folder)
            break
    return info


def export_cindy(body: dict) -> tuple[dict, int]:
    """拼一条 Cindy 导入深链：站点端点 + 明文 Key + 协议/引擎。

    与 CC Switch 导出的三处差别：
      · 不带模型清单 —— 交给 Cindy 导入后自己拉，避免把清单写死、也避免链接超长；
      · 不带余额查询 —— Cindy 对第三方连接没有这个能力（见 docs/cindy-导入可行性分析.md），
        勾了「带余额查询」会被忽略并给出提示，不影响 Key 与端点导入；
      · 引擎只有 Claude Code / Codex / Pi 三个，其余 app 直接拒绝。
    """
    site = find_site(str(body.get("site", "")))
    if not site:
        return {"ok": False, "error": "站点不存在"}, 404

    app = str(body.get("app") or "").strip()
    target = cindy_link.cindy_target_for_app(app)
    if not target:
        supported = "、".join(cindy_link.cindy_app_name(k) for k in cindy_link.CINDY_APP_TARGETS)
        return {"ok": False, "error": f"Cindy 只支持 {supported}；当前选的是：{app or '(空)'}"}, 400

    model = str(body.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "没有指定模型，无法生成与 CC Switch 一致的导入名称"}, 400

    settings = ensure_settings()
    proxy = settings_proxy(settings, site)
    protocol = cindy_link.cindy_protocol_for(app, str(body.get("api_format") or ""))

    try:
        with token_session(site, proxy) as session:
            operation = integration_service._interaction_state(session)
            resolved = integration_service.resolve_export(
                session, site,
                group=str(body.get("group") or "").strip(),
                create=bool(body.get("create", True)),
                name=str(body.get("key_name") or "").strip() or "cindy",
                token_id=body.get("token_id"),
                model=model,
                group_id=body.get("group_id"),
                _operation=operation,
            )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400

    endpoint = str(body.get("endpoint") or "").strip() or cindy_link.cindy_default_endpoint(
        resolved["base_url"], protocol)
    site_name = site.get("name") or site.get("id")
    token_label = str(resolved.get("token_name") or "").strip() or "未命名 Key"
    try:
        link = cindy_link.build_cindy_import_link(
            # 模型仅用于记录名称，仍不向 Cindy 写入或限制模型清单。
            name=f"{site_name}-{token_label}-{model}",
            endpoint=endpoint,
            api_key=resolved["key"],
            protocol=protocol,
            target=target,
            provider_id=str(site.get("id") or ""),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}, 400

    warn = []
    if resolved["created"]:
        warn.append(f"已在分组「{resolved['group']}」新建了一个 Key（{resolved['token_name'] or 'cindy'}）")
    if body.get("with_usage"):
        warn.append("Cindy 目前不支持第三方连接的余额查询，已忽略「带余额查询」；Key 与端点导入不受影响")
    log_line("export", f"生成 Cindy 导入链接：{site.get('id')} / {app} / {protocol}")

    account = None
    if resolved["created"] and resolved.get("created_token"):
        try:
            account = update_token_snapshot(site["id"], token=resolved["created_token"])
            if account is None:
                warn.append("Key 已创建，本地账户绑定已变更或尚无快照，请刷新账户列表")
        except Exception:
            warn.append("Key 已创建，本地列表更新失败，请刷新账户列表")

    return {
        "ok": True,
        "link": link,
        "endpoint": endpoint,
        # 明文 Key 一并给前端：Cindy 在运行时收不到深链（见 _cindy_running），
        # 用户可以直接复制 Key 与端点，在 Cindy 里手动添加连接。
        "api_key": resolved["key"],
        "app": app,
        "app_name": cindy_link.cindy_app_name(app),
        "target": target,
        "protocol": protocol,
        "group": resolved["group"],
        "created": resolved["created"],
        "token_id": resolved["token_id"],
        "token_name": resolved.get("token_name") or "",
        "account": account,
        "usage": False,
        "cindy": {**cindy_info(), "running": _cindy_running()},
        "warn": warn,
    }, 200


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------


def json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def gzip_body(body: bytes) -> bytes:
    """gzip 压缩。返回的数据体积小，本机回环传输更稳，前端首屏也更快。"""
    buf = BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6, mtime=0) as fp:
        fp.write(body)
    return buf.getvalue()


def probe_state_dir() -> bool:
    """健康检查用：状态目录必须可创建临时文件，失败即不可服务。"""
    try:
        DEPLOY.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=DEPLOY.state_dir, prefix=".health-", delete=True):
            pass
        return True
    except OSError:
        return False


# 登录页完全独立：不引用主页面、不嵌入快照，也不返回任何业务数据。
LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · 中转站比价</title>
<style>
  :root{color-scheme:light dark;--bg:#f5f6f8;--panel:#fff;--text:#1a1d21;--muted:#5b6472;
        --border:#d3d8e0;--accent:#3457d5;--danger:#c0392b;}
  @media (prefers-color-scheme:dark){
    :root{--bg:#14171c;--panel:#1c2027;--text:#e8ecf1;--muted:#9aa4b2;--border:#333a45;--accent:#7d97ff;}
  }
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);
       font-family:"PingFang SC","Microsoft YaHei","Segoe UI",system-ui,sans-serif}
  main{width:min(92vw,360px);background:var(--panel);border:1px solid var(--border);border-radius:14px;
       padding:26px 24px;box-shadow:0 10px 30px rgba(16,24,40,.08)}
  h1{font-size:18px;margin:0 0 6px}
  p{color:var(--muted);font-size:13px;margin:0 0 18px;line-height:1.6}
  label{display:block;font-size:13px;margin:12px 0 6px}
  input{width:100%;padding:9px 11px;border:1px solid var(--border);border-radius:9px;
        background:transparent;color:var(--text);font-size:14px}
  input:focus{outline:2px solid var(--accent);outline-offset:1px}
  button{width:100%;margin-top:20px;padding:10px;border:0;border-radius:9px;background:var(--accent);
         color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:progress}
  .error{color:var(--danger);font-size:13px;min-height:18px;margin-top:12px}
</style>
</head>
<body>
<main>
  <h1>中转站比价</h1>
  <p>此服务已开启访问保护，请输入管理员账号后继续。</p>
  <form id="loginForm" autocomplete="on">
    <label for="username">用户名</label>
    <input id="username" name="username" autocomplete="username" required>
    <label for="password">密码</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button id="submit" type="submit">登录</button>
    <div class="error" id="error" role="alert"></div>
  </form>
</main>
<script src="/login.js"></script>
</body>
</html>
"""

LOGIN_JS = """(function(){
  'use strict';
  var challenge = '', csrf = '';
  var form = document.getElementById('loginForm');
  var errorBox = document.getElementById('error');
  var button = document.getElementById('submit');

  async function init(){
    try {
      var response = await fetch('/api/auth/challenge', {cache:'no-store', credentials:'same-origin'});
      var data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '无法开始登录');
      challenge = data.challenge;
      csrf = data.csrf;
    } catch (err) {
      errorBox.textContent = '登录服务暂不可用，请刷新页面重试';
      button.disabled = true;
    }
  }

  form.addEventListener('submit', async function(event){
    event.preventDefault();
    errorBox.textContent = '';
    button.disabled = true;
    try {
      var response = await fetch('/api/auth/login', {
        method:'POST',
        credentials:'same-origin',
        cache:'no-store',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          challenge:challenge,
          csrf:csrf,
          username:document.getElementById('username').value,
          password:document.getElementById('password').value
        })
      });
      var data = await response.json();
      if (response.ok && data.ok) {
        location.replace('/');
        return;
      }
      errorBox.textContent = data.error || '登录失败';
    } catch (err) {
      errorBox.textContent = '网络异常，请稍后重试';
    }
    button.disabled = false;
  });

  init();
})();
"""


class Handler(BaseHTTPRequestHandler):
    # 显式用 HTTP/1.1 + Content-Length，避免某些本机网络过滤对无长度响应的异常行为
    server_version = "RelayLocalServer/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志，避免刷屏
        pass

    def handle_one_request(self):
        """客户端提前断开（关闭标签页、keep-alive 空闲连接被回收）是正常现象。

        BaseHTTPRequestHandler 默认会把 ConnectionResetError 打到 stderr，
        日志里会出现一段与业务无关的 traceback；这里只吞掉断开类异常。
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    # -- 发送辅助 --------------------------------------------------------
    def _bound_port(self) -> int:
        """返回真实监听端口；Handler 单测没有 server 时退回配置端口。"""
        try:
            return int(self.server.server_address[1])
        except (AttributeError, IndexError, TypeError, ValueError):
            return int(DEPLOY.port or 8765)

    def _cors_headers(self) -> dict:
        origin = cors_allow_origin(self.headers.get("Origin"), self._bound_port(), DEPLOY)
        if not origin:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Relay-CSRF",
            "Access-Control-Max-Age": "86400",
        }

    def _reject(self, status: int, message: str):
        """拒绝请求；能安全读完 body 就保持连接，读不完才关闭。

        带着未读 body 直接关连接会让 Windows 发 RST，客户端可能连 403 都收不到；
        反之，body 已读完却主动 close，同样会和服务端的发送缓冲抢跑。
        """
        payload = json_bytes({"ok": False, "error": message})
        try:
            raw_length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raw_length = 0
        drained = True
        if self.headers.get("Transfer-Encoding"):
            # chunked 无法按长度读完，只能关闭连接，交给客户端重连。
            drained = False
        elif 0 < raw_length <= MAX_BODY:
            try:
                self.rfile.read(raw_length)
            except OSError:
                drained = False
        elif raw_length > MAX_BODY:
            drained = False
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if not drained:
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except OSError:
            pass
        if not drained:
            self.close_connection = True

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True

    def write_response(self, body: bytes, ctype: str, status=200, compress=True,
                       extra_headers: dict | None = None):
        headers = {"Content-Type": ctype, "Cache-Control": "no-store"}
        headers.update(self._cors_headers())
        if extra_headers:
            headers.update(extra_headers)
        # 浏览器都支持 gzip；压缩后体积能降到十分之一左右，传输更稳更快
        if compress and len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            body = gzip_body(body)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = (headers["Vary"] + ", Accept-Encoding") if headers.get("Vary") else "Accept-Encoding"
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=200, extra_headers: dict | None = None):
        self.write_response(json_bytes(obj), "application/json; charset=utf-8", status,
                            extra_headers=extra_headers)

    def send_file(self, path: Path, status=200):
        self.write_response(path.read_bytes(),
                            MIME.get(path.suffix.lower(), "application/octet-stream"), status)

    def do_OPTIONS(self):
        """预检同样先校验 Host；来源不匹配时不回 CORS 头。"""
        try:
            check_host(self.headers.get("Host", ""), self._bound_port(), DEPLOY)
        except GuardError as exc:
            self._reject(exc.status, exc.message)
            return
        self.send_response(204)
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    @staticmethod
    def _path_only(raw: str) -> str:
        return unquote(raw.split("?")[0])

    def _auth_state(self) -> AuthState:
        try:
            return AUTH.load()
        except AuthConfigError as exc:
            raise GuardError(503, "认证配置异常，服务已拒绝访问") from exc

    @staticmethod
    def _auth_exempt(path: str) -> bool:
        """只放行登录必需资源和无业务数据的最小健康检查。"""
        return path in {"/login", "/login.js", "/healthz", "/api/ping",
                        "/api/auth/challenge", "/api/auth/login"}

    def _guard(self, write: bool = False) -> dict | None:
        """所有业务动作之前执行：Host → 认证 → Origin/CSRF → JSON。"""
        port = self._bound_port()
        path = self._path_only(self.path)
        check_host(self.headers.get("Host", ""), port, DEPLOY)

        auth = self._auth_state()
        self._auth_state_cache = auth
        self._session_token = None
        self._session_record = None
        if auth.enabled and not self._auth_exempt(path):
            token = parse_cookie(self.headers.get("Cookie"), SESSION_COOKIE)
            record = SESSIONS.get(token or "")
            if not token or record is None:
                if path in {"/", "/index.html"}:
                    raise GuardError(302, "/login")
                raise GuardError(401, "需要登录")
            self._session_token = token
            self._session_record = record

        if not write:
            return None
        check_write_origin(self.headers.get("Origin"), port, DEPLOY)
        if auth.enabled and self._session_record is not None:
            supplied = self.headers.get("X-Relay-CSRF") or ""
            if not hmac.compare_digest(supplied, self._session_record.csrf):
                raise GuardError(403, "CSRF 校验失败")
        return parse_json_body(self.headers, self.rfile, MAX_BODY)

    # -- 路由 ------------------------------------------------------------
    def do_GET(self):
        try:
            self._guard(write=False)
        except GuardError as exc:
            if exc.status == 302:
                return self._redirect(exc.message)
            return self._reject(exc.status, exc.message)
        path = self._path_only(self.path)

        if path == "/healthz":
            ok = probe_state_dir()
            return self.send_json({"ok": ok, "version": VERSION}, 200 if ok else 503)

        if path == "/login":
            return self.write_response(LOGIN_HTML.encode("utf-8"), "text/html; charset=utf-8")

        if path == "/login.js":
            return self.write_response(LOGIN_JS.encode("utf-8"), "application/javascript; charset=utf-8")

        if path == "/api/ping":
            return self.send_json({"ok": True})

        if path == "/api/auth/challenge":
            token, csrf = CHALLENGES.create()
            return self.send_json({"ok": True, "challenge": token, "csrf": csrf})

        if path == "/" or path == "/index.html":
            if HTML_FILE.exists():
                try:
                    page = HTML_FILE.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    return self.send_json({"error": "找不到页面文件"}, 404)
                page = re.sub(r'(<meta\s+name="relay-mode"\s+content=")[^"]*(")',
                              lambda m: m.group(1) + DEPLOY.mode + m.group(2), page, count=1)
                page = page.replace('content="__RELAY_VERSION__"', f'content="{VERSION}"')
                return self.write_response(page.encode("utf-8"), "text/html; charset=utf-8",
                                           extra_headers={"X-Relay-Version": VERSION})
            return self.send_json({"error": "找不到页面文件"}, 404)

        if path.startswith("/data/"):
            allowed_names = {"prices.json", "prices.js", "index.json", "accounts.json", "health.json"}
            relative_name = path[len("/data/"):]
            if relative_name not in allowed_names:
                return self.send_json({"error": "禁止访问"}, 403)
            target = (DATA_DIR / relative_name).resolve()
            try:
                target.relative_to(DATA_DIR.resolve())
            except ValueError:
                return self.send_json({"error": "禁止访问"}, 403)
            if target.parent != DATA_DIR.resolve() or target.name not in allowed_names:
                return self.send_json({"error": "禁止访问"}, 403)
            if target.exists() and target.is_file():
                return self.send_file(target)
            return self.send_json({"error": "文件不存在"}, 404)

        if path.startswith("/api/"):
            return self.handle_api_get(path)

        return self.send_json({"error": "未知路径"}, 404)

    def handle_api_get(self, path: str):
        if path == "/api/state":
            settings = ensure_settings()
            index = CACHE.get_index() or {}
            auth = self._auth_state()
            ccswitch = ccswitch_info()
            ccswitch["location"] = DEPLOY.mode
            cindy = cindy_info()
            cindy["location"] = DEPLOY.mode
            return self.send_json({
                "ok": True,
                # 页面连接方式恒为 server；真实部署模式另由 location 给出。
                "mode": "server",
                "version": VERSION,
                "location": DEPLOY.mode,
                "auth": {
                    "enabled": auth.enabled,
                    "configured": auth.configured,
                    # 用户名不是秘密，设置页用它回显；哈希永远不出现在响应里。
                    "username": auth.username if auth.configured else "",
                    "csrf": getattr(self, "_session_record", None).csrf
                    if getattr(self, "_session_record", None) else None,
                },
                "settings": settings,
                # 当前系统代理解析结果：设置页用来告诉用户选「跟随系统代理」实际会用到哪个地址
                "system_proxy": system_proxy_address(),
                "sites": [public_site(s) for s in load_sites()],
                "schedule": {
                    "enabled": SCHEDULER.enabled,
                    "time": SCHEDULER.time_str,
                    "next_run": SCHEDULER.get_next_run(),
                    "presets": SCHEDULE_PRESETS,
                },
                "task": TASK.snapshot(),
                "accounts": load_accounts(),
                "health": load_health(),
                "account_refresh": dict(ACCOUNT_TASK),
                "data": {
                    "generated_at": index.get("generated_at"),
                    "model_count": index.get("model_count"),
                    "offer_count": index.get("offer_count"),
                    "site_count": index.get("site_count"),
                },
                "site_types": list(SITE_TYPES),
                "ccswitch": ccswitch,
                "ccswitch_apps": [{"id": k, "name": v} for k, v in collector.CC_APPS.items()],
                "cindy": cindy,
                "cindy_apps": [{"id": k, "name": cindy_link.cindy_app_name(k)}
                               for k in cindy_link.CINDY_APP_TARGETS],
            })

        # 日志：date 指定哪一天，since 是前端已读行数（增量拉取）
        if path == "/api/logs":
            qs = parse_qs(urlsplit(self.path).query)
            pick = lambda k, d="": str((qs.get(k) or [d])[0])
            date_str = pick("date").strip() or datetime.now().astimezone().strftime("%Y-%m-%d")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                return self.send_json({"ok": False, "error": "日期格式应为 YYYY-MM-DD"}, 400)
            try:
                since = max(0, int(pick("since", "0")))
            except (TypeError, ValueError):
                since = 0
            return self.send_json({"ok": True, "dates": list_log_dates(), **read_logs(date_str, since)})

        if path == "/api/accounts":
            payload = load_accounts()
            with ACCOUNT_LOCK:
                payload["refresh"] = dict(ACCOUNT_TASK)
            return self.send_json({"ok": True, **payload})

        if path == "/api/operations":
            # 与退出闸门读的是同一份判定，避免"哪里算未确认"出现两套口径
            return self.send_json({"ok": True,
                                   "operations": EvidenceMatrix.pending_writes(DATA_DIR / "write_operations.json")})

        # 兼容退出闸门：按「站点 × 功能」给出停用兼容的四条件判定与阻塞原因。
        # 只读本地证据，不发任何远端请求，也不修改任何配置。
        if path == "/api/compat/retirement":
            settings = ensure_settings()
            matrix = EvidenceMatrix(DATA_DIR / "evidence_matrix.jsonl")
            pending = EvidenceMatrix.pending_writes(DATA_DIR / "write_operations.json")
            scheduled = bool((settings.get("schedule") or {}).get("enabled", True))
            return self.send_json({"ok": True, **build_retirement_report(
                matrix, pending=pending, scheduled_available=scheduled)})

        if path == "/api/index":
            index = CACHE.get_index()
            if not index:
                return self.send_json({"ok": False, "error": "还没有数据，请先执行一次采集"}, 404)
            return self.send_json({"ok": True, **index})

        if path == "/api/task":
            return self.send_json({"ok": True, **TASK.snapshot()})

        if path.startswith("/api/model/"):
            key = path[len("/api/model/"):]
            models = CACHE.get_models([key])
            if not models:
                return self.send_json({"ok": False, "error": "模型不存在"}, 404)
            return self.send_json({"ok": True, "model": models[0]})

        return self._unknown_path(path)

    def _unknown_path(self, path: str):
        """统一的「未知接口」响应。

        顺手拦住一类很常见的低级错误：前端 `state.apiBase` 本身已是 `/api`，
        调用时又写了 `/api/xxx`，实际请求就成了 `/api/api/xxx`。这种情况如果只回
        「未知接口」，排查要绕一大圈，所以这里直接点明前缀重复。
        """
        if path.startswith("/api/api/"):
            fixed = path[len("/api"):]
            log_line("api", f"接口路径重复了 /api 前缀：{path}（应为 {fixed}）")
            return self.send_json({
                "error": f"接口路径重复了 /api 前缀：{path}；前端 state.apiBase 已含 /api，调用应写 {fixed}",
                "path": path,
            }, 404)
        return self.send_json({"error": "未知接口", "path": path}, 404)

    def _client_ip(self) -> str:
        peer = ""
        try:
            peer = str(self.client_address[0])
        except (AttributeError, IndexError, TypeError):
            pass
        return client_address(peer, self.headers.get("X-Forwarded-For"), DEPLOY)

    def _handle_auth_login(self, body: dict):
        auth = self._auth_state()
        if not auth.enabled or not auth.configured:
            return self.send_json({"ok": False, "error": "认证未开启或尚未初始化"}, 400)
        challenge = str(body.get("challenge") or "")
        csrf = str(body.get("csrf") or "")
        if not CHALLENGES.verify(challenge, csrf):
            return self.send_json({"ok": False, "error": "登录挑战已失效，请刷新页面"}, 403)

        ip = self._client_ip()
        username = str(body.get("username") or "")[:256]
        password = str(body.get("password") or "")[:1024]
        wait = LOGIN_LIMITER.check(ip, username)
        if wait > 0:
            return self.send_json({"ok": False, "error": "尝试过于频繁，请稍后再试"},
                                  429, {"Retry-After": str(wait)})
        if not username or not password or not AUTH.verify(username, password):
            retry = LOGIN_LIMITER.record_failure(ip, username)
            headers = {"Retry-After": str(retry)} if retry else None
            return self.send_json({"ok": False, "error": "用户名或密码错误"}, 401, headers)

        LOGIN_LIMITER.record_success(ip, username)
        CHALLENGES.consume(challenge)
        token, session_csrf = SESSIONS.create()
        response = self.send_json({"ok": True, "csrf": session_csrf},
                                  extra_headers={
                                      "Set-Cookie": cookie_header(
                                          token, secure=DEPLOY.secure_cookies,
                                          max_age=SESSIONS.absolute_seconds)
                                  })
        log_line("auth", f"登录成功：{ip}")
        return response

    def _handle_auth_logout(self):
        if self._session_token:
            SESSIONS.destroy(self._session_token)
        return self.send_json({"ok": True}, extra_headers={
            "Set-Cookie": expired_cookie_header(secure=DEPLOY.secure_cookies)
        })

    def _handle_auth_change(self, body: dict):
        auth = self._auth_state()
        if not auth.enabled:
            # 本地默认不启用认证；此时页面仍要能「开启」，首次必须同时给出用户名和新密码。
            if not (body.get("username") and body.get("new_password")):
                return self.send_json({"ok": False, "error": "开启访问保护需要用户名和新密码"}, 400)
            if body.get("enabled") is False:
                return self.send_json({"ok": False, "error": "认证本来就未开启"}, 400)
            try:
                state = AUTH.set_initial(str(body.get("username") or ""),
                                         str(body.get("new_password") or ""), enabled=True)
            except (AuthConfigError, ValueError) as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
            SESSIONS.clear()
            return self.send_json({"ok": True, "auth": {"enabled": True, "configured": True},
                                   "requires_login": True, "message": "访问保护已开启，请重新登录"})
        enabled = body.get("enabled", True)
        if not isinstance(enabled, bool):
            return self.send_json({"ok": False, "error": "enabled 必须是布尔值"}, 400)
        username = body.get("username")
        if username is not None and not isinstance(username, str):
            return self.send_json({"ok": False, "error": "用户名格式不正确"}, 400)
        new_password = body.get("new_password")
        if new_password is not None and not isinstance(new_password, str):
            return self.send_json({"ok": False, "error": "新密码格式不正确"}, 400)
        try:
            state = AUTH.change(str(body.get("current_password") or ""), enabled=enabled,
                                username=username, new_password=new_password or None)
        except (AuthConfigError, ValueError) as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 400)
        # 认证修订号变化后，所有旧会话立即失效。
        SESSIONS.clear()
        return self.send_json({
            "ok": True,
            "auth": {"enabled": state.enabled, "configured": state.configured},
            "requires_login": state.enabled,
            "message": "访问保护已更新，请重新登录" if state.enabled else "访问保护已关闭",
        }, extra_headers={"Set-Cookie": expired_cookie_header(secure=DEPLOY.secure_cookies)})

    def do_POST(self):
        try:
            body = self._guard(write=True)
        except GuardError as exc:
            return self._reject(exc.status, exc.message)
        path = self._path_only(self.path)

        if path == "/api/auth/login":
            return self._handle_auth_login(body or {})
        if path == "/api/auth/logout":
            return self._handle_auth_logout()
        if path == "/api/auth/change":
            return self._handle_auth_change(body or {})

        if path in {"/api/token/update", "/api/operations/reconcile"}:
            site = find_site(str(body.get("site") or ""))
            if not site or not site.get("enabled", True):
                return self.send_json({"ok": False, "error": "站点不存在或已停用"}, 404)
            try:
                with token_session(site, settings_proxy(ensure_settings(), site)) as router:
                    if path == "/api/token/update":
                        value = integration_service.require(router.fetch(Feature.KEYS_UPDATE, id=body.get("id"), changes=body.get("changes")))
                        return self.send_json({"ok": True, "token": value, "message": "Key 已更新，请刷新列表"})
                    profile = router.fetch(Feature.PROFILE)
                    integration_service.require(profile)
                    listing = integration_service.require(router.fetch(Feature.KEYS_LIST))
                    confirmed = router.journal.reconcile(str(body.get("operation_id") or ""),
                        account_id=profile.account_id, rows=listing["items"])
                    return self.send_json({"ok": True, "confirmed": confirmed,
                        "message": "已通过完整列表确认" if confirmed else "尚不能唯一确认；操作继续锁定，未重放写请求"})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)

        if path == "/api/health/refresh":
            site = find_site(str(body.get("site") or ""))
            model = str(body.get("model") or "").strip()
            if not site or not site.get("enabled", True) or not model or len(model) > 512:
                return self.send_json({"ok": False, "error": "请选择已启用站点和有效模型"}, 400)
            # 并发闸门：默认 5 路（可在系统设置里调）；满了排队等，等超时才如实报错
            if not HEALTH_GATE.acquire(timeout=HEALTH_GATE_TIMEOUT):
                return self.send_json({"ok": False, "error": "模型健康更新并发已满，请稍后重试"}, 409)
            try:
                binding = integration_service.binding_of(site)
                # 读盘与缓存判断放进写锁：并发时同站同模型不会重复打到站点
                with HEALTH_WRITE_LOCK:
                    health = load_health()
                    old = health.get("sites", {}).get(site["id"], {}).get(model)
                    # 同一模型短时间重复点击直接复用本地结果，降低站点请求频率。
                    same_binding = bool(old and old.get("binding_ref") == binding)
                    if (same_binding and old.get("health_schema_version") == integration_service.HEALTH_SCHEMA_VERSION
                            and time.time() - float(old.get("cached_at", 0)) < 60):
                        return self.send_json({"ok": True, "health": old, "cached": True})
                record = integration_service.health_snapshot(
                    collector.integration_runtime(data_dir=DATA_DIR).router(site, settings_proxy(ensure_settings(), site)), model,
                    previous=old if same_binding else None)
                record["binding_ref"] = binding
                record["cached_at"] = time.time()
                # 重新读盘再合并写回：多个模型同时在刷新，不能拿旧快照整体覆盖别人刚写的记录
                with HEALTH_WRITE_LOCK:
                    merged = load_health()
                    merged.setdefault("sites", {}).setdefault(site["id"], {})[model] = record
                    collector.write_json_atomic(HEALTH_FILE, merged)
                return self.send_json({"ok": True, "health": record})
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
            finally:
                HEALTH_GATE.release()

        if path == "/api/task/start":
            # 手动采集：前端已做二次确认，这里再拦一次并发
            if TASK.snapshot()["running"]:
                return self.send_json({"ok": False, "error": "已有采集任务正在运行"}, 409)
            scope = "custom" if str(body.get("scope", "all")).lower() == "custom" else "all"
            sites = [str(x).strip() for x in (body.get("sites") or []) if str(x).strip()]
            models = [str(x).strip() for x in (body.get("models") or []) if str(x).strip()]
            if scope == "custom" and not sites:
                return self.send_json({"ok": False, "error": "自定义采集至少要选一个站点"}, 400)
            if scope == "custom":
                available = {s["id"] for s in load_sites() if s.get("enabled", True)}
                if set(sites) - available:
                    return self.send_json({"ok": False, "error": "所选采集站点不存在或已停用"}, 400)
            # 自定义采集必须走合并：只更新选中的站点×模型，没选到的部分保持原样
            merge = scope == "custom"
            if body.get("remember"):
                settings = ensure_settings()
                settings["manual_target"] = sanitize_manual_target(
                    {"scope": scope, "sites": sites, "models": models})
                collector.write_json_atomic(collector.SETTINGS_FILE, settings)
            started = TASK.start(
                only=",".join(sites) if merge else "",
                trigger="manual",
                models=models if merge else None,
                merge=merge,
                scope=scope,
            )
            return self.send_json({"ok": started, "started": started, "scope": scope,
                                   "task_id": TASK.snapshot().get("task_id"),
                                   "message": "采集已开始" if started else "未能启动任务"})

        # 强制停止：不管任务是手动还是定时触发的，都能停
        if path == "/api/task/stop":
            try:
                stopped = TASK.request_stop(body.get("task_id"))
            except ValueError as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 409)
            return self.send_json({
                "ok": True, "stopped": stopped,
                "message": "已发出停止指令，正在等当前站点的请求结束" if stopped
                           else "当前没有正在运行的采集任务",
            })

        # 刷新账户改成后台跑：这里只起任务，进度和结果都从 /api/accounts 轮询
        if path == "/api/accounts/refresh":
            wanted = [str(x).strip() for x in (body.get("sites") or []) if str(x).strip()]
            result = start_account_refresh(wanted or None)
            status = 200 if result.get("ok") else (409 if result.get("busy") else 400)
            return self.send_json(result, status)

        if path == "/api/token/create":
            site = find_site(str(body.get("site", "")))
            if not site:
                return self.send_json({"ok": False, "error": "站点不存在"}, 404)
            settings = ensure_settings()
            try:
                with token_session(site, settings_proxy(settings, site)) as session:
                    token = integration_service.create_token(
                        session, site,
                        name=str(body.get("name") or "").strip() or "比价工具创建",
                        group=str(body.get("group") or "").strip(),
                        unlimited=bool(body.get("unlimited", True)),
                        quota_usd=float(body.get("quota_usd") or 0),
                        days=int(body.get("days") or 0),
                        group_id=body.get("group_id"),
                    )
            except Exception as exc:
                return self.send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400)
            warning = ""
            try:
                account = update_token_snapshot(site["id"], token=token)
            except Exception:
                account = None
                warning = "Key 已创建，但本地列表未能保存；请刷新账户同步，勿重复创建"
            return self.send_json({"ok": True, "token": token, "account": account,
                                   "warning": warning, "message": "Key 已创建"})

        if path == "/api/token/delete":
            site = find_site(str(body.get("site", "")))
            if not site or not site.get("enabled", True):
                return self.send_json({"ok": False, "error": "站点不存在或已停用"}, 404)
            token_id = body.get("id")
            if not str(token_id or "").isascii() or not str(token_id or "").isdigit() or int(token_id) <= 0:
                return self.send_json({"ok": False, "error": "Key ID 必须是正整数"}, 400)
            settings = ensure_settings()
            try:
                with token_session(site, settings_proxy(settings, site)) as session:
                    # 删除后立即使同一短会话中的创建缓存失效，避免误用已撤销 Key。
                    session.interactive_token_cache.pop(str(token_id), None)
                    integration_service.require(session.fetch(Feature.KEYS_DELETE, id=token_id))
            except Exception as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
            warning = ""
            try:
                account = update_token_snapshot(site["id"], deleted_id=token_id)
            except Exception:
                account = None
                warning = "Key 已删除，但本地列表未能保存；请刷新账户同步"
            log_line("account", f"删除 Key：{site['id']} / ID {token_id}")
            return self.send_json({"ok": True, "id": token_id, "account": account,
                                   "warning": warning, "message": "Key 已删除"})

        # 导出到 CC Switch：返回一条深链，由用户在 CC Switch 里确认导入
        if path == "/api/export/ccswitch":
            payload, status = export_ccswitch(body)
            return self.send_json(payload, status)

        # 导出到 Cindy：同样返回深链（cindy://provider/import），由用户在 Cindy 里确认导入
        if path == "/api/export/cindy":
            payload, status = export_cindy(body)
            return self.send_json(payload, status)

        # 关闭正在运行的 Cindy：Cindy 在运行时深链必丢，导入前需要它先退场
        if path == "/api/cindy/close":
            payload, status = close_cindy(
                force=bool(body.get("force")),
                wait_seconds=float(body.get("wait_seconds") or 5.0),
            )
            return self.send_json(payload, status)

        if path == "/api/token/key":
            site = find_site(str(body.get("site", "")))
            if not site:
                return self.send_json({"ok": False, "error": "站点不存在"}, 404)
            settings = ensure_settings()
            try:
                with token_session(site, settings_proxy(settings, site)) as session:
                    token = integration_service.require(session.fetch(Feature.KEYS_REVEAL, id=body.get("id")))
            except Exception as exc:
                return self.send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 400)
            return self.send_json({"ok": True, "token": token})

        # 显式查看**单个**已保存凭据。普通配置响应（/api/state、/api/sites）永远只给
        # has_secret 与掩码、不回填明文；只有用户在本机页面主动点「显示」才走到这里。
        # 一次只回一个字段，且只记动作不记值，避免明文进日志。
        if path == "/api/site/secret":
            site = find_site(str(body.get("site") or ""))
            if not site:
                return self.send_json({"ok": False, "error": "站点不存在"}, 404)
            field = str(body.get("field") or "").strip()
            if field not in SECRET_FIELDS:
                return self.send_json({"ok": False, "error": "不支持的凭据字段"}, 400)
            value = (site.get("auth") or {}).get(field) or ""
            if not value:
                return self.send_json({"ok": False, "error": "该字段没有已保存的值"}, 404)
            log_line("config", f"显示站点凭据：{site.get('id')} / {field}")
            return self.send_json({"ok": True, "site": site.get("id"), "field": field, "value": value})

        # 配置备份与还原：导出返回整份 JSON 由前端下载，导入整体替换站点与偏好
        if path == "/api/config/export":
            payload, status = export_config(body or {})
            return self.send_json(payload, status)
        if path == "/api/config/import":
            payload, status = import_config(body or {})
            return self.send_json(payload, status)

        if path == "/api/sites":
            cleaned, errors = ConfigurationStore(collector.DEFAULT_CONFIG).save(body.get("sites"))
            if errors:
                return self.send_json({"ok": False, "errors": errors, "error": "；".join(errors)}, 400)
            for runtime in collector._INTEGRATION_RUNTIMES.values():
                runtime.invalidate()
            with TOKEN_SESSION_LOCK:
                TOKEN_SESSIONS.clear()
            return self.send_json({"ok": True, "errors": [], "sites": [public_site(s) for s in cleaned],
                                   "message": "站点配置已保存，下次操作生效"})

        if path == "/api/settings":
            try:
                settings = sanitize_settings(body.get("settings"))
            except (ValueError, TypeError) as exc:
                return self.send_json({"ok": False, "error": str(exc)}, 400)
            integration_write_json(collector.SETTINGS_FILE, settings, backup=True)
            for runtime in collector._INTEGRATION_RUNTIMES.values():
                runtime.invalidate()
            with TOKEN_SESSION_LOCK:
                TOKEN_SESSIONS.clear()
            SCHEDULER.apply_settings(settings)
            return self.send_json({"ok": True, "settings": settings, "message": "设置已保存"})

        if path == "/api/reload-data":
            # 采集完成后由前端调用，让服务重新读取本地数据
            CACHE.reload()
            index = CACHE.get_index() or {}
            return self.send_json({"ok": True, "generated_at": index.get("generated_at")})

        return self._unknown_path(path)

    @staticmethod
    def _saved_tip(errors: list[str]) -> str:
        if not errors:
            return "站点配置已保存，下次采集生效"
        return f"部分站点已保存，但有 {len(errors)} 条问题被忽略"


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


LOCAL_PORTS = range(8765, 8771)
STOP_BUDGET_SECONDS = 45.0


def pick_port(start: int, tries: int = 6) -> int:
    """只在 local 模式使用：探测时不设 SO_REUSEADDR，避免 Windows 双开同端口。"""
    for offset in range(tries):
        port = start + offset
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(f"端口 {start}~{start + tries - 1} 都被占用了，换一个试试：python server.py --port 9010")


class LocalHTTPServer(ThreadingHTTPServer):
    """Windows 独占端口，防止第二个实例复用同一端口；Linux 保持默认行为。"""

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def install_stop_handlers(stop_event: threading.Event) -> None:
    """注册当前平台真正可用的停止信号；处理器只置位，不做 IO。"""

    def _handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError):
            pass


def _prepare_log_dir() -> bool:
    """server 模式日志目录必须可写；local 模式只警告，保持旧行为。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=LOG_DIR, prefix=".probe-", delete=True):
            pass
        return True
    except OSError as exc:
        if DEPLOY.mode == "server":
            print(f"日志目录不可写：{LOG_DIR}（{exc}）")
            return False
        print(f"警告：日志目录不可写，日志将不会落盘（{LOG_DIR}）")
        return True


def _init_auth_cli() -> int:
    """交互式初始化认证；不监听端口、不启动调度。"""
    answer = input("是否开启访问保护（需要登录）？[Y/n] ").strip().lower()
    enabled = answer not in {"n", "no", "0"}
    if not enabled:
        AUTH.save(AuthState(False, "", "", 1))
        print("已关闭访问保护，并写入 auth.json。")
        return 0
    username = input("用户名：").strip()
    import getpass
    first = getpass.getpass("密码（至少 8 位）：")
    second = getpass.getpass("再输入一次密码：")
    if first != second:
        print("两次输入的密码不一致，未做任何修改。")
        return 1
    try:
        AUTH.set_initial(username, first, enabled=True)
    except (AuthConfigError, ValueError) as exc:
        print(f"初始化失败：{exc}")
        return 1
    print("访问保护已开启，请重启服务后使用登录页。")
    return 0


def _wait_task_stop(deadline: float) -> None:
    """给在途采集留收尾时间；超时保留 journal 的待确认状态，不重放。"""
    while TASK.running and time.monotonic() < deadline:
        time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(description="中转站比价 · 本地服务")
    parser.add_argument("--port", type=int, default=None, help="覆盖 RELAY_PORT / 默认 8765")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--no-schedule", action="store_true", help="关闭每日定时")
    parser.add_argument("--init-auth", action="store_true", help="交互初始化访问保护后退出")
    args = parser.parse_args()

    if DEPLOY_ERROR is not None:
        print(f"部署配置错误：{DEPLOY_ERROR}")
        return 1

    lock = StateLock(DEPLOY.state_dir)
    if not lock.acquire():
        pid = lock.holder_pid()
        print(f"已有实例在运行（PID {pid or '未知'}），请先关闭那个窗口。")
        return 2

    try:
        if args.init_auth:
            return _init_auth_cli()

        try:
            auth = AUTH.load()
        except AuthConfigError as exc:
            print(f"认证配置错误：{exc}；请修复 auth.json 或重新执行 --init-auth。")
            return 1
        if auth.enabled and not auth.configured:
            print("认证已开启但没有有效密码，请先执行：python server.py --init-auth")
            return 1

        if not _prepare_log_dir():
            return 1

        ensure_settings()
        prune_logs()

        requested = args.port if args.port is not None else (DEPLOY.port or 8765)
        port = requested if DEPLOY.mode == "server" else pick_port(requested, len(LOCAL_PORTS))

        try:
            server = LocalHTTPServer((DEPLOY.host, port), Handler)
        except OSError as exc:
            print(f"端口 {DEPLOY.host}:{port} 无法监听：{exc}")
            return 1

        # 先绑定成功，再启动调度；bind 失败时不能留下会写盘的定时线程。
        SCHEDULER.enabled = SCHEDULER.enabled and not args.no_schedule
        SCHEDULER.start()
        log_line("system", f"服务已启动（{DEPLOY.mode}，定时 "
                           f"{'每天 ' + SCHEDULER.time_str if SCHEDULER.enabled else '已关闭'}）")

        display_host = "127.0.0.1" if DEPLOY.host in {"0.0.0.0", "::"} else DEPLOY.host
        url = f"http://{display_host}:{port}/"
        print("=" * 56)
        print("  中转站比价 · 本地服务已启动")
        print(f"  访问地址：{url}")
        print(f"  版本：{VERSION}　模式：{DEPLOY.mode}")
        print(f"  每日定时：{'每天 ' + SCHEDULER.time_str if SCHEDULER.enabled else '已关闭'}"
              f"（下一次 {SCHEDULER.get_next_run()}）")
        print("  说明：服务启动不会自动采集，只有定时到点或你在页面上手动点才会抓。")
        print("  数据目录：", DATA_DIR)
        print("  日志目录：", LOG_DIR)
        print("  关闭服务：在这个窗口按 Ctrl+C")
        print("=" * 56)

        if args.open:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()

        stop_event = threading.Event()
        install_stop_handlers(stop_event)
        worker = threading.Thread(target=server.serve_forever, name="relay-http", daemon=True)
        worker.start()
        try:
            stop_event.wait()
        except KeyboardInterrupt:
            stop_event.set()

        print("\n正在停止服务…")
        deadline = time.monotonic() + STOP_BUDGET_SECONDS
        TASK.request_stop()
        SCHEDULER.stop()
        SCHEDULER.join(5)
        _wait_task_stop(deadline)
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        print("服务已停止")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
