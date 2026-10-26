"""只读比较账户认证请求；报告只保存脱敏错误，不保存账户正文或凭据。

用法：
    python diagnose_account_auth.py              # 检查 sites.json 里所有启用的站点
    python diagnose_account_auth.py <站点id> ...  # 只检查指定站点

三种请求形式依次尝试，找到能通过身份校验的那一种就停止：
Bearer 单独 / Bearer + New-Api-User / Bearer + New-Api-User + 兼容 UA。
结果写入 logs/account-auth-diagnostic.json，正文只保留脱敏后的片段。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from integrations.contracts import classify_response
from integrations.providers import USER_AGENT, ACCEPT_LANGUAGE, UrllibTransport


ROOT = Path(__file__).resolve().parent


def redact_message(payload, sites):
    if not isinstance(payload, dict):
        return None
    message = payload.get("message")
    if not isinstance(message, str):
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str):
        return None
    # 先还原常见 URL 转义，防止错误正文以编码形式回显凭据。
    for _ in range(2):
        message = urllib.parse.unquote(message)
    secrets = set()
    for site in sites:
        for field, value in (site.get("auth") or {}).items():
            if field not in {"account_id", "scope_source"} and isinstance(value, str) and value:
                secrets.add(value)
                if field == "cookie":
                    secrets.update(part.partition("=")[2].strip() for part in value.split(";")
                                   if part.partition("=")[2].strip())
    variants = set()
    for value in secrets:
        variants.update((value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value),
                         json.dumps(value, ensure_ascii=True)[1:-1]))
    for value in sorted(variants, key=len, reverse=True):
        message = message.replace(value, "[REDACTED]")
    message = re.sub(r"[A-Za-z0-9_./+=-]{24,}", "[REDACTED]", message)
    message = re.sub(r"[\x00-\x1f\x7f]", " ", message)
    return message[:600]


def probe_site(site, sites, proxy="", transport=None):
    auth = site.get("auth") or {}
    token, account_id = auth.get("management_token"), auth.get("account_id")
    output = {"site": site["id"], "probes": []}
    if not token or account_id is None or not str(account_id).isascii() or not str(account_id).isdigit() or int(account_id) <= 0:
        output["result"] = "missing_token_or_account_id"
        return output
    transport = transport if transport is not None else UrllibTransport(proxy)
    scenarios = (("bearer_only", {}),
                 ("bearer_and_account_id", {"New-Api-User": str(account_id)}),
                 ("bearer_account_id_legacy_headers", {"New-Api-User": str(account_id),
                     "User-Agent": USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE}))
    for scenario, extra in scenarios:
        row = {"scenario": scenario}
        try:
            response = transport.request("GET", site["base_url"].rstrip("/") + "/api/user/self",
                headers={"Accept": "application/json", "Authorization": "Bearer " + token, **extra}, timeout=8)
        except Exception as error:
            row.update(result="request_failed", exception_type=type(error).__name__)
        else:
            failure = classify_response(response.status, response.payload, body=response.text, headers=response.headers)
            data = response.payload.get("data") if isinstance(response.payload, dict) else None
            identity_matches = isinstance(data, dict) and str(data.get("id")) == str(account_id)
            row.update(http_status=response.status,
                       error_category=failure.category.value if failure else None,
                       message=redact_message(response.payload, sites),
                       result="rejected" if failure else "success" if identity_matches else "identity_not_verified")
        output["probes"].append(row)
        # 已定位到可用形式即可停止，无需发剩余的探测请求。
        if row["result"] == "success":
            output["working_scenario"] = scenario
            break
        if row.get("http_status") == 429:
            break
        if row["result"] == "identity_not_verified":
            break
    return output


def main(argv=None):
    """按命令行传入的站点 id 过滤；不传则检查所有启用站点。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = ROOT / "sites.json"
    if not config_path.is_file():
        print("找不到 sites.json：请先复制 sites.example.json 为 sites.json 并填入站点。")
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))
    sites = config.get("sites", [])
    wanted = {arg for arg in argv if not arg.startswith("-")}
    targets = [site for site in sites
               if site.get("enabled", True) and (not wanted or site.get("id") in wanted)]
    if not targets:
        print("没有匹配的启用站点：检查 sites.json，或确认传入的站点 id 是否正确。")
        return 1
    print(f"正在只读检查 {len(targets)} 个站点的账户接口，最长约 30 秒。", flush=True)
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        results = list(pool.map(lambda site: probe_site(site, sites, config.get("proxy") or ""), targets))
    report = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sites": results}
    destination = ROOT / "logs" / "account-auth-diagnostic.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    for site in results:
        print("\n" + site["site"])
        for row in site["probes"]:
            print(row["scenario"], row.get("http_status", "-"), row["result"], row.get("message") or "")
    print("\n检查完成，已保存脱敏报告。无需复制令牌。")


if __name__ == "__main__":
    main()
