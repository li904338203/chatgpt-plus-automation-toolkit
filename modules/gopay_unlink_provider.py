from __future__ import annotations

import asyncio
import html
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .utils import env_bool, log, resolve_path


@dataclass
class GoPayUnlinkConfig:
    enabled: bool
    adb_path: str
    device: str = ""
    package: str = "com.gojek.gopay"
    target_app: str = "OpenAI LLC"
    timeout: int = 60
    save_screenshot: bool = True
    debug_screenshots: bool = False
    fast_taps: bool = False

    def adb_command(self, *args: str) -> list[str]:
        command = [self.adb_path]
        if self.device:
            command.extend(["-s", self.device])
        command.extend(args)
        return command


def gopay_unlink_enabled(env: dict[str, str]) -> bool:
    return env_bool(env.get("GOPAY_UNLINK_AFTER_SUCCESS"), default=False)


def resolve_adb_path(env: dict[str, str]) -> str:
    configured = (env.get("GOPAY_ADB_PATH") or env.get("WHATSAPP_ADB_PATH") or env.get("ADB_PATH") or "").strip()
    if configured:
        path = Path(configured)
        return str(path if path.is_absolute() else resolve_path(path))
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


def config_from_env(env: dict[str, str], worker_id: int = 1) -> GoPayUnlinkConfig:
    return GoPayUnlinkConfig(
        enabled=env_bool(_worker_value(env, "GOPAY_UNLINK_ENABLED", worker_id), default=gopay_unlink_enabled(env)),
        adb_path=resolve_adb_path(env),
        device=_worker_value(env, "GOPAY_ADB_DEVICE", worker_id)
        or _worker_value(env, "WHATSAPP_ADB_DEVICE", worker_id)
        or _worker_value(env, "ADB_DEVICE", worker_id),
        package=_worker_value(env, "GOPAY_PACKAGE", worker_id, "com.gojek.gopay") or "com.gojek.gopay",
        target_app=_worker_value(env, "GOPAY_UNLINK_TARGET_APP", worker_id, "OpenAI LLC") or "OpenAI LLC",
        timeout=int(_worker_value(env, "GOPAY_UNLINK_TIMEOUT", worker_id, "60") or "60"),
        save_screenshot=env_bool(_worker_value(env, "GOPAY_UNLINK_SAVE_SCREENSHOT", worker_id, "1"), default=True),
        debug_screenshots=env_bool(_worker_value(env, "GOPAY_UNLINK_DEBUG_SCREENSHOTS", worker_id, "0"), default=False),
        fast_taps=env_bool(_worker_value(env, "GOPAY_UNLINK_FAST_TAPS", worker_id, "1"), default=True),
    )


def _extract_attr(node: str, attr: str) -> str:
    match = re.search(rf'{attr}="([^"]*)"', node)
    return html.unescape(match.group(1)).strip() if match else ""


