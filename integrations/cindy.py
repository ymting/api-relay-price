"""Cindy 供应商导入链接编码：cindy://provider/import?v=1&data=<base64url(UTF-8 JSON)>。

与 ccs.py 并列：那边生成 CC Switch 的 ccswitch:// 深链，这边生成 Cindy 的导入深链。
两套协议**完全不同**（CC Switch 是 query 平铺、一条对一个 app；Cindy 是单个 base64url
JSON、一条可带多个端点并跨引擎），所以不能互相复用，单独成文件。

规范来源（makecindy/cindy）：
  · docs/provider-import-links.md                  —— 官方协议正本
  · apps/desktop/src/main/provider-import/providerImport.ts —— 字段白名单与各项上限

Cindy 的校验是**严格模式**：多一个未知键就整条拒绝，因此这里只写它明确允许的字段。

**协议常量会随 Cindy 版本漂移**（它自己在 openai-chat / openai-completions 之间来回改），
升级 Cindy 后如果「点导入没反应」，先按下面这段核对新版包里的两个常量，再来改这里：
  D:\\Program Files\\Cindy\\resources\\app.asar
  → 解出 .vite/build/bootstrap-electron-*.js
  → 搜 `AGENTS=[` 与 `PROTOCOLS=[`
"""
import base64
import json
import re

# Cindy 的 harness（endpoints[].targets 的取值白名单）。
# 常量以本机 Cindy v0.1.114 包里的 `AGENTS` 为准：v0.1.114 把 pi **加回来了**，
# 而 v0.1.109~v0.1.113 期间是移出状态 —— 版本间来回横跳，别照旧注释想当然。
CINDY_AGENTS = ("claude-code", "codex", "pi")

# Cindy 支持的四种线协议（包内 `PROTOCOLS` 常量）。
# v0.1.114 又用回 openai-chat：`openai-completions` 不在白名单里，
# 写成它会被 `endpoints[0].protocol is unsupported` 整条拒绝（2026-10-23 实测）。
CINDY_PROTOCOLS = (
    "anthropic-messages",
    "openai-responses",
    "openai-chat",
    "google-generative-ai",
)

# CC Switch 的 app → Cindy 的 harness。Cindy 自定义导入只认这三个，
# gemini / grokbuild / opencode / openclaw / hermes 在它那里没有对应目标。
CINDY_APP_TARGETS = {
    "claude": "claude-code",
    "codex": "codex",
    "pi": "pi",
}

# CC Switch 的 apiFormat → Cindy 的 protocol（前者下划线命名，后者连字符命名）
CC_FORMAT_TO_CINDY_PROTOCOL = {
    "anthropic": "anthropic-messages",
    "openai_chat": "openai-chat",
    "openai_responses": "openai-responses",
    "gemini_native": "google-generative-ai",
}

# 没指定 apiFormat 时按 app 取默认协议，对齐 Cindy 自己的优先级：
# Claude Code 优先 anthropic-messages、Codex 优先 openai-responses、Pi 只吃 openai-chat。
CINDY_DEFAULT_PROTOCOL = {
    "claude": "anthropic-messages",
    "codex": "openai-responses",
    "pi": "openai-chat",
}

# Cindy 端的硬上限，超了会被整条拒绝
CINDY_MAX_URL_LEN = 32 * 1024
CINDY_MAX_JSON_LEN = 24 * 1024
CINDY_MAX_NAME_LEN = 56  # 官方上限 60，留点余量给未预期的多字节组合
CINDY_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
# id 是保留字时 Cindy 直接拒绝，遇到就不传 id（不传是合法的）
CINDY_RESERVED_IDS = {
    "anthropic", "openai", "xai", "xd", "cindy", "gemini", "openai-images",
}


def cindy_target_for_app(app: str) -> str:
    """CC Switch 的 app 能不能导入到 Cindy；不能则返回空串。"""
    return CINDY_APP_TARGETS.get((app or "").strip(), "")


def cindy_protocol_for(app: str, api_format: str = "") -> str:
    """确定 Cindy 的线协议：优先用户选的 apiFormat，否则按 app 取默认。"""
    fmt = (api_format or "").strip()
    if fmt in CC_FORMAT_TO_CINDY_PROTOCOL:
        return CC_FORMAT_TO_CINDY_PROTOCOL[fmt]
    return CINDY_DEFAULT_PROTOCOL.get((app or "").strip(), "anthropic-messages")


def cindy_default_endpoint(base_url: str, protocol: str) -> str:
    """按协议给出端点默认值。

    Anthropic 协议下 Claude Code 自己会补 `/v1/messages`，所以给站点根地址；
    OpenAI 系协议（completions / responses）按 Cindy 内置 sub2api 预设的惯例带 `/v1`。
    """
    base = (base_url or "").strip().rstrip("/")
    if protocol == "anthropic-messages":
        return base
    return base + "/v1"


def cindy_app_name(app: str) -> str:
    """导出结果里显示用的引擎名。"""
    return {"claude": "Claude Code", "codex": "Codex", "pi": "Pi"}.get((app or "").strip(), "")


def _cindy_slug(value: str) -> str:
    """把站点 id / 名称压成 Cindy 要求的 id（^[a-z0-9_-]{1,40}$）；不合法返回空串。"""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:40]
    if slug and CINDY_ID_RE.match(slug) and slug not in CINDY_RESERVED_IDS:
        return slug
    return ""


def build_cindy_import_link(*, name, endpoint, api_key, protocol, target,
                            provider_id: str = "") -> str:
    """拼一条 cindy://provider/import 深链。

    只写 Cindy 白名单里的字段——多一个键它就会整条拒绝（exactFields 严格校验）。
    **故意不带 models**：交给 Cindy 导入后自己拉取，既避免把模型清单写死，
    也避免链接超长（官方也建议「优先生成短链接并省略模型清单」）。
    """
    if protocol not in CINDY_PROTOCOLS:
        raise ValueError(f"不支持的协议：{protocol or '(空)'}")
    if target not in CINDY_AGENTS:
        raise ValueError(f"不支持的引擎：{target or '(空)'}")
    key = str(api_key or "").strip()
    if not key:
        raise ValueError("缺少 API Key")

    endpoint = str(endpoint or "").strip().rstrip("/")
    if not endpoint:
        raise ValueError("缺少 API 地址")

    data = {
        "kind": "custom",
        "name": (str(name or "").strip() or "未命名")[:CINDY_MAX_NAME_LEN],
        "auth": {"method": "apiKey", "apiKey": key},
        "endpoints": [{"protocol": protocol, "baseUrl": endpoint, "targets": [target]}],
    }
    # id 可选：不合法/保留字就不传，而不是生成一条会被拒的链接
    slug = _cindy_slug(provider_id)
    if slug:
        data["id"] = slug

    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > CINDY_MAX_JSON_LEN:
        raise ValueError("导入数据超过 Cindy 的 24 KiB 上限")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    link = f"cindy://provider/import?v=1&data={encoded}"
    if len(link) > CINDY_MAX_URL_LEN:
        raise ValueError("导入链接超过 Cindy 的 32 KiB 上限")
    return link
