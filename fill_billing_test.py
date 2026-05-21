from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modules.billing_provider import BillingAddress, fetch_meiguodizhi_us_address
from modules.browser import BrowserSession
from modules.gopay_unlink_provider import GoPayUnlinkProvider, config_from_env as gopay_unlink_config_from_env, gopay_unlink_enabled
from modules.proxy_pool import ProxyPool
from modules.utils import (
    LEGACY_OUTPUT_FILES,
    env_bool,
    load_config,
    load_env,
    log,
    migrate_output_file,
    output_file,
    resolve_path,
)
from modules.whatsapp_otp_provider import WhatsAppOtpProvider, source_from_env, whatsapp_otp_enabled
from modules.terminal_theme import install_print_theme


install_print_theme()


GOPAY_ACTION_BUTTON_SELECTORS = [
    "[data-testid='pay-button']",
    "button:has-text('Link and pay')",
    "button:has-text('Pay now')",
    "button:has-text('Bayar')",
    "button:has-text('Lanjut')",
    "button:has-text('Lanjutkan')",
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button:has-text('Confirm')",
    "button:has-text('Konfirmasi')",
    "button:has-text('OK')",
    "[role='button']:has-text('Bayar')",
    "[role='button']:has-text('Lanjut')",
    "[role='button']:has-text('Continue')",
    "[role='button']:has-text('Confirm')",
]

GOPAY_CONFIRM_BUTTON_SELECTORS = [
    selector for selector in GOPAY_ACTION_BUTTON_SELECTORS if "Pay now" not in selector
]

GOPAY_PIN_INPUT_SELECTORS = [
    "[data-testid='pin-input-field']",
    "input[name*='pin' i]",
    "input[id*='pin' i]",
    "input[autocomplete='one-time-code']",
    "input[inputmode='numeric']",
    "input[type='password']",
    "input[type='tel']",
    "input[type='text']",
]


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"


def parse_inline_payment_record(line: str) -> dict[str, str] | None:
    parts = [part.strip() for part in line.split("----")]
    if len(parts) < 3:
        return None
    account = parts[0]
    payment_link = parts[-1]
    if not re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", account):
        return None
    if not payment_link.startswith(("http://", "https://")):
        return None
    middle = parts[1:-1]
    password = client_id = refresh_token = ""
    code_address = ""
    source_format = "inline_payment"
    if len(middle) == 1:
        code_address = middle[0]
        source_format = "inline_icloud_payment"
    elif len(middle) == 3:
        password, client_id, refresh_token = middle
        code_address = account
        source_format = "inline_hotmail_payment"
    elif len(middle) >= 4:
        password, client_id, refresh_token = middle[:3]
        code_address = middle[3]
        source_format = "inline_hotmail_payment"
    return {
        "account": account,
        "password": password,
        "client_id": client_id,
        "refresh_token": refresh_token,
        "payment_link": payment_link,
        "code_address": code_address or account,
        "source_format": source_format,
        "source_raw": line,
    }


def read_payment_links(path: str | Path = output_file("flow1_success")) -> list[dict[str, str]]:
    input_path = migrate_output_file(path, LEGACY_OUTPUT_FILES["flow1_success"])
    text = input_path.read_text(encoding="utf-8")
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}

    def flush() -> None:
        nonlocal record
        if record.get("payment_link"):
            records.append(record)
        record = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        inline_record = parse_inline_payment_record(line)
        if inline_record:
            if record:
                flush()
            records.append(inline_record)
            continue
        if line.startswith("账号："):
            if record:
                flush()
            record["account"] = line.split("账号：", 1)[1].strip()
        elif line.startswith("接码地址："):
            record["code_address"] = line.split("接码地址：", 1)[1].strip()
        elif line.startswith("支付长链接："):
            record["payment_link"] = line.split("支付长链接：", 1)[1].strip()
    flush()
    return records