def _bounds_center(bounds: str) -> tuple[int, int]:
    nums = [int(item) for item in re.findall(r"\d+", bounds)]
    if len(nums) < 4:
        raise RuntimeError(f"无效 bounds: {bounds}")
    return (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2


def _visible_text(xml_text: str) -> str:
    values: list[str] = []
    for attr in ("text", "content-desc"):
        for value in re.findall(rf'{attr}="([^"]*)"', xml_text or ""):
            value = html.unescape(value).strip()
            if value and value not in values:
                values.append(value)
    return "\n".join(values)


class GoPayUnlinkProvider:
    def __init__(self, config: GoPayUnlinkConfig, *, worker_id: int = 1, out_dir: Path | None = None) -> None:
        self.config = config
        self.worker_id = worker_id
        self.out_dir = out_dir or resolve_path("output/gopay注册plus/gopay_unlink")

    async def unlink_openai(self) -> dict:
        if not self.config.enabled:
            return {"ok": False, "skipped": True, "reason": "disabled"}
        start = time.monotonic()
        try:
            await self.open_gopay()
            await self.maybe_shot("open")

            if self.config.fast_taps:
                fast_result = await self.try_fast_navigate_to_linked_apps()
                if fast_result.get("ok"):
                    log(f"[worker-{self.worker_id:02d}] GoPay unlink fast path: linked apps")
                else:
                    log(f"[worker-{self.worker_id:02d}] GoPay unlink fast path 回退文字识别: {fast_result.get('reason')}")
                    await self.navigate_to_linked_apps()
            else:
                await self.navigate_to_linked_apps()
            if not await self.wait_text(r"(^|\n)Linked apps($|\n)", timeout=8):
                raise RuntimeError("点击 Linked apps 后未确认进入 Linked apps 页面")

            text = await self.wait_text(
                re.escape(self.config.target_app) + r"|No apps linked to your GoPay|No apps linked|(^|\n)Unlink($|\n)",
                timeout=8,
            ) or await self.current_visible_text()
            target = re.escape(self.config.target_app)
            if not re.search(target, text, flags=re.I):
                if not re.search(r"No apps linked to your GoPay|No apps linked", text, flags=re.I):
                    raise RuntimeError("Linked apps 页面未看到 OpenAI LLC，也未看到空状态，不能判定已解绑")
                await self.return_home()
                await self.final_shot("already_unlinked")
                return {"ok": True, "alreadyUnlinked": True, "target": self.config.target_app}

            if self.config.fast_taps:
                fast_unlink = await self.try_fast_unlink_openai()
                if not fast_unlink.get("ok"):
                    log(f"[worker-{self.worker_id:02d}] GoPay unlink button fast path 回退文字识别: {fast_unlink.get('reason')}")
                    await self.tap_by_text(r"(^|\n)Unlink($|\n)", "Unlink list button")
            else:
                await self.tap_by_text(r"(^|\n)Unlink($|\n)", "Unlink list button")
            if not await self.wait_text(r"Unlink .*GoPay|Once unlinked|(^|\n)Unlink($|\n)", timeout=10):
                raise RuntimeError("未检测到 Unlink 二次确认弹窗")
            if self.config.fast_taps:
                fast_confirm = await self.try_fast_confirm_unlink()
                if not fast_confirm.get("ok"):
                    log(f"[worker-{self.worker_id:02d}] GoPay unlink confirm fast path 回退文字识别: {fast_confirm.get('reason')}")
                    await self.tap_unlink_confirm()
            else:
                await self.tap_unlink_confirm()

            success_text = await self.wait_text(r"Successfully unlinked|No apps linked to your GoPay|No apps linked", timeout=15)
            if not success_text:
                raise RuntimeError("未检测到 GoPay 解绑成功提示")

            await self.return_home()
            await self.final_shot("success")
            return {
                "ok": True,
                "alreadyUnlinked": False,
                "target": self.config.target_app,
                "elapsed": round(time.monotonic() - start, 2),
            }
        except Exception as exc:  # noqa: BLE001
            path = await self.final_shot("failed")
            return {"ok": False, "error": str(exc), "screenshot": str(path) if path else ""}

    async def navigate_to_linked_apps(self) -> None:
        for _attempt in range(1, 6):
            text = await self.current_visible_text()
            if self.is_linked_apps_page(text):
                return
            if (
                re.search(r"(^|\n)Linked apps($|\n)", text, flags=re.I)
                and re.search(r"(^|\n)Unlink($|\n)", text, flags=re.I)
                and not re.search(r"List of apps that you link to GoPay|Account & app settings|Popular service permission", text, flags=re.I)
            ):
                return
            if re.search(r"(^|\n)Linked apps($|\n)", text, flags=re.I) and re.search(
                re.escape(self.config.target_app) + r"|No apps linked to your GoPay|No apps linked",
                text,
                flags=re.I,
            ):
                return
            if re.search(r"(^|\n)Linked apps($|\n)", text, flags=re.I) and re.search(r"List of apps that you link to GoPay", text, flags=re.I):
                await self.tap_settings_row(r"Linked apps|List of apps that you link to GoPay", "Linked apps", timeout=8)
                continue
            if re.search(r"Account & app settings", text, flags=re.I):
                await self.tap_by_text(r"Account & app settings", "Account & app settings", timeout=8)
                continue
            if re.search(r"(^|\n)Profile($|\n)", text, flags=re.I):
                await self.tap_by_text(r"(^|\n)Profile($|\n)", "Profile", timeout=8)
                continue
            await self._adb("shell", "input", "keyevent", "KEYCODE_BACK", timeout=8)
            await asyncio.sleep(1.0)
        text = await self.current_visible_text()
        raise RuntimeError(f"无法导航到 Linked apps 页面; visible={text[:300]}")

    def is_linked_apps_page(self, text: str) -> bool:
        if not re.search(r"(^|\n)Linked apps($|\n)", text, flags=re.I):
            return False
        target = re.escape(self.config.target_app)
        if re.search(target, text, flags=re.I) or re.search(r"No apps linked to your GoPay|No apps linked", text, flags=re.I):
            return True
        if re.search(r"List of apps that you link to GoPay|Account & app settings|Popular service permission", text, flags=re.I):
            return False
        if re.search(r"(^|\n)Back($|\n)", text, flags=re.I):
            return True
        return bool(
            re.search(r"(^|\n)Unlink($|\n)", text, flags=re.I)
        )

    async def open_gopay(self) -> None:
        await self._adb("shell", "input", "keyevent", "KEYCODE_WAKEUP", timeout=8)
        await asyncio.sleep(0.4)
        try:
            await self._adb("shell", "wm", "dismiss-keyguard", timeout=8)
        except Exception:
            pass
        await self._adb("shell", "monkey", "-p", self.config.package, "1", timeout=8)
        await asyncio.sleep(4)

    async def try_fast_navigate_to_linked_apps(self) -> dict:
        # Coordinates are for the project's fixed LDPlayer layout: 720x1280, dpi 240.
        try:
            text = await self.current_visible_text()
            if self.is_linked_apps_page(text):
                return {"ok": True, "step": "already_linked_apps"}
            if not re.search(r"Account & app settings", text, flags=re.I):
                await self._adb("shell", "input", "tap", "648", "1226", timeout=8)  # Profile tab
                await asyncio.sleep(1.2)
                text = await self.current_visible_text()
                if not re.search(r"Account & app settings", text, flags=re.I):
                    await self._adb("shell", "input", "tap", "360", "760", timeout=8)  # Account & app settings row
                    await asyncio.sleep(1.8)
            text = await self.current_visible_text()
            if not re.search(r"Account & app settings", text, flags=re.I):
                return {"ok": False, "reason": "settings_not_visible"}
            await self._adb("shell", "input", "tap", "360", "576", timeout=8)  # Linked apps row
            await asyncio.sleep(2.0)
            text = await self.current_visible_text()
            if self.is_linked_apps_page(text) or re.search(
                re.escape(self.config.target_app) + r"|No apps linked to your GoPay|No apps linked",
                text,
                flags=re.I,
            ):
                return {"ok": True, "step": "linked_apps"}
            await self._adb("shell", "input", "tap", "652", "576", timeout=8)  # row chevron fallback
            await asyncio.sleep(2.0)
            text = await self.current_visible_text()
            if self.is_linked_apps_page(text) or re.search(
                re.escape(self.config.target_app) + r"|No apps linked to your GoPay|No apps linked",
                text,
                flags=re.I,
            ):
                return {"ok": True, "step": "linked_apps_chevron"}
            return {"ok": False, "reason": "linked_apps_not_reached", "visible": text[:220]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    async def try_fast_unlink_openai(self) -> dict:
        # Linked apps list in the fixed 720x1280 layout shows OpenAI LLC in a
        # single row with the Unlink action on the right.
        try:
            await self._adb("shell", "input", "tap", "620", "300", timeout=8)
            await asyncio.sleep(1.0)
            text = await self.current_visible_text()
            if re.search(r"Unlink .*GoPay|Once unlinked|(^|\n)Unlink($|\n)", text, flags=re.I):
                return {"ok": True, "step": "confirm_dialog"}
            return {"ok": False, "reason": "confirm_dialog_not_visible", "visible": text[:220]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    async def try_fast_confirm_unlink(self) -> dict:
        # Confirmation sheet primary button is near the bottom on 720x1280.
        # Prefer the real accessibility bounds so the fast path does not report
        # success while the modal is still open.
        try:
            xml_text = await self.dump_ui()
            candidates = self.find_nodes(xml_text, r"(^|\n)Unlink($|\n)")
            if candidates:
                candidates.sort(key=lambda item: _bounds_center(item[1])[1], reverse=True)
                _node, bounds = candidates[0]
                x, y = _bounds_center(bounds)
            else:
                x, y = 360, 1187
            for index, (tap_x, tap_y) in enumerate(((x, y), (360, 1187)), start=1):
                await self._adb("shell", "input", "tap", str(tap_x), str(tap_y), timeout=8)
                text = await self.wait_text(
                    r"Successfully unlinked|No apps linked to your GoPay|No apps linked",
                    timeout=3,
                )
                if text:
                    return {"ok": True, "step": f"success_tap_{index}"}
                current = await self.current_visible_text()
                if not re.search(r"Unlink .*GoPay|Once unlinked|(^|\n)Unlink($|\n)", current, flags=re.I):
                    break
            text = await self.current_visible_text()
            return {"ok": False, "reason": "success_not_visible", "visible": text[:220]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    async def return_home(self) -> None:
        for _ in range(4):
            text = await self.current_visible_text()
            if re.search(r"(^|\n)Home($|\n)", text, flags=re.I):
                break
            await self._adb("shell", "input", "keyevent", "KEYCODE_BACK", timeout=8)
            await asyncio.sleep(1.0)
        try:
            await self.tap_by_text(r"(^|\n)Home($|\n)", "Home tab", timeout=5)
        except Exception:
            await self._adb("shell", "input", "tap", "144", "3005", timeout=8)
            await asyncio.sleep(1.0)

    async def tap_by_text(self, pattern: str, label: str, *, timeout: int | None = None) -> None:
        end_at = time.monotonic() + (timeout or self.config.timeout)
        last_text = ""
        while time.monotonic() < end_at:
            xml_text = await self.dump_ui()
            last_text = _visible_text(xml_text)
            candidates = self.find_nodes(xml_text, pattern)
            if candidates:
                _node, bounds = candidates[0]
                x, y = _bounds_center(bounds)
                log(f"[worker-{self.worker_id:02d}] GoPay unlink tap: {label}")
                await self._adb("shell", "input", "tap", str(x), str(y), timeout=8)
                await asyncio.sleep(2)
                return
            await asyncio.sleep(1)
        raise RuntimeError(f"找不到 GoPay 控件: {label}; visible={last_text[:300]}")

    async def tap_settings_row(self, pattern: str, label: str, *, timeout: int | None = None) -> None:
        end_at = time.monotonic() + (timeout or self.config.timeout)
        last_text = ""
        while time.monotonic() < end_at:
            xml_text = await self.dump_ui()
            last_text = _visible_text(xml_text)
            candidates = self.find_nodes(xml_text, pattern)
            if candidates:
                candidates.sort(key=lambda item: (_bounds_center(item[1])[1], _bounds_center(item[1])[0]))
                _node, bounds = candidates[0]
                x1, y1, x2, y2 = [int(item) for item in re.findall(r"\d+", bounds)[:4]]
                y = (y1 + y2) // 2
                log(f"[worker-{self.worker_id:02d}] GoPay unlink tap: {label} row")
                for x in ((x1 + x2) // 2, max(x1 + 1, x2 - 44)):
                    await self._adb("shell", "input", "tap", str(x), str(y), timeout=8)
                    await asyncio.sleep(1.4)
                    text = await self.current_visible_text()
                    if self.is_linked_apps_page(text):
                        return
                    if re.search(re.escape(self.config.target_app) + r"|No apps linked to your GoPay|No apps linked", text, flags=re.I):
                        return
                return
            await asyncio.sleep(1)
        raise RuntimeError(f"找不到 GoPay 设置行: {label}; visible={last_text[:300]}")

    async def tap_unlink_confirm(self) -> None:
        xml_text = await self.dump_ui()
        candidates = self.find_nodes(xml_text, r"(^|\n)Unlink($|\n)")
        if not candidates:
            raise RuntimeError("找不到二次确认 Unlink 按钮")
        candidates.sort(key=lambda item: _bounds_center(item[1])[1], reverse=True)
        _node, bounds = candidates[0]
        x, y = _bounds_center(bounds)
        log(f"[worker-{self.worker_id:02d}] GoPay unlink tap: Confirm Unlink")
        await self._adb("shell", "input", "tap", str(x), str(y), timeout=8)
        await asyncio.sleep(3)

    def find_nodes(self, xml_text: str, pattern: str) -> list[tuple[str, str]]:
        regex = re.compile(pattern, re.I | re.S)
        candidates: list[tuple[int, str, str]] = []
        for node in re.findall(r"<node\b[^>]*>", xml_text or ""):
            text = _extract_attr(node, "text")
            desc = _extract_attr(node, "content-desc")
            haystack = f"{text}\n{desc}"
            if not regex.search(haystack):
                continue
            bounds = _extract_attr(node, "bounds")
            if not bounds:
                continue
            clickable_rank = 0 if 'clickable="true"' in node else 1
            candidates.append((clickable_rank, node, bounds))
        candidates.sort(key=lambda item: item[0])
        return [(node, bounds) for _rank, node, bounds in candidates]

    async def wait_text(self, pattern: str, *, timeout: int = 10) -> str:
        end_at = time.monotonic() + timeout
        while time.monotonic() < end_at:
            text = await self.current_visible_text()
            if re.search(pattern, text, flags=re.I):
                return text
            await asyncio.sleep(1)
        return ""

    async def current_visible_text(self) -> str:
        return _visible_text(await self.dump_ui())

    async def dump_ui(self) -> str:
        remote_path = f"/sdcard/gopay_unlink_{self.worker_id}.xml"
        await self._adb("shell", "uiautomator", "dump", remote_path, timeout=12)
        return await self._adb("exec-out", "cat", remote_path, timeout=12)

    async def maybe_shot(self, name: str) -> Path | None:
        if not self.config.debug_screenshots:
            return None
        return await self.save_screenshot(name)

    async def final_shot(self, name: str) -> Path | None:
        if not self.config.save_screenshot:
            return None
        return await self.save_screenshot(name)

    async def save_screenshot(self, name: str) -> Path:
        path = self.out_dir / f"gopay_unlink_{name}_worker_{self.worker_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(await self._adb_bytes("exec-out", "screencap", "-p", timeout=15))
        return path

    async def _adb(self, *args: str, timeout: int = 15) -> str:
        return (await self._adb_bytes(*args, timeout=timeout)).decode("utf-8", errors="ignore")

    async def _adb_bytes(self, *args: str, timeout: int = 15) -> bytes:
        command = self.config.adb_command(*args)

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
