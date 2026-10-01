"""P0-3 日志脱敏：统一的 redact() 过滤器，全局挂到 logging 上。

设计要点
- 只做「字符串 -> 字符串」的纯函数，便于单测；日志侧用 RedactingFilter 包装。
- 脱敏是有名字的（<phone> / <id-card> / ...），保证稳定、可断言、可搜索。
- 支持注册「已知密钥明文」：配置里的真实密码/token 会被精确替换，
  这样即使密钥出现在意料之外的格式里也不会泄漏。
- 通用长串兜底规则必须保守：只命中「同时含大小写、数字、_ -」的不透明串，
  否则会把普通英文单词（如 RAIL_DEVICEID 这种 15 位驼峰词）误伤成 <secret>。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Pattern, Sequence, Tuple

PHONE = "<phone>"
ID_CARD = "<id-card>"
EMAIL = "<email>"
COOKIE = "<cookie>"
TOKEN = "<token>"
PASSWORD = "<password>"
SECRET = "<secret>"

# 只要消息里出现这些「便宜关键字」之一，才进入正则脱敏，避免每条日志都跑一串正则。
_HINT_RE = re.compile(
    r"cookie|token|password|passwd|secret|phone|mobile|tel|id_?card|email|身份证|手机|密码|密钥|验证码|session",
    re.IGNORECASE,
)

# 具名赋值：password / token 等字段。保留字段名（便于排障），值一律替换。
# 值既要能匹配裸值（password=hunter2），也要能匹配带引号的值（"password": "hunter2"）。
# 具名赋值：password / token 等字段。保留字段名（便于排障），值一律替换。
# 值既要能匹配裸值（password=hunter2），也要能匹配带引号的值（"password": "hunter2"）。
# 值是 %s / %(name)s 之类的占位符时不能替换：日志的 msg 模板本身可能长这样，
# 替换掉占位符会让 logging 的 msg % args 抛 TypeError。
_ASSIGN = (
    r'(?P<k>{keys}["\x27]?\s*[:=]\s*)'
    r'(?P<v>["\x27][^"\x27\r\n]*["\x27]|(?!%[sd(])[^\s,;&}"\x27]+)'
)
_PASSWORD_KEYS = r'["\x27]?(?:password|passwd|pwd|pass)["\x27]?'

# 通用不透明串：要求同时含小写、大写、数字，并且至少 32 位。
# 「大小写 + 数字」这一条能把绝大多数英文单词和驼峰标识符排除掉，
# 否则 RAIL_DEVICEID 这种 15 位驼峰词会被误伤。
_OPAQUE_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?=[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9_\-]*[A-Za-z])"
    r"(?=[A-Za-z0-9_\-]*\d)"
    r"[A-Za-z0-9_\-]{32,}"
)

# 纯十六进制长串（>=30 位）：md5 / sha1 / session id 的特征，
# 普通英文单词不可能长这样，所以单独兜底一条。
_HEX_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-f]{30,}(?![A-Za-z0-9])")

# 顺序有讲究：
#   1. 身份证必须在手机号之前——身份证里含 11 位数字片段，先匹配手机号会把尾号切碎
#   2. 具名赋值（password/token）在通用长串之前，保证字段名保留下来
_RULES: Tuple[Tuple[str, Pattern[str]], ...] = (
    # 身份证：故意不校验日月范围，避免漏脱敏；18 位带 X 结尾，或 15 位老号
    ("id_card", re.compile(r"(?<!\d)(?:\d{17}[\dXx]|\d{15})(?!\d)")),
    # 邮箱（不含 11 位连续数字，与手机号不冲突）
    ("email", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    # 手机号：+86 / 86 前缀 + 已打码形式（123****5678 不能原样留下尾号）
    (
        "phone",
        re.compile(
            r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d(?:[-\s]?\d{4}[-\s]?\d{4}|\*{4}\d{4})(?!\d)"
        ),
    ),
    # password / token 具名赋值
    ("password", re.compile(_ASSIGN.replace("{keys}", _PASSWORD_KEYS), re.IGNORECASE)),
    (
        "token",
        re.compile(
            _ASSIGN.replace(
                "{keys}",
                r'["\x27]?(?:token|access_token|refresh_token|api[_-]?key|secret[_-]?key|appkey|app_secret|jwt)["\x27]?',
            ),
            re.IGNORECASE,
        ),
    ),
    # Cookie：保留 cookie 名，值一律替换
    (
        "cookie",
        re.compile(
            r'(?P<k>JSESSIONID=|RAIL_EXPIRATION=|RAIL_DEVICEID=|BIGipServer[A-Za-z0-9_\-]*=|tk=|uamtk=|_jc_save_[a-z_]+=)'
            r'(?P<v>[^;\s"\x27]+)'
        ),
    ),
    (
        "cookie",
        re.compile(
            r'(?P<k>(?<![-\w])["\x27]?cookie["\x27]?\s*[:=]\s*)(?P<v>["\x27][^"\x27\r\n]*["\x27]|[^"\x27\r\n]+)',
            re.IGNORECASE,
        ),
    ),
    ("secret", _OPAQUE_RE),
    ("secret", _HEX_RE),
)

_OPAQUE_RE_MIN_LEN = 32

_PLACEHOLDER_BY_RULE = {
    "phone": PHONE,
    "id_card": ID_CARD,
    "email": EMAIL,
    "cookie": COOKIE,
    "token": TOKEN,
    "password": PASSWORD,
    "secret": SECRET,
}


def _replace_keeping_prefix(pattern: Pattern[str], repl: str, text: str) -> str:
    """替换时保留前缀命名组 k 的字面内容（例如 'password=' 或 'RAIL_DEVICEID='）。"""
    if "k" in pattern.groupindex:

        def _sub(match: "re.Match[str]") -> str:
            return match.group("k") + repl

        return pattern.sub(_sub, text)
    return pattern.sub(repl, text)


@dataclass
class RedactionPolicy:
    """可组合的脱敏策略。

    extra_patterns: 额外正则（来自 LOG_REDACT_EXTRA，分号分隔）
    literals:       已知密钥明文，精确替换（来自配置里读到的真实密码/token）
    """

    extra_patterns: Optional[List[str]] = None
    literals: Optional[List[str]] = None
    enabled: bool = True

    _compiled_extra: List[Pattern[str]] = field(default_factory=list, init=False, repr=False)
    _literals: List[str] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.extra_patterns = list(self.extra_patterns or [])
        self.literals = list(self.literals or [])
        self._compiled_extra = [re.compile(p) for p in self.extra_patterns]
        self._refresh_literals()

    def _refresh_literals(self) -> None:
        # 长的先替换，避免 "abc" 是 "abcdef" 的密钥时把后者切碎
        uniq = {lit for lit in self.literals if lit and len(lit) >= 4}
        self._literals = sorted(uniq, key=len, reverse=True)

    def register_secret(self, value: Optional[str]) -> None:
        """注册一个已知密钥明文（配置读到的密码 / webhook token / JWT 密钥 等）。"""
        if not value or len(value) < 4:
            return
        if value not in self.literals:
            self.literals.append(value)
            self._refresh_literals()

    def register_secrets(self, values: Sequence[Optional[str]]) -> None:
        for value in values:
            self.register_secret(value)

    def redact(self, text: str) -> str:
        if not self.enabled or not text:
            return text

        out = text
        # 1) 已知明文密钥：与格式无关，最可靠
        for literal in self._literals:
            if literal in out:
                out = out.replace(literal, SECRET)

        # 2) 结构化标识符（手机号 / 身份证 / 邮箱）可能出现在任何一行日志里，
        #    不能靠关键字预筛跳过。预筛只用来挡掉「又短又没有关键字」的行。
        if not _HINT_RE.search(out) and len(out) < 8:
            return out

        # 3) 内置规则
        for rule, pattern in _RULES:
            if pattern.search(out):
                out = _replace_keeping_prefix(pattern, _PLACEHOLDER_BY_RULE[rule], out)

        # 4) 用户自定义规则：配置写错不能连累日志本身
        for pattern in self._compiled_extra:
            try:
                if pattern.search(out):
                    out = pattern.sub(SECRET, out)
            except re.error:
                continue
        return out

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "RedactionPolicy":
        env = dict(os.environ if env is None else env)
        raw_extra = (env.get("LOG_REDACT_EXTRA") or "").strip()
        extras = [p for p in (s.strip() for s in raw_extra.split(";")) if p]
        literals = [s.strip() for s in (env.get("LOG_REDACT_LITERALS") or "").split(",") if s.strip()]
        enabled = (env.get("LOG_REDACT", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}
        policy = cls(extra_patterns=extras, literals=literals, enabled=enabled)
        for key in (
            "PASSWORD",
            "JWT_SECRET_KEY",
            "RUNTIME_ENC_KEY",
            "DINGTALK_WEBHOOK",
            "DINGTALK_SECRET",
            "SERVERCHAN_KEY",
            "BARK_URL",
            "WEBHOOK_URL",
            "REDIS_URL",
            "AUTO_CODE_ACCOUNT_PWD",
            "WEB_BASIC_AUTH_PASSWORD",
        ):
            policy.register_secret(env.get(key))
        return policy


_DEFAULT_POLICY: Optional[RedactionPolicy] = None


def default_policy() -> RedactionPolicy:
    global _DEFAULT_POLICY
    if _DEFAULT_POLICY is None:
        _DEFAULT_POLICY = RedactionPolicy.from_env()
    return _DEFAULT_POLICY


def set_default_policy(policy: RedactionPolicy) -> None:
    global _DEFAULT_POLICY
    _DEFAULT_POLICY = policy


def redact(text: str, policy: Optional[RedactionPolicy] = None) -> str:
    """全局脱敏入口。"""
    return (policy or default_policy()).redact(text)


def _redact_value(value: str, policy: RedactionPolicy) -> str:
    return policy.redact(value) if isinstance(value, str) else value


def _redact_args(args: object, policy: RedactionPolicy) -> object:
    if isinstance(args, dict):
        return {k: _redact_value(v, policy) for k, v in args.items()}
    if isinstance(args, tuple):
        return tuple(_redact_value(a, policy) for a in args)
    # 单值参数必须重新包成元组：否则 str % args 会因为「参数个数不匹配」直接抛 TypeError
    return (_redact_value(args, policy),)


class RedactingFilter(logging.Filter):
    """logging.Filter：在格式化之前把 record 的 msg 与 args 脱敏。

    注意：logger 上的 filter 不会作用到经 propagate 转到父 logger 的 handler，
    所以必须同时挂到 handler 上，见 install() / install_everywhere()。
    """

    def __init__(self, policy: Optional[RedactionPolicy] = None, name: str = "") -> None:
        super().__init__(name)
        self.policy = policy or default_policy()

    def filter(self, record: logging.LogRecord) -> bool:
        policy = self.policy
        if not policy.enabled:
            return True
        if isinstance(record.msg, str):
            record.msg = policy.redact(record.msg)
        if record.args:
            record.args = _redact_args(record.args, policy)
        return True


def _attach_filter(target, policy: RedactionPolicy):
    for existing in target.filters:
        if isinstance(existing, RedactingFilter):
            existing.policy = policy
            return existing
    flt = RedactingFilter(policy)
    target.addFilter(flt)
    return flt


def _attach_handler(handler: logging.Handler, policy: RedactionPolicy) -> None:
    for existing in handler.filters:
        if isinstance(existing, RedactingFilter):
            existing.policy = policy
            return
    handler.addFilter(RedactingFilter(policy))


def install(logger: Optional[logging.Logger] = None, policy: Optional[RedactionPolicy] = None) -> RedactingFilter:
    """把脱敏过滤器装到指定 logger（默认 root）及其 handler，幂等。"""
    target = logger or logging.getLogger()
    pol = policy or default_policy()
    flt = _attach_filter(target, pol)
    for handler in target.handlers:
        _attach_handler(handler, pol)
    return flt


def install_everywhere(policy: Optional[RedactionPolicy] = None) -> RedactionPolicy:
    """覆盖 root + 所有已存在的 logger 和 handler。进程启动时调用一次。"""
    pol = policy or default_policy()
    set_default_policy(pol)
    root = logging.getLogger()
    _attach_filter(root, pol)
    for handler in root.handlers:
        _attach_handler(handler, pol)
    for name in list(logging.Logger.manager.loggerDict):
        obj = logging.Logger.manager.loggerDict.get(name)
        if isinstance(obj, logging.Logger):
            _attach_filter(obj, pol)
            for handler in obj.handlers:
                _attach_handler(handler, pol)
    return pol
