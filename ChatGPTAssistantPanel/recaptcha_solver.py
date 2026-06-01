"""CaptchaAI reCAPTCHA v2 求解器 + Playwright 集成。

用法:
    # 仅求解，打印 token
    python recaptcha_solver.py solve \\
        --key YOUR_API_KEY \\
        --sitekey 6Lc_xxx \\
        --pageurl https://example.com/login

    # 打开浏览器，自动检测 sitekey、求解、注入、提交
    python recaptcha_solver.py auto \\
        --key YOUR_API_KEY \\
        --pageurl https://example.com/login \\
        [--invisible] [--headless] [--submit-selector "button[type=submit]"]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

import requests

DEFAULT_API_KEY = os.environ.get("CAPTCHAAI_KEY", "").strip()

IN_URL = "https://ocr.captchaai.com/in.php"
RES_URL = "https://ocr.captchaai.com/res.php"


def solve_recaptcha_v2(
    api_key: str,
    site_key: str,
    page_url: str,
    invisible: bool = False,
    enterprise: bool = False,
    timeout: int = 180,
    initial_wait: int = 20,
    poll_interval: int = 5,
    data_s: str = "",
    proxy: str = "",
    proxytype: str = "HTTP",
) -> str:
    submit_params = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": site_key,
        "pageurl": page_url,
        "json": "1",
    }
    if invisible:
        submit_params["invisible"] = "1"
    if enterprise:
        submit_params["enterprise"] = "1"
    if data_s:
        submit_params["data-s"] = data_s
    if proxy:
        submit_params["proxy"] = proxy
        submit_params["proxytype"] = proxytype or "HTTP"

    r = requests.post(IN_URL, data=submit_params, timeout=30).json()
    if r.get("status") != 1:
        raise RuntimeError(f"submit failed: {r.get('request')}")
    task_id = r["request"]
    print(f"[+] task_id = {task_id}")

    time.sleep(initial_wait)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            RES_URL,
            params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
            timeout=30,
        ).json()
        if r.get("status") == 1:
            return r["request"]
        if r.get("request") == "CAPCHA_NOT_READY":
            time.sleep(poll_interval)
            continue
        raise RuntimeError(f"solve failed: {r.get('request')}")

    raise TimeoutError(f"task {task_id} timed out after {timeout}s")


def solve_recaptcha_v3(
    api_key: str,
    site_key: str,
    page_url: str,
    action: str = "verify",
    min_score: float = 0.3,
    enterprise: bool = False,
    timeout: int = 180,
    initial_wait: int = 20,
    poll_interval: int = 5,
) -> str:
    submit_params = {
        "key": api_key,
        "method": "userrecaptcha",
        "version": "v3",
        "googlekey": site_key,
        "pageurl": page_url,
        "action": action or "verify",
        "min_score": str(min_score),
        "json": "1",
    }
    if enterprise:
        submit_params["enterprise"] = "1"

    r = requests.post(IN_URL, data=submit_params, timeout=30).json()
    if r.get("status") != 1:
        raise RuntimeError(f"submit failed: {r.get('request')}")
    task_id = r["request"]
    print(f"[+] task_id = {task_id}")

    time.sleep(initial_wait)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            RES_URL,
            params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
            timeout=30,
        ).json()
        if r.get("status") == 1:
            return r["request"]
        if r.get("request") == "CAPCHA_NOT_READY":
            time.sleep(poll_interval)
            continue
        raise RuntimeError(f"solve failed: {r.get('request')}")

    raise TimeoutError(f"task {task_id} timed out after {timeout}s")


def solve_hcaptcha(
    api_key: str,
    site_key: str,
    page_url: str,
    timeout: int = 180,
    initial_wait: int = 20,
    poll_interval: int = 5,
    invisible: bool = False,
    proxy: str = "",
    proxytype: str = "HTTP",
) -> str:
    submit_params = {
        "key": api_key,
        "method": "hcaptcha",
        "sitekey": site_key,
        "pageurl": page_url,
        "json": "1",
    }
    if invisible:
        submit_params["invisible"] = "1"
    if proxy:
        submit_params["proxy"] = proxy
        submit_params["proxytype"] = proxytype or "HTTP"

    r = requests.post(IN_URL, data=submit_params, timeout=30).json()
    if r.get("status") != 1:
        raise RuntimeError(f"submit failed: {r.get('request')}")
    task_id = r["request"]
    print(f"[+] task_id = {task_id}")

    time.sleep(initial_wait)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(
            RES_URL,
            params={"key": api_key, "action": "get", "id": task_id, "json": "1"},
            timeout=30,
        ).json()
        if r.get("status") == 1:
            return r["request"]
        if r.get("request") == "CAPCHA_NOT_READY":
            time.sleep(poll_interval)
            continue
        raise RuntimeError(f"solve failed: {r.get('request')}")

    raise TimeoutError(f"task {task_id} timed out after {timeout}s")


DETECT_SITEKEY_JS = r"""
() => {
    const el = document.querySelector('[data-sitekey]');
    if (el) {
        return {
            sitekey: el.getAttribute('data-sitekey'),
            invisible: el.getAttribute('data-size') === 'invisible',
            callback: el.getAttribute('data-callback') || null,
        };
    }
    const iframe = document.querySelector('iframe[src*="recaptcha"]');
    if (iframe) {
        const src = iframe.getAttribute('src') || '';
        const m = src.match(/[?&]k=([^&]+)/);
        if (m) {
            return {
                sitekey: decodeURIComponent(m[1]),
                invisible: src.includes('size=invisible'),
                callback: null,
            };
        }
    }
    return null;
}
"""

INJECT_TOKEN_JS = r"""
({ token, callback }) => {
    let ta = document.getElementById('g-recaptcha-response');
    if (!ta) {
        ta = document.createElement('textarea');
        ta.id = 'g-recaptcha-response';
        ta.name = 'g-recaptcha-response';
        ta.style.display = 'none';
        document.body.appendChild(ta);
    }
    ta.style.display = 'block';
    ta.value = token;
    ta.innerHTML = token;

    if (callback && typeof window[callback] === 'function') {
        try { window[callback](token); } catch (e) {}
    }
    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
        const clients = window.___grecaptcha_cfg.clients;
        for (const cid of Object.keys(clients)) {
            const stack = [clients[cid]];
            while (stack.length) {
                const obj = stack.pop();
                if (!obj || typeof obj !== 'object') continue;
                for (const k of Object.keys(obj)) {
                    const v = obj[k];
                    if (typeof v === 'function' && /callback/i.test(k)) {
                        try { v(token); } catch (e) {}
                    } else if (v && typeof v === 'object') {
                        stack.push(v);
                    }
                }
            }
        }
    }
}
"""


async def auto_solve_with_playwright(
    api_key: str,
    page_url: str,
    invisible: bool | None = None,
    enterprise: bool = False,
    headless: bool = False,
    submit_selector: str | None = None,
    proxy: str | None = None,
) -> str:
    from playwright.async_api import async_playwright

    launch_kwargs: dict = {"headless": headless}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(page_url, wait_until="domcontentloaded")
            await page.wait_for_selector(
                '[data-sitekey], iframe[src*="recaptcha"]', timeout=15000
            )

            info = await page.evaluate(DETECT_SITEKEY_JS)
            if not info or not info.get("sitekey"):
                raise RuntimeError("sitekey not found on page")

            site_key = info["sitekey"]
            is_invisible = invisible if invisible is not None else info["invisible"]
            callback = info.get("callback")
            print(f"[+] sitekey={site_key} invisible={is_invisible} callback={callback}")

            current_url = page.url
            token = await asyncio.to_thread(
                solve_recaptcha_v2,
                api_key,
                site_key,
                current_url,
                is_invisible,
                enterprise,
            )
            print(f"[+] token = {token[:48]}...")

            await page.evaluate(INJECT_TOKEN_JS, {"token": token, "callback": callback})

            if submit_selector:
                await page.click(submit_selector)
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                print(f"[+] submitted, now at {page.url}")

            return token
        finally:
            if not headless:
                await asyncio.sleep(2)
            await context.close()
            await browser.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("solve", help="仅调用 CaptchaAI 求解，打印 token")
    s.add_argument("--key", default=os.environ.get("CAPTCHAAI_KEY", DEFAULT_API_KEY), help="CaptchaAI API key (默认已内置；可用 --key 或 env CAPTCHAAI_KEY 覆盖)")
    s.add_argument("--sitekey", required=True)
    s.add_argument("--pageurl", required=True)
    s.add_argument("--invisible", action="store_true")
    s.add_argument("--enterprise", action="store_true")
    s.add_argument("--timeout", type=int, default=180)
    s.add_argument("--version", choices=["v2", "v3"], default="v2")
    s.add_argument("--action", default="verify", help="v3 action parameter, default verify")
    s.add_argument("--min-score", type=float, default=0.3, help="v3 min_score, default 0.3")

    a = sub.add_parser("auto", help="Playwright 全自动：检测 sitekey -> 求解 -> 注入 -> 可选提交")
    a.add_argument("--key", default=os.environ.get("CAPTCHAAI_KEY", DEFAULT_API_KEY))
    a.add_argument("--pageurl", required=True)
    a.add_argument("--invisible", action="store_true", default=None)
    a.add_argument("--enterprise", action="store_true")
    a.add_argument("--headless", action="store_true")
    a.add_argument("--submit-selector", default=None, help="提交按钮的 CSS 选择器")
    a.add_argument("--proxy", default=None, help="例如 http://user:pass@host:port")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if not args.key:
        raise SystemExit("缺少 API key：传 --key 或设 CAPTCHAAI_KEY 环境变量")

    if args.cmd == "solve":
        if args.version == "v3":
            token = solve_recaptcha_v3(
                args.key,
                args.sitekey,
                args.pageurl,
                action=args.action,
                min_score=args.min_score,
                enterprise=args.enterprise,
                timeout=args.timeout,
            )
        else:
            token = solve_recaptcha_v2(
                args.key, args.sitekey, args.pageurl,
                invisible=args.invisible, enterprise=args.enterprise,
                timeout=args.timeout,
            )
        print(token)
    elif args.cmd == "auto":
        token = asyncio.run(auto_solve_with_playwright(
            args.key, args.pageurl,
            invisible=args.invisible, enterprise=args.enterprise,
            headless=args.headless, submit_selector=args.submit_selector,
            proxy=args.proxy,
        ))
        print(token)


if __name__ == "__main__":
    main()
