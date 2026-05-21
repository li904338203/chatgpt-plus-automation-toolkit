from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .utils import env_bool, log, resolve_path


@dataclass
class WhatsAppOtpSource:
    enabled: bool
    adb_path: str
    device: str = ""
    package: str = "com.whatsapp"
    timeout: int = 90
    interval: float = 3.0
    code_regex: str = r"(?<!\d)(\d{6})(?!\d)"
    use_notifications: bool = True
    use_bridge: bool = True
    bridge_package: str = "com.loucer.otpbridge"
    bridge_authority: str = "com.loucer.otpbridge.notifications"
    use_ui_text: bool = True
    use_ocr: bool = False
    auto_open: bool = False
    wake_before_open: bool = True
    save_screenshot: bool = False
    tesseract_cmd: str = ""

    def adb_command(self, *args: str) -> list[str]:
        command = [self.adb_path]
        if self.device:
            command.extend(["-s", self.device])
        command.extend(args)
        return command


def whatsapp_otp_enabled(env: dict[str, str]) -> bool:
    return env_bool(env.get("GOPAY_OTP_AUTO") or env.get("WHATSAPP_OTP_AUTO"), default=False)


def resolve_adb_path(env: dict[str, str]) -> str:
    configured = (env.get("WHATSAPP_ADB_PATH") or env.get("ADB_PATH") or "").strip()
    if configured:
        return str(resolve_path(configured) if not Path(configured).is_absolute() else Path(configured))
    found = shutil.which("adb")
    if found:
        return found
    bundled = resolve_path("tools/adb/adb.exe")
    if bundled.exists():
        return str(bundled)
    bundled = resolve_path("scrcpy-win64-v3.3.4/adb.exe")
    if bundled.exists():
        return str(bundled)
    return "adb"


def _worker_value(env: dict[str, str], base: str, worker_id: int, default: str = "") -> str:
    return (
        env.get(f"{base}_{worker_id}")
        or env.get(f"{base}{worker_id}")
        or env.get(base)
        or default
    ).strip()


def source_from_env(env: dict[str, str], worker_id: int = 1) -> WhatsAppOtpSource:
    return WhatsAppOtpSource(
        enabled=env_bool(_worker_value(env, "WHATSAPP_ENABLED", worker_id), default=whatsapp_otp_enabled(env)),
        adb_path=resolve_adb_path(env),
        device=_worker_value(env, "WHATSAPP_ADB_DEVICE", worker_id)
        or _worker_value(env, "WHATSAPP_DEVICE", worker_id),
        package=_worker_value(env, "WHATSAPP_PACKAGE", worker_id, "com.whatsapp") or "com.whatsapp",
        timeout=int(_worker_value(env, "WHATSAPP_CODE_TIMEOUT", worker_id, "90") or "90"),
        interval=float(_worker_value(env, "WHATSAPP_CODE_INTERVAL", worker_id, "2") or "2"),
        code_regex=_worker_value(env, "WHATSAPP_CODE_REGEX", worker_id, r"(?<!\d)(\d{6})(?!\d)"),
        use_bridge=env_bool(_worker_value(env, "WHATSAPP_USE_BRIDGE", worker_id, "1"), default=True),
        bridge_package=_worker_value(env, "WHATSAPP_BRIDGE_PACKAGE", worker_id, "com.loucer.otpbridge") or "com.loucer.otpbridge",
        bridge_authority=_worker_value(env, "WHATSAPP_BRIDGE_AUTHORITY", worker_id, "com.loucer.otpbridge.notifications")
        or "com.loucer.otpbridge.notifications",
        use_notifications=env_bool(_worker_value(env, "WHATSAPP_USE_NOTIFICATIONS", worker_id, "0"), default=False),
        use_ui_text=env_bool(_worker_value(env, "WHATSAPP_USE_UI_TEXT", worker_id, "0"), default=False),
        use_ocr=env_bool(_worker_value(env, "WHATSAPP_USE_OCR", worker_id, "0"), default=False),
        auto_open=env_bool(_worker_value(env, "WHATSAPP_AUTO_OPEN", worker_id, "0"), default=False),
        wake_before_open=env_bool(_worker_value(env, "WHATSAPP_WAKE_BEFORE_OPEN", worker_id, "1"), default=True),
        save_screenshot=env_bool(_worker_value(env, "WHATSAPP_SAVE_SCREENSHOT", worker_id, "0"), default=False),
        tesseract_cmd=_worker_value(env, "TESSERACT_CMD", worker_id),
    )


