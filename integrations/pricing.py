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


# ---------------------------------------------------------------------------
# 分时计价（闲时 ×1 / 忙时 ×2、工作日与周末不同价）
# ---------------------------------------------------------------------------
#
# New API 的 billing_expr 里，时间条件有两种写法：
#   1) 乘在基础价后面： (tier("空闲期", p*1.5 + c*4.5)) * (hour("Asia/Shanghai") >= 9 && hour(...) < 12 ? 2 : 1)
#   2) 整条三元分叉：   (hour(...) >= 9 && hour(...) < 12) ? tier("peak", p*9) : tier("off_peak", p*4.5)
# 两者都能还原成「时段 → 单价」窗口表。做法是**静态抽样**：把 len 固定为 0、把一周 7×24 小时
# 逐格求值，得到每一格的价格，再把同价且相邻的小时合并成窗口。
# 表达式仍然只按白名单语法解析（不给远端表达式任何执行机会），未知函数一律放弃并保持原「参考价」口径。

FIELDS_ORDER = ("input", "output", "cache_read", "cache_write", "cache_write_1h")
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 显式引用时区的两种函数；两者混用时区说明站点自己在跨时区算，不猜
_TIME_REF = re.compile(r"\b(hour|weekday)\s*\(\s*([\"'])([^\"']*)\2\s*\)")
# 只支持整点边界：出现 9.5 这类非整数阈值时，整点抽样会漏判，直接放弃
_TIME_CMP = re.compile(
    r"(?:(?:hour|weekday)\s*\(\s*[\"'][^\"']*[\"']\s*\)\s*(?:<=|>=|<|>|==|!=)\s*(-?\d+(?:\.\d+)?)"
    r"|(-?\d+(?:\.\d+)?)\s*(?:<=|>=|<|>|==|!=)\s*(?:hour|weekday)\s*\(\s*[\"'][^\"']*[\"']\s*\))",
    re.I,
)


class _ExprError(Exception):
    """白名单表达式无法静态求值（语法外/未知函数/非线性）。"""


class _Price:
    """线性价格式：Σ(coeff × 每百万token量) + 单次固定价。

    p/c/cr/cc/cc1h 的系数本身就是 USD/1M tokens；u("input_tokens") 这类按 token
    计数的写法换算成同一口径（每 token 1 美元 → 每百万 1e-6×1e6）。
    """

    __slots__ = ("coeffs", "call")

    def __init__(self, coeffs: dict | None = None, call: float = 0.0):
        self.coeffs = coeffs or {}
        self.call = call

    def scale(self, factor: float) -> "_Price":
        return _Price({k: v * factor for k, v in self.coeffs.items()}, self.call * factor)

    def merge(self, other: "_Price") -> "_Price":
        merged = dict(self.coeffs)
        for key, value in other.coeffs.items():
            merged[key] = merged.get(key, 0.0) + value
        return _Price(merged, self.call + other.call)


_VAR_FIELDS = {"p": "input", "c": "output", "cr": "cache_read", "cc": "cache_write", "cc1h": "cache_write_1h"}
_UNIT_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_tokens": "cache_read",
    "cache_read_input_tokens": "cache_read",
    "cache_write_tokens": "cache_write",
    "cache_creation_input_tokens": "cache_write",
}
# tier() 标签 → 展示用中文；空串表示标签本身没有区分意义（如 base/standard）
_TIER_LABELS = {
    "peak": "高峰", "busy": "高峰", "high": "高峰", "高峰": "高峰",
    "off_peak": "闲时", "offpeak": "闲时", "off-peak": "闲时", "idle": "闲时",
    "空闲期": "闲时", "空闲": "闲时", "闲时": "闲时", "低峰": "闲时",
    "base": "", "standard": "", "normal": "",
}

_EXPR_TOKEN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<num>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    r"|(?P<str>\"[^\"\\]*\"|'[^'\\]*')"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op><=|>=|==|!=|&&|\|\||[<>+\-*/(),?:!])"
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(expr):
        match = _EXPR_TOKEN.match(expr, pos)
        if not match:
            raise _ExprError("表达式含非白名单字符")
        pos = match.end()
        kind = match.lastgroup
        if kind != "space":
            tokens.append((kind, match.group(kind)))
    return tokens


