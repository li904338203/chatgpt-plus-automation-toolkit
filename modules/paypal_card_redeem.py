from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .paypal_card_pool import CardPool
from .utils import log, resolve_path


_POOL_LOCK = threading.Lock()


@dataclass
class RedeemConfig:
    enabled: bool
    api_url: str
    api_key: str
    timeout_sec: int
    code_field: str
    codes_file: Path
    cards_file: Path
    used_file: Path
    failed_file: Path
    append_when_status_used: bool
    max_auto_fetch: int
    retry_per_code: int
    use_proxy: bool
    proxy_file: Path | None
    stop_on_request_error: bool


def _bool_env(value: str | None, default: bool = False) -> bool:
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_redeem_config(env: dict[str, str]) -> RedeemConfig:
    proxy_file = (
        env.get("PAYPAL_CARD_REDEEM_PROXY_FILE")
        or env.get("PAYPAL_PROXY_FILE")
        or env.get("PROXY_FILE")
        or ""
    ).strip()
    return RedeemConfig(
        enabled=_bool_env(env.get("PAYPAL_CARD_REDEEM_ENABLED"), default=False),
        api_url=(env.get("PAYPAL_CARD_REDEEM_API_URL") or "https://card.52bankcard.com/api/exchange/verify").strip(),
        api_key=(env.get("PAYPAL_CARD_REDEEM_API_KEY") or "").strip(),
        timeout_sec=max(5, int((env.get("PAYPAL_CARD_REDEEM_TIMEOUT") or "20").strip() or "20")),
        code_field=(env.get("PAYPAL_CARD_REDEEM_CODE_FIELD") or "key").strip(),
        codes_file=resolve_path(env.get("PAYPAL_CARD_CODES_FILE") or "data/paypal/card_codes.txt"),
        cards_file=resolve_path(env.get("PAYPAL_CARDS_FILE") or "data/paypal/cards.txt"),
        used_file=resolve_path(env.get("PAYPAL_CARD_CODES_USED_FILE") or "data/paypal/card_codes_used.txt"),
        failed_file=resolve_path(env.get("PAYPAL_CARD_CODES_FAILED_FILE") or "data/paypal/card_codes_failed.txt"),
        append_when_status_used=_bool_env(env.get("PAYPAL_CARD_REDEEM_APPEND_WHEN_STATUS_USED"), default=True),
        max_auto_fetch=max(1, int((env.get("PAYPAL_CARD_REDEEM_MAX_AUTO_FETCH") or "20").strip() or "20")),
        retry_per_code=max(1, int((env.get("PAYPAL_CARD_REDEEM_RETRY_PER_CODE") or "2").strip() or "2")),
        use_proxy=_bool_env(
            env.get("PAYPAL_CARD_REDEEM_USE_PROXY"),
            default=_bool_env(env.get("PAYPAL_USE_PROXY"), default=False),
        ),
        proxy_file=resolve_path(proxy_file) if proxy_file else None,
        stop_on_request_error=_bool_env(env.get("PAYPAL_CARD_REDEEM_STOP_ON_REQUEST_ERROR"), default=True),
    )


def _read_codes(path: Path) -> list[str]:
    if not path.exists():
        return []
    codes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip().lstrip("\ufeff\u200b\u2060")
        if not text or text.startswith("#"):
            continue
        codes.append(text)
    return codes