def extract_otp_codes(text: str, pattern: str, exclude: Iterable[str] | None = None) -> list[str]:
    exclude_set = {re.sub(r"\D+", "", item) for item in (exclude or []) if item}
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    codes: list[str] = []
    keyword_pattern = (
        r"verification code|kode verifikasi|kode otp|otp|one[- ]?time|"
        r"verifikasi|masukkin|masukkan|gunakan kode|gopay|go pay|midtrans|code"
    )

    # WhatsApp chat list usually exposes each preview as one visible text node.
    # Read from top to bottom and only trust a code on the same line as the
    # verification-code phrase, so XML coordinates, timestamps, dates, and unread
    # counts cannot win the match.
    same_line_patterns = [
        rf"(?<!\d)(\d{{6}})(?!\d)[^\n]{{0,120}}(?:{keyword_pattern})",
        rf"(?:{keyword_pattern})[^\n]{{0,120}}(?<!\d)(\d{{6}})(?!\d)",
    ]
    for line in lines:
        low = line.lower()
        if not re.search(keyword_pattern, low, flags=re.I):
            continue
        for regex in same_line_patterns:
            found_codes = re.findall(regex, line, flags=re.I)
            for found in found_codes:
                code = found if isinstance(found, str) else next((part for part in found if part), "")
                code = re.sub(r"\D+", "", code)
                if len(code) != 6:
                    continue
                if code in exclude_set or code in codes:
                    continue
                codes.append(code)
            if codes:
                break
        if codes:
            break

    if codes:
        return codes

    # Some Android/WhatsApp builds split sender/title and message body into
    # adjacent accessibility nodes. In that case use a short context window
    # around GoPay/OTP keywords, but still reject plain timestamps and phone
    # fragments by requiring exactly 6 digits and an OTP-like nearby phrase.
    for index, line in enumerate(lines):
        window = "\n".join(lines[max(0, index - 2): min(len(lines), index + 3)])
        if not re.search(keyword_pattern, window, flags=re.I):
            continue
        for found in re.findall(pattern, line, flags=re.I):
            code = found if isinstance(found, str) else next((part for part in found if part), "")
            code = re.sub(r"\D+", "", code)
            if len(code) != 6:
                continue
            if code in exclude_set or code in codes:
                continue
            codes.append(code)
        if codes:
            return codes

    context_text = "\n".join(
        line for line in lines if re.search(keyword_pattern, line, flags=re.I)
    )
    for found in re.findall(pattern, context_text, flags=re.I):
        code = found if isinstance(found, str) else next((part for part in found if part), "")
        code = re.sub(r"\D+", "", code)
        if len(code) != 6:
            continue
        if code in exclude_set or code in codes:
            continue
        codes.append(code)
    return codes


def extract_all_otp_codes(text: str, pattern: str, exclude: Iterable[str] | None = None) -> list[str]:
    exclude_set = {re.sub(r"\D+", "", item) for item in (exclude or []) if item}
    codes: list[str] = []
    keyword_pattern = (
        r"verification code|kode verifikasi|kode otp|otp|one[- ]?time|"
        r"verifikasi|masukkin|masukkan|gunakan kode|gopay|go pay|midtrans|code"
    )
    for line in [line.strip() for line in (text or "").splitlines() if line.strip()]:
        if not re.search(keyword_pattern, line, flags=re.I):
            continue
        for found in re.findall(pattern, line, flags=re.I):
            code = found if isinstance(found, str) else next((part for part in found if part), "")
            code = re.sub(r"\D+", "", code)
            if len(code) != 6:
                continue
            if code in exclude_set or code in codes:
                continue
            codes.append(code)
    return codes


