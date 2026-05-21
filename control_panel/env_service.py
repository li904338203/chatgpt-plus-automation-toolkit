from __future__ import annotations

from pathlib import Path


KNOWN_ENV_FIELDS = [
    "MOEMAIL_API_KEY",
    "AUTH_SERVER_API_KEY",
    "HERO_SMS_API_KEY",
    "GRIZZLY_API_KEY",
    "FIVESIM_API_KEY",
    "CAPSOLVER_API_KEY",
    "TWOCAPTCHA_API_KEY",
    "PAYPAL_CARD_REDEEM_API_KEY",
    "PAYPAL_CAPTCHA_MODE",
    "CAPTCHA_API_PROVIDER",
    "PAYPAL_USE_PROXY",
    "PAYPAL_REGISTER_USE_PROXY",
    "PAYPAL_PROXY_FILE",
    "PAYPAL_REGISTER_PROXY_FILE",
    "PAYPAL_CARD_REDEEM_ENABLED",
    "PAYPAL_CARD_REDEEM_API_URL",
    "PAYPAL_CARD_REDEEM_CODE_FIELD",
    "PAYPAL_CARD_REDEEM_TIMEOUT",
    "PAYPAL_CARD_REDEEM_MAX_AUTO_FETCH",
    "PAYPAL_CARD_CODES_FILE",
    "PAYPAL_CARD_CODES_USED_FILE",
    "PAYPAL_CARD_CODES_FAILED_FILE",
    "MAIL_SOURCE",
    "FLOW1_MAIL_SOURCE",
    "FLOW3_MAIL_SOURCE",
    "FREE_MAIL_SOURCE",
]


def get_known_env_fields() -> list[str]:
    return list(KNOWN_ENV_FIELDS)


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if not key:
        return None
    return key, value.strip()


def read_env(path: Path | str) -> dict[str, str]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in file_path.read_text(encoding="utf-8-sig").splitlines():
        parsed = _parse_env_line(line)
        if parsed:
            key, value = parsed
            values[key] = value
    return values


def update_env(path: Path | str, updates: dict[str, str]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lines = file_path.read_text(encoding="utf-8-sig").splitlines() if file_path.exists() else []
    remaining = {key: str(value) for key, value in updates.items()}
    output: list[str] = []

    for line in lines:
        parsed = _parse_env_line(line)
        if not parsed:
            output.append(line)
            continue
        key, _old_value = parsed
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)

    for key, value in remaining.items():
        output.append(f"{key}={value}")

    file_path.write_text("\n".join(output) + ("\n" if output else ""), encoding="utf-8")