def dedupe_payment_links(records: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        account = (record.get("account") or "").strip().lower()
        link = (record.get("payment_link") or "").strip()
        key = (account, link)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def read_accounts_from_file(path: str | Path, legacy_path: str | Path | None = None) -> set[str]:
    account_file = migrate_output_file(path, legacy_path)
    if not account_file.exists():
        return set()
    accounts: set[str] = set()
    for line in account_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = re.search(r"([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", line)
        if match:
            accounts.add(match.group(1).strip().lower())
    return accounts


def read_flow2_processed_accounts(
    paid_output: str | Path = output_file("flow2_paid_success"),
    nonzero_output: str | Path = output_file("flow2_nonzero_billing"),
) -> set[str]:
    return (
        read_accounts_from_file(paid_output, LEGACY_OUTPUT_FILES["flow2_paid_success"])
        | read_accounts_from_file(nonzero_output, LEGACY_OUTPUT_FILES["flow2_nonzero_billing"])
    )


def filter_unprocessed_payment_links(
    records: list[dict[str, str]],
    processed_accounts: set[str],
) -> list[dict[str, str]]:
    if not processed_accounts:
        return records
    return [
        record
        for record in records
        if (record.get("account") or "").strip().lower() not in processed_accounts
    ]


def pick_record(records: list[dict[str, str]], index: int | None, account: str | None) -> dict[str, str]:
    if not records:
        raise RuntimeError(f"{output_file('flow1_success')} 里没有支付长链接")
    if account:
        for record in records:
            if record.get("account", "").lower() == account.lower():
                return record
        raise RuntimeError(f"未找到指定账号的支付长链接: {account}")
    if index is not None:
        if index < 1 or index > len(records):
            raise RuntimeError(f"--index 超出范围，当前共有 {len(records)} 条")
        return records[index - 1]
    return records[-1]


def append_paid_success(record: dict[str, str], output_file: str | Path = output_file("flow2_paid_success")) -> Path:
    path = migrate_output_file(output_file, LEGACY_OUTPUT_FILES["flow2_paid_success"])
    path.parent.mkdir(parents=True, exist_ok=True)
    account = record.get("account", "").strip()
    code_address = record.get("code_address", "").strip()
    if record.get("password") and record.get("client_id") and record.get("refresh_token"):
        text = (
            f"{account}----{record.get('password', '').strip()}"
            f"----{record.get('client_id', '').strip()}"
            f"----{record.get('refresh_token', '').strip()}\n"
        )
    else:
        text = f"{account}----{code_address}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if account and account not in existing:
        with path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(text)
    return path


def append_nonzero_billing(
    record: dict[str, str],
    checkout: dict[str, Any],
    output_file: str | Path = output_file("flow2_nonzero_billing"),
) -> Path:
    path = migrate_output_file(output_file, LEGACY_OUTPUT_FILES["flow2_nonzero_billing"])
    path.parent.mkdir(parents=True, exist_ok=True)
    account = record.get("account", "").strip()
    code_address = record.get("code_address", "").strip()
    total_text = str(checkout.get("totalText") or checkout.get("reason") or "").strip()
    url = str(checkout.get("url") or record.get("payment_link") or "").strip()
    line = f"账号：{account}\t接码地址：{code_address}\t今日应付：{total_text}\t链接：{url}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if account and account in existing:
        lines = existing.splitlines()
        replaced = False
        updated_lines = []
        for item in lines:
            if not replaced and item.startswith(f"账号：{account}\t"):
                updated_lines.append(line.rstrip("\n"))
                replaced = True
            else:
                updated_lines.append(item)
        path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")
    elif account:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return path


def write_payment_links(path: str | Path, records: list[dict[str, str]]) -> None:
    output_path = migrate_output_file(path, LEGACY_OUTPUT_FILES["flow1_success"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for record in records:
        if record.get("source_raw") and record.get("source_format", "").startswith("inline_"):
            blocks.append(record["source_raw"].strip())
        else:
            blocks.append(
                "\n".join(
                    [
                        f"账号：{record.get('account', '').strip()}",
                        f"接码地址：{record.get('code_address', '').strip()}",
                        f"支付长链接：{record.get('payment_link', '').strip()}",
                    ]
                )
            )
    output_path.write_text(("\n\n".join(blocks) + ("\n\n" if blocks else "")), encoding="utf-8")


def remove_paid_record(success_file: str | Path, record: dict[str, str]) -> Path:
    records = read_payment_links(success_file)
    account = record.get("account", "").strip().lower()
    payment_link = record.get("payment_link", "").strip()
    remaining = []
    removed = False
    for item in records:
        same_account = account and item.get("account", "").strip().lower() == account
        same_link = payment_link and item.get("payment_link", "").strip() == payment_link
        if not removed and (same_account or same_link):
            removed = True
            continue
        remaining.append(item)
    write_payment_links(success_file, remaining)
    return migrate_output_file(success_file, LEGACY_OUTPUT_FILES["flow1_success"])


def parse_phone_pool(env: dict[str, str]) -> list[str]:
    raw = env.get("GOPAY_PHONES") or env.get("PHONES") or env.get("PHONE_POOL") or ""
    values = []
    for item in re.split(r"[\s,;，；|]+", raw):
        if not item.strip():
            continue
        phone = normalize_phone(item)
        if phone and phone not in values:
            values.append(phone)
    return values


@dataclass(frozen=True)
class Flow2DeviceSlot:
    worker_id: int
    phone: str
    whatsapp_device: str = ""
    gopay_device: str = ""
    enabled: bool = True


def _worker_env_value(env: dict[str, str], base: str, worker_id: int, default: str = "") -> str:
    return (
        env.get(f"{base}_{worker_id}")
        or env.get(f"{base}{worker_id}")
        or env.get(base)
        or default
    ).strip()


def _specific_worker_env_value(env: dict[str, str], base: str, worker_id: int) -> str:
    return (env.get(f"{base}_{worker_id}") or env.get(f"{base}{worker_id}") or "").strip()


def _env_worker_indexes(env: dict[str, str]) -> set[int]:
    indexes: set[int] = set()
    for key in env:
        match = re.match(
            r"^(?:GOPAY_PHONE|GOPAY_SLOT_ENABLED|GOPAY_DEVICE_ENABLED|FLOW2_DEVICE_ENABLED|"
            r"WHATSAPP_ADB_DEVICE|GOPAY_ADB_DEVICE)_?(\d+)$",
            key,
        )
        if match:
            indexes.add(int(match.group(1)))
    return indexes


def parse_flow2_device_slots(env: dict[str, str]) -> list[Flow2DeviceSlot]:
    legacy_phones = parse_phone_pool(env)
    indexes = _env_worker_indexes(env)
    indexes.update(range(1, len(legacy_phones) + 1))
    slots: list[Flow2DeviceSlot] = []
    for worker_id in sorted(indexes):
        explicit_phone = _specific_worker_env_value(env, "GOPAY_PHONE", worker_id)
        if not explicit_phone and worker_id == 1:
            explicit_phone = (env.get("GOPAY_PHONE") or "").strip()
        phone = normalize_phone(explicit_phone) if explicit_phone else (
            legacy_phones[worker_id - 1] if worker_id <= len(legacy_phones) else ""
        )
        if not phone:
            continue
        enabled = env_bool(
            _worker_env_value(env, "FLOW2_DEVICE_ENABLED", worker_id)
            or _worker_env_value(env, "GOPAY_DEVICE_ENABLED", worker_id)
            or _worker_env_value(env, "GOPAY_SLOT_ENABLED", worker_id),
            default=True,
        )
        if not enabled:
            continue
        whatsapp_device = (
            _worker_env_value(env, "WHATSAPP_ADB_DEVICE", worker_id)
            or _worker_env_value(env, "WHATSAPP_DEVICE", worker_id)
        )
        gopay_device = (
            _worker_env_value(env, "GOPAY_ADB_DEVICE", worker_id)
            or whatsapp_device
        )
        slots.append(
            Flow2DeviceSlot(
                worker_id=worker_id,
                phone=phone,
                whatsapp_device=whatsapp_device,
                gopay_device=gopay_device,
                enabled=enabled,
            )
        )
    return slots


def resolve_flow2_adb_path(env: dict[str, str]) -> str:
    configured = (env.get("WHATSAPP_ADB_PATH") or env.get("GOPAY_ADB_PATH") or env.get("ADB_PATH") or "").strip()
    if configured:
        path = Path(configured)
        return str(path if path.is_absolute() else resolve_path(path))
    bundled = resolve_path("tools/adb/adb.exe")
    if bundled.exists():
        return str(bundled)
    return "adb"


def parse_adb_device_states(output: str) -> dict[str, str]:
    states: dict[str, str] = {}
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def collect_slot_adb_devices(slots: list[Flow2DeviceSlot]) -> list[str]:
    devices: list[str] = []
    for slot in slots:
        for device in (slot.whatsapp_device, slot.gopay_device):
            device = (device or "").strip()
            if device and device not in devices:
                devices.append(device)
    return devices


def check_flow2_adb_ready(env: dict[str, str], slots: list[Flow2DeviceSlot]) -> bool:
    devices = collect_slot_adb_devices(slots)
    if not devices:
        return True
    adb_path = resolve_flow2_adb_path(env)
    try:
        completed = subprocess.run(
            [adb_path, "devices", "-l"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=12,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"流程二 ADB 预检失败: 无法执行 {adb_path}: {exc}")
        return False
    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
    states = parse_adb_device_states(output)
    bad = [(device, states.get(device, "not_found")) for device in devices if states.get(device) != "device"]
    if not bad:
        return True
    bad_text = ", ".join(f"{device}={state}" for device, state in bad)
    log(f"流程二 ADB 预检未通过: ADB={adb_path} | {bad_text}")
    if output:
        compact = " | ".join(line.strip() for line in output.splitlines() if line.strip())
        log(f"流程二 ADB 当前列表: {compact}")
    log("请先让雷电 ADB 显示为 device 状态后再跑流程二；当前状态跑到 OTP/PIN 一定会失败。")
    return False


def parse_gopay_pin(env: dict[str, str]) -> str:
    return re.sub(r"\D+", "", env.get("GOPAY_PIN") or env.get("GOPAY_PAYMENT_PIN") or "")


def flow2_fast_defaults(env: dict[str, str] | None = None) -> dict[str, int]:
    env = env or load_env(".env")

    def read_int(key: str, default: int) -> int:
        value = (env.get(key) or "").strip()
        if not value:
            return default
        try:
            return max(1, int(value))
        except ValueError:
            return default

    return {
        "billing_retries": read_int("FLOW2_BILLING_RETRIES", 5),
        "otp_timeout": read_int("FLOW2_OTP_TIMEOUT", 90),
        "retry_interval": read_int("FLOW2_RETRY_INTERVAL", 6),
        "retry_timeout": read_int("FLOW2_RETRY_TIMEOUT", 180),
        "manual_success_timeout": read_int("FLOW2_MANUAL_SUCCESS_TIMEOUT", 180),
    }


def default_country_code(env: dict[str, str] | None = None) -> str:
    env = env or load_env(".env")
    raw = (env.get("GOPAY_COUNTRY_CODE") or env.get("GOPAY_PHONE_COUNTRY_CODE") or "").strip()
    return normalize_country_code(raw or "+62")


def ask_int(prompt: str, default: int, min_value: int, max_value: int) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            value = default
        else:
            try:
                value = int(raw)
            except ValueError:
                print("请输入数字。")
                continue
        if value < min_value or value > max_value:
            print(f"请输入 {min_value} - {max_value} 之间的数字。")
            continue
        return value


async def fill_billing_page(page, billing: BillingAddress) -> dict:
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(2500)
    await page.locator("#payment-method-accordion-item-title-gopay").check(force=True)
    await page.wait_for_timeout(1200)
    try:
        await page.get_by_text("输入地址以计算", exact=True).click(force=True)
        await page.wait_for_timeout(1200)
    except Exception:
        pass
    await page.wait_for_selector("#billingName, #billingAddressLine1", timeout=15000)

    await human_fill(page.locator("#billingName"), billing.name)
    await page.wait_for_timeout(250)
    country_state = await page.locator("#billingCountry").evaluate(
        """(el) => ({
            value: el.value || '',
            disabled: Boolean(el.disabled),
            ariaDisabled: el.getAttribute('aria-disabled') || '',
        })"""
    )
    if country_state.get("disabled") or str(country_state.get("ariaDisabled")).lower() == "true":
        if country_state.get("value") != billing.country:
            await safe_screenshot(page, resolve_path("output/gopay注册plus/billing") / "billing_country_disabled_mismatch.png")
            raise RuntimeError(
                f"账单国家下拉被锁定为 {country_state.get('value') or '空'}，但当前地址需要 {billing.country}，已停止"
            )
        log(f"账单国家已锁定为 {billing.country}，跳过国家选择")
    else:
        await page.locator("#billingCountry").select_option(billing.country)
    await page.wait_for_timeout(700)
    await human_fill(page.locator("#billingAddressLine1"), billing.address_line1)
    await page.wait_for_timeout(1200)
    await page.keyboard.press("ArrowDown")
    await page.wait_for_timeout(250)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1200)
    await human_fill(page.locator("#billingLocality"), billing.city)
    await human_fill(page.locator("#billingPostalCode"), billing.postal_code)
    state_locator = page.locator("#billingAdministrativeArea")
    state_tag = ""
    try:
        state_tag = (await state_locator.evaluate("(el) => el.tagName")).lower()
    except Exception:
        state_tag = ""
    if state_tag == "select":
        try:
            await state_locator.select_option(billing.state)
        except Exception:
            await state_locator.select_option(label=billing.state_full)
    else:
        await human_fill(state_locator, billing.state)
    await page.wait_for_timeout(700)
    await page.locator("#billingAdministrativeArea").blur()
    await page.wait_for_timeout(1200)

    result = await page.evaluate(
        """async ({ billing }) => {
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const setInputValue = (selector, value) => {
                const el = document.querySelector(selector);
                if (!el || !visible(el)) return false;
                const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                desc?.set?.call(el, value);
                el.focus();
                el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: String(value) }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            };
            const setSelectValue = (selector, value) => {
                const el = document.querySelector(selector);
                if (!el || !visible(el)) return false;
                el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            };
            const waitFor = async (fn, timeout = 12000) => {
                const end = Date.now() + timeout;
                while (Date.now() < end) {
                    const value = fn();
                    if (value) return value;
                    await sleep(250);
                }
                return null;
            };
            const readState = () => {
                const text = document.body?.innerText || '';
                const normalized = text.replace(/\\s+/g, ' ');
                const required = ['#billingName', '#billingCountry', '#billingAddressLine1', '#billingLocality', '#billingPostalCode', '#billingAdministrativeArea'];
                const complete = required.every((selector) => {
                    const el = document.querySelector(selector);
                    return el && String(el.value || '').trim();
                });
                const visibleError = Array.from(document.querySelectorAll('[aria-invalid="true"], .is-invalid, .invalid, [data-invalid="true"]'))
                    .some((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    });
                const taxPending = /输入地址以计算|Enter address to calculate|Calculating|正在计算/i.test(normalized);
                const taxError = /无法计算|unable to calculate|can't calculate|cannot calculate/i.test(normalized);
                const dueReady = /今日应付合计\\s*IDR\\s*0(?:[,.]00)?|Total due today\\s*IDR\\s*0(?:[,.]00)?/i.test(normalized)
                    || /IDR\\s*0(?:[,.]00)?/.test(normalized);
                return { text: normalized, complete, visibleError, taxPending, taxError, dueReady };
            };

            const settled = await waitFor(() => {
                const state = readState();
                return state.complete && !state.visibleError && !state.taxPending && !state.taxError && state.dueReady ? state : null;
            }, 30000);
            const finalState = settled || readState();

            return {
                ok: Boolean(settled),
                taxError: finalState.taxError || finalState.taxPending || finalState.visibleError || !finalState.dueReady,
                taxPending: finalState.taxPending,
                visibleError: finalState.visibleError,
                dueReady: finalState.dueReady,
                gopayChecked: document.querySelector('#payment-method-accordion-item-title-gopay')?.checked || false,
                billingName: document.querySelector('#billingName')?.value || null,
                billingCountry: document.querySelector('#billingCountry')?.value || null,
                billingAddressLine1: document.querySelector('#billingAddressLine1')?.value || null,
                billingLocality: document.querySelector('#billingLocality')?.value || null,
                billingPostalCode: document.querySelector('#billingPostalCode')?.value || null,
                billingAdministrativeArea: document.querySelector('#billingAdministrativeArea')?.value || null,
                bodySample: finalState.text.slice(0, 800)
            };
        }""",
        {"billing": billing.as_dict()},
    )
    return result


async def clear_billing_page(page) -> None:
    await page.evaluate(
        """() => {
            const selectors = [
                '#billingName',
                '#billingAddressLine1',
                '#billingAddressLine2',
                '#billingLocality',
                '#billingPostalCode',
                '#billingAdministrativeArea',
            ];
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (!el || el.type === 'file') continue;
                const proto = el instanceof HTMLSelectElement
                    ? HTMLSelectElement.prototype
                    : el instanceof HTMLTextAreaElement
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                desc?.set?.call(el, '');
                el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
            for (const button of Array.from(document.querySelectorAll('button, [role="button"]'))) {
                const text = (button.textContent || '').trim();
                if (/清空|Clear/i.test(text)) {
                    try { button.click(); } catch {}
                }
            }
        }"""
    )
    await page.wait_for_timeout(700)


async def is_billing_ready(page) -> bool:
    return bool(
        await page.evaluate(
            """() => {
                const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
                const required = ['#billingName', '#billingCountry', '#billingAddressLine1', '#billingLocality', '#billingPostalCode', '#billingAdministrativeArea'];
                const complete = required.every((selector) => {
                    const el = document.querySelector(selector);
                    return el && String(el.value || '').trim();
                });
                const visibleError = Array.from(document.querySelectorAll('[aria-invalid="true"], .is-invalid, .invalid, [data-invalid="true"]'))
                    .some((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    });
                const taxPending = /输入地址以计算|Enter address to calculate|Calculating|正在计算/i.test(text);
                const hasTaxError = /无法计算|unable to calculate|can't calculate|cannot calculate/i.test(text);
                const dueReady = /今日应付合计\\s*IDR\\s*0(?:[,.]00)?|Total due today\\s*IDR\\s*0(?:[,.]00)?/i.test(text)
                    || /IDR\\s*0(?:[,.]00)?/.test(text);
                return complete && dueReady && !visibleError && !taxPending && !hasTaxError;
            }"""
        )
    )


async def human_fill(locator, value: str) -> None:
    await locator.scroll_into_view_if_needed()
    await locator.click(force=True)
    await locator.page.keyboard.press("Control+A")
    await locator.page.keyboard.press("Backspace")
    await locator.page.keyboard.type(value, delay=25)


async def fill_if_empty(page, selector: str, value: str) -> None:
    locator = page.locator(selector)
    if await locator.count() <= 0:
        return
    current = await locator.input_value()
    if not current.strip():
        await human_fill(locator, value)


def normalize_country_code(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "+62"
    return raw if raw.startswith("+") else f"+{raw}"


def normalize_phone(value: str) -> str:
    phone = re.sub(r"\D+", "", value or "")
    if not phone:
        raise RuntimeError("--phone 不能为空")
    if phone.startswith("86") and len(phone) > 11:
        phone = phone[2:]
    return phone


def short_status(value: dict[str, Any] | None) -> str:
    if not isinstance(value, dict):
        return str(value)
    parts = []
    for key in ["status", "ok", "success", "returned", "url"]:
        if key in value:
            shown = compact_url(value.get(key)) if key == "url" else value.get(key)
            parts.append(f"{key}={shown}")
    nested = value.get("statusAfterAuthorize") or value.get("statusAfterOtp") or value.get("statusAfterPin")
    if isinstance(nested, dict):
        parts.append(f"next={nested.get('status')}")
        if nested.get("url"):
            parts.append(f"nextUrl={compact_url(nested.get('url'))}")
    linking = value.get("linkingApi")
    if isinstance(linking, dict):
        parts.append(f"api={linking.get('statusCode')}")
        parts.append(f"apiUrl={linking.get('redirectUrl')}")
    if not parts and value.get("bodySample"):
        parts.append(f"body={str(value.get('bodySample'))[:120]}")
    return " | ".join(parts) if parts else str(value)


def compact_url(value: Any, limit: int = 96) -> Any:
    text = str(value or "")
    if not text.startswith(("http://", "https://")) or len(text) <= limit:
        return value
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(text)
        path = parsed.path or "/"
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        compact = f"{parsed.netloc}{path}{fragment}"
        return compact if len(compact) <= limit else compact[: limit - 1] + "…"
    except Exception:
        return text[: limit - 1] + "…"


def flow2_step_status(label: str, status: dict[str, Any] | None) -> str:
    if not isinstance(status, dict):
        return f"{label}: {status}"
    parts = []
    state = status.get("status")
    if state:
        parts.append(str(state))
    if status.get("success") is True:
        parts.append("success=True")
    if status.get("returned") is True:
        parts.append("returned=True")
    nested = status.get("statusAfterAuthorize") or status.get("statusAfterOtp") or status.get("statusAfterPin")
    if isinstance(nested, dict) and nested.get("status"):
        parts.append(f"next={nested.get('status')}")
    return f"{label}: {' | '.join(parts)}" if parts else label


def flow2_billing_summary(result: dict[str, Any], billing: Any) -> str:
    city = getattr(billing, "city", "") or result.get("billingLocality") or ""
    state = getattr(billing, "state", "") or result.get("billingAdministrativeArea") or ""
    postal = getattr(billing, "postal_code", "") or result.get("billingPostalCode") or ""
    location = " ".join(part for part in [city, state, postal] if part)
    return f"账单完成: taxError={bool(result.get('taxError'))} | dueReady={bool(result.get('dueReady'))} | {location}".rstrip()


def display_proxy(proxy: str | None) -> str:
    if not proxy:
        return "未设置"
    text = proxy.strip()
    if "@" in text:
        prefix, suffix = text.rsplit("@", 1)
        scheme = prefix.split("://", 1)[0] + "://" if "://" in prefix else ""
        return f"{scheme}***:***@{suffix}"
    return text


def pick_proxy_for_test(args: argparse.Namespace, cfg: dict) -> str | None:
    if args.proxy:
        return args.proxy
    env = load_env(".env")
    use_proxy = bool(args.use_proxy)
    if not use_proxy and args.use_proxy is None:
        use_proxy = env_bool(env.get("USE_PROXY"), default=bool(cfg.get("browser", {}).get("use_proxy", False)))
    if not use_proxy:
        return None
    proxy_file = args.proxy_file or env.get("PROXY_FILE") or cfg.get("browser", {}).get("proxy_file", "data/proxies/proxies.txt")
    pool = ProxyPool(proxy_file)
    if pool.count() <= 0:
        raise RuntimeError(f"代理已启用，但代理池为空: {proxy_file}")
    return pool.pick(1)


async def safe_screenshot(page, path: Path) -> None:
    if not should_save_flow2_screenshot(path):
        return
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


def log_saved_artifact(prefix: str, label: str, path: Path) -> None:
    if should_save_flow2_screenshot(path) or path.exists():
        log(f"{prefix} {label}已保存: {path}")


def should_save_flow2_success_artifacts() -> bool:
    env = load_env(".env")
    return env_bool(env.get("FLOW2_SAVE_SUCCESS_SCREENSHOTS"), default=False)


def should_save_flow2_html_artifact(name: str, result: dict[str, Any] | None = None) -> bool:
    env = load_env(".env")
    if env_bool(env.get("FLOW2_SAVE_HTML"), default=False):
        return True
    result = result or {}
    if name in {"checkout_due_today.html", "otp_wait_result.html", "phone_page_check.html"}:
        status = str(result.get("status") or "")
        if name == "checkout_due_today.html":
            return status not in {"zero", "nonzero"}
        if name == "otp_wait_result.html":
            return status in {"technical_error", "stuck_loading", "phone_rejected", "rate_limited", "waiting"}
        if name == "phone_page_check.html":
            return not bool(result.get("hasPhoneInput") or result.get("hasPhoneHint"))
    return False


def should_save_flow2_screenshot(path: Path) -> bool:
    if should_save_flow2_success_artifacts():
        return True
    name = path.name
    success_names = {
        "checkout_due_today.png",
        "after_submit.png",
        "after_linking_api.png",
        "otp_wait_result.png",
        "after_otp.png",
        "after_pin.png",
        "after_gopay_authorize.png",
        "gopay_before_phone.png",
        "after_phone.png",
        "after_pay_now.png",
        "after_iframe_bayar.png",
        "after_iframe_pin.png",
        "final_payment_success.png",
        "manual_payment_success.png",
        "after_linking_return.png",
        "after_linking_returned_to_midtrans.png",
        "last_billing.png",
    }
    if name in success_names:
        return False
    if re.fullmatch(r"billing_attempt_\d+\.png", name):
        return False
    return True


def should_check_history_after_pin_chatgpt_return(pin_filled: bool, url: str) -> bool:
    return bool(pin_filled and re.search(r"^https://chatgpt\.com(?:/|$)", url or "", flags=re.I))


def is_flow2_next_status(status: str | None) -> bool:
    return str(status or "") in {
        "otp",
        "pin_or_next",
        "gopay_authorize",
        "pay_now",
        "phone_rejected",
        "rate_limited",
        "technical_error",
        "stuck_loading",
        "success",
    }


async def probe_flow2_status(page) -> dict[str, Any]:
    candidates = [candidate for candidate in page.context.pages if not candidate.is_closed()]
    result: dict[str, Any] = {"status": "waiting", "url": page.url}
    for candidate in reversed(candidates):
        try:
            candidate_result = await inspect_gopay_candidate_page(candidate)
        except Exception as exc:
            candidate_result = {"status": "waiting", "url": candidate.url, "error": str(exc)}
        if candidate_result.get("status") != "waiting":
            return candidate_result
        if candidate_result.get("url") == page.url:
            result = candidate_result
    return result


async def wait_for_known_next_status(page, *, timeout_ms: int = 4000, interval_ms: int = 350) -> dict[str, Any]:
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_status: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < end_at:
        last_status = await probe_flow2_status(page)
        if is_flow2_next_status(last_status.get("status")):
            return last_status | {"earlyReady": True}
        await page.wait_for_timeout(interval_ms)
    return last_status | {"earlyReady": False}


async def wait_for_status_transition(
    page,
    *,
    timeout_ms: int,
    interval_ms: int = 400,
    ignored_statuses: set[str] | None = None,
) -> dict[str, Any]:
    ignored_statuses = ignored_statuses or set()
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_status: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < end_at:
        last_status = await probe_flow2_status(page)
        status = str(last_status.get("status") or "")
        if is_flow2_next_status(status) and status not in ignored_statuses:
            return last_status | {"earlyReady": True}
        await page.wait_for_timeout(interval_ms)
    return last_status | {"earlyReady": False}


async def wait_for_new_page_or_known_status(
    page,
    before_pages: list[Any],
    *,
    timeout_ms: int = 5000,
    interval_ms: int = 250,
    ignored_statuses: set[str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    context = page.context
    ignored_statuses = ignored_statuses or set()
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    active_page = page
    last_status: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < end_at:
        try:
            new_pages = [candidate for candidate in context.pages if candidate not in before_pages and not candidate.is_closed()]
            if new_pages:
                active_page = new_pages[-1]
        except Exception:
            pass
        try:
            last_status = await probe_flow2_status(active_page)
        except Exception as exc:
            last_status = {"status": "waiting", "url": getattr(active_page, "url", ""), "error": str(exc)}
        status = str(last_status.get("status") or "")
        if is_flow2_next_status(status) and status not in ignored_statuses:
            return active_page, last_status | {"earlyReady": True}
        await active_page.wait_for_timeout(interval_ms)
    return active_page, last_status | {"earlyReady": False}


async def inspect_checkout_due_today(page, out_dir: Path, timeout_seconds: int = 30) -> dict[str, Any]:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    evaluator = """() => {
        const collectText = () => {
            const parts = [];
            if (document.body?.innerText) parts.push(document.body.innerText);
            for (const frame of Array.from(document.querySelectorAll('iframe'))) {
                try {
                    const doc = frame.contentDocument;
                    if (doc?.body?.innerText) parts.push(doc.body.innerText);
                } catch {}
            }
            return parts.join(' ');
        };
        const text = collectText();
        const normalized = text.replace(/\\s+/g, ' ').trim();
        const amountRegex = /(?:IDR|US\\$|USD|SGD|S\\$|\\$|Rp)\\s*[-+]?\\s*[\\d.,]+(?:\\.\\d{2})?/gi;
        const amounts = Array.from(normalized.matchAll(amountRegex)).map((match) => match[0].replace(/\\s+/g, ' ').trim());
        const totalPatterns = [
            /(?:今日应付合计|今天应付合计|Total due today|Due today|Pay today)\\s*((?:IDR|US\\$|USD|SGD|S\\$|\\$|Rp)\\s*[-+]?\\s*[\\d.,]+(?:\\.\\d{2})?)/i,
            /((?:IDR|US\\$|USD|SGD|S\\$|\\$|Rp)\\s*[-+]?\\s*[\\d.,]+(?:\\.\\d{2})?)\\s*(?:今日应付合计|今天应付合计|Total due today|Due today|Pay today)/i
        ];
        let totalText = null;
        for (const pattern of totalPatterns) {
            const match = normalized.match(pattern);
            if (match) {
                totalText = (match[1] || match[0]).replace(/\\s+/g, ' ').trim();
                break;
            }
        }
        if (!totalText) {
            const markerMatch = normalized.match(/(?:今日应付合计|今天应付合计|Total due today|Due today|Pay today)/i);
            if (markerMatch) {
                const tail = normalized.slice(markerMatch.index, markerMatch.index + 240);
                const amountMatch = tail.match(amountRegex);
                if (amountMatch) totalText = amountMatch[amountMatch.length - 1].replace(/\\s+/g, ' ').trim();
            }
        }
        const checkoutReady = /OpenAI|ChatGPT|GoPay|支付方式|Payment method|Subscribe|订阅|今日应付|Total due today|Due today/i.test(normalized);
        const terminalComplete = /您已全部完成|您已经完成付款|本结账会话已超时|this checkout session has expired|checkout session has expired|payment session has expired|session expired|已完成付款|付款已完成|订单已完成/i.test(normalized);
        const couponFreeTrial = /1\\s*Month\\s*Free\\s*Trial|1\\s*个月优惠|100%|free trial|promo|coupon|优惠券|折扣/i.test(normalized);
        const zeroAmountSeen = amounts.some((amount) => /(?:^|\\s|[-])0(?:[,.]0+)?\\b/.test(amount.replace(/,/g, '')));
        const isZero = totalText ? /(?:^|\\s|[-])0(?:[,.]0+)?\\b/.test(totalText.replace(/,/g, '')) : false;
        return {
            ok: Boolean(totalText),
            status: totalText ? (isZero ? 'zero' : 'nonzero') : (terminalComplete ? 'terminal_complete' : 'unknown'),
            isZero,
            totalText,
            amounts,
            zeroAmountSeen,
            checkoutReady,
            terminalComplete,
            couponFreeTrial,
            url: location.href,
            bodySample: normalized.slice(0, 1200)
        };
    }"""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    result: dict[str, Any] = {"ok": False, "status": "unknown", "reason": "timeout"}
    stable_unknown_seen = 0
    while asyncio.get_event_loop().time() < deadline:
        try:
            result = await page.evaluate(evaluator)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "status": "unknown", "reason": str(exc), "url": page.url}
        if result.get("ok"):
            break
        if result.get("checkoutReady"):
            stable_unknown_seen += 1
        else:
            stable_unknown_seen = 0
        if stable_unknown_seen >= 8:
            result["reason"] = "checkout_loaded_but_due_not_found"
            break
        await page.wait_for_timeout(1000)
    await safe_screenshot(page, out_dir / "checkout_due_today.png")
    try:
        if should_save_flow2_html_artifact("checkout_due_today.html", result):
            (out_dir / "checkout_due_today.html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        pass
    (out_dir / "checkout_due_today.txt").write_text(str(result), encoding="utf-8")
    return result


async def wait_for_manual_payment_success(
    page,
    out_dir: Path,
    timeout_seconds: int,
    profile_dir: Path | None = None,
    pin: str | None = None,
) -> dict[str, Any]:
    started_at = asyncio.get_event_loop().time()
    deadline = started_at + timeout_seconds
    last_result: dict[str, Any] = {}
    payment_nudge_counts: dict[str, int] = {}
    while asyncio.get_event_loop().time() < deadline:
        try:
            candidates = [item for item in page.context.pages if not item.is_closed()]
        except Exception:
            candidates = [page]
        for candidate in reversed(candidates):
            try:
                text = await candidate.locator("body").inner_text(timeout=1500)
            except Exception:
                text = ""
            frame_texts: list[str] = []
            for frame in candidate.frames:
                if frame is candidate.main_frame:
                    continue
                try:
                    frame_texts.append(await frame.locator("body").inner_text(timeout=700))
                except Exception:
                    continue
            if frame_texts:
                text = " ".join([text, *frame_texts])
            url = candidate.url or ""
            linking_only = bool(
                "merchants-gws-app.gopayapi.com/linking/" in url
                or re.search(r"berhasil menghubungkan|hubungin gopay|linking/success|kembali ke openai llc", text, re.I)
            )
            payment_success_text = bool(
                re.search(
                    r"payment successful|pembayaran berhasil|payment complete|paid successfully|subscription active|订阅成功|付款成功|支付成功|管理订阅|manage subscription",
                    text,
                    re.I,
                )
            )
            payment_success_url = bool(
                ("chatgpt.com/payments/success" in url)
                or ("pay.openai.com/" in url and "redirect_status=succeeded" in url)
                or ("app.midtrans.com/snap/v4/redirection/" in url and "#/success" in url)
                or ("app.midtrans.com/snap/v3/callback/gopay/charge/" in url and "success=true" in url)
                or ("merchants-gws-app.gopayapi.com/payment/success" in url)
            )
            gopay_proceed_failed = bool(re.search(r"failed to proceed to gopay|please place your order again", text, re.I))
            chatgpt_subscription_success = bool(
                "chatgpt.com" in url
                and (
                    "chatgpt.com/payments/success" in url
                    or re.search(r"chatgpt plus|plus subscription|manage subscription|管理订阅|subscription active|订阅成功", text, re.I)
                )
            )
            success_hint = bool(
                not linking_only
                and (payment_success_url or payment_success_text or chatgpt_subscription_success)
            )
            last_result = {
                "url": url,
                "title": await candidate.title() if not candidate.is_closed() else "",
                "success": success_hint,
                "gopayProceedFailed": gopay_proceed_failed,
                "linkingOnly": linking_only,
                "elapsedSeconds": round(asyncio.get_event_loop().time() - started_at, 1),
                "bodySample": re.sub(r"\s+", " ", text)[:800],
            }
            if gopay_proceed_failed:
                await safe_screenshot(candidate, out_dir / "gopay_proceed_failed.png")
                (out_dir / "manual_payment_success.txt").write_text(str(last_result), encoding="utf-8")
                return last_result | {"success": False, "error_type": "gopay_proceed_failed"}
            if success_hint:
                await safe_screenshot(candidate, out_dir / "manual_payment_success.png")
                (out_dir / "manual_payment_success.txt").write_text(str(last_result), encoding="utf-8")
                return last_result
            await maybe_nudge_midtrans_payment_page(candidate, out_dir, payment_nudge_counts, pin=pin)
        await asyncio.sleep(3)
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if profile_dir and "chatgpt.com/" in current_url:
            log("检测到支付后已跳回 chatgpt.com，立即用浏览器历史确认支付结果")
            history_result = recover_payment_success_from_history(profile_dir, out_dir)
            if history_result.get("success"):
                return history_result
    await safe_screenshot(await current_midtrans_page(page), out_dir / "manual_payment_timeout.png")
    (out_dir / "manual_payment_success.txt").write_text(str(last_result), encoding="utf-8")
    return last_result | {"success": False, "timeout": True}


async def maybe_nudge_midtrans_payment_page(
    page,
    out_dir: Path,
    nudge_counts: dict[str, int],
    *,
    pin: str | None = None,
    max_nudges: int = 3,
) -> bool:
    try:
        page = await current_midtrans_page(page)
        url = page.url or ""
    except Exception:
        return False
    is_midtrans_redirection = "app.midtrans.com/snap/v4/redirection/" in url
    is_payment_page = "#/gopay-tokenization/pay" in url
    is_linking_page = "#/gopay-tokenization/linking" in url
    if not is_midtrans_redirection or not (is_payment_page or is_linking_page):
        return False
    count = nudge_counts.get(url, 0)
    if count >= max_nudges:
        return False

    state = await inspect_midtrans_payment_state(page)
    if state.get("success") or state.get("gopayProceedFailed"):
        return False
    is_blank_linking = is_linking_page and state.get("bodyLength", 0) < 80 and state.get("frameCount", 0) <= 0
    if not is_blank_linking and not state.get("hasPayNow") and state.get("frameCount", 0) <= 0:
        return False

    nudge_counts[url] = count + 1
    attempt = nudge_counts[url]
    if is_blank_linking:
        log(f"Midtrans GoPay linking 页空白/未加载，自动刷新恢复第 {attempt}/{max_nudges} 次")
        await safe_screenshot(page, out_dir / f"manual_payment_nudge_before_{attempt}.png")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                await page.evaluate("() => location.reload()")
            except Exception:
                pass
        await page.wait_for_timeout(1800)
    else:
        log(f"Midtrans 支付页仍停留在待付款状态，自动推进第 {attempt}/{max_nudges} 次")
        await safe_screenshot(page, out_dir / f"manual_payment_nudge_before_{attempt}.png")

    clicked_any = False
    try:
        selector = await click_first_enabled_visible_anywhere(
            page,
            [
                "button:has-text('Refresh')",
                "button:has-text('Link and pay')",
                "button:has-text('Link & pay')",
                "button:has-text('Link')",
                *GOPAY_ACTION_BUTTON_SELECTORS,
            ],
            timeout_ms=1400,
        )
        clicked_any = True
        log(f"已自动点击 Midtrans 支付页按钮: {selector}")
        await page.wait_for_timeout(600)
    except Exception:
        pass

    bayar_locator, bayar_selector, bayar_page = await find_visible_locator_anywhere(
        page,
        GOPAY_CONFIRM_BUTTON_SELECTORS,
        timeout_ms=900,
    )
    if bayar_locator is not None:
        try:
            await click_locator_like_user(bayar_locator, bayar_page)
            clicked_any = True
            log(f"已自动点击 iframe 内付款确认按钮: {bayar_selector}")
            await page.wait_for_timeout(500)
        except Exception as exc:
            log(f"自动点击 iframe 内付款确认按钮失败: {exc}")

    pin_value = re.sub(r"\D+", "", pin or "")
    if len(pin_value) == 6:
        try:
            pin_fill_result = await fill_gopay_pin_like_user(page, pin_value, timeout_ms=1200)
            if not pin_fill_result.get("ok"):
                raise RuntimeError(str(pin_fill_result))
            await press_enter_and_confirm_pin(page)
            clicked_any = True
            log(f"已自动补填 GoPay 付款 PIN: {pin_value[:2]}*** | {pin_fill_result.get('mode')}")
            await page.wait_for_timeout(800)
        except Exception as exc:
            log(f"自动补填 GoPay 付款 PIN 失败: {exc}")
    else:
        pin_locator, pin_selector, _pin_page = await find_visible_locator_anywhere(
            page,
            GOPAY_PIN_INPUT_SELECTORS,
            timeout_ms=300,
        )
        if pin_locator is not None:
            log(f"Midtrans 最终付款需要 PIN，但当前未配置 6 位 PIN: {pin_selector}")

    if clicked_any:
        await safe_screenshot(page, out_dir / f"manual_payment_nudge_after_{attempt}.png")
    return clicked_any


async def find_visible_frame_locator(page, selectors: list[str], timeout_ms: int = 12000):
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < end_at:
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            for selector in selectors:
                locator = frame.locator(selector).first
                try:
                    if await locator.count() > 0 and await locator.is_visible(timeout=500):
                        return locator, selector
                except Exception:
                    continue
        await page.wait_for_timeout(500)
    return None, None


async def click_locator_like_user(locator, page) -> None:
    try:
        await locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    try:
        box = await locator.bounding_box()
        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return
    except Exception:
        pass
    try:
        await locator.click(timeout=3000)
        return
    except Exception:
        await locator.click(force=True, timeout=3000)


async def find_visible_locator_anywhere(page, selectors: list[str], timeout_ms: int = 12000):
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < end_at:
        for candidate in ordered_payment_pages(page):
            scopes = [candidate, *[frame for frame in candidate.frames if frame is not candidate.main_frame]]
            for scope in scopes:
                for selector in selectors:
                    locator = scope.locator(selector).first
                    try:
                        if await locator.count() > 0 and await locator.is_visible(timeout=300):
                            return locator, selector, candidate
                    except Exception:
                        continue
        await page.wait_for_timeout(250)
    return None, None, page


async def click_first_enabled_visible_anywhere(page, selectors: list[str], timeout_ms: int = 2500) -> str:
    locator, selector, candidate = await find_visible_locator_anywhere(page, selectors, timeout_ms=timeout_ms)
    if locator is None or not selector:
        raise RuntimeError(f"未找到可点击按钮: {selectors}")
    try:
        disabled = await locator.evaluate(
            """(el) => Boolean(el.disabled)
                || el.getAttribute('aria-disabled') === 'true'
                || el.classList.contains('disabled')"""
        )
        if disabled:
            raise RuntimeError(f"{selector}: disabled")
    except Exception as exc:
        if "disabled" in str(exc):
            raise
    await click_locator_like_user(locator, candidate)
    return selector


def payment_page_rank(candidate, preferred_url: str = "") -> int:
    try:
        url = candidate.url or ""
    except Exception:
        url = ""
    if preferred_url and url == preferred_url:
        return 0
    if "app.midtrans.com" in url and "#/gopay-tokenization/pay" in url:
        return 1
    if "pin-web-client.gopayapi.com" in url or "gopayapi.com/auth/pin" in url:
        return 2
    if "app.midtrans.com" in url:
        return 3
    if "gopayapi.com" in url:
        return 4
    return 99


def ordered_payment_pages(page) -> list[Any]:
    try:
        pages = [candidate for candidate in page.context.pages if not candidate.is_closed()]
    except Exception:
        pages = [page]
    preferred_url = ""
    try:
        preferred_url = page.url or ""
    except Exception:
        pass
    pages.sort(key=lambda candidate: payment_page_rank(candidate, preferred_url))
    payment_pages = [candidate for candidate in pages if payment_page_rank(candidate, preferred_url) < 99]
    return payment_pages or pages


async def fill_code_via_keyboard(locator, page, code: str) -> None:
    await click_locator_like_user(locator, page)
    try:
        await locator.press("Control+A")
        await locator.press("Backspace")
    except Exception:
        pass
    await page.keyboard.type(code, delay=80)
    await page.wait_for_timeout(400)
    try:
        await locator.press("Enter")
    except Exception:
        await page.keyboard.press("Enter")


async def click_gopay_pin_entry_area(scope, page) -> dict[str, Any]:
    target = await scope.evaluate(
        """() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
            if (!/PIN kamu|Masukkin PIN|Masukkan PIN|ketik 6 digit PIN|payment pin|PIN GoPay/i.test(text)) {
                return { ok: false, reason: 'no_pin_hint' };
            }
            const inputs = Array.from(document.querySelectorAll('input')).filter((el) => {
                const type = (el.getAttribute('type') || '').toLowerCase();
                const haystack = [el.id, el.name, el.placeholder, el.getAttribute('aria-label'), el.autocomplete, el.inputMode, type].join(' ').toLowerCase();
                return !['file', 'radio', 'checkbox', 'hidden', 'submit'].includes(type)
                    && (type === 'password' || type === 'tel' || type === 'text' || /pin|password|one-time-code|numeric/.test(haystack));
            });
            const input = inputs.find(visible) || inputs[0];
            if (input) {
                input.focus();
                input.click();
                const rect = input.getBoundingClientRect();
                return { ok: true, mode: 'input', x: rect.left + Math.max(8, Math.min(rect.width / 2, 80)), y: rect.top + Math.max(8, rect.height / 2) };
            }
            const label = Array.from(document.querySelectorAll('label, div, span, p'))
                .filter(visible)
                .find((el) => /PIN kamu|PIN|payment pin/i.test((el.textContent || '').trim()));
            const rect = (label || document.body).getBoundingClientRect();
            return {
                ok: true,
                mode: label ? 'label_anchor' : 'body_anchor',
                x: rect.left + 12,
                y: label ? rect.bottom + 34 : 260
            };
        }"""
    )
    if not target.get("ok"):
        return target
    try:
        await scope.locator("body").click(
            position={"x": max(1, float(target.get("x") or 1)), "y": max(1, float(target.get("y") or 1))},
            timeout=2500,
            force=True,
        )
    except Exception:
        try:
            await page.mouse.click(max(1, float(target.get("x") or 1)), max(1, float(target.get("y") or 1)))
        except Exception as exc:
            target["clickError"] = str(exc)
    return target


async def fill_gopay_pin_like_user(page, pin: str, *, timeout_ms: int = 2500) -> dict[str, Any]:
    pin = re.sub(r"\D+", "", pin or "")
    if len(pin) != 6:
        raise RuntimeError("支付 PIN 必须是 6 位数字")

    locator, selector, pin_page = await find_visible_locator_anywhere(page, GOPAY_PIN_INPUT_SELECTORS, timeout_ms=timeout_ms)
    if locator is not None:
        await fill_code_via_keyboard(locator, pin_page, pin)
        return {"ok": True, "mode": "locator_keyboard", "selector": selector, "url": pin_page.url}

    last_result: dict[str, Any] = {"ok": False, "reason": "pin_page_not_found"}
    scanned_urls: list[str] = []
    for candidate in ordered_payment_pages(page):
        try:
            scanned_urls.append(candidate.url or "")
        except Exception:
            pass
        scopes = [candidate, *[frame for frame in candidate.frames if frame is not candidate.main_frame]]
        for scope in scopes:
            try:
                target = await click_gopay_pin_entry_area(scope, candidate)
            except Exception as exc:
                last_result = {"ok": False, "reason": str(exc), "url": candidate.url}
                continue
            if not target.get("ok"):
                last_result = target | {"url": candidate.url}
                continue
            await candidate.keyboard.type(pin, delay=90)
            await candidate.wait_for_timeout(500)
            try:
                await candidate.keyboard.press("Enter")
            except Exception:
                pass
            return {"ok": True, "mode": target.get("mode"), "target": target, "url": candidate.url}
    last_result["scannedUrls"] = scanned_urls[:6]
    return last_result


async def wait_for_pin_hint_or_return(page, *, timeout_ms: int = 3500) -> dict[str, Any]:
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_state: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < end_at:
        try:
            candidate = await current_gopay_pin_page(page)
            state = await inspect_midtrans_payment_state(candidate)
        except Exception:
            state = {}
        last_state = state
        url = str(state.get("url") or "")
        body = str(state.get("bodySample") or "")
        if should_check_history_after_pin_chatgpt_return(False, url):
            return state | {"ready": True, "reason": "chatgpt_returned"}
        if (
            "pin-web-client.gopayapi.com" in url
            or "gopayapi.com/auth/pin" in url
            or re.search(r"Masukkin PIN|Masukkan PIN|PIN kamu|ketik 6 digit PIN|payment pin|PIN GoPay", body, re.I)
        ):
            return state | {"ready": True, "reason": "pin_hint"}
        await page.wait_for_timeout(250)
    return last_state | {"ready": False, "reason": "pin_hint_timeout"}


async def press_enter_and_confirm_pin(page) -> None:
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass
    for selector in GOPAY_CONFIRM_BUTTON_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible(timeout=300):
                disabled = await locator.evaluate(
                    """(el) => Boolean(el.disabled)
                        || el.getAttribute('aria-disabled') === 'true'
                        || el.classList.contains('disabled')"""
                )
                if not disabled and await locator.is_enabled():
                    await click_locator_like_user(locator, page)
                    return
        except Exception:
            continue
    locator, _selector = await find_visible_frame_locator(page, GOPAY_CONFIRM_BUTTON_SELECTORS, timeout_ms=600)
    if locator is not None:
        try:
            await click_locator_like_user(locator, page)
        except Exception:
            pass


async def inspect_midtrans_payment_state(page) -> dict[str, Any]:
    try:
        if page.is_closed() or "app.midtrans.com" not in (page.url or ""):
            page = await current_midtrans_page(page)
    except Exception:
        page = await current_midtrans_page(page)
    try:
        text = await page.locator("body").inner_text(timeout=1500)
    except Exception:
        text = ""
    frame_texts: list[str] = []
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            frame_texts.append(await frame.locator("body").inner_text(timeout=700))
        except Exception:
            continue
    if frame_texts:
        text = " ".join([text, *frame_texts])
    normalized = re.sub(r"\s+", " ", text)
    url = page.url or ""
    return {
        "url": url,
        "title": await page.title() if not page.is_closed() else "",
            "success": bool(
                "chatgpt.com/payments/success" in url
                or ("pay.openai.com/" in url and "redirect_status=succeeded" in url)
                or ("app.midtrans.com/snap/v4/redirection/" in url and "#/success" in url)
                or ("app.midtrans.com/snap/v3/callback/gopay/charge/" in url and "success=true" in url)
                or ("merchants-gws-app.gopayapi.com/payment/success" in url)
                or re.search(
                    r"payment successful|pembayaran berhasil|kamu bakal diarahin|kembali ke openai llc|payment complete|paid successfully|付款成功|支付成功",
                    normalized,
                    re.I,
                )
            ),
        "gopayProceedFailed": bool(re.search(r"failed to proceed to gopay|please place your order again", normalized, re.I)),
        "hasPayNow": bool(re.search(r"link and pay|\bpay now\b|bayar|continue|lanjut", normalized, re.I)),
        "frameCount": max(0, len(page.frames) - 1),
        "bodyLength": len(normalized),
        "isTokenizationLinking": bool("app.midtrans.com/snap/v4/redirection/" in url and "#/gopay-tokenization/linking" in url),
        "bodySample": normalized[:800],
    }


async def page_loading_signature(page) -> dict[str, Any]:
    try:
        text = await page.locator("body").inner_text(timeout=1000)
    except Exception:
        text = ""
    normalized = re.sub(r"\s+", " ", text).strip()
    url = page.url or ""
    title = ""
    try:
        title = await page.title() if not page.is_closed() else ""
    except Exception:
        pass
    is_gopay_blank = bool(
        "merchants-gws-app.gopayapi.com/linking/otp" in url
        and len(normalized) < 40
        and re.search(r"gopay", title, re.I)
    )
    is_midtrans_tokenization_blank = bool(
        "app.midtrans.com/snap/v4/redirection/" in url
        and "gopay-tokenization/linking" in url
        and len(normalized) < 80
    )
    return {
        "url": url,
        "title": title,
        "bodyLength": len(normalized),
        "bodySample": normalized[:200],
        "isStuckLoading": is_gopay_blank or is_midtrans_tokenization_blank,
        "kind": "gopay_otp_blank" if is_gopay_blank else ("midtrans_tokenization_blank" if is_midtrans_tokenization_blank else ""),
    }


async def maybe_reload_stuck_payment_page(page, out_dir: Path, reload_counts: dict[str, int], *, max_reloads: int = 2) -> bool:
    try:
        signature = await page_loading_signature(page)
    except Exception:
        return False
    if not signature.get("isStuckLoading"):
        return False
    url = str(signature.get("url") or "")
    pending_key = f"{url}::pending"
    pending_count = reload_counts.get(pending_key, 0) + 1
    reload_counts[pending_key] = pending_count
    if pending_count < 2:
        return False
    count = reload_counts.get(url, 0)
    if count >= max_reloads:
        return False
    reload_counts[pending_key] = 0
    reload_counts[url] = count + 1
    log(
        "检测到 GoPay/Midtrans 页面疑似空白转圈，"
        f"自动刷新 {reload_counts[url]}/{max_reloads}: {signature.get('kind')}"
    )
    try:
        await safe_screenshot(page, out_dir / f"stuck_loading_before_reload_{reload_counts[url]}.png")
    except Exception:
        pass
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception:
        try:
            await page.evaluate("() => location.reload()")
        except Exception:
            return False
    await page.wait_for_timeout(3500)
    return True


async def stuck_payment_page_status(page, reload_counts: dict[str, int], *, max_reloads: int = 2) -> dict[str, Any] | None:
    try:
        signature = await page_loading_signature(page)
    except Exception:
        return None
    if not signature.get("isStuckLoading"):
        return None
    url = str(signature.get("url") or "")
    if reload_counts.get(url, 0) < max_reloads:
        return None
    return {
        "status": "stuck_loading",
        "url": url,
        "title": signature.get("title", ""),
        "kind": signature.get("kind", ""),
        "reloads": reload_counts.get(url, 0),
        "bodySample": signature.get("bodySample", ""),
    }


async def handle_midtrans_final_payment(
    page,
    out_dir: Path,
    pin: str | None = None,
    prompt_pin: bool = False,
    timeout_seconds: int = 40,
    profile_dir: Path | None = None,
) -> dict[str, Any]:
    page = await current_midtrans_page(page)
    payment_url = page.url or ""
    await page.wait_for_timeout(300)
    clicked_pay_now = None
    nudge_counts: dict[str, int] = {}
    try:
        clicked_pay_now = await click_first_enabled_visible_anywhere(
            page,
            GOPAY_ACTION_BUTTON_SELECTORS,
            timeout_ms=2200,
        )
        log(f"已点击 Midtrans 最终支付按钮: {clicked_pay_now}")
    except Exception as exc:
        state = await inspect_midtrans_payment_state(page)
        if await maybe_nudge_midtrans_payment_page(page, out_dir, nudge_counts, pin=pin):
            await page.wait_for_timeout(700)
            try:
                clicked_pay_now = await click_first_enabled_visible_anywhere(
                    page,
                    GOPAY_ACTION_BUTTON_SELECTORS,
                    timeout_ms=2200,
                )
                log(f"Midtrans 恢复后已点击最终支付按钮: {clicked_pay_now}")
            except Exception as retry_exc:
                log(f"Midtrans 自动恢复后仍未找到最终支付按钮，转入人工等待: {retry_exc}")
                state = await inspect_midtrans_payment_state(page)
                state.update({"ok": False, "status": "pay_button_not_found", "clickedPayNow": clicked_pay_now})
                (out_dir / "last_final_payment_result.txt").write_text(str(state), encoding="utf-8")
                await safe_screenshot(page, out_dir / "final_pay_button_not_found.png")
                return state
        else:
            log(f"未找到 Midtrans 最终支付按钮，转入人工等待: {exc}")
            state.update({"ok": False, "status": "pay_button_not_found", "clickedPayNow": clicked_pay_now})
            (out_dir / "last_final_payment_result.txt").write_text(str(state), encoding="utf-8")
            await safe_screenshot(page, out_dir / "final_pay_button_not_found.png")
            return state

    await page.wait_for_timeout(600)
    await safe_screenshot(page, out_dir / "after_pay_now.png")
    started_at = asyncio.get_event_loop().time()
    last_progress_at = started_at
    clicked_bayar = None
    pin_filled = False
    iframe_empty_seen = 0

    while asyncio.get_event_loop().time() - started_at < timeout_seconds:
        try:
            if page.is_closed():
                page = await current_midtrans_page(page)
        except Exception:
            page = await current_midtrans_page(page)
        state = await inspect_midtrans_payment_state(page)
        state_url = str(state.get("url") or "")
        if should_check_history_after_pin_chatgpt_return(pin_filled, state_url):
            result = state | {
                "ok": False,
                "status": "chatgpt_returned_after_pin",
                "clickedPayNow": clicked_pay_now,
                "clickedBayar": clicked_bayar,
                "pinFilled": pin_filled,
            }
            (out_dir / "last_final_payment_result.txt").write_text(str(result), encoding="utf-8")
            if profile_dir:
                log("支付 PIN 后页面已跳回 chatgpt.com，立即用浏览器历史确认支付结果")
                history_result = recover_payment_success_from_history(profile_dir, out_dir)
                if history_result.get("success"):
                    success_result = history_result | {
                        "ok": True,
                        "status": "success",
                        "trigger": "chatgpt_return_after_pin",
                        "clickedPayNow": clicked_pay_now,
                        "clickedBayar": clicked_bayar,
                        "pinFilled": pin_filled,
                    }
                    (out_dir / "last_final_payment_result.txt").write_text(str(success_result), encoding="utf-8")
                    return success_result
                result["historyResult"] = history_result
                (out_dir / "last_final_payment_result.txt").write_text(str(result), encoding="utf-8")
            log("支付 PIN 后页面已跳回 chatgpt.com，停止继续找 PIN，交给外层成功兜底")
            return result
        if payment_url and state_url != payment_url and "chatgpt.com/" in state_url:
            state["url"] = payment_url
        state.update({"clickedPayNow": clicked_pay_now, "clickedBayar": clicked_bayar, "pinFilled": pin_filled})
        if state.get("success"):
            await safe_screenshot(page, out_dir / "final_payment_success.png")
            (out_dir / "last_final_payment_result.txt").write_text(str(state | {"ok": True, "status": "success"}), encoding="utf-8")
            return state | {"ok": True, "status": "success"}
        if state.get("gopayProceedFailed"):
            await safe_screenshot(page, out_dir / "gopay_proceed_failed.png")
            (out_dir / "last_final_payment_result.txt").write_text(str(state | {"ok": False, "status": "gopay_proceed_failed"}), encoding="utf-8")
            return state | {"ok": False, "status": "gopay_proceed_failed"}
        if (
            clicked_bayar
            and not pin_filled
            and asyncio.get_event_loop().time() - last_progress_at > 18
            and "#/gopay-tokenization/pay" in str(state.get("url") or "")
        ):
            log("iframe 内付款确认后长时间未出现 PIN，判定钱包加载失败，快速结束该账号")
            state.update({"ok": False, "status": "wallet_pin_not_loaded"})
            await safe_screenshot(page, out_dir / "final_wallet_pin_not_loaded.png")
            (out_dir / "last_final_payment_result.txt").write_text(str(state), encoding="utf-8")
            return state
        if (
            "#/gopay-tokenization/pay" in str(state.get("url") or "")
            and state.get("hasPayNow")
            and state.get("frameCount", 0) >= 1
            and state.get("bodyLength", 0) <= 180
            and asyncio.get_event_loop().time() - last_progress_at > 4
        ):
            iframe_empty_seen += 1
            log(f"Midtrans GoPay 钱包 iframe 空白/转圈，刷新重试第 {iframe_empty_seen}/2 次")
            await safe_screenshot(page, out_dir / f"final_wallet_blank_{iframe_empty_seen}.png")
            if iframe_empty_seen > 2:
                state.update({"ok": False, "status": "wallet_blank_timeout"})
                (out_dir / "last_final_payment_result.txt").write_text(str(state), encoding="utf-8")
                return state
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                try:
                    await page.evaluate("() => location.reload()")
                except Exception:
                    pass
            await page.wait_for_timeout(1800)
            clicked_bayar = None
            last_progress_at = asyncio.get_event_loop().time()
            continue

        if not clicked_bayar:
            bayar_locator, bayar_selector, bayar_page = await find_visible_locator_anywhere(
                page,
                GOPAY_CONFIRM_BUTTON_SELECTORS,
                timeout_ms=900,
            )
            if bayar_locator is not None:
                try:
                    await click_locator_like_user(bayar_locator, bayar_page)
                    clicked_bayar = bayar_selector
                    log(f"已点击 iframe 内最终确认按钮: {bayar_selector}")
                    last_progress_at = asyncio.get_event_loop().time()
                    await page.wait_for_timeout(350)
                    if re.sub(r"\D+", "", pin or ""):
                        pin_ready = await wait_for_pin_hint_or_return(page, timeout_ms=3500)
                        if pin_ready.get("ready"):
                            log(f"GoPay 付款确认后检测到下一步: {pin_ready.get('reason')}")
                    await safe_screenshot(page, out_dir / "after_iframe_bayar.png")
                    continue
                except Exception as exc:
                    log(f"点击 iframe 内最终确认按钮失败: {exc}")

        pin_value = re.sub(r"\D+", "", pin or "")
        if not pin_filled:
            if not pin_value and prompt_pin:
                pin_value = re.sub(r"\D+", "", await asyncio.to_thread(input, "请输入 6 位 GoPay 付款 PIN，直接回车则转人工等待："))
            pin_hint_visible = bool(re.search(r"Masukkin PIN|Masukkan PIN|PIN kamu|ketik 6 digit PIN|payment pin|PIN GoPay", str(state.get("bodySample") or ""), re.I))
            if len(pin_value) == 6 and (pin_hint_visible or clicked_bayar):
                if clicked_bayar and not pin_hint_visible:
                    pin_ready = await wait_for_pin_hint_or_return(page, timeout_ms=2500)
                    if pin_ready.get("ready"):
                        state = pin_ready
                        pin_hint_visible = pin_ready.get("reason") == "pin_hint"
                pin_fill_result = await fill_gopay_pin_like_user(page, pin_value, timeout_ms=2200 if clicked_bayar else 1200)
                if not pin_fill_result.get("ok"):
                    if not pin_hint_visible:
                        await page.wait_for_timeout(350)
                        continue
                    else:
                        log(f"检测到 GoPay PIN 页面但自动输入失败: {short_status(pin_fill_result)}")
                        await page.wait_for_timeout(350)
                        continue
                else:
                    await press_enter_and_confirm_pin(page)
                    pin_filled = True
                    log(f"已填入 GoPay 付款 PIN: {pin_value[:2]}*** | {pin_fill_result.get('mode')}")
                    started_at = asyncio.get_event_loop().time()
                    last_progress_at = started_at
                    await page.wait_for_timeout(500)
                    await safe_screenshot(page, out_dir / "after_iframe_pin.png")
                    continue
            pin_locator, pin_selector, _pin_page = await find_visible_locator_anywhere(
                page,
                GOPAY_PIN_INPUT_SELECTORS,
                timeout_ms=300,
            )
            if pin_locator is not None or pin_hint_visible:
                state.update({"ok": False, "status": "pin_required", "pinSelector": pin_selector or "pin_text_hint"})
                await safe_screenshot(page, out_dir / "final_payment_pin_required.png")
                (out_dir / "last_final_payment_result.txt").write_text(str(state), encoding="utf-8")
                return state

        if await maybe_nudge_midtrans_payment_page(page, out_dir, nudge_counts, pin=pin):
            last_progress_at = asyncio.get_event_loop().time()
            if pin_filled:
                started_at = last_progress_at
            await page.wait_for_timeout(350)
            continue

        await page.wait_for_timeout(300)

    state = await inspect_midtrans_payment_state(page)
    state.update({"ok": False, "status": "timeout", "clickedPayNow": clicked_pay_now, "clickedBayar": clicked_bayar, "pinFilled": pin_filled})
    await safe_screenshot(page, out_dir / "final_payment_timeout.png")
    (out_dir / "last_final_payment_result.txt").write_text(str(state), encoding="utf-8")
    return state


async def recover_final_payment_after_linking_return_timeout(
    page,
    out_dir: Path,
    *,
    original_midtrans_url: str | None,
    pin: str | None = None,
    prompt_pin: bool = False,
    profile_dir: Path | None = None,
    timeout_seconds: int = 18,
) -> dict[str, Any]:
    return_url = choose_gopay_return_url(original_midtrans_url, None)
    result: dict[str, Any] = {
        "ok": False,
        "success": False,
        "status": "linking_return_timeout_recovery_skipped",
        "returnUrl": return_url or "",
    }
    if not return_url:
        return result

    deadline = asyncio.get_event_loop().time() + max(6, int(timeout_seconds or 18))
    try:
        page = await current_midtrans_page(page)
        await page.goto(return_url, wait_until="domcontentloaded", timeout=9000)
        await page.wait_for_timeout(1200)
        remaining = max(6, int(deadline - asyncio.get_event_loop().time()))
        final_result = await handle_midtrans_final_payment(
            page,
            out_dir,
            pin=pin,
            prompt_pin=prompt_pin,
            timeout_seconds=min(remaining, 12),
            profile_dir=profile_dir,
        )
        final_result.update({
            "recoveredAfterLinkingReturnTimeout": True,
            "recoveryReturnUrl": return_url,
        })
        return final_result
    except Exception as exc:
        result.update({
            "status": "linking_return_timeout_recovery_failed",
            "error": str(exc),
        })
        try:
            await safe_screenshot(page, out_dir / "final_payment_return_timeout_recovery_failed.png")
        except Exception:
            pass
        if profile_dir:
            history_result = recover_payment_success_from_history(profile_dir, out_dir)
            if history_result.get("success"):
                return history_result | {
                    "ok": True,
                    "status": "success",
                    "recoveredAfterLinkingReturnTimeout": True,
                    "trigger": "history_after_return_timeout",
                }
        return result


def recover_payment_success_from_history(profile_dir: Path, out_dir: Path) -> dict[str, Any]:
    history_path = profile_dir / "Default" / "History"
    if not history_path.exists():
        return {"success": False, "reason": "history_not_found"}

    out_dir.mkdir(parents=True, exist_ok=True)
    copy_path = out_dir / "History.copy"
    try:
        shutil.copy2(history_path, copy_path)
        con = sqlite3.connect(str(copy_path))
        try:
            rows = con.execute(
                """
                select url, coalesce(title, '') from urls
                where url like '%midtrans%'
                   or url like '%gopay%'
                   or url like '%pay.openai%'
                   or url like '%chatgpt.com/payments/success%'
                order by last_visit_time desc
                limit 80
                """
            ).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "reason": f"history_read_failed: {exc}"}

    evidence: list[dict[str, str]] = []
    for url, title in rows:
        url = str(url or "")
        title = str(title or "")
        is_payment_success = bool(
            ("app.midtrans.com/snap/v4/redirection/" in url and "#/success" in url)
            or ("app.midtrans.com/snap/v3/callback/gopay/charge/" in url and "success=true" in url)
            or ("merchants-gws-app.gopayapi.com/payment/success" in url)
            or ("pay.openai.com/" in url and "redirect_status=succeeded" in url)
            or ("chatgpt.com/payments/success" in url)
        )
        is_linking_only = bool("linking/success" in url or "linking" in url and "payment/success" not in url)
        if is_payment_success and not is_linking_only:
            evidence.append({"title": title[:120], "url": url})

    result = {
        "success": bool(evidence),
        "source": "chrome_history",
        "evidence": evidence[:5],
    }
    (out_dir / "history_payment_success.txt").write_text(str(result), encoding="utf-8")
    return result


async def leave_gopay_linking_success(
    page,
    out_dir: Path,
    timeout_seconds: int = 35,
    transaction_uuid: str | None = None,
    original_midtrans_url: str | None = None,
    pin: str | None = None,
) -> dict[str, Any]:
    started_at = asyncio.get_event_loop().time()
    deadline = started_at + timeout_seconds
    last_result: dict[str, Any] = {}
    clicked_return = False
    clicked_return_at: float | None = None
    linking_success_seen = False
    pin_value = re.sub(r"\D+", "", pin or "")
    pin_filled = False

    while asyncio.get_event_loop().time() < deadline:
        try:
            candidates = [item for item in page.context.pages if not item.is_closed()]
        except Exception:
            candidates = [page]
        for candidate in reversed(candidates):
            url = candidate.url or ""
            try:
                text = await candidate.locator("body").inner_text(timeout=1200)
            except Exception:
                text = ""
            normalized = re.sub(r"\s+", " ", text)
            linking_success = bool(
                "merchants-gws-app.gopayapi.com/linking/success" in url
                or "app.midtrans.com/snap/v3/callback/gopay/linking/" in url
                or re.search(r"berhasil menghubungkan|kembali ke openai llc", normalized, re.I)
            )
            if linking_success:
                linking_success_seen = True
            if (
                url.startswith("chrome-error://")
                and original_midtrans_url
                and (pin_filled or linking_success_seen or clicked_return)
            ):
                try:
                    await candidate.goto(original_midtrans_url, wait_until="domcontentloaded")
                    await wait_for_known_next_status(candidate, timeout_ms=2500, interval_ms=350)
                    result = last_result | {"returned": True, "returnUrl": original_midtrans_url, "returnMode": "chrome_error_fallback"}
                    result["_page"] = candidate
                    await safe_screenshot(candidate, out_dir / "after_linking_returned_to_midtrans.png")
                    (out_dir / "after_linking_return.txt").write_text(
                        str({key: value for key, value in result.items() if key != "_page"}),
                        encoding="utf-8",
                    )
                    return result
                except Exception as exc:
                    last_result["chromeErrorReturnError"] = str(exc)
            if pin_value and not pin_filled:
                pin_page_hint = bool(
                    "pin-web-client.gopayapi.com" in url
                    or "gopayapi.com/auth/pin" in url
                    or re.search(r"pin|payment pin|ketik 6 digit pin|masukkan pin", normalized, re.I)
                )
                if pin_page_hint:
                    try:
                        pin_result = await fill_pin_and_continue(candidate, pin_value, out_dir)
                        pin_filled = True
                        last_result["pinResult"] = pin_result
                        last_result["pinFilled"] = True
                        log(f"已在 GoPay PIN Web SDK 页面自动填入 PIN: {short_status(pin_result)}")
                    except Exception as exc:
                        last_result["pinError"] = str(exc)
                        log(f"GoPay PIN Web SDK 自动填充失败: {exc}")
            last_result = {
                "url": url,
                "title": await candidate.title() if not candidate.is_closed() else "",
                "linkingSuccess": linking_success,
                "elapsedSeconds": round(asyncio.get_event_loop().time() - started_at, 1),
                "bodySample": normalized[:500],
                "pinFilled": pin_filled,
            }
            returned_to_midtrans = bool(
                ("app.midtrans.com" in url and not re.search(r"linking", url, re.I))
                or (
                    "app.midtrans.com/snap/v4/redirection/" in url
                    and "gopay-tokenization/linking" in url
                    and (linking_success_seen or clicked_return or pin_filled)
                )
            )
            if returned_to_midtrans or "pay.openai.com" in url:
                await safe_screenshot(candidate, out_dir / "after_linking_return.png")
                (out_dir / "after_linking_return.txt").write_text(str(last_result), encoding="utf-8")
                result = last_result | {"returned": True}
                result["_page"] = candidate
                return result
            return_url = choose_gopay_return_url(original_midtrans_url, transaction_uuid)
            clicked_return_elapsed = (
                asyncio.get_event_loop().time() - clicked_return_at
                if clicked_return_at is not None
                else None
            )
            if linking_success and return_url and should_force_gopay_return(url, clicked_return, clicked_return_elapsed):
                try:
                    if "merchants-gws-app.gopayapi.com/linking/success" in url:
                        await candidate.wait_for_timeout(500)
                    await candidate.goto(return_url, wait_until="domcontentloaded")
                    await wait_for_known_next_status(candidate, timeout_ms=2500, interval_ms=350)
                    return_mode = (
                        "midtrans_callback"
                        if "app.midtrans.com/snap/v3/callback/gopay/linking/" in url
                        else "gopay_linking_success_forced"
                    )
                    result = last_result | {"returned": True, "returnUrl": return_url, "returnMode": return_mode}
                    result["_page"] = candidate
                    await safe_screenshot(candidate, out_dir / "after_linking_returned_to_midtrans.png")
                    (out_dir / "after_linking_return.txt").write_text(
                        str({key: value for key, value in result.items() if key != "_page"}),
                        encoding="utf-8",
                    )
                    return result
                except Exception as exc:
                    last_result["returnUrlError"] = str(exc)
                continue
            if linking_success and not clicked_return:
                try:
                    await click_first_enabled_visible(
                        candidate,
                        [
                            "button:has-text('Kembali ke OpenAI LLC')",
                            "button:has-text('Kembali')",
                            "button:has-text('Return to OpenAI LLC')",
                            "a:has-text('Kembali ke OpenAI LLC')",
                            "a:has-text('Return to OpenAI LLC')",
                        ],
                        timeout_ms=2000,
                    )
                    clicked_return = True
                    clicked_return_at = asyncio.get_event_loop().time()
                    await wait_for_known_next_status(candidate, timeout_ms=1500, interval_ms=300)
                    continue
                except Exception:
                    pass
        await asyncio.sleep(0.7)

    await safe_screenshot(page, out_dir / "after_linking_return_timeout.png")
    (out_dir / "after_linking_return.txt").write_text(str(last_result | {"returned": False}), encoding="utf-8")
    return last_result | {"returned": False}


async def click_first_visible(page, selectors: list[str], timeout_ms: int = 12000) -> str:
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_error: Exception | None = None
    while asyncio.get_event_loop().time() < end_at:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() > 0 and await locator.is_visible():
                    await locator.scroll_into_view_if_needed()
                    await locator.click(force=True)
                    return selector
            except Exception as exc:
                last_error = exc
        await page.wait_for_timeout(300)
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"未找到可点击按钮: {selectors}{detail}")


async def click_first_enabled_visible(page, selectors: list[str], timeout_ms: int = 12000) -> str:
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_state = ""
    while asyncio.get_event_loop().time() < end_at:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() > 0 and await locator.is_visible():
                    disabled = await locator.evaluate(
                        """(el) => Boolean(el.disabled)
                            || el.getAttribute('aria-disabled') === 'true'
                            || el.classList.contains('disabled')"""
                    )
                    last_state = f"{selector}: disabled={disabled}"
                    if not disabled and await locator.is_enabled():
                        await locator.scroll_into_view_if_needed()
                        await locator.click()
                        return selector
            except Exception as exc:
                last_state = f"{selector}: {exc}"
        await page.wait_for_timeout(500)
    raise RuntimeError(f"未找到可用按钮: {selectors}; last={last_state}")


async def ensure_checkout_terms_checked(page) -> bool:
    checked_any = False
    selectors = [
        "#termsOfServiceConsentCheckbox",
        "[name='termsOfServiceConsentCheckbox']",
        "input[type='checkbox'][name*='terms']",
        "input[type='checkbox']",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() > 0 and await locator.is_visible(timeout=500):
                if not await locator.is_checked():
                    await locator.check(force=True)
                    checked_any = True
                break
        except Exception:
            continue
    try:
        checked_by_dom = await page.evaluate(
            """() => {
                let changed = false;
                for (const el of Array.from(document.querySelectorAll('input[type="checkbox"]'))) {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    if (rect.width <= 0 || rect.height <= 0 || style.display === 'none' || style.visibility === 'hidden') continue;
                    if (!el.checked) {
                        el.click();
                        changed = true;
                    }
                }
                return changed;
            }"""
        )
        checked_any = checked_any or bool(checked_by_dom)
    except Exception:
        pass
    if checked_any:
        await page.wait_for_timeout(500)
    return checked_any


async def checkout_started_navigation(page, before_url: str, before_pages: list[Any]) -> bool:
    try:
        if (page.url or "") != before_url:
            return True
    except Exception:
        pass
    try:
        if any(candidate for candidate in page.context.pages if candidate not in before_pages and not candidate.is_closed()):
            return True
    except Exception:
        pass
    try:
        text = await page.locator("body").inner_text(timeout=800)
    except Exception:
        text = ""
    return bool(re.search(r"正在处理|Processing|Loading|Redirecting|跳转|redirect", text, re.I))


async def click_checkout_submit(page, selectors: list[str], before_pages: list[Any]) -> str:
    before_url = page.url
    last_error = ""
    for attempt in range(1, 4):
        await ensure_checkout_terms_checked(page)
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() <= 0 or not await locator.is_visible(timeout=700):
                    continue
                await locator.scroll_into_view_if_needed()
                disabled = await locator.evaluate(
                    """(el) => Boolean(el.disabled)
                        || el.getAttribute('aria-disabled') === 'true'
                        || el.classList.contains('disabled')"""
                )
                if disabled:
                    last_error = f"{selector}: disabled"
                    continue
                box = await locator.bounding_box()
                if box:
                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    clicked = f"{selector} via mouse attempt={attempt}"
                else:
                    await locator.click(force=True, timeout=3000)
                    clicked = f"{selector} via force attempt={attempt}"
                await page.wait_for_timeout(1800)
                if await checkout_started_navigation(page, before_url, before_pages):
                    return clicked
                last_error = f"{clicked}: no navigation"
            except Exception as exc:
                last_error = f"{selector}: {exc}"
        try:
            await page.evaluate(
                """() => {
                    const btn = document.querySelector('[data-testid="hosted-payment-submit-button"], #hosted-payment-submit-button')
                        || Array.from(document.querySelectorAll('button')).find((button) => /订阅|Subscribe|Pay/i.test(button.textContent || ''));
                    if (btn) btn.click();
                    const form = btn && btn.closest('form');
                    if (form?.requestSubmit) form.requestSubmit();
                }"""
            )
            await page.wait_for_timeout(1800)
            if await checkout_started_navigation(page, before_url, before_pages):
                return f"js-submit attempt={attempt}"
        except Exception as exc:
            last_error = f"js-submit: {exc}"
    raise RuntimeError(f"订阅按钮点击后未跳转: {last_error}")


async def submit_checkout(page, out_dir: Path) -> Any:
    if not await is_billing_ready(page):
        await safe_screenshot(page, out_dir / "billing_not_ready.png")
        raise RuntimeError("账单未就绪或税额计算失败，已停止点击订阅")

    await page.wait_for_timeout(700)
    await ensure_checkout_terms_checked(page)

    before_pages = list(page.context.pages)
    submit_selectors = [
        "[data-testid='hosted-payment-submit-button']",
        "#hosted-payment-submit-button",
        "button:has-text('订阅')",
        "button:has-text('Subscribe')",
        "button:has-text('Pay')",
    ]
    clicked = await click_checkout_submit(page, submit_selectors, before_pages)
    log(f"已点击订阅按钮: {clicked}")
    await page.wait_for_timeout(1500)

    new_pages = [candidate for candidate in page.context.pages if candidate not in before_pages and not candidate.is_closed()]
    next_page = new_pages[-1] if new_pages else page
    try:
        await next_page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    await safe_screenshot(next_page, out_dir / "after_submit.png")
    return next_page


async def wait_for_gopay_phone_page(page, timeout_ms: int = 45000):
    context = page.context
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_page = page
    out_dir = resolve_path("output/gopay注册plus/billing")
    reload_counts: dict[str, int] = {}
    while asyncio.get_event_loop().time() < end_at:
        pages = [candidate for candidate in context.pages if not candidate.is_closed()]
        for candidate in reversed(pages):
            last_page = candidate
            await maybe_reload_stuck_payment_page(candidate, out_dir, reload_counts)
            try:
                text = await candidate.locator("body").inner_text(timeout=1500)
            except Exception:
                text = ""
            url = candidate.url or ""
            if "app.midtrans.com" in url and re.search(r"phone|mobile|nomor|ponsel|GoPay|Link and pay|\\+62|\\+86", text, re.I):
                return candidate
        await page.wait_for_timeout(700)
    return last_page


async def current_midtrans_page(page):
    try:
        if not page.is_closed() and "app.midtrans.com" in (page.url or ""):
            return page
    except Exception:
        pass
    try:
        pages = [item for item in page.context.pages if not item.is_closed()]
        pages.sort(key=lambda candidate: payment_page_rank(candidate, ""))
        for candidate in pages:
            url = candidate.url or ""
            if "app.midtrans.com" in url:
                return candidate
    except Exception:
        pass
    return page


async def current_gopay_pin_page(page):
    try:
        if not page.is_closed() and (
            "pin-web-client.gopayapi.com" in (page.url or "")
            or "gopayapi.com/auth/pin" in (page.url or "")
            or "app.midtrans.com" in (page.url or "")
        ):
            return page
    except Exception:
        pass
    try:
        for candidate in ordered_payment_pages(page):
            url = candidate.url or ""
            if (
                "pin-web-client.gopayapi.com" in url
                or "gopayapi.com/auth/pin" in url
                or ("app.midtrans.com" in url and "#/gopay-tokenization/pay" in url)
            ):
                return candidate
    except Exception:
        pass
    return page


async def inspect_gopay_candidate_page(candidate) -> dict[str, Any]:
    try:
        text = await candidate.locator("body").inner_text(timeout=1500)
    except Exception:
        text = ""
    normalized = re.sub(r"\s+", " ", text)
    url = candidate.url or ""
    try:
        title = await candidate.title() if not candidate.is_closed() else ""
    except Exception:
        title = ""
    try:
        inputs = await candidate.evaluate(
            """() => {
                const visible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                return Array.from(document.querySelectorAll('input')).filter(visible).map((el) => ({
                    type: el.type || null,
                    name: el.name || null,
                    id: el.id || null,
                    placeholder: el.placeholder || null,
                    maxLength: el.maxLength || null,
                    valueLength: String(el.value || '').length
                }));
            }"""
        )
    except Exception:
        inputs = []
    pin_hint = bool(
        re.search(r"pin|payment pin|masukkan pin|ketik 6 digit pin|lupa pin", normalized, re.I)
        or "pin-web-client.gopayapi.com" in url
        or "gopayapi.com/auth/pin" in url
    )
    has_otp_text = (not pin_hint) and bool(re.search(r"otp|verification|verifikasi|kode|code|whatsapp|sms|masukkin", normalized, re.I))
    has_otp_input = any(
        (not pin_hint)
        and (
            re.search(r"otp|code|kode|verification|verifikasi", " ".join(str(el.get(k, "")) for k in ("type", "name", "id", "placeholder")).lower())
            or el.get("maxLength") in {1, 6}
        )
        for el in inputs
    )
    rateLimited = bool(re.search(r"kebanyakan nyoba|too many attempts|too many tries|terlalu banyak|rate limit", normalized, re.I))
    technicalError = bool(re.search(r"technical error|try again|terjadi kesalahan|error teknis", normalized, re.I))
    phoneRejected = bool(re.search(r"use another phone number|gunakan nomor telepon lain|nomor.*tidak|phone number.*invalid", normalized, re.I))
    authorizeHint = bool(
        re.search(r"Hubungkan GoPay|Hubungkan|menghubungkan|link GoPay|connect GoPay", normalized, re.I)
        and await candidate.evaluate(
            """() => Array.from(document.querySelectorAll('button')).some((button) => {
                const text = (button.textContent || '').trim();
                const style = getComputedStyle(button);
                return style.display !== 'none' && style.visibility !== 'hidden' && /Hubungkan|Connect|Link/i.test(text);
            })"""
        )
    )
    payNowHint = bool(await candidate.evaluate(
        """() => Array.from(document.querySelectorAll('button')).some((button) => {
            const text = (button.textContent || '').trim();
            const style = getComputedStyle(button);
            if (style.display === 'none' || style.visibility === 'hidden' || button.disabled) return false;
            return /Pay now|Bayar|Pay|Lanjut|Continue/i.test(text);
        })"""
    ))
    status = "waiting"
    if rateLimited:
        status = "rate_limited"
    elif technicalError:
        status = "technical_error"
    elif phoneRejected:
        status = "phone_rejected"
    elif pin_hint:
        status = "pin_or_next"
    elif has_otp_text or has_otp_input:
        status = "otp"
    elif authorizeHint:
        status = "gopay_authorize"
    elif payNowHint:
        status = "pay_now"
    return {
        "url": url,
        "title": title,
        "status": status,
        "hasOtpText": has_otp_text,
        "hasOtpInput": has_otp_input,
        "rateLimited": rateLimited,
        "technicalError": technicalError,
        "phoneRejected": phoneRejected,
        "authorizeHint": authorizeHint,
        "payNowHint": payNowHint,
        "pinHint": pin_hint,
        "inputs": inputs,
        "bodySample": normalized[:800],
    }


def extract_midtrans_uuid(url: str) -> str | None:
    match = re.search(
        r"/(?:snap/v4/redirection|snap/v3/accounts)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        url or "",
        re.I,
    )
    return match.group(1) if match else None


def choose_gopay_return_url(original_midtrans_url: str | None, transaction_uuid: str | None) -> str | None:
    original_midtrans_url = (original_midtrans_url or "").strip()
    if (
        original_midtrans_url
        and "app.midtrans.com" in original_midtrans_url
        and "/callback/gopay/linking" not in original_midtrans_url
    ):
        return original_midtrans_url
    transaction_uuid = (transaction_uuid or "").strip()
    if transaction_uuid:
        return f"https://app.midtrans.com/snap/v4/redirection/{transaction_uuid}"
    return None


def should_force_gopay_return(
    current_url: str,
    clicked_return: bool,
    clicked_return_elapsed_seconds: float | None,
) -> bool:
    current_url = current_url or ""
    if (
        "app.midtrans.com/snap/v3/callback/gopay/linking/" in current_url
        or "merchants-gws-app.gopayapi.com/linking/success" in current_url
    ):
        return True
    return bool(clicked_return and clicked_return_elapsed_seconds is not None and clicked_return_elapsed_seconds >= 8)


def is_otp_rejected(status: dict[str, Any] | None) -> bool:
    if not isinstance(status, dict):
        return False
    text = str(status.get("bodySample") or "")
    return bool(re.search(r"kode otp.*salah|otp.*salah|wrong otp|invalid otp|incorrect otp|验证码.*错误|验证码.*不正确", text, re.I))


async def call_midtrans_linking_api(page, country_code: str, phone: str, out_dir: Path) -> dict[str, Any]:
    page = await current_midtrans_page(page)
    uuid = extract_midtrans_uuid(page.url)
    if not uuid:
        return {"ok": False, "status": "no_uuid", "url": page.url}

    payload = {
        "type": "gopay",
        "country_code": normalize_country_code(country_code).lstrip("+"),
        "phone_number": normalize_phone(phone),
    }
    result = await page.evaluate(
        """async ({ uuid, payload }) => {
            const response = await fetch(`/snap/v3/accounts/${uuid}/linking`, {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });
            const text = await response.text();
            let data = null;
            try { data = text ? JSON.parse(text) : null; } catch {}
            const bodyText = JSON.stringify(data || text || '');
            const redirectUrl = data?.activation_link_url
                || data?.redirect_url
                || data?.redirectUrl
                || data?.url
                || data?.deeplink
                || null;
            const accountAlreadyLinked = /account already linked|already linked|sudah terhubung|telah terhubung/i.test(bodyText);
            return {
                ok: response.ok || accountAlreadyLinked,
                statusCode: response.status,
                statusText: response.statusText,
                redirectUrl,
                accountAlreadyLinked,
                rateLimited: response.status === 429 || /too many|kebanyakan|terlalu banyak|rate/i.test(bodyText),
                phoneRejected: /another phone|gunakan nomor|invalid|tidak valid/i.test(bodyText),
                bodySample: bodyText.slice(0, 1000),
            };
        }""",
        {"uuid": uuid, "payload": payload},
    )
    result["uuid"] = uuid
    result["url"] = page.url
    (out_dir / "last_linking_api_result.txt").write_text(str(result), encoding="utf-8")
    if result.get("redirectUrl"):
        try:
            await page.goto(result["redirectUrl"], wait_until="domcontentloaded")
        except Exception:
            await page.evaluate("url => { location.href = url; }", result["redirectUrl"])
        status = await wait_for_known_next_status(page, timeout_ms=1200, interval_ms=250)
        if is_flow2_next_status(status.get("status")):
            result["statusAfterRedirect"] = status
    elif result.get("accountAlreadyLinked"):
        log("Midtrans/GoPay 提示该手机号已绑定，当前账号将换手机号重试")
    elif result.get("ok"):
        status = await wait_for_known_next_status(page, timeout_ms=1200, interval_ms=250)
        if is_flow2_next_status(status.get("status")):
            result["statusAfterApi"] = status
            await safe_screenshot(page, out_dir / "after_linking_api.png")
            return result
        try:
            await page.reload(wait_until="domcontentloaded")
            status = await wait_for_known_next_status(page, timeout_ms=1200, interval_ms=250)
            if is_flow2_next_status(status.get("status")):
                result["statusAfterReload"] = status
        except Exception:
            pass
    await safe_screenshot(page, out_dir / "after_linking_api.png")
    return result


async def wait_for_otp_or_error(page, out_dir: Path, timeout_ms: int = 90000) -> dict[str, Any]:
    end_at = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_result: dict[str, Any] = {}
    reload_counts: dict[str, int] = {}
    while asyncio.get_event_loop().time() < end_at:
        candidates = [candidate for candidate in page.context.pages if not candidate.is_closed()]
        result = {"status": "waiting", "url": page.url}
        for candidate in reversed(candidates):
            if await maybe_reload_stuck_payment_page(candidate, out_dir, reload_counts):
                result = {"status": "waiting", "url": candidate.url, "autoReloaded": True}
                page = candidate
                break
            stuck_status = await stuck_payment_page_status(candidate, reload_counts)
            if stuck_status:
                result = stuck_status
                page = candidate
                break
            try:
                candidate_result = await inspect_gopay_candidate_page(candidate)
            except Exception as exc:
                candidate_result = {"status": "waiting", "url": candidate.url, "error": str(exc)}
            if candidate_result.get("status") != "waiting":
                result = candidate_result
                page = candidate
                break
            if not last_result or candidate_result.get("url") == page.url:
                result = candidate_result
        last_result = result
        if result.get("status") in {"otp", "technical_error", "pin_or_next", "phone_rejected", "gopay_authorize", "pay_now", "rate_limited", "stuck_loading"}:
            break
        await asyncio.sleep(1)

    await safe_screenshot(page, out_dir / "otp_wait_result.png")
    try:
        if should_save_flow2_html_artifact("otp_wait_result.html", result):
            (out_dir / "otp_wait_result.html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        pass
    (out_dir / "otp_wait_result.txt").write_text(str(last_result), encoding="utf-8")
    return last_result


async def fill_otp_and_continue(page, otp: str, out_dir: Path) -> dict[str, Any]:
    page = await current_midtrans_page(page)
    otp = re.sub(r"\D+", "", otp or "")
    if not otp:
        raise RuntimeError("OTP 不能为空")
    result = await page.evaluate(
        """({ otp }) => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const inputs = Array.from(document.querySelectorAll('input')).filter((el) => {
                if (!visible(el)) return false;
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (['file', 'password', 'radio', 'checkbox', 'hidden', 'submit'].includes(type)) return false;
                const haystack = [el.id, el.name, el.placeholder, el.getAttribute('aria-label'), type].join(' ').toLowerCase();
                return /otp|code|kode|verification|verifikasi/.test(haystack) || el.maxLength === 1 || el.maxLength === 6 || type === 'tel' || type === 'text';
            });
            if (!inputs.length) return { ok: false, reason: '未找到 OTP 输入框' };
            const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            const singleCharInputs = inputs.filter((el) => el.maxLength === 1);
            if (singleCharInputs.length >= 4) {
                otp.split('').forEach((digit, index) => {
                    const el = singleCharInputs[index];
                    if (!el) return;
                    el.focus();
                    desc?.set?.call(el, digit);
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: digit }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                });
                return { ok: true, mode: 'split', count: singleCharInputs.length };
            }
            const el = inputs[0];
            el.focus();
            desc?.set?.call(el, otp);
            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: otp }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return { ok: true, mode: 'single', type: el.type || null, name: el.name || null, id: el.id || null };
        }""",
        {"otp": otp},
    )
    if not result.get("ok"):
        await safe_screenshot(page, out_dir / "otp_input_not_found.png")
        raise RuntimeError(result.get("reason") or "OTP 填充失败")

    log("OTP 已填入，等待 GoPay 自动跳转到下一步。")
    await page.wait_for_timeout(600)
    status = await wait_for_status_transition(page, timeout_ms=12400, interval_ms=400, ignored_statuses={"otp"})
    if str(status.get("status") or "") == "otp":
        status = await wait_for_otp_or_error(page, out_dir, timeout_ms=1000)
    await safe_screenshot(page, out_dir / "after_otp.png")
    return {"ok": True, "otpFilled": result, "autoSubmitted": True, "statusAfterOtp": status, "url": page.url}


async def fill_pin_and_continue(page, pin: str, out_dir: Path) -> dict[str, Any]:
    page = await current_gopay_pin_page(page)
    pin = re.sub(r"\D+", "", pin or "")
    if len(pin) != 6:
        raise RuntimeError("支付 PIN 必须是 6 位数字")

    result = await fill_gopay_pin_like_user(page, pin, timeout_ms=4000)

    if not result.get("ok"):
        await safe_screenshot(page, out_dir / "pin_input_not_found.png")
        raise RuntimeError(result.get("reason") or "PIN 填充失败")

    # 多数 GoPay PIN 页输满 6 位会自动提交；如果出现确认按钮，再点一次。
    await page.wait_for_timeout(1200)
    clicked = None
    try:
        await press_enter_and_confirm_pin(page)
        clicked = "enter_or_confirm"
    except Exception:
        pass

    status = await wait_for_status_transition(page, timeout_ms=13000, interval_ms=400, ignored_statuses={"pin_or_next"})
    if not is_flow2_next_status(status.get("status")):
        try:
            status = await inspect_gopay_candidate_page(page)
        except Exception:
            status = await wait_for_otp_or_error(await current_midtrans_page(page), out_dir, timeout_ms=8000)
    await safe_screenshot(page, out_dir / "after_pin.png")
    return {"ok": True, "pinFilled": result, "continueClicked": clicked, "statusAfterPin": status, "url": page.url}


async def click_gopay_authorize(page, out_dir: Path) -> dict[str, Any]:
    page = await current_midtrans_page(page)
    before_pages = list(page.context.pages)
    clicked = await click_first_enabled_visible(
        page,
        [
            "[data-testid='consent-button']",
            "button:has-text('Hubungkan')",
            "button:has-text('Connect')",
            "button:has-text('Link')",
            ".button-cta button",
            ".linking-cta button",
        ],
        timeout_ms=30000,
    )
    log(f"已点击 GoPay 授权确认: {clicked}")
    active_page, quick_status = await wait_for_new_page_or_known_status(
        page,
        before_pages,
        timeout_ms=1800,
        interval_ms=250,
        ignored_statuses={"gopay_authorize"},
    )
    await safe_screenshot(active_page, out_dir / "after_gopay_authorize.png")
    if is_flow2_next_status(quick_status.get("status")):
        status = quick_status
    else:
        status = await wait_for_otp_or_error(active_page, out_dir, timeout_ms=30000)
    return {"ok": True, "clicked": clicked, "statusAfterAuthorize": status, "url": active_page.url, "_page": active_page}


async def retry_technical_error_until_next(
    page,
    out_dir: Path,
    interval_seconds: int,
    timeout_seconds: int,
    country_code: str | None = None,
    phone: str | None = None,
    use_linking_api: bool = False,
) -> dict[str, Any]:
    started_at = asyncio.get_event_loop().time()
    deadline = started_at + timeout_seconds
    attempts = 0
    history: list[dict[str, Any]] = []
    reload_counts: dict[str, int] = {}

    while asyncio.get_event_loop().time() < deadline:
        page = await current_midtrans_page(page)
        await maybe_reload_stuck_payment_page(page, out_dir, reload_counts)
        status = await wait_for_otp_or_error(page, out_dir, timeout_ms=6000)
        elapsed = round(asyncio.get_event_loop().time() - started_at, 1)
        history.append({"attempt": attempts, "elapsedSeconds": elapsed, "status": status.get("status"), "url": status.get("url")})

        if status.get("status") in {"otp", "pin_or_next", "phone_rejected", "gopay_authorize", "pay_now", "rate_limited"}:
            result = {
                "ok": status.get("status") in {"otp", "pin_or_next", "pay_now"},
                "status": status.get("status"),
                "elapsedSeconds": elapsed,
                "attempts": attempts,
                "final": status,
                "history": history,
            }
            (out_dir / "technical_retry_result.txt").write_text(str(result), encoding="utf-8")
            return result

        if status.get("status") == "stuck_loading":
            log(f"GoPay/Midtrans 页面连续刷新后仍空白转圈，继续按 technical error 方式重试: {status.get('kind')}")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass
            await asyncio.sleep(max(1, interval_seconds))
            attempts += 1
            continue

        if status.get("status") == "technical_error":
            log(f"GoPay technical error，第 {attempts + 1} 次重试将在 {interval_seconds} 秒后进行，已耗时 {elapsed} 秒")
            try:
                await click_first_visible(
                    page,
                    [
                        "button:has-text('Back')",
                        "button:has-text('Kembali')",
                        "button:has-text('返回')",
                    ],
                    timeout_ms=6000,
                )
                await page.wait_for_timeout(1200)
            except Exception as exc:
                log(f"technical error 弹窗 Back 未点到，继续尝试直接重点 Link and pay: {exc}")
        else:
            log(f"GoPay 还未进入 OTP/错误终态，{interval_seconds} 秒后继续检查，当前状态={status.get('status')}")

        await asyncio.sleep(max(1, interval_seconds))
        attempts += 1
        try:
            page = await current_midtrans_page(page)
            if hasattr(page, "is_closed") and page.is_closed():
                raise RuntimeError("页面已关闭")
            if use_linking_api and country_code and phone:
                api_result = await call_midtrans_linking_api(page, country_code, phone, out_dir)
                log(
                    "technical error 重试调用 linking API: "
                    f"status={api_result.get('statusCode')}, ok={api_result.get('ok')}, 第 {attempts} 次"
                )
                if api_result.get("accountAlreadyLinked"):
                    status_after_linked = await wait_for_otp_or_error(page, out_dir, timeout_ms=8000)
                    result = {
                        "ok": status_after_linked.get("status") in {"otp", "pin_or_next", "gopay_authorize", "pay_now", "waiting"},
                        "status": status_after_linked.get("status", "account_already_linked"),
                        "elapsedSeconds": elapsed,
                        "attempts": attempts,
                        "final": status_after_linked,
                        "history": history,
                    }
                    (out_dir / "technical_retry_result.txt").write_text(str(result), encoding="utf-8")
                    return result
                if api_result.get("rateLimited"):
                    result = {
                        "ok": False,
                        "status": "rate_limited",
                        "elapsedSeconds": elapsed,
                        "attempts": attempts,
                        "final": api_result,
                        "history": history,
                    }
                    (out_dir / "technical_retry_result.txt").write_text(str(result), encoding="utf-8")
                    return result
            else:
                clicked = await click_first_visible(
                    page,
                    [
                        "button:has-text('Link and pay')",
                        "button:has-text('Link & pay')",
                        "button:has-text('Link')",
                        ".linking-cta button",
                    ],
                    timeout_ms=10000,
                )
                log(f"technical error 重试点击: {clicked} | 第 {attempts} 次")
        except Exception as exc:
            log(f"第 {attempts} 次重试未找到 Link and pay: {exc}")
        await asyncio.sleep(3.5)
        try:
            page = await current_midtrans_page(page)
            if hasattr(page, "is_closed") and page.is_closed():
                raise RuntimeError("页面已关闭")
            await safe_screenshot(page, out_dir / f"technical_retry_{attempts}.png")
        except Exception as exc:
            elapsed = round(asyncio.get_event_loop().time() - started_at, 1)
            result = {
                "ok": False,
                "status": "page_closed",
                "elapsedSeconds": elapsed,
                "attempts": attempts,
                "error": str(exc),
                "history": history,
            }
            (out_dir / "technical_retry_result.txt").write_text(str(result), encoding="utf-8")
            return result

    final_status = await wait_for_otp_or_error(page, out_dir, timeout_ms=3000)
    elapsed = round(asyncio.get_event_loop().time() - started_at, 1)
    result = {
        "ok": False,
        "status": final_status.get("status", "timeout"),
        "elapsedSeconds": elapsed,
        "attempts": attempts,
        "final": final_status,
        "history": history,
    }
    (out_dir / "technical_retry_result.txt").write_text(str(result), encoding="utf-8")
    return result


async def select_gopay_country_code(page, country_code: str) -> bool:
    code = normalize_country_code(country_code)
    await page.wait_for_timeout(800)

    current = await page.evaluate("() => document.querySelector('.phone-code')?.textContent?.trim() || ''")
    if current == code:
        return True

    opened = await page.evaluate(
        """() => {
            const wrapper = document.querySelector('.phone-code-wrapper');
            if (!wrapper) return false;
            wrapper.click();
            return true;
        }"""
    )
    if not opened:
        return False
    await page.wait_for_timeout(500)

    selected = await page.evaluate(
        """(code) => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const search = document.querySelector('.search-country input[type="search"], input[type="search"]');
            if (search) {
                search.focus();
                const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                desc?.set?.call(search, code.replace('+', ''));
                search.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: code.replace('+', '') }));
                search.dispatchEvent(new Event('change', { bubbles: true }));
            }
            const options = Array.from(document.querySelectorAll('li.country-item'))
                .filter(visible);
            const target = options.find((el) => (el.textContent || '').includes(`(${code})`))
                || options.find((el) => (el.textContent || '').includes(code));
            target?.click();
            return Boolean(target);
        }""",
        code,
    )
    if selected:
        await page.wait_for_timeout(800)
    current = await page.evaluate("() => document.querySelector('.phone-code')?.textContent?.trim() || ''")
    return current == code


async def force_set_gopay_country_dom(page, country_code: str) -> bool:
    code = normalize_country_code(country_code)
    return bool(
        await page.evaluate(
            """(code) => {
                const flagMap = {
                    '+63': 'ph',
                    '+86': 'cn',
                    '+62': 'id',
                    '+65': 'sg',
                    '+1': 'us'
                };
                const phoneCode = document.querySelector('.phone-code');
                if (!phoneCode) return false;
                phoneCode.textContent = code;
                const flagImg = document.querySelector('.selected-flag');
                const flag = flagMap[code];
                if (flagImg && flag) {
                    flagImg.src = `https://flagcdn.com/${flag}.svg`;
                    flagImg.srcset = `https://flagcdn.com/${flag}.svg 1x, https://flagcdn.com/${flag}.svg 2x`;
                }
                phoneCode.dispatchEvent(new Event('input', { bubbles: true }));
                phoneCode.dispatchEvent(new Event('change', { bubbles: true }));
                return (document.querySelector('.phone-code')?.textContent || '').trim() === code;
            }""",
            code,
        )
    )


async def fill_gopay_phone(
    page,
    phone: str,
    country_code: str,
    out_dir: Path,
    force_country_dom: bool = False,
    use_linking_api: bool = False,
) -> dict[str, Any]:
    phone = normalize_phone(phone)
    country_code = normalize_country_code(country_code)
    await page.wait_for_timeout(2500)

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    page = await wait_for_gopay_phone_page(page, timeout_ms=45000)
    if "app.midtrans.com" not in (page.url or ""):
        await safe_screenshot(page, out_dir / "not_midtrans_phone_page.png")
        raise RuntimeError(f"未进入 GoPay/Midtrans 手机页，当前 URL: {page.url}")
    original_midtrans_url = page.url
    await safe_screenshot(page, out_dir / "gopay_before_phone.png")

    selected_country = await select_gopay_country_code(page, country_code)
    if not selected_country:
        if force_country_dom:
            log(f"真实下拉未确认国家码 {country_code}，启用 DOM 兜底强制显示")
            selected_country = await force_set_gopay_country_dom(page, country_code)
        if not selected_country:
            await safe_screenshot(page, out_dir / "country_code_not_selected.png")
            raise RuntimeError(f"未确认选中国家码 {country_code}，已停止填手机号")

    input_info = await page.evaluate(
        """() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const preferred = document.querySelector('.phone-number-input input.valid-input-value, .phone-number-input input[type="tel"], input.valid-input-value[type="tel"]');
            const candidates = [
                preferred,
                ...Array.from(document.querySelectorAll('input'))
            ].filter(Boolean)
                .filter((el) => visible(el))
                .filter((el) => {
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const text = [
                        el.id,
                        el.name,
                        el.placeholder,
                        el.getAttribute('aria-label'),
                        el.autocomplete,
                    ].join(' ').toLowerCase();
                    return type !== 'file'
                        && type !== 'password'
                        && type !== 'radio'
                        && type !== 'checkbox'
                        && type !== 'hidden'
                        && type !== 'submit'
                        && !/otp|pin|password|verification|code/.test(text)
                        && (type === 'tel' || /phone|mobile|nomor|ponsel|whatsapp|wa|tel/.test(text));
                });
            const el = candidates[0] || Array.from(document.querySelectorAll('input')).filter(visible).find((input) => {
                const type = (input.getAttribute('type') || '').toLowerCase();
                return type !== 'file' && type !== 'password' && type !== 'radio' && type !== 'checkbox' && type !== 'hidden' && type !== 'submit';
            });
            if (!el) return { ok: false, reason: '未找到手机号输入框' };
            const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
            el.focus();
            desc?.set?.call(el, '');
            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return {
                ok: true,
                id: el.id || null,
                name: el.name || null,
                type: el.type || null,
                placeholder: el.placeholder || null,
                selector: el.classList.contains('valid-input-value') ? '.phone-number-input input.valid-input-value' : null
            };
        }"""
    )
    if not input_info.get("ok"):
        await safe_screenshot(page, out_dir / "gopay_phone_not_found.png")
        raise RuntimeError(input_info.get("reason") or "手机号输入框定位失败")

    await page.keyboard.type(phone, delay=180)
    await page.wait_for_timeout(700)
    filled = await page.evaluate(
        """() => {
            const el = document.querySelector('.phone-number-input input.valid-input-value, .phone-number-input input[type="tel"], input.valid-input-value[type="tel"], input[type="tel"]');
            return {
                ok: Boolean(el),
                id: el?.id || null,
                name: el?.name || null,
                type: el?.type || null,
                placeholder: el?.placeholder || null,
                value: el?.value || null
            };
        }"""
    )
    if filled.get("value") != phone:
        await safe_screenshot(page, out_dir / "gopay_phone_value_mismatch.png")
        raise RuntimeError(f"手机号键盘输入后校验失败: {filled}")

    await page.wait_for_timeout(700)
    clicked = None
    linking_api_result = None
    if use_linking_api:
        try:
            linking_api_result = await call_midtrans_linking_api(page, country_code, phone, out_dir)
            log(
                "已填手机号并调用 Midtrans linking API: "
                f"status={linking_api_result.get('statusCode')}, ok={linking_api_result.get('ok')}"
            )
        except Exception as exc:
            linking_api_result = {"ok": False, "status": "exception", "error": str(exc)}
            (out_dir / "last_linking_api_result.txt").write_text(str(linking_api_result), encoding="utf-8")
            log(f"Midtrans linking API 调用失败，准备回退按钮点击: {exc}")

    should_click_fallback = not use_linking_api or not linking_api_result or (
        not linking_api_result.get("ok")
        and not linking_api_result.get("rateLimited")
    )
    if should_click_fallback:
        try:
            clicked = await click_first_visible(
                page,
                [
                    "button:has-text('Link and pay')",
                    "button:has-text('Link & pay')",
                    "button:has-text('Link')",
                    "button:has-text('Continue')",
                    "button:has-text('Lanjut')",
                    "button:has-text('Bayar')",
                ],
                timeout_ms=10000,
            )
            log(f"已填手机号并点击继续: {clicked}")
        except Exception as exc:
            log(f"手机号已填入，但未找到继续按钮: {exc}")
    elif linking_api_result.get("rateLimited"):
        log("Midtrans/GoPay 返回尝试过多，已停止继续点击，等待换号或稍后重试")
    elif linking_api_result.get("accountAlreadyLinked"):
        log("Midtrans/GoPay 提示账号已绑定，跳过重复 Link and pay，直接继续后续支付判断")

    next_status = {}
    if isinstance(linking_api_result, dict):
        for key in ("statusAfterRedirect", "statusAfterApi", "statusAfterReload"):
            candidate_status = linking_api_result.get(key)
            if isinstance(candidate_status, dict) and is_flow2_next_status(candidate_status.get("status")):
                next_status = candidate_status
                break
    if not next_status:
        status_wait_ms = 4000 if should_click_fallback or not use_linking_api else 1800
        next_status = await wait_for_known_next_status(page, timeout_ms=status_wait_ms, interval_ms=300)
    await safe_screenshot(page, out_dir / "after_phone.png")
    return {
        "ok": True,
        "countryCodeRequested": country_code,
        "countrySelected": selected_country,
        "phoneFilled": filled,
        "continueClicked": clicked,
        "linkingApi": linking_api_result,
        "statusAfterPhone": next_status,
        "originalMidtransUrl": original_midtrans_url,
        "url": page.url,
    }


async def inspect_gopay_phone_page(page, out_dir: Path) -> dict[str, Any]:
    await page.wait_for_timeout(3500)
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    result = await page.evaluate(
        """() => {
            const visible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const text = document.body?.innerText || '';
            const inputs = Array.from(document.querySelectorAll('input')).filter(visible).map((el) => ({
                id: el.id || null,
                name: el.name || null,
                type: el.type || null,
                placeholder: el.placeholder || null,
                ariaLabel: el.getAttribute('aria-label') || null,
                value: el.type === 'password' ? '' : (el.value || '')
            }));
            const hasPhoneHint = /phone|mobile|whatsapp|nomor|ponsel|telepon|link and pay|gopay|\\+62|\\+86/i.test(text);
            const hasPhoneInput = inputs.some((el) => {
                const haystack = [el.id, el.name, el.type, el.placeholder, el.ariaLabel].join(' ').toLowerCase();
                return el.type === 'tel' || /phone|mobile|whatsapp|nomor|ponsel|telepon|wa/.test(haystack);
            });
            return {
                url: location.href,
                title: document.title,
                hasPhoneHint,
                hasPhoneInput,
                inputs,
                bodySample: text.replace(/\\s+/g, ' ').slice(0, 600)
            };
        }"""
    )
    await safe_screenshot(page, out_dir / "phone_page_check.png")
    try:
        if should_save_flow2_html_artifact("phone_page_check.html", result):
            (out_dir / "phone_page_check.html").write_text(await page.content(), encoding="utf-8")
    except Exception:
        pass
    (out_dir / "phone_page_check.txt").write_text(str(result), encoding="utf-8")
    return result


class PhoneRetryableError(RuntimeError):
    def __init__(self, status: str, message: str, *, phone: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.phone = phone


async def process_record(args: argparse.Namespace, record: dict[str, str], phone: str, worker_id: int = 1) -> bool:
    prefix = f"[worker-{worker_id:02d}][{record.get('account', 'unknown')}]"
    log(f"{prefix} 读取支付长链接")
    cfg = load_config(args.config)
    env = load_env(".env")
    browser_cfg = cfg.get("browser", {})
    proxy = pick_proxy_for_test(args, cfg)
    gopay_pin = re.sub(r"\D+", "", getattr(args, "pin", "") or "") or parse_gopay_pin(env)
    log(f"{prefix} 浏览器代理: {display_proxy(proxy)}")
    profile_dir = resolve_path("profiles/billing_test") / safe_worker_profile_name(record, worker_id)
    out_dir = resolve_path("output/gopay注册plus/billing") / safe_worker_profile_name(record, worker_id)
    whatsapp_otp = WhatsAppOtpProvider(source_from_env(env, worker_id), worker_id=worker_id, phone=phone, out_dir=out_dir)
    gopay_unlink = GoPayUnlinkProvider(gopay_unlink_config_from_env(env, worker_id), worker_id=worker_id, out_dir=out_dir)
    payment_success = False
    run_gopay_unlink_after_browser = False
    async with BrowserSession(
        profile_dir=profile_dir,
        headless=bool(args.headless if args.headless is not None else browser_cfg.get("headless", False)),
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
    ) as session:
        page = await session.current_page()
        await page.goto(record["payment_link"], wait_until="domcontentloaded")
        out_dir.mkdir(parents=True, exist_ok=True)
        checkout_due = await inspect_checkout_due_today(page, out_dir)
        total_text = checkout_due.get("totalText") or "未识别"
        due_status = str(checkout_due.get("status") or ("zero" if checkout_due.get("isZero") else "unknown"))
        if due_status == "terminal_complete":
            removed_path = remove_paid_record(args.success_file, record)
            log(f"{prefix} 检测到结账会话已完成/超时，直接移除该号: {removed_path}")
            return "terminal_complete"
        if due_status == "unknown":
            log(f"{prefix} 今日应付=未识别，保留账号不删除；原因={checkout_due.get('reason') or '页面金额未稳定加载'}")
            return "unknown_due"
        zero_text = "是" if due_status == "zero" else "否"
        log(f"{prefix} 今日应付={total_text} | 0元={zero_text}")
        if due_status == "nonzero":
            nonzero_path = append_nonzero_billing(record, checkout_due, args.nonzero_output)
            log(f"{prefix} 确认非0元，跳过该号；记录: {nonzero_path}")
            if getattr(args, "remove_nonzero_source", False):
                removed_path = remove_paid_record(args.success_file, record)
                log(f"{prefix} 已从长链接账号池移除非 0 元账号: {removed_path}")
            return "nonzero"
        result = None
        billing = None
        for attempt in range(1, args.billing_retries + 1):
            if attempt > 1:
                log("检测到账单不可用，清空旧地址并重新拉取")
                await clear_billing_page(page)
            billing = await fetch_meiguodizhi_us_address()
            log(f"{prefix} 账单地址[{attempt}/{args.billing_retries}]: {billing.name} | {billing.address_line1}, {billing.city}, {billing.state} {billing.postal_code}")
            result = await fill_billing_page(page, billing)
            await safe_screenshot(page, out_dir / f"billing_attempt_{attempt}.png")
            if not result.get("taxError") and await is_billing_ready(page):
                break
            log(f"{prefix} 账单地址不可用，准备换地址重试: {result}")
        if result is None or billing is None:
            raise RuntimeError("账单填充未执行")
        if result.get("taxError") or not await is_billing_ready(page):
            await safe_screenshot(page, out_dir / "billing_failed_final.png")
            raise RuntimeError(f"账单多次重试仍不可用，已停止点击订阅: {result}")
        await safe_screenshot(page, out_dir / "last_billing.png")
        (out_dir / "last_billing_result.txt").write_text(str(result), encoding="utf-8")
        log(f"{prefix} {flow2_billing_summary(result, billing)}")
        if args.submit:
            if not phone and not args.stop_at_phone:
                raise RuntimeError("继续到 GoPay 手机页必须传 --phone，例如 --submit --phone 173xxxxxxx")
            payment_page = await submit_checkout(page, out_dir)
            if args.stop_at_phone:
                phone_result = await inspect_gopay_phone_page(payment_page, out_dir)
                log(f"{prefix} GoPay 手机页检测结果: {phone_result}")
                log_saved_artifact(prefix, "手机页检测截图", out_dir / "phone_page_check.png")
                return False
            phone_result = await fill_gopay_phone(
                payment_page,
                phone,
                args.country_code,
                out_dir,
                force_country_dom=args.force_country_dom,
                use_linking_api=args.use_linking_api,
            )
            (out_dir / "last_phone_result.txt").write_text(str(phone_result), encoding="utf-8")
            log(f"{prefix} {flow2_step_status('GoPay 手机号提交', phone_result.get('statusAfterPhone') if isinstance(phone_result.get('statusAfterPhone'), dict) else phone_result)}")
            linking_result = phone_result.get("linkingApi") if isinstance(phone_result.get("linkingApi"), dict) else {}
            if linking_result.get("accountAlreadyLinked"):
                raise PhoneRetryableError(
                    "account_already_linked",
                    f"手机号 {phone} 已被 GoPay/Midtrans 判定为已绑定，需要先清理本设备 GoPay linked app 后同号重试",
                    phone=phone,
                )
            if linking_result.get("phoneRejected"):
                raise PhoneRetryableError(
                    "phone_rejected",
                    f"手机号 {phone} 被 GoPay/Midtrans 拒绝，本设备槽位本轮停止",
                    phone=phone,
                )
            if linking_result.get("rateLimited"):
                raise PhoneRetryableError(
                    "rate_limited",
                    f"手机号 {phone} 触发 GoPay/Midtrans 限频，本设备槽位本轮停止",
                    phone=phone,
                )
            if args.wait_otp or args.prompt_otp:
                old_whatsapp_codes: set[str] = set()
                if whatsapp_otp.source.enabled:
                    log(f"{prefix} WhatsApp ADB 自动取码已开启: device={whatsapp_otp.source.device or 'default'} package={whatsapp_otp.source.package}")
                    try:
                        old_whatsapp_codes = await whatsapp_otp.snapshot_codes()
                        if old_whatsapp_codes:
                            log(f"{prefix} 已记录 WhatsApp 旧码排除数: {len(old_whatsapp_codes)}")
                    except Exception as exc:  # noqa: BLE001
                        log(f"{prefix} WhatsApp ADB 旧码快照失败，将继续后续流程: {exc}")
                phone_next_status = phone_result.get("statusAfterPhone") if isinstance(phone_result.get("statusAfterPhone"), dict) else {}
                if is_flow2_next_status(phone_next_status.get("status")):
                    otp_status = phone_next_status
                else:
                    otp_status = await wait_for_otp_or_error(payment_page, out_dir, timeout_ms=args.otp_timeout * 1000)
                log(f"{prefix} {flow2_step_status('OTP 状态', otp_status)}")
                if otp_status.get("status") in {"technical_error", "stuck_loading"}:
                    if args.retry_technical_error:
                        retry_result = await retry_technical_error_until_next(
                            payment_page,
                            out_dir,
                            interval_seconds=args.retry_interval,
                            timeout_seconds=args.retry_timeout,
                            country_code=args.country_code,
                            phone=phone,
                            use_linking_api=args.use_linking_api,
                        )
                        log(f"{prefix} technical error 重试结果: {short_status(retry_result)}")
                        if retry_result.get("status") not in {"otp", "pin_or_next", "gopay_authorize", "pay_now"}:
                            log(f"{prefix} GoPay technical error 重试后仍未进入 OTP，已停止，不继续 OTP/PIN")
                            return False
                        otp_status = retry_result.get("final", otp_status)
                    else:
                        log(f"{prefix} GoPay 页面异常/空白转圈，已停止，不继续 OTP/PIN")
                        return False
                if otp_status.get("status") == "phone_rejected":
                    log(f"{prefix} GoPay 返回 Please use another phone number，手机号被拒绝，已停止")
                    return False
                if otp_status.get("status") == "rate_limited":
                    log(f"{prefix} GoPay 返回尝试次数过多/限频，已停止，避免继续触发风控")
                    return False
                if otp_status.get("status") == "gopay_authorize":
                    if whatsapp_otp.source.enabled:
                        try:
                            if whatsapp_otp.source.auto_open:
                                if whatsapp_otp.source.use_bridge and not whatsapp_otp.source.use_ui_text and not whatsapp_otp.source.use_ocr:
                                    log(f"{prefix} WhatsApp Bridge-only 模式，授权前打开 WhatsApp 触发收信，验证码仍只从 Bridge 读取")
                                else:
                                    log(f"{prefix} 点击 GoPay 授权前，先打开对应设备 WhatsApp 以稳定接收新 OTP")
                                await whatsapp_otp.open_whatsapp()
                            elif whatsapp_otp.source.use_ui_text or whatsapp_otp.source.use_ocr:
                                log(f"{prefix} 点击 GoPay 授权前，先打开对应设备 WhatsApp 以稳定接收新 OTP")
                                await whatsapp_otp.open_whatsapp()
                            else:
                                log(f"{prefix} WhatsApp Bridge-only 模式，授权前不打开 WhatsApp，只刷新旧码排除")
                            pre_authorize_codes = await whatsapp_otp.snapshot_codes()
                            if pre_authorize_codes:
                                old_whatsapp_codes.update(pre_authorize_codes)
                                log(f"{prefix} 授权前已刷新 WhatsApp 旧码排除数: {len(old_whatsapp_codes)}")
                        except Exception as exc:  # noqa: BLE001
                            log(f"{prefix} 授权前打开 WhatsApp/刷新旧码失败，将继续点击授权: {exc}")
                    authorize_result = await click_gopay_authorize(payment_page, out_dir)
                    (out_dir / "last_authorize_result.txt").write_text(str(authorize_result), encoding="utf-8")
                    log(f"{prefix} {flow2_step_status('GoPay 授权确认', authorize_result)}")
                    otp_status = authorize_result.get("statusAfterAuthorize", otp_status)
                    if authorize_result.get("_page") is not None:
                        payment_page = authorize_result["_page"]
                if otp_status.get("status") == "pay_now":
                    log(f"{prefix} 已回到交易页，检测到最终支付按钮，继续等待/点击最终支付")
                if whatsapp_otp.source.enabled and otp_status.get("status") == "otp":
                    otp = ""
                    otp_result: dict[str, Any] | None = None
                    for otp_attempt in range(1, 3):
                        otp = await whatsapp_otp.wait_code(exclude=old_whatsapp_codes, timeout=args.otp_timeout)
                        if not otp:
                            break
                        old_whatsapp_codes.add(otp)
                        otp_result = await fill_otp_and_continue(payment_page, otp, out_dir)
                        (out_dir / "last_whatsapp_otp_result.txt").write_text(str(otp_result), encoding="utf-8")
                        retry_suffix = f" | attempt={otp_attempt}/2" if otp_attempt > 1 else ""
                        log(f"{prefix} {flow2_step_status('WhatsApp OTP 已填', otp_result)}{retry_suffix}")
                        otp_status = otp_result.get("statusAfterOtp", otp_status)
                        if otp_status.get("status") != "otp":
                            break
                        if is_otp_rejected(otp_status):
                            log(f"{prefix} GoPay 提示 OTP 错误，已排除 {otp[:2]}****，继续等待新验证码")
                            continue
                        break
                    if not otp:
                        log(f"{prefix} WhatsApp ADB 未取到 OTP，本设备槽位本轮停止")
                        raise PhoneRetryableError(
                            "otp_timeout",
                            f"手机号 {phone} 的 WhatsApp OTP 等待超时，本设备槽位本轮停止",
                            phone=phone,
                        )
                    if otp_status.get("status") == "otp":
                        raise PhoneRetryableError(
                            "otp_not_accepted",
                            f"手机号 {phone} 的 WhatsApp OTP 填入后未进入下一步，本设备槽位本轮停止",
                            phone=phone,
                        )
                if otp_status.get("status") == "pin_or_next":
                    log(f"{prefix} 已到 GoPay 6 位 PIN 页面")
                    if len(gopay_pin) == 6:
                        pin_result = await fill_pin_and_continue(payment_page, gopay_pin, out_dir)
                        (out_dir / "last_pin_result.txt").write_text(str(pin_result), encoding="utf-8")
                        log(f"{prefix} {flow2_step_status('GoPay 绑定 PIN 已填', pin_result)}")
                        otp_status = pin_result.get("statusAfterPin", otp_status)
                    elif not args.prompt_pin:
                        log(f"{prefix} 未配置 GOPAY_PIN/GOPAY_PAYMENT_PIN，且未启用 --prompt-pin，无法自动填写 PIN")
                        return False
                if args.prompt_otp:
                    otp = await asyncio.to_thread(input, "请输入手机 OTP，直接回车则停止：")
                    if not otp.strip():
                        log(f"{prefix} 未输入 OTP，已停止")
                        return False
                    otp_result = await fill_otp_and_continue(payment_page, otp, out_dir)
                    (out_dir / "last_otp_result.txt").write_text(str(otp_result), encoding="utf-8")
                    log(f"{prefix} {flow2_step_status('OTP 已填', otp_result)}")
                    otp_status = otp_result.get("statusAfterOtp", otp_status)
                if args.prompt_pin and otp_status.get("status") == "pin_or_next":
                    pin = await asyncio.to_thread(input, "请输入 6 位 GoPay 支付 PIN，直接回车则停止：")
                    if not pin.strip():
                        log(f"{prefix} 未输入 PIN，已停止")
                        return False
                    pin_result = await fill_pin_and_continue(payment_page, pin, out_dir)
                    (out_dir / "last_pin_result.txt").write_text(str(pin_result), encoding="utf-8")
                    log(f"{prefix} {flow2_step_status('PIN 已填', pin_result)}")
                if len(gopay_pin) == 6 and otp_status.get("status") not in {"pay_now"}:
                    pin_page = await current_gopay_pin_page(payment_page)
                    if "pin-web-client.gopayapi.com" in (pin_page.url or "") or "gopayapi.com/auth/pin" in (pin_page.url or ""):
                        pin_result = await fill_pin_and_continue(pin_page, gopay_pin, out_dir)
                        (out_dir / "last_pin_result.txt").write_text(str(pin_result), encoding="utf-8")
                        log(f"{prefix} {flow2_step_status('GoPay PIN Web SDK 已填', pin_result)}")
                        otp_status = pin_result.get("statusAfterPin", otp_status)
            if args.wait_manual_success:
                log(f"{prefix} 等待验证码/绑定返回；如检测到 GoPay PIN 页面会自动填写，绑定成功后会回到交易页继续等最终支付成功")
                transaction_uuid = None
                if isinstance(phone_result.get("linkingApi"), dict):
                    transaction_uuid = phone_result["linkingApi"].get("uuid")
                return_result = await leave_gopay_linking_success(
                    payment_page,
                    out_dir,
                    timeout_seconds=35,
                    transaction_uuid=transaction_uuid,
                    original_midtrans_url=phone_result.get("originalMidtransUrl"),
                    pin=gopay_pin,
                )
                log(f"{prefix} {flow2_step_status('GoPay 绑定返回交易页', return_result)}")
                final_payment_result: dict[str, Any] | None = None
                if return_result.get("returned") and return_result.get("_page") is not None:
                    payment_page = return_result["_page"]
                    final_payment_result = await handle_midtrans_final_payment(
                        payment_page,
                        out_dir,
                        pin=gopay_pin,
                        prompt_pin=args.prompt_pin,
                        profile_dir=profile_dir,
                    )
                    log(f"{prefix} {flow2_step_status('Midtrans 最终支付', final_payment_result)}")
                    if final_payment_result.get("status") == "pin_required":
                        log(f"{prefix} 最终付款需要 GoPay PIN；可在 .env 配置 GOPAY_PIN 或加 --prompt-pin 手动输入")
                    if final_payment_result.get("status") == "gopay_proceed_failed":
                        log(f"{prefix} GoPay 发起付款失败，已保存截图: {out_dir / 'gopay_proceed_failed.png'}")
                    if final_payment_result.get("success") or final_payment_result.get("status") == "success":
                        success_result = final_payment_result
                    else:
                        success_result = None
                else:
                    if return_result.get("linkingSuccess") and not return_result.get("returned"):
                        log(f"{prefix} 绑定成功但返回 Midtrans 超时，进入短兜底确认（最多约 18 秒）")
                        final_payment_result = await recover_final_payment_after_linking_return_timeout(
                            payment_page,
                            out_dir,
                            original_midtrans_url=phone_result.get("originalMidtransUrl"),
                            pin=gopay_pin,
                            prompt_pin=args.prompt_pin,
                            profile_dir=profile_dir,
                            timeout_seconds=18,
                        )
                        log(f"{prefix} Midtrans 返回超时短兜底结果: {short_status(final_payment_result)}")
                        if final_payment_result.get("success") or final_payment_result.get("status") == "success":
                            success_result = final_payment_result
                        else:
                            success_result = None
                    else:
                        success_result = None
                if success_result is None:
                    success_result = {"success": False, "status": "final_payment_not_confirmed"}
                if not success_result.get("success"):
                    recovered_result = recover_payment_success_from_history(profile_dir, out_dir)
                    if recovered_result.get("success"):
                        log(f"{prefix} 页面检测漏掉成功，但浏览器历史确认支付成功: {short_status(recovered_result)}")
                        success_result = recovered_result
                log(f"{prefix} {flow2_step_status('支付完成检测', success_result)}")
                if success_result.get("success"):
                    payment_success = True
                    paid_path = append_paid_success(record, args.paid_output)
                    if args.remove_paid_source:
                        removed_path = remove_paid_record(args.success_file, record)
                        log(f"{prefix} 已从长链接账号池移除")
                    log(f"{prefix} 已落盘支付成功账号: {paid_path.name}")
                    if gopay_unlink.config.enabled:
                        run_gopay_unlink_after_browser = True
                        log(f"{prefix} 支付成功已落盘，先关闭浏览器，再执行 GoPay linked app 清理")
                else:
                    log(f"{prefix} 未检测到支付成功，未落盘")
                    return False
        if args.keep_open and not payment_success:
            await asyncio.to_thread(input, "检查页面后按回车关闭浏览器：")
    if run_gopay_unlink_after_browser:
        log(f"{prefix} 浏览器已关闭，开始执行 GoPay linked app 清理")
        unlink_result = await gopay_unlink.unlink_openai()
        (out_dir / "last_gopay_unlink_result.txt").write_text(str(unlink_result), encoding="utf-8")
        if unlink_result.get("ok"):
            status = "已解绑" if not unlink_result.get("alreadyUnlinked") else "本来已解绑"
            log(f"{prefix} GoPay linked app 清理完成: {status}")
        else:
            log(f"{prefix} GoPay linked app 清理失败，不影响支付成功落盘: {short_status(unlink_result)}")
        return True
    if payment_success:
        return True
    return False


def safe_worker_profile_name(record: dict[str, str], worker_id: int) -> str:
    account = record.get("account") or f"worker-{worker_id}"
    safe_account = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", account)
    return f"worker_{worker_id:02d}_{safe_account}"


async def run(args: argparse.Namespace) -> None:
    records = read_payment_links(args.success_file)
    records = filter_unprocessed_payment_links(
        dedupe_payment_links(records),
        read_flow2_processed_accounts(args.paid_output, args.nonzero_output),
    )
    record = pick_record(records, args.index, args.account)
    env = load_env(".env")
    slots = parse_flow2_device_slots(env)
    phone = args.phone or (slots[0].phone if slots else "")
    await process_record(args, record, phone=phone, worker_id=1)


class BatchState:
    def __init__(self, target_count: int) -> None:
        self.target_count = target_count
        self.success_count = 0
        self.active_count = 0
        self.lock = asyncio.Lock()


class BatchDeviceState:
    def __init__(self) -> None:
        self.bad: dict[int, str] = {}
        self.lock = asyncio.Lock()

    async def mark_bad(self, slot: Flow2DeviceSlot, reason: str) -> None:
        async with self.lock:
            self.bad[slot.worker_id] = reason

    async def is_bad(self, slot: Flow2DeviceSlot) -> bool:
        async with self.lock:
            return slot.worker_id in self.bad


async def unlink_gopay_for_slot(args: argparse.Namespace, slot: Flow2DeviceSlot, record: dict[str, str]) -> dict:
    env = load_env(".env")
    out_dir = resolve_path("output/gopay注册plus/billing") / safe_worker_profile_name(record, slot.worker_id)
    provider = GoPayUnlinkProvider(
        gopay_unlink_config_from_env(env, slot.worker_id),
        worker_id=slot.worker_id,
        out_dir=out_dir,
    )
    log(
        f"[worker-{slot.worker_id:02d}][{record.get('account', 'unknown')}] "
        f"检测到手机号已绑定，先清理本设备 GoPay linked app 后同号重试"
    )
    result = await provider.unlink_openai()
    (out_dir / "last_gopay_unlink_before_retry.txt").write_text(str(result), encoding="utf-8")
    if result.get("ok"):
        status = "本来已解绑" if result.get("alreadyUnlinked") else "已解绑"
        log(f"[worker-{slot.worker_id:02d}][{record.get('account', 'unknown')}] GoPay 预清理完成: {status}")
    else:
        log(
            f"[worker-{slot.worker_id:02d}][{record.get('account', 'unknown')}] "
            f"GoPay 预清理失败: {short_status(result)}"
        )
    return result


async def batch_worker(
    args: argparse.Namespace,
    slot: Flow2DeviceSlot,
    queue: asyncio.Queue,
    device_state: BatchDeviceState,
    results: list[bool],
    state: BatchState,
) -> None:
    worker_id = slot.worker_id
    log(
        f"[worker-{worker_id:02d}] 固定设备槽位启动: phone={slot.phone} | "
        f"WhatsApp={slot.whatsapp_device or 'default'} | GoPay={slot.gopay_device or slot.whatsapp_device or 'default'}"
    )
    while True:
        if await device_state.is_bad(slot):
            log(f"[worker-{worker_id:02d}] 当前设备槽位已标记异常，停止该 worker")
            return
        async with state.lock:
            if state.success_count >= state.target_count:
                return
            if state.success_count + state.active_count >= state.target_count:
                return
            try:
                record = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            state.active_count += 1
        result: bool | str = False
        try:
            ok = await process_record(args, record, phone=slot.phone, worker_id=worker_id)
            if ok == "nonzero":
                result = "nonzero"
            elif ok == "terminal_complete":
                result = "terminal_complete"
            elif ok == "unknown_due":
                result = "unknown_due"
            else:
                result = ok
        except PhoneRetryableError as exc:
            log(f"[worker-{worker_id:02d}][{record.get('account', 'unknown')}] {exc}")
            if exc.status == "account_already_linked":
                unlink_result = await unlink_gopay_for_slot(args, slot, record)
                if unlink_result.get("ok"):
                    try:
                        log(f"[worker-{worker_id:02d}][{record.get('account', 'unknown')}] 使用同一手机号重试一次")
                        ok = await process_record(args, record, phone=slot.phone, worker_id=worker_id)
                        if ok == "nonzero":
                            result = "nonzero"
                        elif ok == "terminal_complete":
                            result = "terminal_complete"
                        elif ok == "unknown_due":
                            result = "unknown_due"
                        else:
                            result = ok
                    except PhoneRetryableError as retry_exc:
                        log(f"[worker-{worker_id:02d}][{record.get('account', 'unknown')}] 同号重试仍失败: {retry_exc}")
                        await device_state.mark_bad(slot, retry_exc.status)
                        result = "device_bad"
                else:
                    await device_state.mark_bad(slot, "unlink_failed")
                    result = "device_bad"
            else:
                await device_state.mark_bad(slot, exc.status)
                result = "device_bad"
        except Exception as exc:  # noqa: BLE001
            log(f"[worker-{worker_id:02d}][{record.get('account', 'unknown')}] 失败: {exc}")
            result = False
        finally:
            async with state.lock:
                state.active_count = max(0, state.active_count - 1)
                results.append(result)
                if result is True:
                    state.success_count += 1
            queue.task_done()


async def run_batch(args: argparse.Namespace, count: int, workers: int, slots: list[Flow2DeviceSlot]) -> None:
    started_at = asyncio.get_event_loop().time()
    raw_records = read_payment_links(args.success_file)
    deduped_records = dedupe_payment_links(raw_records)
    processed_accounts = read_flow2_processed_accounts(args.paid_output, args.nonzero_output)
    records = filter_unprocessed_payment_links(deduped_records, processed_accounts)
    if len(records) < len(raw_records):
        log(
            f"流程二长链接列表已过滤: 原始={len(raw_records)} | "
            f"去重={len(deduped_records)} | 已处理账号={len(processed_accounts)} | 待处理={len(records)}"
        )
    if len(records) < count:
        log(f"流程二可处理长链接不足，目标从 {count} 调整为 {len(records)}")
        count = len(records)
    queue: asyncio.Queue = asyncio.Queue()
    args.batch_target_count = count
    for record in records:
        queue.put_nowait(record)
    results: list[bool] = []
    state = BatchState(count)
    device_state = BatchDeviceState()
    active_slots = slots[:workers]
    tasks = [
        asyncio.create_task(batch_worker(args, slot, queue, device_state, results, state))
        for slot in active_slots
    ]
    await asyncio.gather(*tasks)
    success_count = sum(1 for item in results if item is True)
    nonzero_count = sum(1 for item in results if item == "nonzero")
    terminal_count = sum(1 for item in results if item == "terminal_complete")
    unknown_due_count = sum(1 for item in results if item == "unknown_due")
    device_bad_count = sum(1 for item in results if item == "device_bad")
    failed_count = sum(1 for item in results if item is False)
    elapsed = asyncio.get_event_loop().time() - started_at
    finished_accounts = success_count + nonzero_count + terminal_count + unknown_due_count + failed_count
    avg_accounts = finished_accounts if finished_accounts > 0 else max(1, count)
    avg_elapsed = elapsed / avg_accounts
    log(
        f"批量结束: 成功={success_count}/{count} | 非0跳过={nonzero_count} | "
        f"会话已完成移除={terminal_count} | 金额未识别保留={unknown_due_count} | "
        f"设备异常={device_bad_count} | 失败={failed_count}"
    )
    log(
        f"流程二耗时统计: 本次总耗时={format_duration(elapsed)} | "
        f"平均每个账号={format_duration(avg_elapsed)} | 并发线程={workers}"
    )
    if device_state.bad:
        bad_text = ", ".join(f"worker-{worker_id:02d}:{reason}" for worker_id, reason in device_state.bad.items())
        log(f"本轮标记异常设备槽位: {bad_text}")


def interactive_batch(args: argparse.Namespace) -> int:
    raw_records = read_payment_links(args.success_file)
    deduped_records = dedupe_payment_links(raw_records)
    processed_accounts = read_flow2_processed_accounts(args.paid_output, args.nonzero_output)
    records = filter_unprocessed_payment_links(deduped_records, processed_accounts)
    env = load_env(".env")
    slots = parse_flow2_device_slots(env)
    print()
    print("流程二：GoPay 支付长链接")
    import authorization_flow

    slot_rows = [
        [
            f"槽位 worker-{slot.worker_id}",
            f"{slot.phone} / WA={slot.whatsapp_device or 'default'} / GP={slot.gopay_device or slot.whatsapp_device or 'default'}",
        ]
        for slot in slots
    ] or [["槽位绑定", "未配置"]]
    authorization_flow.print_table(
        ["项目", "当前值"],
        [
            [
                "待处理长链接账号数",
                f"{len(records)}"
                + (
                    f"（原始 {len(raw_records)}，去重 {len(deduped_records)}，已处理 {len(processed_accounts)}）"
                    if len(records) < len(deduped_records) or len(deduped_records) < len(raw_records)
                    else ""
                ),
            ],
            ["设备槽位数", len(slots)],
            *slot_rows,
            ["WhatsApp ADB 自动取码", "开启" if whatsapp_otp_enabled(env) else "关闭"],
            ["GoPay 支付后自动解绑", "开启" if gopay_unlink_enabled(env) else "关闭"],
        ],
    )
    if len(records) <= 0:
        print("没有可处理的长链接账号。")
        return 0
    if len(slots) <= 0:
        print("请先在 .env 配置 GOPAY_PHONE_1/GOPAY_PHONE_2，或兼容旧写法 GOPAY_PHONES=15700000001,15700000002")
        return 1
    if not check_flow2_adb_ready(env, slots):
        return 1
    max_count = len(records)
    count = ask_int("请输入这次要处理几个长链接账号", default=min(1, max_count), min_value=1, max_value=max_count)
    max_workers = min(count, len(slots))
    workers = ask_int("请输入并发线程数", default=1, min_value=1, max_value=max_workers)
    if workers > len(slots):
        print(f"线程数不能超过设备槽位数量：{len(slots)}")
        return 1
    args.submit = True
    args.wait_otp = True
    args.wait_manual_success = True
    args.remove_paid_source = True
    args.remove_nonzero_source = True
    args.continue_after_nonzero = True
    args.retry_technical_error = True
    asyncio.run(run_batch(args, count=count, workers=workers, slots=slots))
    return 0


def main() -> int:
    fast_defaults = flow2_fast_defaults(load_env(".env"))
    parser = argparse.ArgumentParser(description="打开支付长链接并测试填 GoPay 账单")
    parser.add_argument("--success-file", default=output_file("flow1_success"), help="流程 1 注册成功长链接文件")
    parser.add_argument("--index", type=int, help="使用第几条支付长链接，默认最后一条")
    parser.add_argument("--account", help="按账号选择支付长链接")
    parser.add_argument("--config", default="config.yaml", help="配置文件")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None, help="是否无头运行")
    parser.add_argument("--keep-open", action="store_true", help="填完后保持浏览器打开，方便人工检查")
    parser.add_argument("--submit", action="store_true", help="填完账单后点击订阅并进入 GoPay 手机号页")
    parser.add_argument("--stop-at-phone", action="store_true", help="只验证跳到 GoPay 手机页，不填手机号")
    parser.add_argument("--phone", help="GoPay 手机号，只在 --submit 时使用")
    parser.add_argument("--pin", help="GoPay 6 位 PIN；默认读取 .env 的 GOPAY_PIN/GOPAY_PAYMENT_PIN")
    parser.add_argument("--country-code", default=default_country_code(load_env(".env")), help="GoPay 手机国家码，默认读 .env 的 GOPAY_COUNTRY_CODE，未配置则用 +62")
    parser.add_argument("--billing-retries", type=int, default=fast_defaults["billing_retries"], help="账单地址不可用时换地址重试次数")
    parser.add_argument("--wait-otp", action="store_true", help="点击 Link and pay 后等待 OTP/错误/PIN 下一页并停止")
    parser.add_argument("--prompt-otp", action="store_true", help="等待 OTP 后在终端输入验证码，脚本填入并继续到下一页")
    parser.add_argument("--prompt-pin", action="store_true", help="到 GoPay 6 位 PIN 页后在终端输入支付 PIN，脚本填入并继续")
    parser.add_argument("--otp-timeout", type=int, default=fast_defaults["otp_timeout"], help="等待 OTP 页超时时间，单位秒")
    parser.add_argument("--retry-technical-error", action="store_true", help="GoPay technical error 时点 Back 并按间隔重试 Link and pay")
    parser.add_argument("--retry-interval", type=int, default=fast_defaults["retry_interval"], help="technical error 重试间隔，单位秒")
    parser.add_argument("--retry-timeout", type=int, default=fast_defaults["retry_timeout"], help="technical error 最长重试时长，单位秒")
    parser.add_argument("--force-country-dom", action="store_true", help="调试兜底：真实国家码下拉失败时强制改 .phone-code 和国旗显示")
    parser.add_argument("--use-linking-api", action=argparse.BooleanOptionalAction, default=True, help="优先用 Midtrans linking API 提交手机号，失败时回退按钮点击")
    parser.add_argument("--use-proxy", action=argparse.BooleanOptionalAction, default=None, help="本次账单/支付测试是否启用代理")
    parser.add_argument("--proxy-file", help="代理池文件，默认读取 .env 的 PROXY_FILE")
    parser.add_argument("--proxy", help="直接指定本次使用的代理，优先级最高")
    parser.add_argument("--wait-manual-success", action="store_true", help="手机号发送后等待你手动完成支付，检测成功后落盘")
    parser.add_argument("--manual-success-timeout", type=int, default=fast_defaults["manual_success_timeout"], help="等待手动支付成功超时时间，单位秒")
    parser.add_argument("--paid-output", default=output_file("flow2_paid_success"), help="流程 2 支付成功待授权文件，只写账号和接码地址")
    parser.add_argument("--remove-paid-source", action="store_true", help="支付成功落盘后，从 success-file 删除对应长链接账号块")
    parser.add_argument("--nonzero-output", default=output_file("flow2_nonzero_billing"), help="流程 2 今日应付明确非 0 元时的记录文件")
    parser.add_argument("--remove-nonzero-source", action="store_true", help="今日应付明确非 0 元时，从 success-file 删除对应账号块；金额未识别不会删除")
    parser.add_argument("--continue-after-nonzero", action="store_true", help="批量模式遇到非 0 元账号后继续消费队列中的后续账号")
    parser.add_argument("--batch", action="store_true", help="批量模式：从 .env 读取 GoPay 设备槽位，询问处理数量和线程数")
    args = parser.parse_args()
    if args.batch or (len(sys.argv) <= 1):
        return interactive_batch(args)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
