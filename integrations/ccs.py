"""中立的 CC Switch 链接编码与各平台的显式用量脚本模板。"""
import base64
import ipaddress
import math
import re
import urllib.parse

CC_APPS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "gemini": "Gemini CLI",
    "grokbuild": "Grok Build",
    "opencode": "OpenCode",
    "openclaw": "OpenClaw",
    "hermes": "Hermes",
    "pi": "Pi",
}

CC_API_FORMATS = ("", "anthropic", "openai_chat", "openai_responses", "gemini_native")

CC_API_FORMAT_MIN_VERSION = (4, 0, 10)

CC_USAGE_SCRIPT = (
    '({\n'
    '  request: {\n'
    '    url: "{{baseUrl}}/api/user/self",\n'
    '    method: "GET",\n'
    '    headers: {\n'
    '      "Content-Type": "application/json",\n'
    '      "Authorization": "Bearer {{accessToken}}",\n'
    '      "New-Api-User": "{{userId}}",\n'
    '    },\n'
    '  },\n'
    '  extractor: function (response) {\n'
    '    if (response.success && response.data) {\n'
    '      var result = {\n'
    '        isValid: true,\n'
    '        planName: response.data.group || "Default Plan",\n'
    '        unit: "USD",\n'
    '      };\n'
    '      // 余额与已用分别核实数字类型，缺项不按零补齐，也不把字符串相加。\n'
    '      if (typeof response.data.quota === "number" && isFinite(response.data.quota)) {\n'
    '        result.remaining = response.data.quota / 500000;\n'
    '      }\n'
    '      if (typeof response.data.used_quota === "number" && isFinite(response.data.used_quota)) {\n'
    '        result.used = response.data.used_quota / 500000;\n'
    '      }\n'
    '      if (typeof result.remaining === "number" && typeof result.used === "number") {\n'
    '        result.total = result.remaining + result.used;\n'
    '      }\n'
    '      return result;\n'
    '    }\n'
    '    return { isValid: false, invalidMessage: response.message || "查询失败" };\n'
    '  },\n'
    '})'
)

def build_ccswitch_link(*, app: str, name: str, endpoint: str = "", api_key: str = "",
                        model: str = "", homepage: str = "", notes: str = "",
                        api_format: str = "", usage: dict | None = None,
                        icon: str = "", icon_url: str = "") -> str:
    """拼一条 ccswitch://v1/import 深链。参数为空就不传，交给客户端用自己的默认值。"""
    app = (app or "").strip()
    if app not in CC_APPS:
        raise ValueError(f"不支持的应用类型：{app or '(空)'}")

    params: list[tuple[str, str]] = [
        ("resource", "provider"),
        ("app", app),
        ("name", (name or "").strip() or "未命名"),
    ]
    for key, value in (("endpoint", endpoint), ("apiKey", api_key), ("homepage", homepage),
                       ("model", model), ("notes", notes)):
        if str(value or "").strip():
            params.append((key, str(value).strip()))
    fmt = (api_format or "").strip()
    if fmt and fmt in CC_API_FORMATS:
        params.append(("apiFormat", fmt))

    # 只传显式配置的品牌信息；不把站点 ID 冒充为 CCS 内置图标名。
    builtin_icon = str(icon or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_.-]{1,64}", builtin_icon):
        params.append(("icon", builtin_icon))
    image_url = str(icon_url or "").strip()
    try:
        parsed_icon = urllib.parse.urlsplit(image_url)
        host = parsed_icon.hostname or ""
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            is_ip = False
        # CCS 还会自行检查公网解析、Content-Type、大小，并重新编码图片。
        if (len(image_url.encode("utf-8")) <= 2048
                and parsed_icon.scheme == "https" and "." in host and not is_ip
                and not host.lower().endswith((".localhost", ".local"))
                and parsed_icon.port in (None, 443) and not parsed_icon.username
                and not parsed_icon.password):
            params.append(("iconUrl", image_url))
    except ValueError:
        pass

    if usage and usage.get("access_token"):
        params.extend([
            # CCS 先 URL 解码，再将 usageScript 按 Base64 解码成 UTF-8 脚本。
            ("usageScript", base64.b64encode(str(usage.get("script") or CC_USAGE_SCRIPT).encode("utf-8")).decode("ascii")),
            ("usageEnabled", "true"),
            ("usageBaseUrl", str(usage.get("base_url") or "")),
            ("usageAccessToken", str(usage.get("access_token") or "")),
            ("usageUserId", str(usage.get("user_id") or "")),
            ("usageAutoInterval", str(int(usage.get("interval") or 30))),
        ])
    return "ccswitch://v1/import?" + urllib.parse.urlencode(params)


def newapi_usage(*, base_url, token, quota_per_unit, unit, user_id=None):
    if (not token or unit not in {"USD", "CNY"} or type(quota_per_unit) not in {int, float}
            or not math.isfinite(quota_per_unit) or quota_per_unit <= 0):
        return None
    # 身份已由本站账户读取核验；没有 ID 时保持无头模板，不能猜测用户身份。
    valid_user_id = str(user_id or "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", valid_user_id):
        valid_user_id = ""
    script = CC_USAGE_SCRIPT
    if not valid_user_id:
        script = script.replace('      "New-Api-User": "{{userId}}",\n', '')
    script = script.replace("500000", str(float(quota_per_unit))).replace('unit: "USD"', 'unit: ' + __import__('json').dumps(unit))
    return {"base_url": base_url, "access_token": token, "user_id": valid_user_id,
            "script": script, "interval": 30}


SUB2_USAGE_SCRIPT = '''({
  request: {url: "{{baseUrl}}/v1/usage", method: "GET", headers: {
    "Authorization": "Bearer {{accessToken}}", "User-Agent": "cc-switch/1.0"
  }},
  extractor: function(response) {
    var d = response.data || response;
    var quota = d.quota || {};
    var kind = d.mode === "quota_limited" ? "key_quota" : d.subscription ? "subscription" :
      (typeof d.balance === "number" ? "wallet" : "unknown");
    var result = {isValid: d.isValid !== false, planName: d.planName || kind,
      unit: d.unit || quota.unit || "unknown"};
    // 标准接口已给出套餐/Key/钱包的 remaining；缺值、负数及无限额 -1 均不伪装成零。
    if (typeof d.remaining === "number" && isFinite(d.remaining) && d.remaining >= 0) {
      result.remaining = d.remaining;
    } else if (kind === "wallet" && typeof d.remaining === "undefined" && isFinite(d.balance) && d.balance >= 0) {
      result.remaining = d.balance;
    }
    if (typeof quota.limit === "number" && isFinite(quota.limit) && quota.limit > 0) {
      result.total = quota.limit;
      if (typeof quota.used === "number" && isFinite(quota.used) && quota.used >= 0) result.used = quota.used;
    }
    if (!result.isValid) result.invalidMessage = d.invalidMessage || String(d.status || "查询失败");
    // 日/周/月窗口不可相加，接口的套餐剩余额度不改称钱包余额。
    return result;
  }
})'''


def sub2api_usage(*, base_url, key):
    # 防止调用方的 API 根地址已带 /v1 时重复拼接；保留部署在子路径的站点前缀。
    base = str(base_url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return {"base_url": base, "access_token": key, "script": SUB2_USAGE_SCRIPT, "interval": 30} if key else None