def _remove_code_once(path: Path, code: str) -> None:
    if not path.exists():
        return
    target = code.strip().lstrip("\ufeff\u200b\u2060")
    removed = False
    out_lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip().lstrip("\ufeff\u200b\u2060")
        if not removed and text == target:
            removed = True
            continue
        out_lines.append(line)
    path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def _load_first_proxy(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip().lstrip("\ufeff\u200b\u2060")
        if line and not line.startswith("#"):
            return line
    return ""


def _request_proxies(cfg: RedeemConfig) -> dict[str, str] | None:
    if not cfg.use_proxy:
        return None
    proxy = _load_first_proxy(cfg.proxy_file)
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _pick_first(d: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = d.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _extract_payload(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    status = ""
    card_meta = data.get("card")
    if isinstance(card_meta, dict):
        status = str(card_meta.get("status") or "").strip().lower()

    candidates: list[dict[str, Any]] = []
    content = data.get("content")
    if isinstance(content, dict):
        candidates.append(content)
    nested = data.get("data")
    if isinstance(nested, dict):
        nested_content = nested.get("content")
        if isinstance(nested_content, dict):
            candidates.append(nested_content)
        candidates.append(nested)
    candidates.append(data)

    for item in candidates:
        card_number = _pick_first(item, ["card_number", "cardNumber", "number", "pan"])
        expiry = _pick_first(item, ["expiry_date", "expiryDate", "expiry", "exp"])
        cvv = _pick_first(item, ["cvv", "cvc"])
        if card_number and expiry and cvv:
            return item, status
    return None, status


def _build_card_line(code: str, payload: dict[str, Any]) -> str:
    card_number = _pick_first(payload, ["card_number", "cardNumber", "number", "pan"])
    expiry = _pick_first(payload, ["expiry_date", "expiryDate", "expiry", "exp"])
    cvv = _pick_first(payload, ["cvv", "cvc"])
    phone = _pick_first(payload, ["phone", "mobile", "phone_number"])
    holder = _pick_first(payload, ["name", "holder_name", "cardholder", "cardholder_name"])
    address = _pick_first(payload, ["address", "billing_address"])
    sms_api = _pick_first(payload, ["sms_api", "smsApi", "sms_url", "otp_api", "api"])

    if not card_number or not expiry or not cvv:
        raise ValueError("card payload missing required card fields")
    return f"{code}----{card_number}----{expiry}----{cvv}----{phone}----{holder}----{address}----{sms_api}"


def redeem_code_once(code: str, cfg: RedeemConfig) -> tuple[bool, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    if cfg.api_key:
        headers["X-API-Key"] = cfg.api_key
    body = {cfg.code_field: code}
    try:
        response = requests.post(
            cfg.api_url,
            headers=headers,
            json=body,
            timeout=cfg.timeout_sec,
            proxies=_request_proxies(cfg),
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"request_error: {exc}"

    if response.status_code >= 400:
        return False, f"http_{response.status_code}: {response.text[:200]}"

    try:
        data = response.json()
    except Exception:
        return False, f"invalid_json: {response.text[:200]}"

    payload, status = _extract_payload(data if isinstance(data, dict) else {})
    if payload is None:
        msg = ""
        if isinstance(data, dict):
            msg = _pick_first(data, ["message", "msg", "error"])
        return False, f"no_card_payload status={status or '-'} msg={msg or '-'} raw={json.dumps(data, ensure_ascii=False)[:220]}"

    if status == "used" and not cfg.append_when_status_used:
        return False, "card_status_used"

    try:
        line = _build_card_line(code, payload)
    except Exception as exc:  # noqa: BLE001
        return False, f"build_line_failed: {exc}"
    return True, line


def ensure_card_supply(env: dict[str, str], min_available: int, *, log_prefix: str = "PayPal flow2") -> int:
    cfg = load_redeem_config(env)
    if not cfg.enabled:
        return 0

    with _POOL_LOCK:
        if not cfg.cards_file.exists():
            cfg.cards_file.parent.mkdir(parents=True, exist_ok=True)
            cfg.cards_file.write_text("", encoding="utf-8")
        # 使用与流程2一致的解析口径统计“真实可用卡”，避免表头/乱码行被误算为可用卡
        cards_count = CardPool(cfg.cards_file).count()
        if cards_count >= min_available:
            return 0

        need = min(cfg.max_auto_fetch, max(0, min_available - cards_count))
        codes = _read_codes(cfg.codes_file)
        if need <= 0 or not codes:
            log(f"{log_prefix}: card auto-redeem enabled but no redeem codes available in {cfg.codes_file}")
            return 0

        added = 0
        for code in codes:
            if added >= need:
                break
            ok = False
            details = ""
            for attempt in range(1, cfg.retry_per_code + 1):
                ok, details = redeem_code_once(code, cfg)
                if ok:
                    break
                if str(details).startswith("request_error:") and cfg.stop_on_request_error:
                    log(f"{log_prefix}: card auto-redeem network error, stopped this round; code kept in pool {code} | {details}")
                    return added
                if str(details).startswith("request_error:") and attempt < cfg.retry_per_code:
                    log(f"{log_prefix}: card code request error, retrying ({attempt}/{cfg.retry_per_code}) {code}")
                    time.sleep(1.2)
                    continue
                break
            if ok:
                line = details
                existing = cfg.cards_file.read_text(encoding="utf-8")
                if line not in existing:
                    _append_line(cfg.cards_file, line)
                _append_line(cfg.used_file, code)
                _remove_code_once(cfg.codes_file, code)
                added += 1
                log(f"{log_prefix}: card redeemed from code {code}")
            else:
                # Server responded but did not return card payload: mark as failed and consume code.
                if not details.startswith("request_error:"):
                    _append_line(cfg.failed_file, f"{code}----{details}")
                    _remove_code_once(cfg.codes_file, code)
                    log(f"{log_prefix}: card code failed and removed {code} | {details}")
                else:
                    log(f"{log_prefix}: card code request error (kept in pool) {code} | {details}")

        if added > 0:
            log(f"{log_prefix}: auto redeemed {added} card(s) into {cfg.cards_file}")
        return added
