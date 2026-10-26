"""配置备份与还原（sites.json + settings.json，访问保护另算）。

三条设计约束，改这个文件前先看完：

1. **备份不含凭据就等于没备份。** 站点凭据（Cookie / 账号密码 / 管理令牌 / 模型 Key）
   是重配成本最高的部分，所以导出默认带明文；调用方必须把这一点写在界面上，
   不能偷偷脱敏后让用户以为备份是完整的。
2. **还原是整体替换，不是字段合并。** 一份备份代表"导出那一刻的完整配置"，
   与当前配置逐字段合并会留下备份里本不该存在的旧值。因此导入时把每个站点
   当作全新配置校验（merge_sites 的 previous 传空列表），任何一条不合法就整份拒绝。
3. **落盘前把旧文件读进内存。** 设置与站点是两次写入，中间进程可能挂掉；
   任一步抛异常都按内存里的旧字节原样写回。宁可这次还原失败，也不留半套配置。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .configuration import SECRET_FIELDS, merge_sites, read_json, write_json

BACKUP_FORMAT = "relay-price-config"
BACKUP_SCHEMA_VERSION = 1

# 上限用来兜住"把别的大文件当备份拖进来"：本工具正常配置远小于这些值
MAX_SITES = 200
MAX_SITE_BYTES = 64 * 1024
MAX_SETTINGS_BYTES = 256 * 1024

# 服务端内部状态，跨机器搬运没有意义（导入后会重新计算），导出时剔掉
INTERNAL_SITE_FIELDS = ("_revision", "credential_version", "_token_pair_ref",
                        "has_secret", "secret_actions", "credential_expires_at")


class BackupError(ValueError):
    """备份文件不可用。消息直接展示给用户，绝不拼入文件内容本身。"""


def _now_iso(now: datetime | None = None) -> str:
    moment = datetime.now().astimezone() if now is None else now
    return moment.isoformat(timespec="seconds")


def _clean_site(site: dict, include_secrets: bool) -> dict:
    """导出前清洗单站：去掉内部状态；脱敏模式下保留 has_secret 便于对照。"""
    item = copy.deepcopy(site)
    for key in INTERNAL_SITE_FIELDS:
        item.pop(key, None)
    if not include_secrets:
        auth = item.get("auth") or {}
        item["auth"] = {key: value for key, value in auth.items() if key not in SECRET_FIELDS}
        # 脱敏版只告诉"这里原本有没有值"，值本身不带走
        item["has_secret"] = {key: bool(auth.get(key)) for key in SECRET_FIELDS}
    return item


def build_export(sites, settings, *, app_version: str = "", include_secrets: bool = True,
                 auth: dict | None = None, exported_at: datetime | str | None = None) -> dict:
    """拼一份自描述的备份 JSON。

    auth 传 None 表示不带访问保护；传 dict 时原样带上（含密码哈希与 auth_revision）。
    """
    stamp = exported_at if isinstance(exported_at, str) else _now_iso(exported_at)
    items = [_clean_site(site, include_secrets) for site in sites or [] if isinstance(site, dict)]
    payload: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "schema_version": BACKUP_SCHEMA_VERSION,
        "exported_at": stamp,
        "app_version": str(app_version or ""),
        "includes_secrets": bool(include_secrets),
        "site_count": len(items),
        "sites": items,
        "settings": copy.deepcopy(settings or {}),
    }
    if auth is not None:
        payload["auth"] = {
            "enabled": bool(auth.get("enabled")),
            "username": str(auth.get("username") or ""),
            "password_hash": str(auth.get("password_hash") or ""),
            "auth_revision": int(auth.get("auth_revision") or 1),
        }
    return payload


def _payload_bytes(value) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise BackupError("备份内容含无法序列化的数据") from exc


def parse_export(payload) -> dict:
    """校验备份文件结构，返回规范化结果。不碰磁盘，可安全用于预检。"""
    if not isinstance(payload, dict):
        raise BackupError("备份文件内容不是 JSON 对象")
    if payload.get("format") != BACKUP_FORMAT:
        raise BackupError("这不是本工具的配置文件（format 标识不匹配）")
    try:
        schema = int(payload.get("schema_version"))
    except (TypeError, ValueError) as exc:
        raise BackupError("备份文件缺少有效的 schema_version") from exc
    if schema != BACKUP_SCHEMA_VERSION:
        raise BackupError(f"备份文件版本为 {schema}，当前只支持 {BACKUP_SCHEMA_VERSION}")

    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise BackupError("备份文件里没有站点列表")
    if len(sites) > MAX_SITES:
        raise BackupError(f"备份包含 {len(sites)} 个站点，超过上限 {MAX_SITES}")

    settings = payload.get("settings")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise BackupError("备份文件里的设置不是对象")
    if _payload_bytes(settings) > MAX_SETTINGS_BYTES:
        raise BackupError("备份里的设置体积异常，已拒绝导入")

    for index, site in enumerate(sites):
        if not isinstance(site, dict):
            raise BackupError(f"第 {index + 1} 个站点不是对象")
        if _payload_bytes(site) > MAX_SITE_BYTES:
            raise BackupError(f"第 {index + 1} 个站点体积异常，已拒绝导入")

    auth = payload.get("auth")
    if auth is not None and not isinstance(auth, dict):
        raise BackupError("备份文件里的访问保护配置不是对象")

    warnings: list[str] = []
    if payload.get("includes_secrets") is False:
        warnings.append("这份备份不含凭据，还原后各站点的 Cookie / 密码 / 管理令牌 / 模型 Key 需要重新填写")
    if not sites:
        warnings.append("备份里没有任何站点，还原后站点列表会变成空的")

    return {
        "sites": sites,
        "settings": settings,
        "auth": auth,
        "warnings": warnings,
        "exported_at": str(payload.get("exported_at") or ""),
        "app_version": str(payload.get("app_version") or ""),
        "includes_secrets": payload.get("includes_secrets") is not False,
    }


@dataclass
class RestorePlan:
    """已经过校验、可以直接落盘的还原计划。"""

    sites: list[dict]
    settings: dict | None
    auth: dict | None                 # 备份里的访问保护原文，供摘要显示
    restore_auth: bool = False        # 用户是否勾了「同时还原访问保护」
    warnings: list[str] = field(default_factory=list)
    exported_at: str = ""
    app_version: str = ""

    def summary(self) -> dict:
        return {
            "site_count": len(self.sites),
            "site_ids": [str(site.get("id") or "") for site in self.sites],
            "exported_at": self.exported_at,
            "app_version": self.app_version,
            "warnings": list(self.warnings),
            # has_auth 说的是"备份里有没有"，前端据此决定要不要露出那个勾选框
            "has_auth": self.auth is not None,
            "restore_auth": self.restore_auth and self.auth is not None,
        }


def plan_restore(payload, *, sanitize_settings=None, restore_auth: bool = False) -> RestorePlan:
    """校验备份并生成还原计划；任何问题都抛 BackupError，调用方据此整份拒绝。

    sanitize_settings 由调用方注入（server 侧的字段级清洗），这里不重复实现，
    避免两处规则漂移。
    """
    parsed = parse_export(payload)
    cleaned, errors = merge_sites(parsed["sites"], [])
    if errors:
        # 只回前三条，避免整页都是报错
        detail = "；".join(errors[:3])
        more = f"（另有 {len(errors) - 3} 条同类问题）" if len(errors) > 3 else ""
        raise BackupError(f"备份里的站点配置无法使用：{detail}{more}")

    settings = None
    if sanitize_settings is not None:
        settings = sanitize_settings(dict(parsed["settings"]))

    return RestorePlan(
        sites=cleaned,
        settings=settings,
        auth=parsed["auth"],
        restore_auth=bool(restore_auth),
        warnings=parsed["warnings"],
        exported_at=parsed["exported_at"],
        app_version=parsed["app_version"],
    )


def _snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BackupError(f"读取现有配置失败，已取消还原：{exc.strerror or exc}") from exc


def apply_restore(plan: RestorePlan, *, sites_path, settings_path) -> dict:
    """落盘：先设置后站点，任一步失败按内存里的旧字节回滚。

    成功路径仍走 write_json(backup=True)，所以导入前的版本会留在 *.versions/ 里。
    """
    sites_path, settings_path = Path(sites_path), Path(settings_path)
    # 先把两份旧文件读进内存：回滚不能依赖磁盘上"应该还在"的旧文件
    previous = {settings_path: _snapshot(settings_path), sites_path: _snapshot(sites_path)}
    wrote: list[Path] = []
    try:
        if plan.settings is not None:
            write_json(settings_path, plan.settings, backup=True)
            wrote.append(settings_path)

        config = read_json(sites_path, {}) or {}
        if not isinstance(config, dict):
            config = {}
        config["sites"] = copy.deepcopy(plan.sites)
        # 与 ConfigurationStore.migrate 写的版本号保持一致，避免还原后又被迁移一次
        config.setdefault("integration_schema_version", 1)
        write_json(sites_path, config, backup=True)
        wrote.append(sites_path)
    except Exception as exc:
        rolled_back = _rollback(wrote, previous)
        suffix = "，已回滚到导入前的配置" if rolled_back else "，回滚也失败了，请从 *.versions/ 手工恢复"
        raise BackupError(f"写入配置失败：{exc}{suffix}") from exc

    return {"sites": len(plan.sites), "settings": plan.settings is not None,
            "warnings": list(plan.warnings)}


def _rollback(wrote: list[Path], previous: dict[Path, bytes | None]) -> bool:
    """按写反序把旧字节写回。返回是否全部成功。"""
    ok = True
    for path in reversed(wrote):
        try:
            old = previous.get(path)
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old)
        except OSError:
            ok = False
    return ok


def export_filename(now: datetime | None = None) -> str:
    """下载文件名：带时间，避免多次导出互相覆盖。"""
    moment = datetime.now() if now is None else now
    return f"relay-price-config-{moment.strftime('%Y%m%d-%H%M%S')}.json"
