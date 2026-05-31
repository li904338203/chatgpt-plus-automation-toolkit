from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

import state_db
from error_classifier import classify_exit
from modules.hero_sms_provider import (
    HeroSMSProvider,
    OperatorQuote,
    PhoneCountry,
    configured_country_catalog,
    enrich_countries_with_api,
    match_country,
)
from modules.grizzly_sms_provider import GrizzlySMSProvider
from modules.fivesim_sms_provider import (
    FiveSimProvider,
    FIVESIM_ISO_TO_COUNTRY,
    configured_fivesim_countries,
)
from modules.paypal_phone_pool import PhoneInfo, PhonePool
from modules.terminal_theme import install_print_theme
from modules.utils import LEGACY_OUTPUT_FILES, log, migrate_output_file, output_file, resolve_path


AUTH_SCRIPT = resolve_path("get_oauth_rt.py")
AUTH_ROOT = AUTH_SCRIPT.parent
ANSI_PURPLE = "\033[95m"
ANSI_RESET = "\033[0m"

install_print_theme()


def read_paid_accounts(path: str | Path = output_file("flow2_paid_success")) -> list[dict[str, str]]:
    input_path = migrate_output_file(path, LEGACY_OUTPUT_FILES["flow2_paid_success"])
    if not input_path.exists():
        return []
    text = input_path.read_text(encoding="utf-8")
    pattern = re.compile(r"璐﹀彿锛歕s*([^\s\t]+).*?鎺ョ爜鍦板潃锛歕s*(https?://.*?)(?=\s*璐﹀彿锛殀\Z)", re.S)
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff\u200b\u2060")
        if not line or "----" not in line:
            continue
        parts = [part.strip() for part in line.split("----", 3)]
        account = parts[0] if parts else ""
        if not re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", account):
            continue
        if account.lower() in seen:
            continue
        seen.add(account.lower())
        if len(parts) == 4:
            _, password, client_id, refresh_token = parts
            records.append(
                {
                    "account": account,
                    "code_address": account,
                    "password": password,
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "source_format": "hotmail_graph",
                }
            )
            continue
        if len(parts) == 2:
            _, code_address = parts
            if (not code_address or code_address.lower() == account.lower()) and account.lower().endswith("@edu.hanyiz2.com"):
                code_address = "imap163"
            source_format = "domain163" if (code_address or "").strip().lower() == "imap163" else "icloud_query"
            records.append(
                {
                    "account": account,
                    "code_address": code_address,
                    "source_format": source_format,
                }
            )
            continue
    for account, code_address in pattern.findall(text):
        account = account.strip()
        code_address = re.sub(r"\s+", "", code_address).strip()
        if account and code_address and account.lower() not in seen:
            seen.add(account.lower())
            records.append({"account": account, "code_address": code_address})
    return records


def write_single_account_input(record: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if record.get("password") and record.get("client_id") and record.get("refresh_token"):
        text = (
            f"{record['account']}----{record.get('password', '').strip()}"
            f"----{record.get('client_id', '').strip()}"
            f"----{record.get('refresh_token', '').strip()}\n"
        )
    else:
        text = f"账号：{record['account']}\n接码地址：{record['code_address']}\n"
    path.write_text(text, encoding="utf-8")


def write_paid_accounts(path: str | Path, records: list[dict[str, str]]) -> None:
    output_path = migrate_output_file(path, LEGACY_OUTPUT_FILES["flow2_paid_success"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        if not record.get("account"):
            continue
        if record.get("password") and record.get("client_id") and record.get("refresh_token"):
            lines.append(
                f"{record['account']}----{record.get('password', '').strip()}"
                f"----{record.get('client_id', '').strip()}"
                f"----{record.get('refresh_token', '').strip()}"
            )
        elif record.get("code_address"):
            lines.append(f"{record['account']}----{record['code_address']}")
    text = "\n".join(lines) + ("\n" if lines else "")
    output_path.write_text(text, encoding="utf-8")


def authorized_accounts(output_root: Path) -> set[str]:
    accounts: set[str] = set()
    rt_file = output_root / "account-rt.txt"
    if rt_file.exists():
        for line in rt_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            account = line.split("----", 1)[0].strip()
            if "@" in account:
                accounts.add(account.lower())

    token_dir = output_root / "tokens"
    if token_dir.exists():
        for token_file in token_dir.glob("*.json"):
            match = re.search(r"_([^_\\]+@[^_\\]+?)_(?:plus|free|team)?\.json$", token_file.name, re.I)
            if match:
                accounts.add(match.group(1).lower())
                continue
            text = token_file.name
            email_match = re.search(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9_.-]+)", text)
            if email_match:
                accounts.add(email_match.group(1).lower())

    sub_file = output_root / "sub2api_accounts.json"
    if sub_file.exists():
        try:
            data = json.loads(sub_file.read_text(encoding="utf-8"))
            for item in data.get("accounts", []):
                email = (
                    item.get("name")
                    or item.get("extra", {}).get("email")
                    or item.get("credentials", {}).get("email")
                )
                if isinstance(email, str) and "@" in email:
                    accounts.add(email.lower())
        except Exception:
            pass
    return accounts


def remove_accounts_from_paid_file(path: str | Path, accounts: set[str]) -> int:
    if not accounts:
        return 0
    records = read_paid_accounts(path)
    remaining = [record for record in records if record["account"].lower() not in accounts]
    removed = len(records) - len(remaining)
    if removed:
        write_paid_accounts(path, remaining)
    return removed


def accounts_by_error_type(db_path: Path, error_type: str) -> set[str]:
    if not db_path.exists():
        return set()
    result: set[str] = set()
    try:
        for item in state_db.latest_tasks(db_path, limit=500):
            if str(item.get("error_type") or "") == error_type and str(item.get("email") or "").strip():
                result.add(str(item["email"]).strip().lower())
    except Exception:
        return set()
    return result


def accounts_by_invalid_state_count(db_path: Path, min_count: int = 2) -> set[str]:
    if not db_path.exists():
        return set()
    result: set[str] = set()
    try:
        for item in state_db.latest_tasks(db_path, limit=500):
            if int(item.get("invalid_state_count") or 0) >= min_count and str(item.get("email") or "").strip():
                result.add(str(item["email"]).strip().lower())
    except Exception:
        return set()
    return result


def append_discarded_accounts(records: list[dict[str, str]], reason: str, output_root: Path) -> Path:
    path = output_root / "娴佺▼3_寮冪疆璐﹀彿.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    existing_emails = {match.group(0).lower() for match in re.finditer(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", existing)}
    lines = []
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for record in records:
        account = record.get("account", "").strip()
        if account and account.lower() not in existing_emails:
            lines.append(f"{stamp}\t{account}\t{reason}")
    if lines:
        with path.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write("\n".join(lines) + "\n")
    return path


