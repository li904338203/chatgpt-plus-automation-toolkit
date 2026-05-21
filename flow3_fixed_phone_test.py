from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import requests

import get_oauth_rt as oauth
from modules.hero_sms_provider import PhoneCountry, SmsActivation


def _pick_default_account_file(root: Path) -> Path:
    candidates = [
        root / "dist" / "ChatGPTAssistantPanel" / "output" / "paypal注册" / "待授权账号" / "account.txt",
        root / "output" / "paypal注册" / "待授权账号" / "account.txt",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _pick_first_email(account_file: Path) -> str:
    if not account_file.exists():
        return ""
    for raw in account_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("----")]
        if parts and "@" in parts[0]:
            return parts[0]
    return ""


class FixedSmsProvider:
    def __init__(self, phone: str, sms_url: str) -> None:
        self.phone = phone
        self.sms_url = sms_url
        self.activation = SmsActivation(activation_id=1, phone_number=phone, activation_cost=None)

    def get_number(self, service: str, country: int, *, operator: str = "") -> SmsActivation:
        print(f"[SMS] [fixed] use fixed phone: {self.phone} | service={service} country={country} operator={operator or 'any'}", flush=True)
        return self.activation

    def mark_ready(self, activation_id: int) -> None:
        print(f"[SMS] [fixed] ready activation={activation_id}", flush=True)

    def complete(self, activation_id: int) -> None:
        print(f"[SMS] [fixed] complete activation={activation_id}", flush=True)

    def cancel(self, activation_id: int) -> None:
        print(f"[SMS] [fixed] cancel activation={activation_id}", flush=True)

    def poll_for_code(self, activation_id: int, *, interval: float = 5.0, max_attempts: int = 60) -> str:
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.get(self.sms_url, timeout=20)
                text = response.text.strip()
            except Exception as exc:
                print(f"[SMS] [fixed] fetch sms failed: {exc} ({attempt}/{max_attempts})", flush=True)
                time.sleep(max(1.0, interval))
                continue

            match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
            if match:
                code = match.group(1)
                print(f"[SMS] [fixed] sms code: {code}", flush=True)
                return code
            print(f"[SMS] [fixed] no code yet ({attempt}/{max_attempts})", flush=True)
            time.sleep(max(1.0, interval))
        raise TimeoutError("fixed sms url timed out without 6-digit code")


def run_fixed_flow(args: argparse.Namespace) -> int:
    account_file = Path(args.account_file).resolve()
    account_email = args.account_email.strip() or _pick_first_email(account_file)
    if not account_email:
        print(f"[FAIL] no account email found in: {account_file}")
        return 1

    fixed_country = PhoneCountry(
        iso_code=args.country_iso.strip().upper() or "US",
        dial_code=args.country_dial.strip().lstrip("+") or "1",
        name=args.country_name.strip() or "United States",
        hero_sms_country=int(args.country_id),
    )
    provider = FixedSmsProvider(args.phone.strip(), args.sms_url.strip())

    def handle_phone_required_with_fixed_provider(page, login_args, remaining_seconds) -> bool:
        activation = None
        try:
            service = str(getattr(login_args, "sms_service", "") or "dr").strip() or "dr"
            operator = str(getattr(login_args, "sms_operator", "") or "").strip()
            print(
                f"[SMS] fixed phone mode start: service={service}, country={fixed_country.name}({fixed_country.hero_sms_country}), "
                f"operator={operator or 'any'}",
                flush=True,
            )
            activation = provider.get_number(service, fixed_country.hero_sms_country, operator=operator)
            provider.mark_ready(activation.activation_id)
            oauth.fill_phone_and_wait_sms_page(page, activation.phone_number, fixed_country)

            poll_interval = float(getattr(login_args, "sms_poll_interval", 5.0) or 5.0)
            max_attempts = int(getattr(login_args, "sms_max_attempts", 60) or 60)
            deadline = time.time() + min(max(30, int(remaining_seconds())), int(poll_interval * max_attempts) + 10)
            while time.time() < deadline:
                if oauth.page_looks_like_sms_verification(page):
                    break
                if oauth.capture_code_from_url(page.url):
                    provider.complete(activation.activation_id)
                    return True
                time.sleep(1)

            code = provider.poll_for_code(
                activation.activation_id,
                interval=poll_interval,
                max_attempts=max_attempts,
            )
            oauth.fill_sms_code(page, code)
            status, detail = oauth.wait_for_code_submit_result(page, timeout=12)
            if status == "invalid":
                raise RuntimeError(f"sms code invalid/expired: {detail}")
            provider.complete(activation.activation_id)
            return True
        except Exception:
            if activation:
                provider.cancel(activation.activation_id)
            raise

    # Monkey-patch only in this isolated runner.
    oauth.handle_phone_required_with_sms_provider = handle_phone_required_with_fixed_provider

    login_argv = [
        "get_oauth_rt.py",
        "login",
        "--account-file",
        str(account_file),
        "--account-email",
        account_email,
        "--auth-mode",
        "team_helper",
        "--sms-provider",
        "herosms",
        "--sms-api-key",
        "fixed-phone-mode",
        "--sms-service",
        (args.sms_service or "dr"),
        "--sms-country",
        str(args.country_id),
        "--sms-country-iso",
        fixed_country.iso_code,
        "--sms-dial-code",
        fixed_country.dial_code,
        "--sms-country-name",
        fixed_country.name,
        "--sms-poll-interval",
        str(args.sms_poll_interval),
        "--sms-max-attempts",
        str(args.sms_max_attempts),
    ]

    old_argv = sys.argv[:]
    try:
        sys.argv = login_argv
        return oauth.main()
    finally:
        sys.argv = old_argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated Flow3 test with fixed phone + SMS URL")
    root = Path(__file__).resolve().parent
    parser.add_argument("--account-file", default=str(_pick_default_account_file(root)))
    parser.add_argument("--account-email", default="")
    parser.add_argument("--phone", required=True, help="fixed phone like +18435858025")
    parser.add_argument("--sms-url", required=True, help="sms api url that returns code text")
    parser.add_argument("--sms-service", default="dr")
    parser.add_argument("--country-id", type=int, default=187)
    parser.add_argument("--country-iso", default="US")
    parser.add_argument("--country-dial", default="1")
    parser.add_argument("--country-name", default="United States")
    parser.add_argument("--sms-poll-interval", type=float, default=5.0)
    parser.add_argument("--sms-max-attempts", type=int, default=60)
    return parser


if __name__ == "__main__":
    cli_args = build_parser().parse_args()
    raise SystemExit(run_fixed_flow(cli_args))