class _ExprParser:
    """递归下降解析：三元 / || / && / 比较 / 加减 / 乘除 / 函数调用。"""

    def __init__(self, tokens: list[tuple[str, str]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self, value: str | None = None) -> bool:
        if self.pos >= len(self.tokens):
            return False
        return value is None or self.tokens[self.pos][1] == value

    def take(self, value: str | None = None) -> tuple[str, str]:
        if self.pos >= len(self.tokens):
            raise _ExprError("表达式提前结束")
        token = self.tokens[self.pos]
        if value is not None and token[1] != value:
            raise _ExprError(f"期望 {value}")
        self.pos += 1
        return token

    def parse(self):
        node = self.ternary()
        if self.pos != len(self.tokens):
            raise _ExprError("表达式尾部有多余内容")
        return node

    def ternary(self):
        cond = self.logical_or()
        if self.peek("?"):
            self.take("?")
            left = self.ternary()
            self.take(":")
            right = self.ternary()
            return ("tern", cond, left, right)
        return cond

    def logical_or(self):
        node = self.logical_and()
        while self.peek("||"):
            self.take()
            node = ("bin", "||", node, self.logical_and())
        return node

    def logical_and(self):
        node = self.unary_logic()
        while self.peek("&&"):
            self.take()
            node = ("bin", "&&", node, self.unary_logic())
        return node

    def unary_logic(self):
        if self.peek("!"):
            self.take()
            return ("un", "!", self.unary_logic())
        return self.compare()

    def compare(self):
        node = self.additive()
        while self.peek() and self.tokens[self.pos][1] in ("<", "<=", ">", ">=", "==", "!="):
            op = self.take()[1]
            node = ("bin", op, node, self.additive())
        return node

    def additive(self):
        node = self.multiplicative()
        while self.peek() and self.tokens[self.pos][1] in ("+", "-"):
            op = self.take()[1]
            node = ("bin", op, node, self.multiplicative())
        return node

    def multiplicative(self):
        node = self.unary()
        while self.peek() and self.tokens[self.pos][1] in ("*", "/"):
            op = self.take()[1]
            node = ("bin", op, node, self.unary())
        return node

    def unary(self):
        if self.peek("-"):
            self.take()
            return ("un", "-", self.unary())
        return self.primary()

    def primary(self):
        if self.peek("("):
            self.take()
            node = self.ternary()
            self.take(")")
            return node
        kind, value = self.take()
        if kind == "num":
            return ("num", float(value))
        if kind == "str":
            return ("str", value[1:-1])
        if kind != "ident":
            raise _ExprError("无法识别的记号")
        if self.peek("("):
            self.take()
            args = []
            if not self.peek(")"):
                args.append(self.ternary())
                while self.peek(","):
                    self.take()
                    args.append(self.ternary())
            self.take(")")
            return ("call", value, args)
        return ("var", value)


def _scale(value, factor: float):
    if value[0] == "num":
        return ("num", value[1] * factor)
    if value[0] == "price":
        return ("price", value[1].scale(factor))
    raise _ExprError("不支持的类型参与乘法")


def _eval(node, ctx: dict):
    """在当前上下文（时区/星期/小时/长度）求值，返回 ("num"|"price"|"bool"|"str"|"param", ...)。"""
    kind = node[0]
    if kind == "num":
        return ("num", node[1])
    if kind == "str":
        return ("str", node[1])
    if kind == "var":
        name = node[1]
        if name == "len":
            return ("num", float(ctx["len"]))
        field = _VAR_FIELDS.get(name)
        if field is None:
            raise _ExprError(f"未知变量 {name}")
        return ("price", _Price({field: 1.0}))

    if kind == "un":
        operand = _eval(node[2], ctx)
        if node[1] == "-":
            if operand[0] == "num":
                return ("num", -operand[1])
            raise _ExprError("不支持的价格取负")
        if operand[0] != "bool":
            raise _ExprError("! 只能作用于布尔值")
        return ("bool", not operand[1])

    if kind == "tern":
        cond = _eval(node[1], ctx)
        if cond[0] != "bool":
            raise _ExprError("三元条件不是布尔值")
        return _eval(node[2] if cond[1] else node[3], ctx)

    if kind == "call":
        return _eval_call(node, ctx)

    # kind == "bin"
    op = node[1]
    if op in ("&&", "||"):
        left = _eval(node[2], ctx)
        if left[0] != "bool":
            raise _ExprError("逻辑运算要求布尔值")
        # 短路：右侧只在需要时求值（与站点实现一致）
        if op == "&&" and not left[1]:
            return ("bool", False)
        if op == "||" and left[1]:
            return ("bool", True)
        right = _eval(node[3], ctx)
        if right[0] != "bool":
            raise _ExprError("逻辑运算要求布尔值")
        return ("bool", right[1])

    left = _eval(node[2], ctx)
    right = _eval(node[3], ctx)

    if op in ("<", "<=", ">", ">=", "==", "!="):
        if left[0] == "param" or right[0] == "param":
            # 未指定 service_tier 时，等值判断按「不成立」处理；不等判断无法确定，直接放弃
            if op == "==" and (left[0] == "param") != (right[0] == "param"):
                return ("bool", False)
            raise _ExprError("无法确定的参数比较")
        if left[0] == "num" and right[0] == "num":
            a, b = left[1], right[1]
            return ("bool", {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
                             "==": a == b, "!=": a != b}[op])
        if left[0] == "str" and right[0] == "str":
            return ("bool", (left[1] == right[1]) if op == "==" else (left[1] != right[1]))
        raise _ExprError("比较双方类型不支持")

    if op == "+":
        if left[0] == "num" and right[0] == "num":
            return ("num", left[1] + right[1])
        if left[0] == "price" and right[0] == "price":
            return ("price", left[1].merge(right[1]))
        raise _ExprError("价格与常数不能直接相加")
    if op == "-":
        if left[0] == "num" and right[0] == "num":
            return ("num", left[1] - right[1])
        raise _ExprError("不支持价格相减")
    if op == "*":
        if left[0] == "num" and right[0] == "num":
            return ("num", left[1] * right[1])
        if left[0] == "price" and right[0] == "num":
            return _scale(left, right[1])
        if left[0] == "num" and right[0] == "price":
            return _scale(right, left[1])
        raise _ExprError("价格之间不能相乘")
    if op == "/":
        if right[0] != "num" or right[1] == 0:
            raise _ExprError("除数必须是常数")
        if left[0] == "num":
            return ("num", left[1] / right[1])
        return _scale(left, 1 / right[1])
    raise _ExprError(f"未知运算符 {op}")


def _eval_call(node, ctx: dict):
    name, args = node[1], node[2]
    if name == "tier":
        if len(args) != 2:
            raise _ExprError("tier 需要两个参数")
        label = _eval(args[0], ctx)
        if label[0] != "str":
            raise _ExprError("tier 的标签必须是字符串")
        value = _eval(args[1], ctx)
        # 记录实际命中的档位标签，用来给时段起名
        ctx["labels"].append(label[1])
        return value
    if name == "fixed":
        if len(args) != 1:
            raise _ExprError("fixed 需要一个参数")
        value = _eval(args[0], ctx)
        if value[0] != "num":
            raise _ExprError("fixed 只接受常数")
        return ("price", _Price({}, value[1]))
    if name in ("hour", "weekday"):
        if len(args) != 1 or args[0][0] != "str":
            raise _ExprError(f"{name} 需要一个时区字符串")
        if args[0][1] != ctx["tz"]:
            raise _ExprError("同一表达式出现多个时区")
        return ("num", float(ctx["hour"] if name == "hour" else ctx["weekday"]))
    if name == "param":
        if len(args) != 1 or args[0][0] != "str":
            raise _ExprError("param 需要一个参数名")
        return ("param", args[0][1])
    if name == "u":
        if len(args) != 1 or args[0][0] != "str":
            raise _ExprError("u 需要一个字段名")
        field = _UNIT_FIELDS.get(args[0][1])
        if field is None:
            raise _ExprError("未知的用量字段")
        # 每 token 计价：换成「每百万」口径
        return ("price", _Price({field: 1e-6}))
    raise _ExprError(f"未知函数 {name}")


def _day_label(days: list[int]) -> str:
    if len(days) == 7:
        return "每天"
    if days == [1, 2, 3, 4, 5]:
        return "周一至周五"
    if days == [1, 2, 3, 4, 5, 6]:
        return "周一至周六"
    if days == [6, 7]:
        return "周六、周日"
    return "、".join(WEEKDAY_NAMES[d - 1] for d in days)


def _tier_name(labels: tuple) -> str:
    """取最深一层的 tier 标签（最靠内的才是真正生效的档位）。"""
    for raw in reversed(labels):
        key = (raw or "").strip().lower()
        if key in _TIER_LABELS:
            return _TIER_LABELS[key]
        if raw.strip():
            return raw.strip()
    return ""


def _minutes_label(minutes: int) -> str:
    minutes = max(0, min(1440, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _time_windows_from_grid(grid: list, tz: str) -> list[dict] | None:
    """把 7×24 的价格网格压成若干「星期 × 时段」窗口。"""
    day_groups: dict[tuple, list[int]] = {}
    for index, row in enumerate(grid, start=1):
        signature = tuple(row)
        day_groups.setdefault(signature, []).append(index)

    windows: list[dict] = []
    for signature, days in day_groups.items():
        cells = list(signature)
        segments: list[list] = []
        start = 0
        for hour in range(1, 25):
            if hour == 24 or cells[hour] != cells[start]:
                segments.append([start, hour, cells[start], False])
                start = hour
        # 首尾同价且跨零点时合成一段（18:00–次日09:00），避免表格里出现两行同样价格
        if len(segments) > 1 and segments[0][2] == segments[-1][2]:
            tail = segments.pop()
            head = segments.pop(0)
            segments.append([tail[0], head[1], tail[2], True])
        segments.sort(key=lambda item: item[0])
        for seg_start, seg_end, cell, wrap in segments:
            coeffs, call, labels = cell
            prices = dict(zip(FIELDS_ORDER, coeffs))
            windows.append({
                "label": _tier_name(labels),
                "days": list(days),
                "day_label": _day_label(list(days)),
                "start": seg_start * 60,
                "end": seg_end * 60,
                "wrap": wrap,
                **prices,
                "per_call": call or None,
                "tz": tz,
            })
    return windows or None


def parse_time_pricing(expr: str) -> dict | None:
    """把含 hour()/weekday() 的计费表达式展开成「时段 → 价格」窗口表。

    价格为表达式原始系数（USD/1M tokens；按次为 USD/次），**未乘分组倍率**，
    由调用方折算。返回 None 表示无法静态确定，保持原「参考价」口径。
    start/end 是当天 0 点起的分钟数；wrap=True 表示跨零点。
    """
    if not isinstance(expr, str) or not expr or len(expr) > 100000:
        return None
    refs = _TIME_REF.findall(expr)
    if not refs:
        return None
    zones = {match[2] for match in refs}
    if len(zones) != 1:
        return None                      # 跨时区混算不做猜测
    for match in _TIME_CMP.finditer(expr):
        literal = match[1] or match[2]
        try:
            if not float(literal).is_integer():
                return None              # 非整点边界，整点抽样会漏判
        except (TypeError, ValueError):
            return None
    tz = zones.pop()
    if not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
        return None

    try:
        node = _ExprParser(_tokenize(expr)).parse()
    except _ExprError:
        return None

    grid = []
    for weekday in range(1, 8):
        row = []
        for hour in range(24):
            ctx = {"tz": tz, "weekday": weekday, "hour": hour, "len": 0, "labels": []}
            try:
                value = _eval(node, ctx)
            except _ExprError:
                return None
            if value[0] != "price":
                return None
            coeffs = tuple(value[1].coeffs.get(field) for field in FIELDS_ORDER)
            call = value[1].call
            for amount in list(coeffs) + [call]:
                if amount is not None and (not math.isfinite(amount) or amount < 0):
                    return None
            row.append((coeffs, call, tuple(ctx["labels"])))
        grid.append(row)

    windows = _time_windows_from_grid(grid, tz)
    if not windows:
        return None

    # 倍率以最便宜的一档为基准，方便前端标「高峰 ×2」
    base_input = min((w["input"] for w in windows if w["input"]), default=None)
    base_call = min((w["per_call"] for w in windows if w["per_call"]), default=None)
    for window in windows:
        anchor, own = (base_input, window["input"]) if base_input else (base_call, window["per_call"])
        window["multiplier"] = (round(own / anchor, 4)
                                if anchor and own is not None and anchor > 0 else 1.0)
        # 倍率是实际价差的来源，优先用它命名；倍率为 1 的档位才沿用表达式里的标签
        if window["multiplier"] > 1:
            window["label"] = "高峰"
        elif not window["label"]:
            window["label"] = "闲时"
        window["peak"] = window["multiplier"] > 1

    minimum = {field: min((w[field] for w in windows if w[field] is not None), default=None)
               for field in FIELDS_ORDER}
    maximum = {field: max((w[field] for w in windows if w[field] is not None), default=None)
               for field in FIELDS_ORDER}
    return {
        "tz": tz,
        "source": "billing_expr",
        "assumed_timezone": False,
        "windows": windows,
        "min": minimum,
        "max": maximum,
        "min_per_call": min((w["per_call"] for w in windows if w["per_call"] is not None), default=None),
        "max_per_call": max((w["per_call"] for w in windows if w["per_call"] is not None), default=None),
        "variants": len({(w["input"], w["output"], w["per_call"]) for w in windows}) > 1,
    }


def scale_time_pricing(info: dict, ratio: float) -> dict:
    """把时段窗口价按分组倍率折算成最终价（USD/1M、USD/次），前端直接展示。"""
    def scaled(value):
        return value * ratio if value is not None else None

    windows = []
    for window in info.get("windows") or []:
        item = dict(window)
        for field in FIELDS_ORDER:
            item[field] = scaled(window.get(field))
        item["per_call"] = scaled(window.get("per_call"))
        windows.append(item)
    return {
        **{k: info.get(k) for k in ("tz", "source", "assumed_timezone", "variants")},
        "windows": windows,
        "min": {field: scaled(info.get("min", {}).get(field)) for field in FIELDS_ORDER},
        "max": {field: scaled(info.get("max", {}).get(field)) for field in FIELDS_ORDER},
        "min_per_call": scaled(info.get("min_per_call")),
        "max_per_call": scaled(info.get("max_per_call")),
    }


def parse_clock_span(start, end) -> tuple[int, int] | None:
    """解析 "09:00" / "9:00:00" 这类时段边界，返回 (起始分钟, 结束分钟)。"""
    def minutes(text):
        match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{1,2}))?(?::\d{1,2})?\s*", str(text or ""))
        if not match:
            return None
        hour, minute = int(match[1]), int(match[2] or 0)
        if hour > 24 or minute > 59 or (hour == 24 and minute):
            return None
        return hour * 60 + minute

    begin, finish = minutes(start), minutes(end)
    if begin is None or finish is None or begin == finish:
        return None
    return begin, finish


def build_peak_windows(prices: dict, per_call, multiplier: float, start: int, end: int,
                       tz: str = "Asia/Shanghai") -> dict | None:
    """Sub2API 的分组高峰倍率 → 时段窗口表（闲时 = 基础价，高峰 = 基础价 × 高峰倍率）。"""
    if not (isinstance(multiplier, (int, float)) and math.isfinite(multiplier) and multiplier > 0):
        return None

    def scaled(factor):
        return {field: (prices.get(field) * factor if prices.get(field) is not None else None)
                for field in FIELDS_ORDER}

    peak = scaled(multiplier)
    flat = scaled(1.0)
    wrap = end < start
    windows = [{
        "label": "高峰", "days": list(range(1, 8)), "day_label": "每天",
        "start": start, "end": end, "wrap": wrap, "multiplier": round(multiplier, 4),
        "peak": multiplier > 1, **peak, "per_call": per_call * multiplier if per_call is not None else None,
        "tz": tz,
    }]
    # 高峰之外的时段要显式写出来，前端才知道「现在不是高峰」而不是「匹配不到」
    spans = [(0, start), (end, 1440)] if not wrap else [(end, start)]
    for span_start, span_end in spans:
        if span_end <= span_start:
            continue
        windows.append({
            "label": "闲时", "days": list(range(1, 8)), "day_label": "每天",
            "start": span_start, "end": span_end, "wrap": False,
            "multiplier": 1.0, "peak": False, **flat, "per_call": per_call, "tz": tz,
        })
    windows.sort(key=lambda item: item["start"])
    minimum = {field: min((w[field] for w in windows if w[field] is not None), default=None)
               for field in FIELDS_ORDER}
    maximum = {field: max((w[field] for w in windows if w[field] is not None), default=None)
               for field in FIELDS_ORDER}
    return {
        "tz": tz,
        "source": "peak_rate",
        "assumed_timezone": True,
        "windows": windows,
        "min": minimum,
        "max": maximum,
        "min_per_call": min((w["per_call"] for w in windows if w["per_call"] is not None), default=None),
        "max_per_call": max((w["per_call"] for w in windows if w["per_call"] is not None), default=None),
        "variants": multiplier != 1,
    }


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
                # 含 hour()/weekday() 的表达式旧口径解不出来，改走「时段窗口」：主表填最低时段价，
                # 每个时段的单价一起落库，前端按当前时间挑生效价。
                time_info = parse_time_pricing(expr) if expr and not coeffs else None
                if expr:
                    # 动态计价：表达式系数即 USD/1M tokens，再乘分组倍率
                    offer["dynamic"] = True
                    offer["billing_expr"] = expr
                    if time_info:
                        scaled = scale_time_pricing(time_info, ratio)
                        offer["pricing_verified"] = True
                        offer["pricing_basis"] = "time_windows"
                        offer["time_windows"] = scaled
                        offer["price_windows"] = scaled["windows"]
                        offer["time_variant"] = bool(scaled.get("variants"))
                        for key in FIELDS_ORDER:
                            offer[key] = scaled["min"].get(key)
                        offer["per_call"] = scaled.get("min_per_call")
                    else:
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
            per_call = price("per_request_price", token_price=False)
            # 分组开了高峰倍率且时段可解析 → 直接算成「闲时/高峰」两档窗口，
            # 主表先按便宜的时段入价，前端打开时再按当前时间挑生效价。
            peak_span = (parse_clock_span(group.get("peak_start"), group.get("peak_end"))
                         if group.get("peak_rate_enabled") else None)
            peak_windows = None
            if peak_span:
                peak_windows = build_peak_windows(
                    prices, per_call, to_float(group.get("peak_rate_multiplier")), *peak_span,
                    tz=group.get("peak_timezone") or "Asia/Shanghai")
            pending = bool(time_pricing or pricing.get("intervals") or image_independent
                           or (group.get("peak_rate_enabled") and not peak_windows)
                           or group.get("video_rate_independent"))
            base_prices = dict(prices)
            if peak_windows:
                base_prices = dict(peak_windows["min"])
                per_call = peak_windows.get("min_per_call", per_call)
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
                "dynamic": pending or bool(peak_windows),
                "billing_expr": None,
                **base_prices,
                "per_call": per_call,
                "price_unit": "USD/request" if pricing.get("per_request_price") is not None else "USD/1M tokens",
                "source_price_unit": "USD/request" if pricing.get("per_request_price") is not None else "USD/token",
                "pricing_verified": not pending,
                "pricing_basis": ("base_rate_pending_dynamic" if pending
                                  else ("time_windows" if peak_windows else "base_rate")),
                "time_pricing": time_pricing,
                "time_windows": peak_windows,
                "price_windows": (peak_windows or {}).get("windows") or [],
                "time_variant": bool(peak_windows and peak_windows.get("variants")),
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