def ask_int(prompt: str, default: int, min_value: int, max_value: int) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        value = default
        if raw:
            try:
                value = int(raw)
            except ValueError:
                print("请输入数字。")
                continue
        if value < min_value or value > max_value:
            print(f"请输入 {min_value} - {max_value} 之间的数字。")
            continue
        return value


def auth_output_root() -> Path:
    path = resolve_path("output/gopay娉ㄥ唽plus/鎺堟潈杈撳嚭")
    path.mkdir(parents=True, exist_ok=True)
    (path / "tokens").mkdir(parents=True, exist_ok=True)
    return path


def read_env_keys(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def env_bool(value: str | None, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def env_int(value: str | None, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default


def env_float(value: str | None, default: float) -> float:
    try:
        return float(str(value or "").strip())
    except ValueError:
        return default


def server_upload_enabled(env: dict[str, str]) -> bool:
    return (env.get("AUTH_SERVER_UPLOAD") or "").strip().lower() in {"1", "true", "yes", "on"}


def auth_server_env_status() -> tuple[bool, bool, bool]:
    env = read_env_keys(AUTH_ROOT / ".env")
    return (
        server_upload_enabled(env),
        bool((env.get("AUTH_SERVER_URL") or "").strip()),
        bool((env.get("AUTH_SERVER_API_KEY") or "").strip()),
    )


def price_text(value: float | None, digits: int = 3) -> str:
    return f"${value:.{digits}f}" if isinstance(value, (int, float)) else "-"


def count_text(value: int | None) -> str:
    return str(value) if isinstance(value, int) else "-"


def display_width(value: object) -> int:
    text = str(value)
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_display(value: object, width: int, align: str = "left") -> str:
    text = str(value)
    padding = max(0, width - display_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def purple(text: str) -> str:
    return f"{ANSI_PURPLE}{text}{ANSI_RESET}"


def print_table(headers: list[str], rows: list[list[object]], *, aligns: list[str] | None = None) -> None:
    aligns = aligns or ["left"] * len(headers)
    widths = [
        max(display_width(headers[index]), *(display_width(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def border(left: str, middle: str, right: str) -> str:
        return purple(left + middle.join("-" * (width + 2) for width in widths) + right)

    def line(values: list[object], header: bool = False) -> str:
        cells = []
        for index, value in enumerate(values):
            align = "left" if header else aligns[index]
            cells.append(f" {pad_display(value, widths[index], align)} ")
        return purple("|") + purple("|").join(cells) + purple("|")

    print(border("+", "+", "+"))
    print(line(headers, header=True))
    print(border("+", "+", "+"))
    for row in rows:
        print(line(row))
    print(border("+", "+", "+"))


def print_country_price_table(rows: list[PhoneCountry], title: str = "[SMS] 鎺ョ爜骞冲彴鏈€渚垮疁鍥藉 Top 鍒楄〃", country_id_label: str = "骞冲彴ID") -> None:
    print()
    print(title)
    print_table(
        ["搴忓彿", "ISO", "鍥藉", "鍖哄彿", country_id_label, "浠锋牸", "搴撳瓨"],
        [
            [index, row.iso_code, row.name, f"+{row.dial_code}", row.hero_sms_country, price_text(row.price), count_text(row.count)]
            for index, row in enumerate(rows, start=1)
        ],
        aligns=["right", "left", "left", "right", "right", "right", "right"],
    )


def print_operator_table(rows: list[OperatorQuote], country: PhoneCountry) -> None:
    print()
    print(f"[SMS] {country.name} 鍙€夎繍钀ュ晢 / 鎶ヤ环鍒楄〃")
    print_table(
        ["序号", "运营商", "价格", "库存", "备注"],
        [
            [index, row.label, price_text(row.price, 4), count_text(row.count), row.note or "-"]
            for index, row in enumerate(rows, start=1)
        ],
        aligns=["right", "left", "right", "right", "left"],
    )


def choose_country_interactively(rows: list[PhoneCountry], default_country: PhoneCountry, forced: str = "", provider_label: str = "鎺ョ爜骞冲彴") -> PhoneCountry:
    if forced:
        matched = match_country(forced, rows)
        if matched:
            print(f"[SMS] 已通过 --country 指定国家: {matched.name} (+{matched.dial_code})，{provider_label} 国家ID={matched.hero_sms_country}")
            return matched
        print(f"[SMS] --country={forced} 未匹配当前列表，改用交互选择。")
    while True:
        answer = input(f"璇烽€夋嫨鍥藉锛堝簭鍙烽檺涓婃柟鍒楄〃锛汭SO / {provider_label} 鍥藉ID 鍙粠瀹屾暣浠锋牸琛ㄥ尮閰嶏紝鐩存帴鍥炶溅榛樿 {default_country.iso_code}锛? ").strip()
        if not answer:
            return default_country
        matched = match_country(answer, rows)
        if matched:
            return matched
        print("输入无效，请重新选择。")


def choose_operator_interactively(rows: list[OperatorQuote], default_option: OperatorQuote, country: PhoneCountry) -> OperatorQuote:
    while True:
        answer = input(f"璇烽€夋嫨 {country.name} 鐨勮繍钀ュ晢锛堣緭鍏ュ簭鍙?/ 鍚嶇О锛岀洿鎺ュ洖杞﹂粯璁?{default_option.label}锛? ").strip()
        if not answer:
            return default_option
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(rows):
                return rows[index - 1]
        lowered = answer.lower()
        for row in rows:
            if row.operator.lower() == lowered or row.label.lower() == lowered:
                return row
        print("输入无效，请重新选择。")


def _provider_uses_country_slug(provider) -> bool:
    return isinstance(provider, FiveSimProvider)


def resolve_sms_operator(provider, service: str, country: PhoneCountry, threshold: int, *, always_prompt: bool = False) -> OperatorQuote:
    aggregate_count = country.count
    aggregate_option = OperatorQuote("", "任何运营商", country.price, country.count, "聚合报价")
    if not always_prompt and (aggregate_count is None or aggregate_count >= threshold):
        print(f"[SMS] {country.name} 当前聚合库存 {count_text(aggregate_count)}，不触发二次运营商选择")
        return aggregate_option
    try:
        # HeroSMS/Grizzly 使用 hero_sms_country int；FiveSim 支持直接传 PhoneCountry
        operator_arg: Any = country if _provider_uses_country_slug(provider) else country.hero_sms_country
        operator_options = provider.get_operator_quote_options(service, operator_arg)
    except Exception as exc:
        print(f"[SMS] 获取 {country.name} 运营商列表失败，使用“任何运营商”: {exc}")
        return aggregate_option
    if always_prompt and not operator_options:
        print(f"[SMS] {country.name} 暂未返回可选服务商明细，使用“任何运营商”")
        return aggregate_option
    rows = [aggregate_option, *operator_options]
    if always_prompt and not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(f"[SMS] 当前不是交互终端，自动使用默认服务商: {aggregate_option.label}")
        return aggregate_option
    if always_prompt:
        print(f"[SMS] {country.name} 聚合库存 {count_text(aggregate_count)}，请选择服务商；直接回车使用“任何运营商”")
    else:
        print(f"[SMS] {country.name} 聚合库存 {count_text(aggregate_count)}，低于 {threshold}，进入运营商二次选择")
    print_operator_table(rows, country)
    better = next((row for row in operator_options if isinstance(row.count, int) and row.count > int(country.count or 0)), None)
    default_option = better or aggregate_option
    selected = choose_operator_interactively(rows, default_option, country)
    print(f"[SMS] 已选择运营商: {selected.label} ({country.name})")
    return selected


def flow_env_value(env: dict[str, str], flow_key: str, name: str) -> str:
    if flow_key:
        value = (env.get(f"{flow_key.upper()}_{name}") or "").strip()
        if value:
            return value
    return (env.get(name) or "").strip()


def resolve_authorization_sms_selection(args: argparse.Namespace, flow_label: str = "流程三", flow_key: str = "") -> dict[str, object] | None:
    env = read_env_keys(AUTH_ROOT / ".env")
    sms_enabled_value = flow_env_value(env, flow_key, "SMS_ENABLED")
    if not env_bool(sms_enabled_value, default=True):
        if flow_key.upper() == "FREE":
            print(f"[SMS] 手机号接码已关闭，{flow_label}无法继续；请开启 FREE_SMS_ENABLED。")
            return None
        print(f"[SMS] 手机号接码已关闭，{flow_label}保持原逻辑：遇到手机号必填页会弃置账号。")
        return None

    provider_name = (getattr(args, "sms_provider", "") or flow_env_value(env, flow_key, "SMS_PROVIDER") or "herosms").strip().lower()
    if provider_name in {"hero", "hero_sms", "herosms"}:
        provider_name = "herosms"
    elif provider_name in {"grizzly", "grizzlysms", "grizzly_sms"}:
        provider_name = "grizzly"
    elif provider_name in {"5sim", "fivesim", "5sims", "five_sim"}:
        provider_name = "fivesim"
    else:
        print(f"[SMS] SMS_PROVIDER={provider_name} 暂不支持，{flow_label}保持原手机号失败处理。")
        return None

    if provider_name == "grizzly":
        api_key = (getattr(args, "grizzly_api_key", "") or env.get("GRIZZLY_API_KEY") or env.get("SMS_API_KEY") or "").strip()
        api_key_name = "GRIZZLY_API_KEY"
        provider_label = "GrizzlySMS"
        provider = GrizzlySMSProvider(api_key) if api_key else None
        raw_service = (getattr(args, "grizzly_service", "") or env.get("GRIZZLY_SERVICE") or "auto").strip() or "auto"
        service = provider.resolve_openai_service(raw_service) if provider else "auto"
        top_n = env_int(str(getattr(args, "grizzly_country_top_n", "") or env.get("GRIZZLY_COUNTRY_TOP_N") or ""), 10)
        threshold = env_int(str(getattr(args, "grizzly_provider_threshold", "") or env.get("GRIZZLY_PROVIDER_THRESHOLD") or ""), 20)
        prompt_operator = env_bool(env.get("GRIZZLY_PROMPT_PROVIDER_SELECTION"), default=True)
        forced_country = (getattr(args, "country", "") or env.get("GRIZZLY_COUNTRY_SELECT") or "").strip()
        prompt_country = env_bool(env.get("GRIZZLY_PROMPT_COUNTRY_SELECTION"), default=True)
        poll_interval = env_float(env.get("GRIZZLY_POLL_INTERVAL"), 5.0)
        max_attempts = env_int(env.get("GRIZZLY_MAX_ATTEMPTS"), 60)
    elif provider_name == "fivesim":
        api_key = (getattr(args, "fivesim_api_key", "") or env.get("FIVESIM_API_KEY") or env.get("SMS_API_KEY") or "").strip()
        api_key_name = "FIVESIM_API_KEY"
        provider_label = "5sim"
        provider = FiveSimProvider(api_key) if api_key else None
        raw_service = (getattr(args, "fivesim_service", "") or env.get("FIVESIM_SERVICE") or "openai").strip() or "openai"
        service = provider.resolve_openai_service(raw_service) if provider else "openai"
        top_n = env_int(str(getattr(args, "fivesim_country_top_n", "") or env.get("FIVESIM_COUNTRY_TOP_N") or ""), 10)
        threshold = env_int(str(getattr(args, "fivesim_operator_threshold", "") or env.get("FIVESIM_OPERATOR_THRESHOLD") or ""), 20)
        prompt_operator = env_bool(env.get("FIVESIM_PROMPT_OPERATOR_SELECTION"), default=True)
        forced_country = (getattr(args, "country", "") or env.get("FIVESIM_COUNTRY_SELECT") or "").strip()
        prompt_country = env_bool(env.get("FIVESIM_PROMPT_COUNTRY_SELECTION"), default=True)
        poll_interval = env_float(env.get("FIVESIM_POLL_INTERVAL"), 5.0)
        max_attempts = env_int(env.get("FIVESIM_MAX_ATTEMPTS"), 60)
    else:
        api_key = (
            getattr(args, "hero_sms_api_key", "")
            or env.get("HERO_SMS_API_KEY")
            or env.get("HEROSMS_API_KEY")
            or env.get("SMS_API_KEY")
            or ""
        ).strip()
        api_key_name = "HERO_SMS_API_KEY"
        provider_label = "HeroSMS"
        provider = HeroSMSProvider(api_key) if api_key else None
        service = (getattr(args, "hero_sms_service", "") or env.get("HERO_SMS_SERVICE") or "dr").strip() or "dr"
        top_n = env_int(str(getattr(args, "hero_sms_country_top_n", "") or env.get("HERO_SMS_COUNTRY_TOP_N") or ""), 10)
        threshold = env_int(str(getattr(args, "hero_sms_operator_threshold", "") or env.get("HERO_SMS_OPERATOR_THRESHOLD") or ""), 20)
        prompt_operator = env_bool(env.get("HERO_SMS_PROMPT_OPERATOR_SELECTION"), default=True)
        forced_country = (getattr(args, "country", "") or env.get("HERO_SMS_COUNTRY_SELECT") or "").strip()
        prompt_country = env_bool(env.get("HERO_SMS_PROMPT_COUNTRY_SELECTION"), default=True)
        poll_interval = env_float(env.get("HERO_SMS_POLL_INTERVAL"), 5.0)
        max_attempts = env_int(env.get("HERO_SMS_MAX_ATTEMPTS"), 60)

    if not api_key:
        print(f"[SMS] 未配置 {api_key_name}，{flow_label}将不启用外部接码平台。")
        return None
    if provider is None:
        return None

    catalog = configured_fivesim_countries() if provider_name == "fivesim" else configured_country_catalog()
    try:
        if provider_name == "fivesim":
            priced = provider.list_country_prices(service, catalog)
            if not priced:
                raise RuntimeError("5sim 未返回可用国家报价")
        else:
            api_countries = provider.get_countries()
            countries = enrich_countries_with_api(catalog, api_countries) if api_countries else catalog
            priced = provider.list_country_prices(service, countries)
            if not priced:
                raise RuntimeError("未解析出任何可用国家报价")

        if provider_name == "grizzly":
            known_rows = [row for row in priced if row.iso_code and row.dial_code]
            if known_rows:
                priced = known_rows + [row for row in priced if not (row.iso_code and row.dial_code)]

        top_rows = priced[: max(1, top_n)]
        print_country_price_table(top_rows, title=f"[SMS] {provider_label} 最便宜国家 Top 列表", country_id_label=f"{provider_label} ID")
        default_country = top_rows[0]
        selected_country = match_country(forced_country, priced) if forced_country else None
        if selected_country:
            print(f"[SMS] 已通过 --country 指定国家: {selected_country.name} (+{selected_country.dial_code})，价格 {price_text(selected_country.price)}")
        elif prompt_country and sys.stdin.isatty() and sys.stdout.isatty():
            selected_country = choose_country_interactively(priced, default_country, forced_country, provider_label)
        else:
            if prompt_country:
                print(f"[SMS] 当前不是交互终端，自动使用默认国家: {default_country.name} (+{default_country.dial_code})")
            selected_country = default_country

        selected_operator = resolve_sms_operator(provider, service, selected_country, threshold, always_prompt=prompt_operator)
        print(
            f"[SMS] 已选择国家: {selected_country.name} (+{selected_country.dial_code})，"
            f"{provider_label} 国家ID={selected_country.hero_sms_country}，价格 {price_text(selected_country.price)}"
        )
        return {
            "provider": provider_name,
            "provider_label": provider_label,
            "api_key": api_key,
            "service": service,
            "country": selected_country,
            "operator": selected_operator,
            "poll_interval": poll_interval,
            "max_attempts": max_attempts,
        }
    except Exception as exc:
        print(f"[SMS] 获取 {provider_label} 国家/价格失败，{flow_label}将不启用外部接码平台: {exc}")
        return None


def build_auth_command(
    record: dict[str, str],
    account_file: Path,
    output_root: Path,
    sms_selection: dict[str, object] | None = None,
    auth_phone: PhoneInfo | None = None,
) -> list[str]:
    if getattr(sys, "frozen", False):
        command = [
            sys.executable,
            "--runner",
            "oauth-login",
            "login",
        ]
    else:
        command = [
            sys.executable,
            "-u",
            str(AUTH_SCRIPT),
            "login",
        ]
    command.extend(
        [
        "--account-file",
        str(account_file),
        "--account-email",
        record["account"],
        "--auth-mode",
        "normal",
        "--prefer-otp",
        "--mail-code-timeout",
        "120",
        "--mail-code-interval",
        "2",
        "--standard-output",
        "--output-dir",
        str(output_root / "tokens"),
        "--rt-txt",
        str(output_root / "account-rt.txt"),
        "--sub-out",
        str(output_root / "sub2api_accounts.json"),
        "--store",
        str(output_root / "oauth-rt-accounts.json"),
        "--state-db",
        str(output_root / "auth_tasks.db"),
        "--remove-after-success",
        "--invalid-state-retries",
        "2",
        ]
    )
    if sms_selection:
        country = sms_selection.get("country")
        operator = sms_selection.get("operator")
        if isinstance(country, PhoneCountry):
            provider_name = str(sms_selection.get("provider") or "herosms")
            # 5sim 需要国家 slug（例如 indonesia）
            fivesim_slug = FIVESIM_ISO_TO_COUNTRY.get(country.iso_code.upper(), "") if provider_name in {"fivesim", "5sim"} else ""
            command.extend(
                [
                    "--sms-provider",
                    provider_name,
                    "--sms-api-key",
                    str(sms_selection.get("api_key") or ""),
                    "--sms-service",
                    str(sms_selection.get("service") or "dr"),
                    "--sms-country",
                    str(country.hero_sms_country),
                    "--sms-country-iso",
                    country.iso_code,
                    "--sms-dial-code",
                    country.dial_code,
                    "--sms-country-name",
                    country.name,
                    "--sms-poll-interval",
                    str(sms_selection.get("poll_interval") or 5),
                    "--sms-max-attempts",
                    str(sms_selection.get("max_attempts") or 60),
                    "--hero-sms-api-key",
                    str(sms_selection.get("api_key") or ""),
                    "--hero-sms-service",
                    str(sms_selection.get("service") or "dr"),
                    "--hero-sms-country",
                    str(country.hero_sms_country),
                    "--hero-sms-country-iso",
                    country.iso_code,
                    "--hero-sms-dial-code",
                    country.dial_code,
                    "--hero-sms-country-name",
                    country.name,
                    "--hero-sms-poll-interval",
                    str(sms_selection.get("poll_interval") or 5),
                    "--hero-sms-max-attempts",
                    str(sms_selection.get("max_attempts") or 60),
                ]
            )
            if fivesim_slug:
                command.extend(["--fivesim-country-slug", fivesim_slug])
        if isinstance(operator, OperatorQuote) and operator.operator:
            command.extend(["--sms-operator", operator.operator, "--hero-sms-operator", operator.operator])
    if auth_phone and auth_phone.number and auth_phone.api_url:
        command.extend(
            [
                "--auth-phone-number",
                auth_phone.number,
                "--auth-phone-api-url",
                auth_phone.api_url,
            ]
        )
    return command


def run_one(
    record: dict[str, str],
    index: int,
    total: int,
    output_root: Path,
    sms_selection: dict[str, object] | None = None,
    auth_phone_pool: PhonePool | None = None,
) -> tuple[bool, str]:
    safe_name = re.sub(r"[^a-zA-Z0-9_.@-]+", "_", record["account"])
    work_dir = output_root / "inputs" / safe_name
    account_file = work_dir / "account.txt"
    write_single_account_input(record, account_file)

    log(f"[授权 {index}/{total}] 开始: {record['account']}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    state_db_path = output_root / "auth_tasks.db"
    source_path = str(account_file)
    attempts = 1
    if auth_phone_pool is not None:
        attempts = max(1, auth_phone_pool.count())
    final_returncode = 1
    final_error_text = ""
    final_error_type = ""
    for attempt in range(1, attempts + 1):
        assigned_phone: PhoneInfo | None = None
        phone_marked_failed = False
        if auth_phone_pool is not None:
            assigned_phone = auth_phone_pool.acquire(index)
            if not assigned_phone:
                final_returncode = 1
                final_error_type = "auth_phone_pool_empty"
                final_error_text = "授权手机号池已无可用号码"
                log(f"[授权 {index}/{total}] 授权手机号池已无可用号码，终止当前账号: {record['account']}")
                break
            log(f"[授权 {index}/{total}] 已分配授权手机号池号码: {assigned_phone.number}")
        command = build_auth_command(record, account_file, output_root, sms_selection, assigned_phone)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(AUTH_ROOT),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            output_lines: list[str] = []
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="")
                output_lines.append(line)
            process.wait()
            final_returncode = int(process.returncode or 0)
            final_error_text = "".join(output_lines).strip()
            final_error_type = classify_exit(final_returncode, final_error_text)
            if final_returncode == 0:
                break
            lower_error_text = final_error_text.lower()
            phone_link_limited = (
                final_error_type == "auth_phone_link_limit"
                or "phone_link_limit" in lower_error_text
                or "最大账户" in final_error_text
            )
            if phone_link_limited and assigned_phone and auth_phone_pool is not None:
                auth_phone_pool.mark_failed(assigned_phone.number)
                phone_marked_failed = True
                log(f"[授权 {index}/{total}] 授权手机号池号码不可用，已移除: {assigned_phone.number}")
                if attempt < attempts:
                    log(f"[授权 {index}/{total}] 检测到号码关联上限，切换下一手机号重试当前账号 ({attempt}/{attempts})")
                    continue
            state_db.ensure_task(
                state_db_path,
                email=record["account"],
                account_type="normal",
                source_type="flow2_paid",
                source_path=source_path,
                headless=False,
            )
            state_db.finish_task(
                state_db_path,
                email=record["account"],
                account_type="normal",
                source_path=source_path,
                status="failed",
                error_type=final_error_type,
                last_error=final_error_text[:1000],
            )
            break
        finally:
            if assigned_phone and auth_phone_pool is not None and not phone_marked_failed:
                auth_phone_pool.release(assigned_phone.number, success=(final_returncode == 0))

    ok = final_returncode == 0
    status = "成功" if ok else f"失败(code={final_returncode})"
    log(f"[授权 {index}/{total}] {status}: {record['account']}")
    return ok, record["account"]


def interactive_authorize(args: argparse.Namespace | None = None) -> int:
    args = args or argparse.Namespace()
    paid_file = getattr(args, "paid_file", output_file("flow2_paid_success"))
    records = read_paid_accounts(paid_file)

    custom_output_root = getattr(args, "output_root", None)
    output_root = Path(custom_output_root) if custom_output_root else auth_output_root()
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "tokens").mkdir(parents=True, exist_ok=True)

    already_authorized = authorized_accounts(output_root)
    removed_existing = remove_accounts_from_paid_file(paid_file, already_authorized)
    if removed_existing:
        log(f"已从待授权账号池移除历史已授权账号: {removed_existing} 个")
        records = read_paid_accounts(paid_file)

    upload_enabled, upload_url_ok, upload_key_ok = auth_server_env_status()
    upload_status = "开启" if upload_enabled else "关闭"
    upload_ready = "配置完整" if upload_url_ok and upload_key_ok else "缺少 URL/API_KEY"
    print()
    print("流程三：账号授权")
    print_table(
        ["项目", "当前值"],
        [
            ["账号来源", resolve_path(paid_file)],
            ["授权输出", output_root],
            ["可授权账号数", len(records)],
            ["服务器上传", f"{upload_status}（{upload_ready}，读取当前项目 .env）"],
        ],
    )

    if not getattr(sys, "frozen", False) and not AUTH_SCRIPT.exists():
        print(f"[error] 找不到授权脚本: {AUTH_SCRIPT}")
        return 1
    if not records:
        print("没有可授权账号，请先完成流程2支付成功输出。")
        return 0

    raw_count = getattr(args, "count", None)
    if raw_count is None:
        count = ask_int("请输入这次要授权几个账号", default=min(1, len(records)), min_value=1, max_value=len(records))
    else:
        count = int(raw_count)
        if count < 1 or count > len(records):
            print(f"[error] 授权数量必须在 1 - {len(records)} 之间。")
            return 1

    raw_workers = getattr(args, "workers", None)
    if raw_workers is None:
        workers = ask_int("请输入并发线程数", default=1, min_value=1, max_value=count)
    else:
        workers = int(raw_workers)
        if workers < 1 or workers > count:
            print(f"[error] 并发线程数必须在 1 - {count} 之间。")
            return 1

    selected = records[:count]
    sms_selection = resolve_authorization_sms_selection(args, flow_label="流程三", flow_key="FLOW3")

    flow_env = read_env_keys(AUTH_ROOT / ".env")
    phone_pool_file = flow_env_value(flow_env, "FLOW3", "PHONE_POOL_FILE") or "data/auth/phones.txt"
    phone_pool_max_uses = env_int(flow_env_value(flow_env, "FLOW3", "PHONE_POOL_MAX_USES"), 5)
    auth_phone_pool: PhonePool | None = None
    try:
        pool_candidate = PhonePool(phones_file=phone_pool_file, max_uses=max(1, phone_pool_max_uses))
        pool_available = pool_candidate.count()
        if pool_available > 0:
            auth_phone_pool = pool_candidate
            log(
                f"流程三授权手机号池已启用: {resolve_path(phone_pool_file)} | 可用号码={pool_available} | max_uses={max(1, phone_pool_max_uses)}"
            )
        else:
            log(f"流程三授权手机号池为空或不可用: {resolve_path(phone_pool_file)}，将回退到接码平台")
    except Exception as exc:
        log(f"流程三授权手机号池初始化失败: {exc}，将回退到接码平台")

    run_log = output_root / f"授权运行_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    run_log.write_text(
        "\n".join(f"账号：{item['account']}\t接码地址：{item['code_address']}" for item in selected) + "\n",
        encoding="utf-8",
    )
    log(f"流程三账号授权启动: count={count}, workers={workers}")

    results: list[tuple[bool, str]] = []
    if workers == 1:
        for index, record in enumerate(selected, start=1):
            results.append(run_one(record, index, len(selected), output_root, sms_selection, auth_phone_pool))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(run_one, record, index, len(selected), output_root, sms_selection, auth_phone_pool)
                for index, record in enumerate(selected, start=1)
            ]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

    success = sum(1 for ok, _account in results if ok)
    failed = len(results) - success
    success_accounts = {account.lower() for ok, account in results if ok}
    removed_after_success = remove_accounts_from_paid_file(paid_file, success_accounts)
    if removed_after_success:
        log(f"已从待授权账号池移除本次授权成功账号: {removed_after_success} 个")

    phone_required_accounts = accounts_by_error_type(output_root / "auth_tasks.db", "phone_required")
    selected_by_email = {record["account"].lower(): record for record in selected}
    phone_required_selected = [selected_by_email[email] for email in phone_required_accounts if email in selected_by_email]
    removed_phone_required = remove_accounts_from_paid_file(
        paid_file,
        {selected_by_email[email]["account"].lower() for email in phone_required_accounts if email in selected_by_email},
    )
    if removed_phone_required:
        discarded_path = append_discarded_accounts(phone_required_selected, "授权阶段出现手机号必填页", output_root)
        log(f"已从待授权账号池移除手机号必填弃置账号: {removed_phone_required} 个；记录: {discarded_path}")

    no_valid_org_accounts = accounts_by_error_type(output_root / "auth_tasks.db", "no_valid_organizations")
    no_valid_org_selected = [selected_by_email[email] for email in no_valid_org_accounts if email in selected_by_email]
    removed_no_valid_org = remove_accounts_from_paid_file(
        paid_file,
        {selected_by_email[email]["account"].lower() for email in no_valid_org_accounts if email in selected_by_email},
    )
    if removed_no_valid_org:
        discarded_path = append_discarded_accounts(no_valid_org_selected, "授权阶段 no_valid_organizations 当前页重试2次仍失败", output_root)
        log(f"已从待授权账号池移除 no_valid_organizations 弃置账号: {removed_no_valid_org} 个；记录: {discarded_path}")

    invalid_state_accounts = accounts_by_invalid_state_count(output_root / "auth_tasks.db", 2)
    invalid_state_accounts -= no_valid_org_accounts
    invalid_state_once_accounts = (
        accounts_by_invalid_state_count(output_root / "auth_tasks.db", 1)
        - invalid_state_accounts
        - no_valid_org_accounts
    )
    if invalid_state_once_accounts:
        kept = sorted(email for email in invalid_state_once_accounts if email in selected_by_email)
        if kept:
            log(f"invalid_state 第1次失败，暂不移除账号: {', '.join(kept)}")

    invalid_state_selected = [selected_by_email[email] for email in invalid_state_accounts if email in selected_by_email]
    removed_invalid_state = remove_accounts_from_paid_file(
        paid_file,
        {selected_by_email[email]["account"].lower() for email in invalid_state_accounts if email in selected_by_email},
    )
    if removed_invalid_state:
        discarded_path = append_discarded_accounts(invalid_state_selected, "授权阶段验证状态异常 invalid_state 累计2次", output_root)
        log(f"已从待授权账号池移除 invalid_state 弃置账号: {removed_invalid_state} 个；记录: {discarded_path}")

    log(f"流程三账号授权结束: 成功={success}/{len(selected)}，失败={failed}")
    print()
    print("流程三输出")
    print_table(
        ["产物", "路径"],
        [
            ["CPA tokens", output_root / "tokens"],
            ["RT 文件", output_root / "account-rt.txt"],
            ["SUB 合并文件", output_root / "sub2api_accounts.json"],
            ["SUB 单账号目录", output_root / "sub2api_authorized"],
        ],
    )
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="流程三：账号授权")
    parser.add_argument("--paid-file", default=output_file("flow2_paid_success"), help="流程2支付成功待授权账号文件")
    parser.add_argument("--count", type=int, help="授权账号数量")
    parser.add_argument("--workers", type=int, help="并发线程数")
    parser.add_argument("--country", default="", help="指定国家：序号 / ISO / 接码平台国家ID")
    parser.add_argument("--sms-provider", default="", help="接码平台：herosms / grizzly / fivesim")
    parser.add_argument("--hero-sms-api-key", default="", help="HeroSMS API Key")
    parser.add_argument("--hero-sms-service", default="", help="HeroSMS 服务代码，默认 dr")
    parser.add_argument("--hero-sms-country-top-n", type=int, help="列出最便宜国家数量")
    parser.add_argument("--hero-sms-operator-threshold", type=int, help="库存低于该值时二次选择运营商")
    parser.add_argument("--grizzly-api-key", default="", help="GrizzlySMS API Key")
    parser.add_argument("--grizzly-service", default="", help="GrizzlySMS 服务代码")
    parser.add_argument("--grizzly-country-top-n", type=int, help="列出最便宜国家数量")
    parser.add_argument("--grizzly-provider-threshold", type=int, help="库存低于该值时二次选择服务商")
    parser.add_argument("--fivesim-api-key", default="", help="5sim API Key")
    parser.add_argument("--fivesim-service", default="", help="5sim 服务代码")
    parser.add_argument("--fivesim-country-top-n", type=int, help="5sim 列出最便宜国家数量")
    parser.add_argument("--fivesim-operator-threshold", type=int, help="5sim 库存低于该值时二次选择运营商")
    args = parser.parse_args()
    return interactive_authorize(args)


if __name__ == "__main__":
    raise SystemExit(main())

