from __future__ import annotations

from typing import Callable


FetchLatest = Callable[[str, str], str]
FetchExternal = Callable[[str], str]


def wait_code(
    *,
    email: str,
    mail_url: str,
    timeout: int,
    interval: float,
    exclude: set[str] | None,
    fetch_latest: FetchLatest,
    fetch_external: FetchExternal,
    sleep,
    now,
    log=print,
) -> str:
    deadline = now() + max(1, int(timeout or 1))
    exclude = exclude or set()
    while now() < deadline:
        code = ""
        if mail_url:
            code = fetch_latest(mail_url, email)
        if not code and email:
            code = fetch_external(email)
        if code and code not in exclude:
            return code
        if code in exclude:
            log("[mail] 邮箱里还是上一条已尝试验证码，继续等待新验证码。")
        left = max(0, int(deadline - now()))
        if left <= 0:
            break
        log(f"[mail] 等待验证码邮件中... {left}s")
        sleep(max(1.0, float(interval or 5.0)))
    return ""
