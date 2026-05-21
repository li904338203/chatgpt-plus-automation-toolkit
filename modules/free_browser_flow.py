from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from playwright.async_api import Page

from .utils import log, resolve_path, safe_filename


SMS_CODE_CALLBACK = Callable[[], Awaitable[str]]


class FreeBrowserFlow:
    """Browser flow helpers for free/email/phone registration and OAuth steps."""

    def __init__(self, page: Page, prefix: str) -> None:
        self.page = page
        self.prefix = prefix

    async def sleep(self, ms: int) -> None:
        await self.page.wait_for_timeout(ms)

    def say(self, message: str) -> None:
        log(f"{self.prefix} {message}")

    async def screenshot(self, filename: str) -> None:
        out = resolve_path("output/free_register/debug")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{int(time.time())}_{safe_filename(self.prefix)[-80:]}_{filename}"
        await self.page.screenshot(path=str(path), full_page=True)
        self.say(f"[Browser] screenshot: {path}")

    async def goto_chatgpt_entry(self, timeout_ms: int = 60_000) -> None:
        urls = [
            "https://chatgpt.com",
            "https://chatgpt.com/",
            "https://chat.openai.com/",
        ]
        last_error: Exception | None = None
        for idx, url in enumerate(urls, start=1):
            try:
                self.say(f"[Browser] open: {url}")
                await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                self.say(f"[Browser] open failed ({idx}/{len(urls)}): {str(exc)[:180]}")
                if idx < len(urls):
                    await self.sleep(1500)
        if last_error:
            raise last_error
        raise RuntimeError("cannot open ChatGPT entry")

    async def wait_for_cloudflare(self, timeout_ms: int = 60_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                title = (await self.page.title()).lower()
                text = (await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"))[:2000]
                if "just a moment" not in title and "checking your browser" not in text and "cloudflare" not in title:
                    return
            except Exception:
                pass
            await self.sleep(2000)

    async def wait_for_text_on_page(self, text: str | list[str], timeout_ms: int = 30_000) -> None:
        candidates = text if isinstance(text, list) else [text]
        lower_candidates = [c.lower() for c in candidates]
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                body = await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()")
                if any(c in body for c in lower_candidates):
                    return
            except Exception:
                pass
            await self.sleep(1000)
        raise RuntimeError(f"wait text timeout: {'/'.join(candidates)}")

    async def _find_email_input(self):
        selector = (
            'input[type="email"], input[name*="email" i], input[id*="email" i], '
            'input[name="username"], input[autocomplete="username"], input[inputmode="email"], '
            'input[placeholder*="mail" i], input[placeholder*="邮箱" i], '
            '[role="dialog"] input, dialog input, [aria-modal="true"] input'
        )
        try:
            main_loc = self.page.locator(selector)
            if await main_loc.count() > 0:
                return main_loc.first
        except Exception:
            pass
        for frame in self.page.frames:
            if frame == self.page.main_frame:
                continue
            try:
                loc = frame.locator(selector)
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None

    async def _is_probable_auth_input(self, loc) -> tuple[bool, str]:
        try:
            meta = await loc.evaluate(
                """(el) => {
                    const v = (x) => String(x || '').toLowerCase();
                    const type = v(el.getAttribute('type'));
                    const name = v(el.getAttribute('name'));
                    const id = v(el.getAttribute('id'));
                    const placeholder = v(el.getAttribute('placeholder'));
                    const autocomplete = v(el.getAttribute('autocomplete'));
                    const inputmode = v(el.getAttribute('inputmode'));
                    const aria = v(el.getAttribute('aria-label'));
                    const inDialog = !!el.closest('[role="dialog"], dialog, [aria-modal="true"], [data-radix-dialog-content]');
                    return { type, name, id, placeholder, autocomplete, inputmode, aria, inDialog };
                }"""
            )
        except Exception:
            return False, "meta-unavailable"
        hint_text = " ".join(
            [
                meta.get("name", ""),
                meta.get("id", ""),
                meta.get("placeholder", ""),
                meta.get("autocomplete", ""),
                meta.get("inputmode", ""),
                meta.get("aria", ""),
            ]
        )
        probable = bool(
            meta.get("type") == "email"
            or meta.get("inputmode") == "email"
            or any(k in hint_text for k in ("mail", "email", "user", "账号", "邮箱", "login", "signin", "sign in"))
            or meta.get("inDialog")
        )
        return probable, str(meta)

    async def wait_for_email_input(self, timeout_ms: int = 30_000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            loc = await self._find_email_input()
            if loc is not None:
                ok, meta = await self._is_probable_auth_input(loc)
                if ok:
                    self.say(f"[Browser] auth input matched: {meta}")
                    return loc
            await self.sleep(800)
        return None

    async def _find_clickable(self, candidates: list[str]) -> bool:
        # Prefer Playwright native click first (trusted user-like event).
        for text in candidates:
            if not str(text or "").strip():
                continue
            css_text = str(text).replace("\\", "\\\\").replace('"', '\\"')
            selectors = [
                f'button:has-text("{css_text}")',
                f'a:has-text("{css_text}")',
                f'[role="button"]:has-text("{css_text}")',
                f'input[type="button"][value*="{css_text}"]',
                f'input[type="submit"][value*="{css_text}"]',
            ]
            for selector in selectors:
                try:
                    loc = self.page.locator(selector).first
                    if await loc.count() <= 0:
                        continue
                    await loc.scroll_into_view_if_needed(timeout=1500)
                    await loc.click(timeout=2500)
                    return True
                except Exception:
                    continue
        return bool(
            await self.page.evaluate(
                """(texts) => {
                    const visible = (el) => {
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const wanted = texts.map(t => String(t || '').toLowerCase().trim());
                    for (const el of document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')) {
                        if (!visible(el)) continue;
                        const label = String(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').toLowerCase();
                        if (!wanted.some(w => label.includes(w))) continue;
                        el.scrollIntoView({ block: 'center', inline: 'center' });
                        ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                            el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                        });
                        return true;
                    }
                    return false;
                }""",
                candidates,
            )
        )

    async def wait_for_button_by_text(self, text: str | list[str], timeout_ms: int = 30_000) -> None:
        candidates = text if isinstance(text, list) else [text]
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self._find_clickable(candidates):
                return
            await self.sleep(1000)
        raise RuntimeError(f"wait button timeout: {'/'.join(candidates)}")

    async def click_button_by_text(self, text: str | list[str], timeout_ms: int = 10_000) -> None:
        candidates = text if isinstance(text, list) else [text]
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self._find_clickable(candidates):
                await self.sleep(700)
                return
            await self.sleep(800)
        raise RuntimeError(f"button not found: {'/'.join(candidates)}")

    async def click_submit_button(self) -> None:
        clicked = await self.page.evaluate(
            """() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const preferred = ['continue', 'next', 'verify', 'submit', 'sign up', '注册', '继续'];
                const blocked = ['google', 'apple', 'phone', '手机号', '电话', '手机'];
                const nodes = Array.from(document.querySelectorAll('button[type="submit"], button, input[type="submit"]'));
                for (const el of nodes) {
                    if (!visible(el) || el.disabled) continue;
                    const text = String(el.innerText || el.textContent || el.value || '').toLowerCase();
                    if (blocked.some(b => text.includes(b))) continue;
                    if (!preferred.some(p => text.includes(p))) continue;
                    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                    });
                    return true;
                }
                for (const el of nodes) {
                    if (!visible(el) || el.disabled) continue;
                    const text = String(el.innerText || el.textContent || el.value || '').toLowerCase();
                    if (blocked.some(b => text.includes(b))) continue;
                    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(type => {
                        el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                    });
                    return true;
                }
                return false;
            }"""
        )
        if not clicked:
            await self.page.keyboard.press("Enter")
        await self.sleep(800)

    async def wait_for_url_change(self, current_url: str, timeout_ms: int = 15_000) -> str:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            new_url = self.page.url
            if new_url != current_url:
                return new_url
            await self.sleep(500)
        return self.page.url

    async def wait_until_url_leaves(self, keyword: str, timeout_ms: int = 15_000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if keyword.lower() not in self.page.url.lower():
                return
            await self.sleep(500)

    async def detect_email_already_registered(self) -> None:
        text = await self.page.evaluate("() => (document.body?.innerText || '')")
        low = text.lower()
        hints = [
            "already have an account",
            "already registered",
            "email already",
            "邮箱已被注册",
            "此邮箱已",
        ]
        if any(h in low for h in hints):
            raise RuntimeError("email already registered")

    async def navigate_to_signup(self) -> None:
        await self.goto_chatgpt_entry(timeout_ms=60_000)
        await self.wait_for_cloudflare()
        await self.wait_for_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 30_000)
        await self.click_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 12_000)
        await self.sleep(1200)
        await self.click_button_by_text(["Continue with phone", "phone number", "手机", "电话"], 12_000)
        await self.page.locator('input[name="phoneNumberInput"], input[type="tel"]').first.wait_for(timeout=15_000)

    async def navigate_to_signup_email(self, email: str) -> None:
        await self.goto_chatgpt_entry(timeout_ms=60_000)
        await self.wait_for_cloudflare()
        # Best-effort dismiss cookie banners that can intercept clicks.
        try:
            await self.click_button_by_text(["Reject non-essential", "拒绝非必需", "Accept all", "全部接受"], 3_000)
        except Exception:
            pass
        email_input = await self.wait_for_email_input(6_000)
        if email_input is not None:
            self.say("[Browser] direct email auth page detected, skipping Sign up button search")
        else:
            await self.wait_for_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 30_000)
            for _ in range(3):
                await self.click_button_by_text(["Sign up", "Sign up for free", "免费注册", "注册"], 12_000)
                await self.sleep(1200)
                try:
                    await self.click_button_by_text(
                        ["Continue with email", "Use email", "Email", "继续使用邮箱", "邮箱"],
                        timeout_ms=3_000,
                    )
                except Exception:
                    pass
                email_input = await self.wait_for_email_input(8_000)
                if email_input is not None:
                    break
        if email_input is None:
            raise RuntimeError("email input not found after signup click")
        await email_input.click(click_count=3)
        await email_input.fill("")
        await email_input.type(email, delay=30)
        await self.sleep(500)
        before = self.page.url
        for attempt in range(1, 4):
            submitted = False
            # Re-acquire the email input each attempt to avoid stale execution context
            # after in-dialog navigation transitions.
            refreshed = await self.wait_for_email_input(1_500)
            if refreshed is not None:
                email_input = refreshed
            try:
                clicked_form_submit = await asyncio.wait_for(
                    email_input.evaluate(
                        """(el) => {
                            const block = (txt) => ['google', 'apple', 'phone', '手机', '电话', '手机号'].some(k => txt.includes(k));
                            const form = el.closest('form');
                            if (form) {
                                const btn = form.querySelector('button[type="submit"], input[type="submit"], button');
                                if (btn) {
                                    const text = String(btn.innerText || btn.textContent || btn.value || '').toLowerCase();
                                    if (!block(text)) {
                                        btn.click();
                                        return true;
                                    }
                                }
                            }
                            const dialog = el.closest('[role="dialog"], dialog, [aria-modal="true"], [data-radix-dialog-content]');
                            if (dialog) {
                                const btns = Array.from(dialog.querySelectorAll('button[type="submit"], button, input[type="submit"]'));
                                for (const btn of btns) {
                                    const text = String(btn.innerText || btn.textContent || btn.value || '').toLowerCase();
                                    if (block(text)) continue;
                                    if (['continue', 'next', 'submit', '继续', '下一步', '提交', '注册'].some(k => text.includes(k))) {
                                        btn.click();
                                        return true;
                                    }
                                }
                            }
                            return false;
                        }"""
                    ),
                    timeout=2.5,
                )
                submitted = bool(clicked_form_submit)
            except Exception:
                pass
            if not submitted:
                try:
                    await self.page.keyboard.press("Enter")
                    submitted = True
                except Exception:
                    pass
            if await self._wait_for_code_stage(timeout_ms=6_000):
                self.say(f"[Browser] email submit advanced to code stage on attempt {attempt}")
                break
            self.say(f"[Browser] email submit did not reach code stage (attempt {attempt})")
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=1_500)
            except Exception:
                pass
            await self.sleep(800)
        await self.wait_for_url_change(before, timeout_ms=10_000)
        await self.wait_for_cloudflare(30_000)
        await self.detect_email_already_registered()

    async def click_resend_code(self) -> bool:
        try:
            await self.click_button_by_text(["Resend", "Send again", "重新发送", "再次发送"], 6000)
            return True
        except Exception:
            return False

    async def enter_sms_code(self, code: str) -> None:
        inputs = self.page.locator(
            'input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"], input[type="tel"], input[type="text"]'
        )
        await inputs.first.wait_for(timeout=20_000)
        count = await inputs.count()
        if count >= 6:
            for i, ch in enumerate(code[:6]):
                box = inputs.nth(i)
                try:
                    await box.fill(ch)
                except Exception:
                    await box.click()
                    await box.type(ch, delay=40)
        else:
            box = inputs.first
            await box.click(click_count=3)
            await box.fill("")
            await box.type(code, delay=60)
        await self.sleep(800)

    async def enter_email_verification_code(self, code: str) -> None:
        await self.enter_sms_code(code)
        before = self.page.url
        await self.click_submit_button()
        await self.wait_for_url_change(before, timeout_ms=15_000)
        await self.wait_for_cloudflare(30_000)

    async def _wait_for_code_stage(self, timeout_ms: int = 20_000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            probe_timeout = min(1.0, max(0.25, remaining))
            try:
                code_inputs = self.page.locator(
                    'input[name*="code" i], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
                )
                count = await asyncio.wait_for(code_inputs.count(), timeout=probe_timeout)
                if count > 0:
                    return True
            except Exception:
                pass
            try:
                body = await asyncio.wait_for(
                    self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"),
                    timeout=probe_timeout,
                )
                if any(
                    k in body
                    for k in (
                        "verification code",
                        "enter code",
                        "check your email",
                        "验证码",
                        "输入验证码",
                    )
                ):
                    return True
            except Exception:
                pass
            await self.sleep(250)
        return False

    async def detect_post_email_stage(self, timeout_ms: int = 12_000) -> str:
        """Detect whether the flow is currently at password page or email-code page."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                if await self.page.locator("input[type='password']").count() > 0:
                    return "password"
            except Exception:
                pass
            if await self._wait_for_code_stage(timeout_ms=1200):
                return "email_code"
            await self.sleep(250)
        return "unknown"

    async def fill_password_input(self, selector: str, password: str) -> None:
        box = self.page.locator(selector).first
        await box.wait_for(timeout=15_000)
        await box.click(click_count=3)
        await box.fill("")
        await box.type(password, delay=35)

    async def fill_password_if_shown(self, password: str) -> bool:
        if await self.page.locator("input[type='password']").count() <= 0:
            # Some auth variants show a "continue with password" gate first.
            try:
                await self.click_button_by_text(
                    ["Continue with password", "Use password", "使用密码继续", "密码继续"],
                    timeout_ms=4_000,
                )
                await self.sleep(800)
            except Exception:
                pass
        if await self.page.locator("input[type='password']").count() <= 0:
            return False
        await self.fill_password_input("input[type='password']", password)
        await self.click_submit_button()
        await self.wait_for_cloudflare(30_000)
        return True

    async def _fill_first_visible_input(self, selectors: list[str], value: str) -> bool:
        if not value:
            return False
        try:
            return bool(
                await self.page.evaluate(
                    """(args) => {
                        const selectors = args.selectors || [];
                        const value = String(args.value ?? "");
                        const visible = (el) => {
                            const r = el.getBoundingClientRect();
                            const s = getComputedStyle(el);
                            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                        };
                        for (const selector of selectors) {
                            const nodes = document.querySelectorAll(selector);
                            for (const el of nodes) {
                                if (!el) continue;
                                if (el.disabled || el.readOnly) continue;
                                if (!visible(el)) continue;
                                try { el.focus(); } catch {}
                                try {
                                    el.value = '';
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.value = value;
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                } catch {}
                            }
                        }
                        return false;
                    }""",
                    {"selectors": selectors, "value": value},
                )
            )
        except Exception:
            return False

    async def _fill_locator_with_verify(self, locator, value: str) -> bool:
        if not value:
            return False
        val = str(value)
        try:
            await locator.wait_for(state="visible", timeout=700)
        except Exception:
            return False
        try:
            await locator.click(timeout=700)
        except Exception:
            pass
        try:
            await locator.fill("", timeout=700)
        except Exception:
            pass
        try:
            await locator.type(val, delay=20, timeout=2000)
        except Exception:
            try:
                await locator.fill(val, timeout=1000)
            except Exception:
                return False
        await self.sleep(60)
        try:
            cur = (await locator.input_value()).strip()
            if cur:
                if cur == val or val in cur or cur in val:
                    return True
        except Exception:
            pass
        try:
            cur = await locator.evaluate(
                """(el, v) => {
                    const proto = Object.getPrototypeOf(el);
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && typeof desc.set === 'function') {
                        desc.set.call(el, '');
                        desc.set.call(el, String(v));
                    } else {
                        el.value = String(v);
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return String(el.value || '');
                }""",
                val,
            )
            return bool((cur or "").strip())
        except Exception:
            return False

    async def _fill_profile_field(
        self,
        *,
        value: str,
        label_keywords: list[str],
        selectors: list[str],
        exclude_code_input: bool = True,
    ) -> bool:
        if not value:
            return False
        # Prefer inputs near explicit labels.
        for keyword in label_keywords:
            kw = keyword.strip()
            if not kw:
                continue
            xpath = (
                "xpath=(//*[self::label or self::span or self::div or self::p]"
                f"[contains(normalize-space(.), \"{kw}\")])[1]/following::input[1]"
            )
            loc = self.page.locator(xpath).first
            if await self._fill_locator_with_verify(loc, value):
                return True
        # Fallback to selector list.
        for selector in selectors:
            locs = self.page.locator(selector)
            try:
                count = await locs.count()
            except Exception:
                continue
            for idx in range(min(count, 6)):
                loc = locs.nth(idx)
                if exclude_code_input:
                    try:
                        is_code_like = bool(
                            await loc.evaluate(
                                """(el) => {
                                    const t = String(
                                        (el.getAttribute('name') || '') + ' ' +
                                        (el.getAttribute('id') || '') + ' ' +
                                        (el.getAttribute('autocomplete') || '') + ' ' +
                                        (el.getAttribute('aria-label') || '')
                                    ).toLowerCase();
                                    return t.includes('code') || t.includes('otp') || t.includes('one-time');
                                }"""
                            )
                        )
                    except Exception:
                        is_code_like = False
                    if is_code_like:
                        continue
                if await self._fill_locator_with_verify(loc, value):
                    return True
        return False

    async def _has_profile_validation_errors(self) -> bool:
        try:
            body = (await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"))[:4000]
        except Exception:
            return False
        return any(
            msg in body
            for msg in (
                "请输入姓名",
                "请输入有效年龄",
                "please enter your name",
                "enter a valid age",
                "name is required",
                "age is required",
            )
        )

    async def _is_combined_verification_profile_page(self) -> bool:
        try:
            return bool(
                await self.page.evaluate(
                    """() => {
                        const hasCode = !!document.querySelector(
                            'input[name*="code" i], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
                        );
                        const hasNameOrAge = !!document.querySelector(
                            'input[name*="name" i], input[placeholder*="name" i], input[placeholder*="姓名"], '
                            + 'input[name*="age" i], input[placeholder*="age" i], input[placeholder*="年龄"]'
                        );
                        return hasCode && hasNameOrAge;
                    }"""
                )
            )
        except Exception:
            return False

    async def fill_about_you_and_submit(
        self,
        full_name: str,
        age: str,
        birth_date: str,
        tag: str = "[AboutYou]",
        verification_code: str = "",
    ) -> None:
        await self.sleep(200)
        age_text = str(age or "").strip()
        y, m, d = "", "", ""
        if isinstance(birth_date, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", birth_date):
            y, m, d = birth_date.split("-")

        for step in range(1, 5):
            try:
                body = (await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"))[:4000]
            except Exception:
                body = ""

            # If no profile-related hints remain, we consider this stage complete.
            if step > 1 and not any(
                k in body
                for k in (
                    "about you",
                    "tell us",
                    "your name",
                    "birthday",
                    "birth",
                    "age",
                    "关于你",
                    "你的姓名",
                    "出生",
                    "年龄",
                )
            ):
                break

            filled_any = False
            if await self._is_combined_verification_profile_page():
                self.say(f"{tag} detected combined verification+profile page")

            # Some variants place verification code and profile fields on the same page.
            code_text = re.sub(r"\D+", "", str(verification_code or ""))[:6]
            if code_text and await self._fill_first_visible_input(
                [
                    'input[name*="code" i]',
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]',
                    'input[aria-label*="验证码"]',
                    'input[placeholder*="验证码"]',
                ],
                code_text,
            ):
                filled_any = True
                self.say(f"{tag} filled verification code")

            # Fill name with label-aware matching and strict verification.
            if await self._fill_profile_field(
                value=full_name,
                label_keywords=["全名", "姓名", "Name", "Full name"],
                selectors=[
                    'input[name*="name" i]',
                    'input[placeholder*="name" i]',
                    'input[id*="name" i]',
                    'input[placeholder*="姓名"]',
                    'input[aria-label*="姓名"]',
                ],
            ):
                filled_any = True
                self.say(f"{tag} filled name")

            # Fill age
            if age_text:
                if await self._fill_profile_field(
                    value=age_text,
                    label_keywords=["年龄", "Age"],
                    selectors=[
                        'input[name*="age" i]',
                        'input[id*="age" i]',
                        'input[placeholder*="age" i]',
                        'input[aria-label*="age" i]',
                        'input[placeholder*="年龄"]',
                        'input[aria-label*="年龄"]',
                        'input[name*="年龄"]',
                    ],
                ):
                    filled_any = True
                    self.say(f"{tag} filled age")

            # Single birthday field
            if y and m and d:
                if await self._fill_first_visible_input(
                    [
                        'input[type="date"]',
                        'input[name*="birth" i]',
                        'input[id*="birth" i]',
                        'input[placeholder*="birthday" i]',
                        'input[placeholder*="出生" i]',
                    ],
                    f"{y}-{m}-{d}",
                ):
                    filled_any = True

            # Split birthday fields
            if y:
                filled_any = (await self._fill_first_visible_input(['input[name*="year" i]', 'input[placeholder*="year" i]'], y)) or filled_any
            if m:
                filled_any = (await self._fill_first_visible_input(['input[name*="month" i]', 'input[placeholder*="month" i]'], m)) or filled_any
            if d:
                filled_any = (await self._fill_first_visible_input(['input[name*="day" i]', 'input[placeholder*="day" i]'], d)) or filled_any

            if not filled_any:
                self.say(f"{tag} no fillable fields on step {step}, skip submit")
                break

            before = self.page.url
            await self.sleep(120)
            try:
                await self.click_button_by_text(["Continue", "继续", "下一步", "Submit", "提交"], timeout_ms=1_500)
            except Exception:
                await self.click_submit_button()
            await self.wait_for_url_change(before, timeout_ms=1_500)
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=1_200)
            except Exception:
                pass
            await self.wait_for_cloudflare(2_500)
            if await self._has_profile_validation_errors():
                self.say(f"{tag} detected profile validation error, retry refill")
                await self._fill_profile_field(
                    value=full_name,
                    label_keywords=["全名", "姓名", "Name", "Full name"],
                    selectors=[
                        'input[name*="name" i]',
                        'input[placeholder*="name" i]',
                        'input[id*="name" i]',
                        'input[placeholder*="姓名"]',
                        'input[aria-label*="姓名"]',
                    ],
                )
                if age_text:
                    await self._fill_profile_field(
                        value=age_text,
                        label_keywords=["年龄", "Age"],
                        selectors=[
                            'input[name*="age" i]',
                            'input[id*="age" i]',
                            'input[placeholder*="age" i]',
                            'input[aria-label*="age" i]',
                            'input[placeholder*="年龄"]',
                            'input[aria-label*="年龄"]',
                            'input[name*="年龄"]',
                        ],
                    )
                try:
                    await self.click_button_by_text(["Continue", "继续", "下一步", "Submit", "提交"], timeout_ms=1_500)
                except Exception:
                    await self.click_submit_button()

    async def select_country(self, dial_code: str, country_name: str = "", iso_code: str = "") -> None:
        # best-effort: country picker is dynamic; do not hard-fail if not found
        targets = [dial_code.strip(), country_name.strip(), iso_code.strip()]
        targets = [t for t in targets if t]
        if not targets:
            return
        try:
            await self.click_button_by_text(["country", "国家", "地区", "region"], 5000)
        except Exception:
            pass
        for t in targets:
            try:
                await self.click_button_by_text([t], 3000)
                return
            except Exception:
                continue

    def get_local_phone_number(self, phone_number: str, country: Any) -> str:
        number = re.sub(r"\D+", "", str(phone_number or ""))
        dial = re.sub(r"\D+", "", str(getattr(country, "dial_code", "") or ""))
        if dial and number.startswith(dial):
            return number[len(dial):] or number
        return number

    async def enter_phone(self, local_number: str) -> None:
        inp = self.page.locator('input[name="phoneNumberInput"], input[type="tel"]').first
        await inp.wait_for(timeout=20_000)
        await inp.click(click_count=3)
        await inp.fill("")
        await inp.type(local_number, delay=35)
        await self.sleep(500)
        await self.click_submit_button()

    async def complete_profile(self, profile: Any, sms_code_callback: SMS_CODE_CALLBACK) -> bool:
        code = await sms_code_callback()
        if not code:
            raise RuntimeError("empty sms code")
        await self.enter_sms_code(code)
        await self.click_submit_button()
        await self.wait_for_cloudflare(30_000)

        await self.fill_password_if_shown(getattr(profile, "password", ""))
        await self.sleep(1200)
        await self.fill_about_you_and_submit(
            getattr(profile, "full_name", ""),
            getattr(profile, "age", ""),
            getattr(profile, "birth_date", ""),
            "[Phase1]",
            verification_code=code,
        )
        return True

    async def navigate_to_oauth(self, auth_url: str) -> None:
        await self.page.goto(auth_url, wait_until="domcontentloaded", timeout=90_000)
        await self.wait_for_cloudflare(45_000)

    def _is_redirect_callback(self, url: str, redirect_base: Any) -> bool:
        current = urlparse(url)
        query = current.query or ""
        return (
            current.hostname == redirect_base.hostname
            and str(current.port or "") == str(redirect_base.port or "")
            and current.path == redirect_base.path
            and ("code=" in query or "error=" in query)
        )

    async def _fill_login_form_if_present(self, email: str, password: str) -> bool:
        email_loc = self.page.locator('input[type="email"], input[name="email"], input[name="username"]')
        pass_loc = self.page.locator('input[type="password"]')

        if await email_loc.count() > 0:
            box = email_loc.first
            await box.click(click_count=3)
            await box.fill("")
            await box.type(email, delay=35)
            await self.click_submit_button()
            await self.sleep(1000)
            return True

        if await pass_loc.count() > 0 and password:
            box = pass_loc.first
            await box.click(click_count=3)
            await box.fill("")
            await box.type(password, delay=35)
            await self.click_submit_button()
            await self.sleep(1000)
            return True

        return False

    async def oauth_login_and_authorize(self, options: dict[str, Any]) -> str:
        redirect_uri = str(options.get("redirectUri") or "").strip()
        if not redirect_uri:
            raise RuntimeError("oauth_login_and_authorize: redirectUri is required")

        redirect_base = urlparse(redirect_uri)
        email = str(options.get("email") or "")
        password = str(options.get("password") or "")
        on_sms_needed = options.get("onSmsNeeded")
        on_email_code_needed = options.get("onEmailCodeNeeded")

        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            url = self.page.url
            if self._is_redirect_callback(url, redirect_base):
                return url

            low_text = ""
            try:
                low_text = (await self.page.evaluate("() => (document.body?.innerText || '').toLowerCase()"))
            except Exception:
                pass

            if await self._fill_login_form_if_present(email, password):
                await self.wait_for_cloudflare(20_000)
                continue

            if on_email_code_needed and (
                "email verification" in low_text
                or "verification code" in low_text
                or "邮箱验证码" in low_text
            ):
                code = await on_email_code_needed()
                if code:
                    await self.enter_sms_code(code)
                    await self.click_submit_button()
                    await self.sleep(1200)
                    continue

            if on_sms_needed and ("sms" in low_text or "phone verification" in low_text or "手机号" in low_text):
                code = await on_sms_needed()
                if code:
                    await self.enter_sms_code(code)
                    await self.click_submit_button()
                    await self.sleep(1200)
                    continue

            # OAuth consent page
            try:
                await self.click_button_by_text(["Continue", "Allow", "Authorize", "同意", "继续", "授权"], 2000)
                await self.wait_for_cloudflare(20_000)
                await self.sleep(1200)
                continue
            except Exception:
                pass

            await self.sleep(1000)

        raise RuntimeError("OAuth login/authorize timeout")