def extract_visible_text_from_uiautomator_xml(xml_text: str) -> str:
    values: list[str] = []
    for attr in ("text", "content-desc"):
        for value in re.findall(rf'{attr}="([^"]*)"', xml_text or ""):
            value = html.unescape(value).strip()
            if not value:
                continue
            if re.fullmatch(r"[\d,\[\]\s.-]+", value):
                continue
            if value not in values:
                values.append(value)
    return "\n".join(values)


class WhatsAppOtpProvider:
    def __init__(self, source: WhatsAppOtpSource, *, worker_id: int = 1, phone: str = "", out_dir: Path | None = None) -> None:
        self.source = source
        self.worker_id = worker_id
        self.phone = phone
        self.out_dir = out_dir
        self.last_bridge_text = ""

    async def snapshot_codes(self) -> set[str]:
        if not self.source.enabled:
            return set()
        text = await self.collect_text()
        return set(extract_all_otp_codes(text, self.source.code_regex))

    async def wait_code(self, exclude: Iterable[str] | None = None, timeout: int | None = None) -> str:
        if not self.source.enabled:
            return ""
        end_at = time.monotonic() + int(timeout or self.source.timeout)
        exclude_set = {re.sub(r"\D+", "", item) for item in (exclude or []) if item}
        last_error = ""
        opened_on_miss = False
        if self.source.auto_open:
            await self.open_whatsapp()
        while time.monotonic() < end_at:
            try:
                text = await self.collect_text()
                codes = extract_otp_codes(text, self.source.code_regex, exclude_set)
                if codes:
                    code = codes[0]
                    all_codes = extract_all_otp_codes(text, self.source.code_regex)
                    if all_codes and all_codes[0] in exclude_set:
                        await asyncio.sleep(max(1.0, self.source.interval))
                        continue
                    if code in self.last_bridge_text:
                        log(f"[worker-{self.worker_id:02d}] WhatsApp OTP Bridge 已识别 OTP: {code}")
                    else:
                        log(f"[worker-{self.worker_id:02d}] WhatsApp ADB 已识别 OTP: {code}")
                    return code
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            remaining = end_at - time.monotonic()
            if (
                not opened_on_miss
                and not self.source.auto_open
                and self.source.use_ui_text
                and remaining < max(15, int(timeout or self.source.timeout) * 0.55)
            ):
                opened_on_miss = True
                try:
                    log(f"[worker-{self.worker_id:02d}] WhatsApp 通知未读到新码，打开 WhatsApp 前台再取一次")
                    await self.open_whatsapp()
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
            await asyncio.sleep(max(1.0, self.source.interval))
        if last_error:
            log(f"[worker-{self.worker_id:02d}] WhatsApp ADB 取码超时: {last_error}")
        else:
            log(f"[worker-{self.worker_id:02d}] WhatsApp ADB 取码超时，未识别到新 OTP")
        return ""

    async def collect_text(self) -> str:
        if self.source.auto_open and (self.source.use_ui_text or self.source.use_ocr):
            await self.ensure_whatsapp_foreground()
        if self.source.save_screenshot:
            try:
                await self.save_current_screenshot("whatsapp_current")
            except Exception as exc:  # noqa: BLE001
                log(f"[worker-{self.worker_id:02d}] WhatsApp 当前屏幕截图失败，继续取码: {exc}")
        parts: list[str] = []
        self.last_bridge_text = ""
        if self.source.use_bridge:
            try:
                self.last_bridge_text = await self.read_bridge_notifications()
                parts.append(self.last_bridge_text)
            except Exception as exc:  # noqa: BLE001
                log(f"[worker-{self.worker_id:02d}] WhatsApp 通知桥不可用，回退 ADB 通知读取: {exc}")
        if self.source.use_notifications:
            parts.append(await self.read_notifications())
        if self.source.use_ui_text:
            parts.append(await self.read_ui_text())
        if self.source.use_ocr:
            parts.append(await self.read_screenshot_ocr())
        return "\n".join(part for part in parts if part)

    async def open_whatsapp(self) -> None:
        if self.source.wake_before_open:
            await self._run_adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=8)
            await asyncio.sleep(0.5)
            try:
                await self._run_adb("shell", "wm", "dismiss-keyguard", timeout=8)
                await asyncio.sleep(0.5)
            except Exception:
                pass
        await self._run_adb("shell", "monkey", "-p", self.source.package, "1", timeout=8)
        await asyncio.sleep(1.5)

    async def ensure_whatsapp_foreground(self) -> None:
        if await self.is_whatsapp_foreground():
            return
        log(f"[worker-{self.worker_id:02d}] 当前前台不是 WhatsApp，自动打开 {self.source.package} 后取码。")
        await self.open_whatsapp()

    async def is_whatsapp_foreground(self) -> bool:
        text = await self._run_adb("shell", "dumpsys", "window", timeout=10)
        package = re.escape(self.source.package)
        return bool(re.search(rf"(mCurrentFocus|mFocusedApp).*{package}", text, flags=re.I))

    async def save_current_screenshot(self, name: str) -> Path:
        image_path = (self.out_dir or resolve_path("output/gopay注册plus/whatsapp_otp")) / f"{name}_worker_{self.worker_id}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        data = await self._run_adb_bytes("exec-out", "screencap", "-p", timeout=15)
        image_path.write_bytes(data)
        return image_path

    async def read_notifications(self) -> str:
        return await self._run_adb("shell", "dumpsys", "notification", "--noredact", timeout=12)

    async def read_bridge_notifications(self) -> str:
        uri = f"content://{self.source.bridge_authority}/all"
        raw = await self._run_adb("shell", "content", "query", "--uri", uri, timeout=5)
        values: list[str] = []
        for line in raw.splitlines():
            payload = self._extract_content_value(line, "payload_b64")
            if payload:
                try:
                    obj = json.loads(base64.b64decode(payload).decode("utf-8", errors="ignore"))
                except Exception:
                    obj = {}
                package = str(obj.get("package") or "")
                if package and package != self.source.package:
                    continue
                values.append(str(obj.get("title") or ""))
                values.append(str(obj.get("text") or ""))
                continue
            package = self._extract_content_value(line, "package")
            if package and package != self.source.package:
                continue
            values.append(self._extract_content_value(line, "title"))
            values.append(self._extract_content_value(line, "text"))
        return "\n".join(value for value in values if value)

    @staticmethod
    def _extract_content_value(line: str, key: str) -> str:
        match = re.search(rf"\b{re.escape(key)}=(.*?)(?:,\s+\w+=|$)", line)
        return (match.group(1).strip() if match else "")

    async def read_ui_text(self) -> str:
        remote_path = f"/sdcard/window_{self.worker_id}.xml"
        await self._run_adb("shell", "uiautomator", "dump", remote_path, timeout=12)
        xml_text = await self._run_adb("exec-out", "cat", remote_path, timeout=12)
        return extract_visible_text_from_uiautomator_xml(xml_text)

    async def read_screenshot_ocr(self) -> str:
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
        except Exception:
            log(f"[worker-{self.worker_id:02d}] WHATSAPP_USE_OCR=1 但未安装 pillow/pytesseract，跳过 OCR")
            return ""

        if self.source.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.source.tesseract_cmd
        image_path = await self.save_current_screenshot("whatsapp_screen")
        return await asyncio.to_thread(lambda: pytesseract.image_to_string(Image.open(image_path), lang="eng"))

    async def _run_adb(self, *args: str, timeout: int = 15) -> str:
        data = await self._run_adb_bytes(*args, timeout=timeout)
        return data.decode("utf-8", errors="ignore")

    async def _run_adb_bytes(self, *args: str, timeout: int = 15) -> bytes:
        command = self.source.adb_command(*args)

        def run() -> bytes:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="ignore").strip()
                raise RuntimeError(detail or f"adb 返回非 0: {completed.returncode}")
            return completed.stdout

        return await asyncio.to_thread(run)
