from __future__ import annotations

import builtins
import os
import re
import sys
from typing import Any


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GRAY = "\033[90m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
WHITE = "\033[97m"

_ORIGINAL_PRINT = builtins.print
_INSTALLED = False


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    raw = os.environ.get("CODEX_COLOR_LOGS", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def paint(text: str, color: str, *, bold: bool = False) -> str:
    if not color_enabled() or not text:
        return text
    prefix = color + (BOLD if bold else "")
    return f"{prefix}{text}{RESET}"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def classify(text: str) -> str:
    plain = strip_ansi(text).lower()
    if "批量结束" in plain and re.search(r"失败\s*=\s*0", plain):
        return "ok"
    if any(key in plain for key in ["[fail", "[error", "失败", "错误", "异常", "超时", "不能为空", "未找到", "无效", "bad_key", "no_balance"]):
        return "fail"
    if any(key in plain for key in ["[warn", "警告", "跳过", "回退", "暂未", "未识别", "保留", "重试", "不可用"]):
        return "warn"
    if any(key in plain for key in ["等待", "拉取", "读取", "轮询", "检测", "观察", "处理中", "启动", "开始", "正在", "请求", "选择"]):
        return "run"
    if any(key in plain for key in ["[ok", "成功", "完成", "已写入", "已保存", "已落盘", "已同步", "已获取", "已填", "已点击", "已提交", "已选择", "已进入"]):
        return "ok"
    if any(key in plain for key in ["[sms]", "验证码", "手机号", "herosms", "whatsapp", "otp"]):
        return "sms"
    if any(key in plain for key in ["[gopay]", "支付", "账单", "付款", "订阅", "midtrans"]):
        return "pay"
    if any(key in plain for key in ["[mail]", "邮箱", "接码"]):
        return "mail"
    if any(key in plain for key in ["[login]", "[oauth]", "授权", "登录"]):
        return "auth"
    if any(key in plain for key in ["[worker", "并发", "线程"]):
        return "worker"
    return "info"


LEVEL_STYLE = {
    "ok": ("[OK]", GREEN),
    "fail": ("[FAIL]", RED),
    "warn": ("[WARN]", YELLOW),
    "run": ("[RUN]", CYAN),
    "sms": ("[SMS]", MAGENTA),
    "pay": ("[PAY]", BLUE),
    "mail": ("[MAIL]", CYAN),
    "auth": ("[AUTH]", MAGENTA),
    "worker": ("[WORK]", BLUE),
    "info": ("[INFO]", WHITE),
}


PREFIX_STYLE = {
    "sms": MAGENTA,
    "gopay": BLUE,
    "mail": CYAN,
    "login": MAGENTA,
    "oauth": MAGENTA,
    "ok": GREEN,
    "warn": YELLOW,
    "error": RED,
    "fail": RED,
    "browser": BLUE,
    "worker": BLUE,
    "summary": GREEN,
    "scheduler": CYAN,
    "parallel": CYAN,
    "input": CYAN,
    "state": CYAN,
    "list": CYAN,
    "hint": GRAY,
    "debug": GRAY,
}


def style_bracket_prefix(text: str, fallback_level: str) -> str:
    if not color_enabled() or "\033[" in text:
        return text
    match = re.match(r"^(\[[^\]]+\])(\s*)", text)
    if not match:
        return text
    tag = match.group(1)
    key = tag.strip("[]").split(":", 1)[0].split("-", 1)[0].lower()
    color = PREFIX_STYLE.get(key) or LEVEL_STYLE.get(fallback_level, LEVEL_STYLE["info"])[1]
    return paint(tag, color, bold=True) + match.group(2) + text[match.end():]


def style_message(message: str, *, add_marker: bool = False) -> str:
    if not color_enabled() or not message:
        return message
    if "\033[" in message:
        return message
    level = classify(message)
    marker, color = LEVEL_STYLE.get(level, LEVEL_STYLE["info"])
    text = style_bracket_prefix(message, level)
    if add_marker:
        return f"{paint(marker, color, bold=True)} {text}"
    return text


def style_timed_log(message: str) -> str:
    level = classify(message)
    marker, color = LEVEL_STYLE.get(level, LEVEL_STYLE["info"])
    # Caller prepends the timestamp separately; this helper returns the marker + body.
    return f"{paint(marker, color, bold=True)} {style_bracket_prefix(message, level)}"


def themed_print(*args: Any, **kwargs: Any) -> None:
    if kwargs.pop("_unstyled", False):
        return _ORIGINAL_PRINT(*args, **kwargs)
    file = kwargs.get("file")
    if args and isinstance(args[0], str):
        sep = kwargs.get("sep", " ")
        try:
            text = sep.join(str(arg) for arg in args)
        except Exception:
            text = str(args[0])
        end = kwargs.get("end", "\n")
        if file is sys.stderr:
            level = "fail"
            styled = style_bracket_prefix(text, level)
            text = f"{paint('[FAIL]', RED, bold=True)} {styled}" if not text.startswith("[") else styled
        else:
            text = style_message(text, add_marker=False)
        return _ORIGINAL_PRINT(text, end=end, file=file or sys.stdout, flush=kwargs.get("flush", False))
    return _ORIGINAL_PRINT(*args, **kwargs)


def install_print_theme() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    builtins.print = themed_print
    _INSTALLED = True
