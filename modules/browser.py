from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from .utils import resolve_path


def parse_proxy(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    raw = value.strip()
    # Tolerate BOM/zero-width characters from edited proxy files.
    raw = raw.lstrip("\ufeff\u200b\u2060")
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理格式错误，应为 host:port、http://host:port 或 socks5://host:port")
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


SYSTEM_CHROME_PATHS = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _resolve_system_chrome_executable() -> str:
    override = os.environ.get("PLAYWRIGHT_CHROME_EXECUTABLE", "").strip()
    if override and Path(override).exists():
        return override
    for path in SYSTEM_CHROME_PATHS:
        if path.exists():
            return str(path)
    return ""


def _bundled_playwright_browsers_exist() -> bool:
    # In PyInstaller onedir, this module is in "<app>/_internal/modules".
    internal_dir = Path(__file__).resolve().parents[1]
    local_browsers_dir = internal_dir / "playwright" / "driver" / "package" / ".local-browsers"
    return local_browsers_dir.exists()


def _should_use_system_chrome() -> bool:
    if _env_flag("PLAYWRIGHT_USE_SYSTEM_CHROME", default=False):
        return True
    return bool(getattr(sys, "frozen", False)) and not _bundled_playwright_browsers_exist()


class BrowserSession:
    def __init__(self, profile_dir: str | Path, headless: bool, slow_mo: int, timeout_ms: int, proxy: str | None = None, **kwargs):
        self.profile_dir = resolve_path(profile_dir)
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout_ms = timeout_ms
        self.proxy = proxy
        self._playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def __aenter__(self) -> "BrowserSession":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()

        launch_kwargs = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "viewport": {"width": 1365, "height": 900},
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
            "proxy": parse_proxy(self.proxy),
        }
        if _should_use_system_chrome():
            chrome_executable = _resolve_system_chrome_executable()
            if chrome_executable:
                launch_kwargs["executable_path"] = chrome_executable

        self.context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        self.context.set_default_timeout(self.timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        return self

    async def current_page(self) -> Page:
        if not self.context:
            raise RuntimeError("浏览器上下文未启动")
        if self.page and not self.page.is_closed():
            return self.page
        pages = [page for page in self.context.pages if not page.is_closed()]
        if pages:
            self.page = pages[-1]
            return self.page
        self.page = await self.context.new_page()
        return self.page

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.context:
            await self.context.close()
        if self._playwright:
            await self._playwright.stop()
