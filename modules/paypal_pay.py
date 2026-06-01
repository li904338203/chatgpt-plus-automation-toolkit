"""PayPal 流程2：从长链接池取账号 → Stripe → PayPal 注册绑卡 → 支付。

输入：output/paypal成品/长链接账号/account.txt + cards.txt + phones.txt
输出：output/paypal成品/待授权账号/account.txt
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
import traceback
from pathlib import Path
from typing import Any

import requests

from .browser import BrowserSession
from .paypal_card_pool import CardInfo, CardPool
from .paypal_phone_pool import PhoneInfo, PhonePool
from .utils import load_env, log, resolve_path, safe_filename


PAYPAL_OUTPUT_ROOT = resolve_path("output/paypal注册")
LINK_POOL_FILE = PAYPAL_OUTPUT_ROOT / "长链接账号" / "account.txt"
PENDING_AUTH_DIR = PAYPAL_OUTPUT_ROOT / "待授权账号"
PENDING_AUTH_FILE = PENDING_AUTH_DIR / "account.txt"
PAYPAL_FLOW2_CODE_VERSION = "PAYPAL_JP_PREFECTURE_FIX_2026-05-31_01"

_RANDOM_CARD_PROFILES: list[tuple[str, str, str, str, str]] = [
    ("New York", "NY", "10001", "W 34th St", "US"),
    ("Los Angeles", "CA", "90017", "S Grand Ave", "US"),
    ("Chicago", "IL", "60606", "N LaSalle St", "US"),
    ("Houston", "TX", "77002", "Louisiana St", "US"),
    ("Phoenix", "AZ", "85004", "E Washington St", "US"),
    ("Philadelphia", "PA", "19103", "Market St", "US"),
    ("San Antonio", "TX", "78205", "E Houston St", "US"),
    ("San Diego", "CA", "92101", "Broadway", "US"),
    ("Dallas", "TX", "75201", "Main St", "US"),
    ("San Jose", "CA", "95113", "Santa Clara St", "US"),
    ("Austin", "TX", "78701", "Congress Ave", "US"),
    ("Jacksonville", "FL", "32202", "Bay St", "US"),
    ("Fort Worth", "TX", "76102", "Houston St", "US"),
    ("Columbus", "OH", "43215", "High St", "US"),
    ("Charlotte", "NC", "28202", "Trade St", "US"),
    ("Indianapolis", "IN", "46204", "Meridian St", "US"),
    ("Seattle", "WA", "98101", "Pike St", "US"),
    ("Denver", "CO", "80202", "17th St", "US"),
    ("Boston", "MA", "02110", "Atlantic Ave", "US"),
    ("Nashville", "TN", "37219", "Church St", "US"),
]

_RANDOM_CARD_PROFILES_JP: list[tuple[str, str, str, str, str]] = [
    ("Tokyo", "Tokyo", "1000001", "Chiyoda 1-1", "JP"),
    ("Osaka", "Osaka", "5300001", "Kita Umeda 2-3", "JP"),
    ("Yokohama", "Kanagawa", "2200012", "Nishi Minatomirai 1-4", "JP"),
    ("Nagoya", "Aichi", "4600008", "Naka Sakae 3-5", "JP"),
    ("Sapporo", "Hokkaido", "0600001", "Chuo Odori 2-6", "JP"),
    ("Fukuoka", "Fukuoka", "8100001", "Chuo Tenjin 1-8", "JP"),
    ("Kyoto", "Kyoto", "6008001", "Shimogyo Shijo 4-2", "JP"),
    ("Kobe", "Hyogo", "6500001", "Chuo Sannomiya 2-9", "JP"),
    ("Sendai", "Miyagi", "9800004", "Aoba Ichibancho 1-7", "JP"),
    ("Hiroshima", "Hiroshima", "7300011", "Naka Motomachi 1-3", "JP"),
]

_MEIGUODIZHI_ADDRESS_URL = "https://www.meiguodizhi.com/api/v1/dz"
_BILLING_ADDRESS_REGION_PATHS = {
    "US": "/",
    "JP": "/jp-address",
}
_VISA_BIN_PREFIXES = (
    "4859",
    "424631",
    "414709",
)

_JP_PREFECTURE_LABELS: dict[str, str] = {
    "Tokyo": "東京都",
    "Osaka": "大阪府",
    "Kanagawa": "神奈川県",
    "Aichi": "愛知県",
    "Hokkaido": "北海道",
    "Aomori": "青森県",
    "Iwate": "岩手県",
    "Akita": "秋田県",
    "Yamagata": "山形県",
    "Fukushima": "福島県",
    "Fukuoka": "福岡県",
    "Kyoto": "京都府",
    "Hyogo": "兵庫県",
    "Miyagi": "宮城県",
    "Ibaraki": "茨城県",
    "Tochigi": "栃木県",
    "Gunma": "群馬県",
    "Saitama": "埼玉県",
    "Chiba": "千葉県",
    "Niigata": "新潟県",
    "Toyama": "富山県",
    "Ishikawa": "石川県",
    "Fukui": "福井県",
    "Yamanashi": "山梨県",
    "Nagano": "長野県",
    "Gifu": "岐阜県",
    "Shizuoka": "静岡県",
    "Mie": "三重県",
    "Shiga": "滋賀県",
    "Nara": "奈良県",
    "Wakayama": "和歌山県",
    "Tottori": "鳥取県",
    "Shimane": "島根県",
    "Okayama": "岡山県",
    "Hiroshima": "広島県",
    "Yamaguchi": "山口県",
    "Tokushima": "徳島県",
    "Kagawa": "香川県",
    "Ehime": "愛媛県",
    "Kochi": "高知県",
    "Saga": "佐賀県",
    "Nagasaki": "長崎県",
    "Kumamoto": "熊本県",
    "Oita": "大分県",
    "Miyazaki": "宮崎県",
    "Kagoshima": "鹿児島県",
    "Okinawa": "沖縄県",
}

_JP_PAYPAL_PREFECTURE_CODES: dict[str, str] = {
    "Tokyo": "TOKYO-TO",
    "Osaka": "OSAKA-FU",
    "Kanagawa": "KANAGAWA-KEN",
    "Aichi": "AICHI-KEN",
    "Hokkaido": "HOKKAIDO",
    "Aomori": "AOMORI-KEN",
    "Iwate": "IWATE-KEN",
    "Akita": "AKITA-KEN",
    "Yamagata": "YAMAGATA-KEN",
    "Fukushima": "FUKUSHIMA-KEN",
    "Fukuoka": "FUKUOKA-KEN",
    "Kyoto": "KYOTO-FU",
    "Hyogo": "HYOGO-KEN",
    "Miyagi": "MIYAGI-KEN",
    "Ibaraki": "IBARAKI-KEN",
    "Tochigi": "TOCHIGI-KEN",
    "Gunma": "GUNMA-KEN",
    "Saitama": "SAITAMA-KEN",
    "Chiba": "CHIBA-KEN",
    "Niigata": "NIIGATA-KEN",
    "Toyama": "TOYAMA-KEN",
    "Ishikawa": "ISHIKAWA-KEN",
    "Fukui": "FUKUI-KEN",
    "Yamanashi": "YAMANASHI-KEN",
    "Nagano": "NAGANO-KEN",
    "Gifu": "GIFU-KEN",
    "Shizuoka": "SHIZUOKA-KEN",
    "Mie": "MIE-KEN",
    "Shiga": "SHIGA-KEN",
    "Nara": "NARA-KEN",
    "Wakayama": "WAKAYAMA-KEN",
    "Tottori": "TOTTORI-KEN",
    "Shimane": "SHIMANE-KEN",
    "Okayama": "OKAYAMA-KEN",
    "Hiroshima": "HIROSHIMA-KEN",
    "Yamaguchi": "YAMAGUCHI-KEN",
    "Tokushima": "TOKUSHIMA-KEN",
    "Kagawa": "KAGAWA-KEN",
    "Ehime": "EHIME-KEN",
    "Kochi": "KOCHI-KEN",
    "Saga": "SAGA-KEN",
    "Nagasaki": "NAGASAKI-KEN",
    "Kumamoto": "KUMAMOTO-KEN",
    "Oita": "OITA-KEN",
    "Miyazaki": "MIYAZAKI-KEN",
    "Kagoshima": "KAGOSHIMA-KEN",
    "Okinawa": "OKINAWA-KEN",
}

_JP_FIRST_NAME_META: dict[str, tuple[str, str]] = {
    "Haruto": ("はると", "晴斗"),
    "Yui": ("ゆい", "結衣"),
    "Sota": ("そうた", "蒼太"),
    "Sakura": ("さくら", "桜"),
    "Ren": ("れん", "蓮"),
    "Yuna": ("ゆな", "優奈"),
    "Daiki": ("だいき", "大輝"),
    "Mio": ("みお", "美桜"),
}

_JP_LAST_NAME_META: dict[str, tuple[str, str]] = {
    "Sato": ("さとう", "佐藤"),
    "Suzuki": ("すずき", "鈴木"),
    "Takahashi": ("たかはし", "高橋"),
    "Tanaka": ("たなか", "田中"),
    "Watanabe": ("わたなべ", "渡辺"),
    "Ito": ("いとう", "伊藤"),
    "Yamamoto": ("やまもと", "山本"),
    "Nakamura": ("なかむら", "中村"),
}


def _normalize_flow2_region_mode(mode: str | None) -> str:
    value = (mode or "").strip().lower()
    if value in {"jp", "japan", "日本"}:
        return "jp"
    return "default"


def _billing_country_code(region_mode: str) -> str:
    return "JP" if _normalize_flow2_region_mode(region_mode) == "jp" else "US"


def _with_billing_profile(base_card: CardInfo, billing_card: CardInfo) -> CardInfo:
    """保留原卡号/有效期/CVV，仅替换账单资料。"""
    return CardInfo(
        number=base_card.number,
        exp_month=base_card.exp_month,
        exp_year=base_card.exp_year,
        cvv=base_card.cvv,
        holder_name=billing_card.holder_name,
        first_name=billing_card.first_name,
        last_name=billing_card.last_name,
        street=billing_card.street,
        city=billing_card.city,
        state=billing_card.state,
        zip_code=billing_card.zip_code,
        country=billing_card.country,
        phone=base_card.phone,
        sms_api_url=base_card.sms_api_url,
        raw_line=base_card.raw_line,
    )


def _jp_birthdate_for_email(email: str) -> str:
    seed = int(hashlib.sha256((email or "").lower().encode("utf-8")).hexdigest()[:8], 16)
    year = 1986 + (seed % 14)  # 1986-1999
    month = 1 + ((seed >> 8) % 12)
    day = 1 + ((seed >> 16) % 28)
    return f"{year:04d}/{month:02d}/{day:02d}"


def _jp_identity_values(card: CardInfo, email: str) -> tuple[str, str, str, str, str]:
    first_key = (card.first_name or "").strip()
    last_key = (card.last_name or "").strip()
    first_kana, first_kanji = _JP_FIRST_NAME_META.get(first_key, ("だいき", "大輝"))
    last_kana, last_kanji = _JP_LAST_NAME_META.get(last_key, ("すずき", "鈴木"))
    birth = _jp_birthdate_for_email(email)
    return birth, first_kana, last_kana, first_kanji, last_kanji


def _hiragana_to_katakana(text: str) -> str:
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        # Hiragana range -> Katakana range
        if 0x3041 <= code <= 0x3096:
            out.append(chr(code + 0x60))
        else:
            out.append(ch)
    return "".join(out)


def is_local_random_card_mode(env: dict[str, str]) -> bool:
    source = (env.get("PAYPAL_CARD_SOURCE") or "").strip().lower()
    return source in {"local_random", "random_local", "local"}


def _luhn_check_digit(number_without_check: str) -> str:
    digits = [int(ch) for ch in number_without_check]
    total = 0
    parity = (len(digits) + 1) % 2
    for idx, value in enumerate(digits):
        if idx % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - (total % 10)) % 10)


def _build_luhn_card_number(prefix: str, random_part_length: int, *, rng: random.Random) -> str:
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(random_part_length))
    return body + _luhn_check_digit(body)


def _normalize_card_expiry(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    parts = [p for p in re.split(r"\D+", text) if p]
    if len(parts) < 2:
        return "", ""
    first, second = parts[0], parts[1]
    try:
        first_num = int(first)
        second_num = int(second)
    except ValueError:
        return "", ""
    if first_num > 12 and 1 <= second_num <= 12:
        first, second = second, first
        first_num = second_num
    if not (1 <= first_num <= 12):
        return "", ""
    year = str(second)
    if len(year) == 2:
        year = "20" + year
    return str(first_num).zfill(2), year


def _generate_abai_visa_card(rng: random.Random) -> dict[str, str]:
    bin_prefix = _VISA_BIN_PREFIXES[rng.randrange(len(_VISA_BIN_PREFIXES))]
    random_len = 16 - len(bin_prefix) - 1
    year = time.gmtime().tm_year + rng.randint(2, 4)
    return {
        "card_number": _build_luhn_card_number(bin_prefix, random_len, rng=rng),
        "card_exp_month": str(rng.randint(1, 12)).zfill(2),
        "card_exp_year": str(year),
        "card_cvv": "".join(str(rng.randint(0, 9)) for _ in range(3)),
    }


def _normalize_meiguodizhi_billing_address(data: dict[str, Any], *, email: str, country: str) -> dict[str, str]:
    address = data.get("address") if isinstance(data, dict) else {}
    if not isinstance(address, dict):
        address = {}
    exp_month, exp_year = _normalize_card_expiry(
        str(address.get("Expires") or address.get("expires") or address.get("card_expiry") or "")
    )
    normalized = {
        "name": str(address.get("Full_Name") or address.get("name") or "").strip(),
        "line1": str(address.get("Address") or address.get("line1") or "").strip(),
        "city": str(address.get("City") or address.get("city") or "").strip(),
        "state": str(address.get("State") or address.get("state") or "").strip(),
        "postal_code": str(address.get("Zip_Code") or address.get("postal_code") or "").strip(),
        "phone": str(address.get("Telephone") or address.get("phone") or "").strip(),
        "country": str(country or "US").strip().upper() or "US",
        "email": str(email or address.get("Temporary_mail") or "").strip(),
    }
    card_number = str(
        address.get("Credit_Card_Number")
        or address.get("credit_card_number")
        or address.get("card_number")
        or ""
    ).strip()
    card_cvv = str(address.get("CVV2") or address.get("cvv") or address.get("card_cvv") or "").strip()
    if card_number:
        normalized["card_number"] = card_number
    if exp_month:
        normalized["card_exp_month"] = exp_month
    if exp_year:
        normalized["card_exp_year"] = exp_year
    if card_cvv:
        normalized["card_cvv"] = card_cvv
    return normalized


def _fetch_abai_billing_address(region: str, *, email: str, rng: random.Random) -> dict[str, str]:
    region_key = str(region or "").strip().upper()
    if region_key not in _BILLING_ADDRESS_REGION_PATHS:
        region_key = "US"
    path = _BILLING_ADDRESS_REGION_PATHS[region_key]
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                _MEIGUODIZHI_ADDRESS_URL,
                json={"path": path, "method": "address"},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            address = _normalize_meiguodizhi_billing_address(
                data if isinstance(data, dict) else {},
                email=email,
                country=region_key,
            )
            missing = [key for key in ("name", "line1", "city", "state", "postal_code") if not address.get(key)]
            if missing:
                raise ValueError(f"{region_key} 地址接口返回字段不完整: {', '.join(missing)}")
            address.update(_generate_abai_visa_card(rng))
            return address
        except Exception as exc:
            last_exc = exc
            if attempt >= 3:
                break
            time.sleep(0.5 * (2 ** (attempt - 1)))
    raise RuntimeError(f"{region_key} 地址接口获取失败: {last_exc}")


def _generate_local_random_card(
    index: int,
    email: str,
    env: dict[str, str],
    *,
    region_mode: str = "default",
) -> CardInfo:
    seed_raw = f"{email.lower()}::{index}::{time.time_ns()}"
    seed = int(hashlib.sha256(seed_raw.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)
    region_key = "JP" if _normalize_flow2_region_mode(region_mode) == "jp" else "US"

    if region_key == "JP":
        first_pool = ["Haruto", "Yui", "Sota", "Sakura", "Ren", "Yuna", "Daiki", "Mio"]
        last_pool = ["Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto", "Nakamura"]
        profile = _RANDOM_CARD_PROFILES_JP[rng.randrange(len(_RANDOM_CARD_PROFILES_JP))]
    else:
        first_pool = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda"]
        last_pool = ["Smith", "Johnson", "Williams", "Brown", "Davis", "Miller", "Wilson", "Moore"]
        profile = _RANDOM_CARD_PROFILES[rng.randrange(len(_RANDOM_CARD_PROFILES))]

    first_name = first_pool[rng.randrange(len(first_pool))]
    last_name = last_pool[rng.randrange(len(last_pool))]
    holder = f"{first_name} {last_name}"

    try:
        abai_address = _fetch_abai_billing_address(region_key, email=email, rng=rng)
        api_name = str(abai_address.get("name") or "").strip()
        name_parts = api_name.split()
        if len(name_parts) >= 2:
            first_name = name_parts[0]
            last_name = " ".join(name_parts[1:])
            holder = api_name
        elif api_name:
            holder = api_name
        log(
            f"PayPal flow2: 使用 aBaiAutoplus 账单生成逻辑: "
            f"region={region_key}, city={abai_address.get('city', '')}, "
            f"state={abai_address.get('state', '')}, zip={abai_address.get('postal_code', '')}"
        )
        return CardInfo(
            number=str(abai_address["card_number"]),
            exp_month=str(abai_address["card_exp_month"]).zfill(2),
            exp_year=str(abai_address["card_exp_year"])[-2:],
            cvv=str(abai_address["card_cvv"]),
            holder_name=holder,
            first_name=first_name,
            last_name=last_name,
            street=str(abai_address.get("line1") or ""),
            city=str(abai_address.get("city") or ""),
            state=str(abai_address.get("state") or ""),
            zip_code=str(abai_address.get("postal_code") or ""),
            country=str(abai_address.get("country") or region_key),
            phone=str(abai_address.get("phone") or ""),
            sms_api_url="",
            raw_line=f"ABAI_RANDOM::{region_key}::{email}::{index}",
        )
    except Exception as exc:
        log(f"PayPal flow2: aBaiAutoplus 账单生成失败，回退本地随机资料: {exc}")

    city, state, zip_code, street_base, country = profile
    street_no = rng.randint(10, 9999)
    street = f"{street_no} {street_base}"

    year = time.gmtime().tm_year + rng.randint(2, 5)
    exp_year = str(year)[-2:]
    exp_month = str(rng.randint(1, 12)).zfill(2)
    cvv = str(rng.randint(100, 999))

    custom_bin = re.sub(r"\D+", "", (env.get("PAYPAL_RANDOM_CARD_BIN") or ""))[:8]
    custom_bin_allowed = len(custom_bin) >= 6 and not custom_bin.startswith("5200")
    if custom_bin_allowed:
        bin_prefix = custom_bin
    else:
        # 固定本地随机卡头池（流程2）
        allowed_prefixes = ["485954", "490714", "491688"]
        bin_prefix = allowed_prefixes[rng.randrange(len(allowed_prefixes))]
    random_len = 15 - len(bin_prefix)
    number = _build_luhn_card_number(bin_prefix, random_len, rng=rng)

    return CardInfo(
        number=number,
        exp_month=exp_month,
        exp_year=exp_year,
        cvv=cvv,
        holder_name=holder,
        first_name=first_name,
        last_name=last_name,
        street=street,
        city=city,
        state=state,
        zip_code=zip_code,
        country=country,
        phone="",
        sms_api_url="",
        raw_line=f"LOCAL_RANDOM::{email}::{index}",
    )


def _display_proxy(proxy: str | None) -> str:
    """隐藏代理密码用于日志显示。"""
    if not proxy:
        return "无"
    text = proxy.strip()
    if "@" in text:
        prefix, suffix = text.rsplit("@", 1)
        scheme = prefix.split("://", 1)[0] + "://" if "://" in prefix else ""
        return f"{scheme}***:***@{suffix}"
    return text


def load_link_pool() -> list[dict[str, str]]:
    if not LINK_POOL_FILE.exists():
        return []
    items = []
    for line in LINK_POOL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) >= 3:
            items.append({"email": parts[0].strip(), "query_code": parts[1].strip(), "payment_link": "----".join(parts[2:]).strip()})
    return items


def save_pending_auth(email: str, query_code: str) -> None:
    PENDING_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    with PENDING_AUTH_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{email}----{query_code}\n")


def remove_from_link_pool(email: str) -> None:
    if not LINK_POOL_FILE.exists():
        return
    lines = LINK_POOL_FILE.read_text(encoding="utf-8").splitlines()
    remaining = [l for l in lines if not l.strip().lower().startswith(email.lower())]
    LINK_POOL_FILE.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")


def generate_paypal_password(email: str) -> str:
    local = email.split("@")[0] if "@" in email else email
    prefix_raw = re.sub(r"[^a-zA-Z0-9]", "", local)
    prefix = (prefix_raw[:3] or "usr").lower()

    upper_pool = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower_pool = "abcdefghjkmnpqrstuvwxyz"
    digit_pool = "246789"
    seed = hashlib.sha256(email.lower().encode("utf-8")).digest()

    def pick(pool: str, idx: int) -> str:
        return pool[seed[idx] % len(pool)]

    candidate = (
        f"{prefix}{pick(upper_pool, 0)}{pick(lower_pool, 1)}"
        f"#{pick(digit_pool, 2)}{pick(upper_pool, 3)}{pick(digit_pool, 4)}"
        f"!{pick(lower_pool, 5)}{pick(upper_pool, 6)}"
    )

    def has_4_key_sequence(s: str) -> bool:
        t = s.lower()
        rows = ("0123456789", "qwertyuiop", "asdfghjkl", "zxcvbnm")
        for row in rows:
            for i in range(len(row) - 3):
                seq = row[i : i + 4]
                if seq in t or seq[::-1] in t:
                    return True
        for i in range(len(t) - 3):
            chunk = t[i : i + 4]
            if chunk.isdigit():
                vals = [ord(c) for c in chunk]
                if all(vals[j + 1] - vals[j] == 1 for j in range(3)) or all(
                    vals[j + 1] - vals[j] == -1 for j in range(3)
                ):
                    return True
            if chunk.isalpha():
                vals = [ord(c) for c in chunk]
                if all(vals[j + 1] - vals[j] == 1 for j in range(3)) or all(
                    vals[j + 1] - vals[j] == -1 for j in range(3)
                ):
                    return True
        return False

    if has_4_key_sequence(candidate):
        candidate = (
            f"{prefix}{pick(upper_pool, 7)}{pick(lower_pool, 8)}"
            f"#{pick(digit_pool, 9)}{pick(upper_pool, 10)}{pick(digit_pool, 11)}"
            f"!{pick(lower_pool, 12)}{pick(upper_pool, 13)}"
        )
    return candidate


def poll_sms_code(api_url: str, *, timeout: int = 120, interval: int = 5) -> str:
    """轮询手机 API 获取验证码。"""
    def _extract_code(text: str) -> str | None:
        raw = (text or "").strip()
        if not raw:
            return None

        # 1) 兼容历史 "ok|短信内容" / "no|暂无验证码" 风格
        parts = raw.split("|", 2)
        status = parts[0].lower() if parts else ""
        content = parts[1] if len(parts) > 1 else ""
        if status and status != "no" and content and content != "暂无验证码":
            m = re.search(r"\b(\d{4,8})\b", content)
            if m:
                return m.group(1)

        # 2) 兼容 JSON 风格（常见字段：SmsCode / code / smsCode / SmsContent / message）
        if raw.startswith("{") or raw.startswith("["):
            try:
                data = json.loads(raw)
            except Exception:
                data = None
            if data is None:
                return None
            rows = data if isinstance(data, list) else [data]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for key in ("SmsCode", "smsCode", "code", "Code", "otp", "Otp"):
                    val = str(row.get(key, "")).strip()
                    if re.fullmatch(r"\d{4,8}", val):
                        return val
                merged = " ".join(
                    str(row.get(k, "")).strip()
                    for k in ("SmsContent", "smsContent", "content", "message", "msg", "body")
                ).strip()
                if merged:
                    m = re.search(r"\b(\d{4,8})\b", merged)
                    if m:
                        return m.group(1)

        # 3) 兜底：直接从全文本提取 4-8 位数字
        m = re.search(r"\b(\d{4,8})\b", raw)
        if m:
            return m.group(1)
        return None

    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(api_url, timeout=10)
            text = resp.text.strip()
            code = _extract_code(text)
            if code:
                return code
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"手机验证码超时 ({timeout}s)")


async def _fill_paypal_jp_identity(page, *, email: str, card: CardInfo) -> None:
    birth, first_kana, last_kana, first_kanji, last_kanji = _jp_identity_values(card, email)

    # 1) 生日
    dob_selectors = [
        'input[name*="birth" i]',
        'input[id*="birth" i]',
        'input[placeholder*="年/月/日"]',
        'input[aria-label*="生年月日" i]',
    ]
    for sel in dob_selectors:
        try:
            loc = page.locator(f"{sel}:visible").first
            if await loc.is_visible(timeout=800):
                await loc.fill("", timeout=1500)
                await loc.fill(birth, timeout=3000)
                break
        except Exception:
            continue

    # 2) 假名 + 汉字（兜底用 JS，按字段语义和空值状态填充）
    try:
        result = await page.evaluate(
            """(payload) => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (Number(st.opacity || '1') < 0.05) return false;
                    return !el.disabled && !el.readOnly;
                };
                const setVal = (el, v) => {
                    const proto = HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && typeof desc.set === 'function') desc.set.call(el, v);
                    else el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                };
                const sig = (el) => {
                    const p = String(el.placeholder || '').toLowerCase();
                    const a = String(el.getAttribute('aria-label') || '').toLowerCase();
                    const n = String(el.name || '').toLowerCase();
                    const i = String(el.id || '').toLowerCase();
                    const t = (el.closest('label')?.innerText || el.parentElement?.innerText || '').toLowerCase();
                    return `${p} ${a} ${n} ${i} ${t}`;
                };
                const isExcluded = (s) => /email|mail|phone|zip|postal|address|city|street|state|country|card|cvv|exp|password/.test(s);
                const inputs = Array.from(document.querySelectorAll('input'))
                    .filter(isVisible)
                    .filter((el) => {
                        const type = String(el.type || 'text').toLowerCase();
                        return ['text', 'search', 'tel', ''].includes(type);
                    });

                const used = new Set();

                // DOB
                for (const el of inputs) {
                    const s = sig(el);
                    if (s.includes('生年月日') || s.includes('birth') || s.includes('年/月/日')) {
                        if (!String(el.value || '').trim()) setVal(el, payload.birth);
                        used.add(el);
                        break;
                    }
                }

                // Kana fields
                const kanaCandidates = inputs.filter((el) => {
                    const s = sig(el);
                    return s.includes('かな') || s.includes('カナ') || s.includes('furigana') || s.includes('phonetic') || s.includes('kana');
                }).filter((el) => !used.has(el));
                const setIfEmpty = (el, v) => {
                    if (!el) return false;
                    if (String(el.value || '').trim()) return false;
                    setVal(el, v);
                    return true;
                };
                let kanaFirst = false;
                let kanaLast = false;
                for (const el of kanaCandidates) {
                    const s = sig(el);
                    if (!kanaFirst && /(^|\\s)(名|first|given)($|\\s)/.test(s)) kanaFirst = setIfEmpty(el, payload.firstKana) || kanaFirst;
                    if (!kanaLast && /(^|\\s)(姓|last|family)($|\\s)/.test(s)) kanaLast = setIfEmpty(el, payload.lastKana) || kanaLast;
                }
                const kanaByLeft = kanaCandidates.slice().sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                if (!kanaFirst && kanaByLeft[0]) kanaFirst = setIfEmpty(kanaByLeft[0], payload.firstKana) || kanaFirst;
                if (!kanaLast && kanaByLeft[1]) kanaLast = setIfEmpty(kanaByLeft[1], payload.lastKana) || kanaLast;
                kanaCandidates.forEach((el) => used.add(el));

                // Kanji fields: 优先“漢字 区域 + 名/姓语义 + 非地址邮箱类字段”
                const kanjiCandidates = inputs.filter((el) => {
                    if (used.has(el)) return false;
                    const s = sig(el);
                    if (isExcluded(s)) return false;
                    if (s.includes('かな') || s.includes('カナ') || s.includes('furigana') || s.includes('phonetic') || s.includes('kana')) return false;
                    if (s.includes('漢字')) return true;
                    return s.includes('名') || s.includes('姓') || s.includes('name') || s.includes('first') || s.includes('last');
                });
                let kanjiFirst = false;
                let kanjiLast = false;
                for (const el of kanjiCandidates) {
                    const s = sig(el);
                    if (!kanjiFirst && /(^|\\s)(名|first|given)($|\\s)/.test(s)) kanjiFirst = setIfEmpty(el, payload.firstKanji) || kanjiFirst;
                    if (!kanjiLast && /(^|\\s)(姓|last|family)($|\\s)/.test(s)) kanjiLast = setIfEmpty(el, payload.lastKanji) || kanjiLast;
                }
                const kanjiByLeft = kanjiCandidates.slice().sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                if (!kanjiFirst && kanjiByLeft[0]) kanjiFirst = setIfEmpty(kanjiByLeft[0], payload.firstKanji) || kanjiFirst;
                if (!kanjiLast && kanjiByLeft[1]) kanjiLast = setIfEmpty(kanjiByLeft[1], payload.lastKanji) || kanjiLast;

                // 必填兜底：如果还有“名/姓”空框，按语义再补一次（避免只填了一个）
                const empties = inputs.filter((el) => !String(el.value || '').trim());
                for (const el of empties) {
                    const s = sig(el);
                    if (isExcluded(s)) continue;
                    if (s.includes('かな') || s.includes('カナ') || s.includes('furigana') || s.includes('phonetic') || s.includes('kana')) continue;
                    if (!kanjiFirst && /(^|\\s)(名|first|given)($|\\s)/.test(s)) kanjiFirst = setIfEmpty(el, payload.firstKanji) || kanjiFirst;
                    if (!kanjiLast && /(^|\\s)(姓|last|family)($|\\s)/.test(s)) kanjiLast = setIfEmpty(el, payload.lastKanji) || kanjiLast;
                }

                return {
                    kanaCount: kanaCandidates.length,
                    kanjiCount: kanjiCandidates.length,
                    kanaFirst,
                    kanaLast,
                    kanjiFirst,
                    kanjiLast,
                };
            }""",
            {
                "birth": birth,
                "firstKana": first_kana,
                "lastKana": last_kana,
                "firstKanji": first_kanji,
                "lastKanji": last_kanji,
            },
        )
        log(
            f"[PayPal][JP] 已尝试填充生日/假名/汉字: dob={birth}, "
            f"kana_candidates={(result or {}).get('kanaCount', 0)}, "
            f"kanji_candidates={(result or {}).get('kanjiCount', 0)}, "
            f"kana_ok=({(result or {}).get('kanaFirst', False)},{(result or {}).get('kanaLast', False)}), "
            f"kanji_ok=({(result or {}).get('kanjiFirst', False)},{(result or {}).get('kanjiLast', False)})"
        )
    except Exception as exc:
        log(f"[PayPal][JP] 日本实名字段填充异常: {exc}")

async def fill_stripe(page, email: str, card: CardInfo, *, country_code: str = "US") -> None:
    """Stripe 页面：选 PayPal + 填地址 + Subscribe。"""
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    await page.wait_for_timeout(8000)

    # 防御性修正：部分卡源会把 "CITY ZIP" 合并到 city 字段
    city_value = (card.city or "").strip()
    zip_value = (card.zip_code or "").strip()
    m_city_zip = re.search(r"^(?P<city>.*)\s+(?P<zip>\d{5}(?:-\d{4})?)$", city_value)
    if m_city_zip:
        city_value = (m_city_zip.group("city") or "").strip()
        if not zip_value:
            zip_value = (m_city_zip.group("zip") or "").strip()

    # 选 PayPal
    paypal_btn = page.locator('[data-testid="paypal-accordion-item-button"], [aria-label*="PayPal" i], text=PayPal').first
    try:
        await paypal_btn.click(timeout=5000, force=True)
    except Exception:
        await page.evaluate('document.querySelector("[data-testid=\\"paypal-accordion-item-button\\"]")?.click()')
    await page.wait_for_timeout(2000)

    desired_country = "JP" if str(country_code or "").upper() == "JP" else "US"
    desired_labels = ["Japan", "日本"] if desired_country == "JP" else ["United States", "美国"]

    # 国家选择 - 先等待下拉框可交互
    country_select = page.locator('#billingCountry, select[name*="country" i], select[autocomplete="country"]').first
    try:
        await country_select.wait_for(state="visible", timeout=8000)
        await country_select.select_option(desired_country, timeout=5000)
    except Exception:
        for lbl in desired_labels:
            try:
                await country_select.select_option(label=lbl, timeout=3000)
                break
            except Exception:
                continue

    # 等待国家切换后页面重新渲染地址字段
    await page.wait_for_timeout(3000)

    # 验证国家是否选中目标国家
    try:
        current_val = await country_select.input_value()
        if current_val != desired_country:
            log(f"[Stripe] 国家仍为 {current_val}，再次尝试切到 {desired_country}...")
            await country_select.select_option(desired_country, timeout=3000)
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    # 手动输入地址
    try:
        manual = page.locator('text=手动输入地址, text=Enter address manually, a:has-text("手动"), a:has-text("manually")').first
        await manual.click(timeout=5000)
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    async def _safe_fill(selector: str, value: str, timeout: int = 5000) -> bool:
        if not str(value or "").strip():
            return False
        val = str(value).strip()
        # 先尝试可见输入框
        try:
            loc = page.locator(f"{selector}:visible").first
            if await loc.is_visible(timeout=1200):
                await loc.fill("", timeout=2000)
                await loc.fill(val, timeout=timeout)
                read_back = (await loc.input_value()).strip()
                if read_back:
                    return True
        except Exception:
            pass

        # 兜底：遍历可见节点，使用原生 setter 写入并触发事件链
        try:
            ok = await page.evaluate(
                """(selector, value) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width < 6 || r.height < 6) return false;
                        const st = window.getComputedStyle(el);
                        if (!st) return false;
                        if (st.display === 'none' || st.visibility === 'hidden') return false;
                        if (Number(st.opacity || '1') < 0.05) return false;
                        return !el.disabled && !el.readOnly;
                    };
                    const nodes = Array.from(document.querySelectorAll(selector)).filter(isVisible);
                    if (!nodes.length) return false;
                    const input = nodes[0];
                    const proto = input.tagName.toLowerCase() === 'textarea'
                        ? HTMLTextAreaElement.prototype
                        : HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && typeof desc.set === 'function') desc.set.call(input, value);
                    else input.value = value;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.dispatchEvent(new Event('blur', { bubbles: true }));
                    return String(input.value || '').trim().length > 0;
                }""",
                selector,
                val,
            )
            return bool(ok)
        except Exception:
            return False

    # 日本地址表单通常需要“邮编 -> 都道府县 -> 城市 -> 地址”顺序
    if desired_country == "JP":
        await _safe_fill(
            '#billingPostalCode, input[name*="postalCode" i], input[name*="zip" i], input[placeholder*="邮编" i], input[placeholder*="ZIP" i]',
            zip_value,
        )

        # 选择辖区（都道府县）
        pref_label = _JP_PREFECTURE_LABELS.get(card.state, card.state)
        pref_keys = [
            pref_label,
            card.state,
            f"{pref_label} {card.state}",
            f"{pref_label} - {card.state}",
            f"{pref_label} — {card.state}",
        ]
        pref_select_selector = (
            "#billingAdministrativeArea, #billingRegion, "
            'select[name*="administrative" i], select[name*="state" i], '
            'select[name*="region" i], select[name*="province" i], '
            'select[id*="administrative" i], select[id*="state" i], '
            'select[id*="region" i], select[id*="province" i], '
            'select[autocomplete="address-level1"]'
        )
        pref_any_selector = (
            f"{pref_select_selector}, "
            'input[name*="administrative" i], input[name*="state" i], '
            'input[name*="region" i], input[name*="province" i], '
            'input[id*="administrative" i], input[id*="state" i], '
            'input[id*="region" i], input[id*="province" i], '
            'input[autocomplete="address-level1"], '
            '[role="combobox"][aria-label*="都道府県"], '
            '[role="combobox"][aria-label*="prefecture" i], '
            '[role="combobox"][aria-label*="state" i], '
            '[role="combobox"][name*="administrative" i], '
            '[role="combobox"][name*="state" i], '
            '[role="combobox"][name*="region" i], '
            '[aria-label*="都道府県"], [aria-label*="prefecture" i], [aria-label*="state" i]'
        )

        async def _switch_stripe_prefecture_like_country(labels: list[str]) -> bool:
            # 第一段：像国家切换一样，先尝试原生 select_option
            try:
                pref_sel = page.locator(
                    pref_select_selector
                ).first
                if await pref_sel.is_visible(timeout=2000):
                    for key in labels:
                        if not key:
                            continue
                        try:
                            await pref_sel.select_option(key, timeout=2000)
                            return True
                        except Exception:
                            pass
                        try:
                            await pref_sel.select_option(label=key, timeout=2000)
                            return True
                        except Exception:
                            continue
            except Exception:
                pass

            # 第二段：像国家切换一样，JS 遍历 select 的 options 做 value/label 匹配
            try:
                result = await page.evaluate(
                    """(labels) => {
                        const keys = (labels || []).map(x => String(x || '').toLowerCase()).filter(Boolean);
                        const selects = Array.from(document.querySelectorAll('select'));
                        const candidates = selects.filter((sel) => {
                            const name = String(sel.name || '').toLowerCase();
                            const id = String(sel.id || '').toLowerCase();
                            const ac = String(sel.getAttribute('autocomplete') || '').toLowerCase();
                            return (
                                name.includes('administrative') || name.includes('state') || name.includes('region') || name.includes('province') ||
                                id.includes('administrative') || id.includes('state') || id.includes('region') || id.includes('province') ||
                                ac.includes('address-level1')
                            );
                        });
                        for (const sel of candidates) {
                            const opts = Array.from(sel.options || []);
                            let hit = opts.find(o => keys.some(k => String(o.value || '').toLowerCase() === k));
                            if (!hit) {
                                hit = opts.find(o => {
                                    const t = String(o.text || o.label || '').toLowerCase();
                                    return keys.some(k => t.includes(k));
                                });
                            }
                            if (!hit) continue;
                            sel.value = String(hit.value || '');
                            sel.dispatchEvent(new Event('input', {bubbles: true}));
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            return {ok: true, value: sel.value || '', text: String(hit.text || hit.label || '')};
                        }
                        return {ok: false, value: '', text: ''};
                    }""",
                    labels,
                )
                return bool((result or {}).get("ok"))
            except Exception:
                return False

        async def _verify_prefecture_selected(pref_label_value: str, state_key: str) -> tuple[bool, str]:
            try:
                result = await page.evaluate(
                    """(payload) => {
                        const prefLabel = String((payload && payload.prefLabel) || '');
                        const stateKey = String((payload && payload.stateKey) || '');
                        const isVisible = (el) => {
                            if (!el) return false;
                            const r = el.getBoundingClientRect();
                            if (r.width < 8 || r.height < 8) return false;
                            const st = window.getComputedStyle(el);
                            if (!st) return false;
                            if (st.display === 'none' || st.visibility === 'hidden') return false;
                            if (Number(st.opacity || '1') < 0.05) return false;
                            return true;
                        };
                        const keyA = String(prefLabel || '').toLowerCase();
                        const keyB = String(stateKey || '').toLowerCase();
                        const hasKey = (s) => {
                            const t = String(s || '').toLowerCase();
                            return (!!keyA && t.includes(keyA)) || (!!keyB && t.includes(keyB));
                        };
                        const textOf = (el) => String(el?.innerText || el?.textContent || '').trim();
                        let candidates = Array.from(document.querySelectorAll(
                            String((payload && payload.selector) || '')
                        )).filter(isVisible);
                        if (!candidates.length) {
                            // 文本反查：从“都道府県”标签附近找真实控件
                            const labels = Array.from(document.querySelectorAll('*'))
                                .filter(isVisible)
                                .filter(el => {
                                    const t = String(el.innerText || el.textContent || '').trim();
                                    return /都道府県/.test(t) && t.length <= 30;
                                });
                            if (labels.length) {
                                const anchor = labels[0];
                                const host = anchor.closest('label,div,section,fieldset,li') || anchor.parentElement || anchor;
                                const scoped = Array.from(
                                    (host.parentElement || host).querySelectorAll('select,input,[role="combobox"],[aria-haspopup="listbox"],button')
                                ).filter(isVisible);
                                if (scoped.length) {
                                    candidates = scoped;
                                } else {
                                    const globalCands = Array.from(
                                        document.querySelectorAll('select,input,[role="combobox"],button,[aria-haspopup="listbox"]')
                                    ).filter(isVisible).filter(el => {
                                        const hint = [
                                            el.id || '', el.name || '', el.getAttribute('aria-label') || '',
                                            el.getAttribute('placeholder') || '', el.getAttribute('data-testid') || '',
                                            textOf(el),
                                        ].join(' ').toLowerCase();
                                        return /都道府県|prefecture|state|administrative|province|region/.test(hint);
                                    });
                                    if (globalCands.length) candidates = globalCands;
                                    else return { ok: false, reason: 'no_candidate_with_label' };
                                }
                            } else {
                                const globalCands = Array.from(
                                    document.querySelectorAll('select,input,[role="combobox"],button,[aria-haspopup="listbox"]')
                                ).filter(isVisible).filter(el => {
                                    const hint = [
                                        el.id || '', el.name || '', el.getAttribute('aria-label') || '',
                                        el.getAttribute('placeholder') || '', el.getAttribute('data-testid') || '',
                                        textOf(el),
                                    ].join(' ').toLowerCase();
                                    return /prefecture|state|administrative|province|region/.test(hint);
                                });
                                if (globalCands.length) candidates = globalCands;
                                else return { ok: false, reason: 'no_candidate_no_label' };
                            }
                        }
                        const el = candidates[0];
                        const tag = el.tagName.toLowerCase();
                        const ariaInvalid = String(el.getAttribute('aria-invalid') || '').toLowerCase();
                        const rawValue = String(el.value || '').trim();
                        const rawText = String(el.innerText || el.textContent || '').trim();
                        const placeholderLike = /都道府県/.test(rawText) && !hasKey(rawText);
                        let selected = false;
                        if (tag === 'select') {
                            const opt = el.options && el.options[el.selectedIndex || 0];
                            const optText = String((opt && (opt.text || opt.label)) || '').trim();
                            const optVal = String((opt && opt.value) || '').trim();
                            selected = (ariaInvalid !== 'true') && (hasKey(optText) || hasKey(optVal));
                            return {
                                ok: !!selected,
                                reason: selected ? 'select_ok' : `select_miss text=${optText} value=${optVal} ariaInvalid=${ariaInvalid}`,
                            };
                        }
                        selected = (ariaInvalid !== 'true') && !placeholderLike && (hasKey(rawValue) || hasKey(rawText));
                        return {
                            ok: !!selected,
                            reason: selected ? 'combo_ok' : `combo_miss value=${rawValue} text=${rawText} ariaInvalid=${ariaInvalid}`,
                        };
                    }""",
                    {"prefLabel": pref_label_value, "stateKey": state_key, "selector": pref_any_selector},
                )
                return bool((result or {}).get("ok")), str((result or {}).get("reason") or "")
            except Exception as exc:
                return False, f"verify_exception:{exc}"

        async def _pick_prefecture_by_role() -> bool:
            names = [
                f"{pref_label} — {card.state}",
                f"{pref_label} - {card.state}",
                f"{pref_label} {card.state}",
                pref_label,
                card.state,
            ]
            # 先用页面脚本做一次“打开下拉 + 匹配点击”，兼容自定义组件
            try:
                picked_js = await page.evaluate(
                    """(labels) => {
                        const norm = (s) => String(s || '')
                            .toLowerCase()
                            .replace(/[\\s\\-ー—‐－]/g, '')
                            .replace(/[()（）]/g, '');
                        const keys = (labels || []).map(norm).filter(Boolean);
                        if (!keys.length) return false;
                        const isVisible = (el) => {
                            if (!el) return false;
                            const r = el.getBoundingClientRect();
                            if (r.width < 8 || r.height < 8) return false;
                            const st = window.getComputedStyle(el);
                            if (!st) return false;
                            if (st.display === 'none' || st.visibility === 'hidden') return false;
                            if (Number(st.opacity || '1') < 0.05) return false;
                            return true;
                        };
                        const textOf = (el) => String(el?.innerText || el?.textContent || '').trim();
                        const clickEl = (el) => {
                            if (!el) return false;
                            try { el.click(); return true; } catch {}
                            try {
                                const r = el.getBoundingClientRect();
                                el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:r.left+8, clientY:r.top+8}));
                                el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:r.left+8, clientY:r.top+8}));
                                el.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:r.left+8, clientY:r.top+8}));
                                return true;
                            } catch {}
                            return false;
                        };

                        // 1) 打开下拉
                        const triggerSelectors = [
                            '[role="combobox"][aria-label*="都道府県"]',
                            '[role="combobox"][name*="administrative" i]',
                            '[role="combobox"]',
                            'button[aria-haspopup="listbox"]',
                        ];
                        let trigger = null;
                        for (const sel of triggerSelectors) {
                            const cands = Array.from(document.querySelectorAll(sel)).filter(isVisible);
                            const hit = cands.find(el => /都道府県|prefecture|state/i.test(textOf(el) + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('name') || '')));
                            if (hit) { trigger = hit; break; }
                            if (!trigger && cands.length) trigger = cands[0];
                        }
                        if (!trigger) {
                            const labelsText = Array.from(document.querySelectorAll('*'))
                                .filter(isVisible)
                                .find(el => /都道府県/.test(textOf(el)) && textOf(el).length < 20);
                            if (labelsText) {
                                const near = labelsText.closest('label,div,section') || labelsText.parentElement;
                                if (near) {
                                    trigger = near.querySelector('[role="combobox"],button[aria-haspopup="listbox"],input,select') || near;
                                }
                            }
                        }
                        if (trigger) clickEl(trigger);

                        // 2) 匹配选项（role=option/listbox/menuitem/li）
                        const optionSelectors = [
                            '[role="option"]',
                            '[role="listbox"] [role="option"]',
                            'li[role="option"]',
                            'div[role="option"]',
                            '[role="menu"] [role="menuitem"]',
                            'ul li',
                        ];
                        const options = [];
                        for (const sel of optionSelectors) {
                            for (const el of Array.from(document.querySelectorAll(sel))) {
                                if (!isVisible(el)) continue;
                                const t = textOf(el);
                                if (!t) continue;
                                options.push(el);
                            }
                        }
                        const uniq = Array.from(new Set(options));
                        const best = uniq.find(el => {
                            const t = norm(textOf(el));
                            return keys.some(k => t.includes(k));
                        });
                        if (best) return clickEl(best);

                        // 3) 未命中时，选第一个可见候选（JP专用兜底，宁可先选上）
                        if (uniq.length) return clickEl(uniq[0]);
                        return false;
                    }""",
                    names,
                )
                if picked_js:
                    return True
            except Exception:
                pass

            # 打开都道府县下拉
            opened = False
            open_selectors = [
                '[role="combobox"][aria-label*="都道府県"]',
                '[role="combobox"][name*="administrative" i]',
                '[role="combobox"][name*="region" i]',
                '[role="combobox"][name*="province" i]',
                '[role="combobox"]:has-text("都道府県")',
                'button[aria-haspopup="listbox"]:has-text("都道府県")',
                'button[aria-haspopup="listbox"][aria-label*="都道府県"]',
                'button[aria-haspopup="listbox"][aria-label*="prefecture" i]',
                'button[aria-haspopup="listbox"][aria-label*="state" i]',
            ]
            for sel in open_selectors:
                try:
                    trigger = page.locator(sel).first
                    if await trigger.is_visible(timeout=1200):
                        await trigger.click(timeout=2000)
                        opened = True
                        break
                except Exception:
                    continue
            if not opened:
                try:
                    # 点击标签附近触发
                    lbl = page.locator('text=都道府県').first
                    if await lbl.is_visible(timeout=1200):
                        await lbl.click(timeout=2000)
                        opened = True
                except Exception:
                    pass
            if not opened:
                return False

            await page.wait_for_timeout(350)

            # 优先 role=option
            for name in names:
                if not name:
                    continue
                try:
                    opt = page.get_by_role("option", name=name).first
                    if await opt.is_visible(timeout=1200):
                        await opt.click(timeout=2000)
                        return True
                except Exception:
                    pass

            # 兜底：listbox 内文本匹配
            for name in names:
                if not name:
                    continue
                listbox_selectors = [
                    f'[role="listbox"] >> text={name}',
                    f'li[role="option"]:has-text("{name}")',
                    f'div[role="option"]:has-text("{name}")',
                ]
                for sel in listbox_selectors:
                    try:
                        opt2 = page.locator(sel).first
                        if await opt2.is_visible(timeout=1000):
                            await opt2.click(timeout=2000)
                            return True
                    except Exception:
                        continue

            # 最后一招：方向键+回车
            try:
                await page.keyboard.press("ArrowDown")
                await page.wait_for_timeout(180)
                await page.keyboard.press("Enter")
                return True
            except Exception:
                return False

        async def _pick_prefecture_by_tab_fallback() -> bool:
            """不依赖下拉 DOM：从邮编框 Tab 到都道府県并键盘选中。"""
            zip_sel = '#billingPostalCode, input[name*="postalCode" i], input[name*="zip" i], input[placeholder*="邮编" i], input[placeholder*="ZIP" i]'
            try:
                zip_input = page.locator(f"{zip_sel}:visible").first
                if await zip_input.is_visible(timeout=1200):
                    await zip_input.click(timeout=1500)
                    await page.wait_for_timeout(120)
                    await page.keyboard.press("Tab")
                    await page.wait_for_timeout(150)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.type(pref_label, delay=35)
                    await page.wait_for_timeout(250)
                    await page.keyboard.press("ArrowDown")
                    await page.wait_for_timeout(150)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(250)
                    return True
            except Exception:
                return False
            return False

        async def _pick_prefecture_by_label_api() -> bool:
            """Playwright 标签定位兜底：直接按 label/aria-label 定位都道府県控件。"""
            patterns = [r"都道府県", r"prefecture", r"state", r"province", r"region"]
            for pattern in patterns:
                try:
                    field = page.get_by_label(re.compile(pattern, re.I)).first
                    if not await field.is_visible(timeout=900):
                        continue
                    tag = await field.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        for key in [pref_label, card.state, f"{pref_label} {card.state}"]:
                            if not key:
                                continue
                            try:
                                await field.select_option(label=key, timeout=1400)
                                return True
                            except Exception:
                                try:
                                    await field.select_option(value=key, timeout=1400)
                                    return True
                                except Exception:
                                    continue
                    else:
                        await field.click(timeout=1200)
                        try:
                            await field.fill("", timeout=1200)
                        except Exception:
                            pass
                        await field.type(pref_label, delay=30)
                        await page.wait_for_timeout(180)
                        for key in [pref_label, card.state]:
                            try:
                                opt = page.locator(f'[role="option"]:has-text("{key}")').first
                                if await opt.is_visible(timeout=800):
                                    await opt.click(timeout=1200)
                                    return True
                            except Exception:
                                continue
                        try:
                            await page.keyboard.press("Enter")
                            return True
                        except Exception:
                            pass
                except Exception:
                    continue
            return False

        await page.wait_for_timeout(800)
        await _safe_fill(
            '#billingAddressLine1, input[name*="addressLine1" i], input[name*="address" i], input[placeholder*="地址" i], input[placeholder*="Address" i]',
            card.street,
        )
        # Stripe 日本表单在地址输入后可能重置“城市”，最后再回填一次并校验
        city_selector = '#billingLocality, input[name*="locality" i], input[name*="city" i], input[placeholder*="城市" i], input[placeholder*="City" i]'
        city_ok = await _safe_fill(city_selector, city_value)
        if not city_ok:
            await page.wait_for_timeout(600)
            city_ok = await _safe_fill(city_selector, city_value)
        if not city_ok:
            log(f"[Stripe] ⚠️ 日本地址城市填充失败: city={city_value}")

        # 地址阶段再统一执行都道府县选择与校验
        pref_ok = await _switch_stripe_prefecture_like_country(pref_keys)
        v_ok, v_reason = await _verify_prefecture_selected(pref_label, card.state)
        log(
            f"[Stripe][JP] pref step1 like-country: attempted={pref_ok} "
            f"committed={v_ok} reason={v_reason}"
        )

        try:
            state_el = page.locator(pref_any_selector).first
            if await state_el.is_visible(timeout=3000):
                tag = await state_el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    picked = False
                    for v in [pref_label, card.state, f"{pref_label} — {card.state}", f"{pref_label} {card.state}"]:
                        if not v:
                            continue
                        try:
                            await state_el.select_option(value=v, timeout=2500)
                            picked = True
                            break
                        except Exception:
                            try:
                                await state_el.select_option(label=v, timeout=2500)
                                picked = True
                                break
                            except Exception:
                                continue
                    if not picked:
                        await page.evaluate(
                            """(targetKeys) => {
                                const keys = (targetKeys || []).map(x => String(x || '').toLowerCase()).filter(Boolean);
                                const selectors = [
                                    '#billingAdministrativeArea',
                                    '#billingRegion',
                                    'select[name*="administrative" i]',
                                    'select[name*="state" i]',
                                    'select[name*="region" i]',
                                    'select[name*="province" i]',
                                    'select[id*="administrative" i]',
                                    'select[id*="state" i]',
                                    'select[id*="region" i]',
                                    'select[id*="province" i]',
                                    'select[autocomplete="address-level1"]'
                                ];
                                const sel = selectors.map(s => document.querySelector(s)).find(Boolean);
                                if (!sel || sel.tagName.toLowerCase() !== 'select') return false;
                                const opts = Array.from(sel.options || []);
                                const hit = opts.find(o => {
                                    const v = String(o.value || '').toLowerCase();
                                    const t = String(o.text || o.label || '').toLowerCase();
                                    return keys.some(k => v === k || t.includes(k));
                                });
                                if (!hit) return false;
                                sel.value = String(hit.value || '');
                                sel.dispatchEvent(new Event('input', {bubbles: true}));
                                sel.dispatchEvent(new Event('change', {bubbles: true}));
                                return true;
                            }""",
                            [pref_label, card.state, f"{pref_label} {card.state}", f"{pref_label} — {card.state}"],
                        )
                else:
                    try:
                        await state_el.click(timeout=1500)
                    except Exception:
                        pass
                    try:
                        await state_el.fill("", timeout=1500)
                        await state_el.fill(pref_label, timeout=3000)
                    except Exception:
                        try:
                            await page.keyboard.type(pref_label, delay=30)
                        except Exception:
                            pass
                    picked_combo = False
                    for key in [pref_label, card.state]:
                        if not key:
                            continue
                        option_selectors = [
                            f'[role="option"]:has-text("{key}")',
                            f'li:has-text("{key}")',
                            f'div[role="option"]:has-text("{key}")',
                        ]
                        for sel in option_selectors:
                            try:
                                opt = page.locator(sel).first
                                if await opt.is_visible(timeout=1200):
                                    await opt.click(timeout=2000)
                                    picked_combo = True
                                    break
                            except Exception:
                                continue
                        if picked_combo:
                            break
                    try:
                        await page.keyboard.press("Enter")
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            pref_verified, verify_reason = await _verify_prefecture_selected(pref_label, card.state)
            if not pref_verified:
                pref_verified = await _pick_prefecture_by_role()
                if pref_verified:
                    await page.wait_for_timeout(300)
                pref_verified2, verify_reason2 = await _verify_prefecture_selected(pref_label, card.state)
                log(
                    f"[Stripe][JP] pref step2 role: attempted={pref_verified} "
                    f"committed={pref_verified2} reason={verify_reason2}"
                )
                pref_verified = pref_verified2
                verify_reason = verify_reason2
            if not pref_verified:
                pref_verified = await _pick_prefecture_by_label_api()
                if pref_verified:
                    await page.wait_for_timeout(280)
                pref_verified2, verify_reason2 = await _verify_prefecture_selected(pref_label, card.state)
                log(
                    f"[Stripe][JP] pref step2b label: attempted={pref_verified} "
                    f"committed={pref_verified2} reason={verify_reason2}"
                )
                pref_verified = pref_verified2
                verify_reason = verify_reason2
            if not pref_verified:
                pref_verified = await _pick_prefecture_by_tab_fallback()
                if pref_verified:
                    await page.wait_for_timeout(300)
                pref_verified2, verify_reason2 = await _verify_prefecture_selected(pref_label, card.state)
                log(
                    f"[Stripe][JP] pref step3 tab: attempted={pref_verified} "
                    f"committed={pref_verified2} reason={verify_reason2}"
                )
                pref_verified = pref_verified2
                verify_reason = verify_reason2
            if not pref_verified:
                log(
                    f"[Stripe] ⚠️ 日本地址都道府县未命中: state={card.state}, label={pref_label}, "
                    f"verify_reason={verify_reason}"
                )
        except Exception:
            pass

    else:
        # 美区等旧流程
        for selector, value in [
            ('#billingAddressLine1, input[name*="addressLine1" i], input[name*="address" i], input[placeholder*="地址" i], input[placeholder*="Address" i]', card.street),
            ('#billingLocality, input[name*="locality" i], input[name*="city" i], input[placeholder*="城市" i], input[placeholder*="City" i]', city_value),
            ('#billingPostalCode, input[name*="postalCode" i], input[name*="zip" i], input[placeholder*="邮编" i], input[placeholder*="ZIP" i]', zip_value),
        ]:
            await _safe_fill(selector, value)

        # State - 尝试多种方式匹配
        try:
            state_el = page.locator('#billingAdministrativeArea, select[name*="state" i]').first
            if await state_el.is_visible(timeout=3000):
                tag = await state_el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    try:
                        await state_el.select_option(value=card.state, timeout=3000)
                    except Exception:
                        # 尝试用全称
                        state_names = {
                            "MN": "Minnesota", "TN": "Tennessee", "CA": "California",
                            "TX": "Texas", "NY": "New York", "FL": "Florida",
                            "IL": "Illinois", "PA": "Pennsylvania", "OH": "Ohio",
                            "GA": "Georgia", "NC": "North Carolina", "MI": "Michigan",
                            "NJ": "New Jersey", "VA": "Virginia", "WA": "Washington",
                            "AZ": "Arizona", "MA": "Massachusetts", "IN": "Indiana",
                            "MO": "Missouri", "MD": "Maryland", "WI": "Wisconsin",
                            "CO": "Colorado", "SC": "South Carolina", "AL": "Alabama",
                            "LA": "Louisiana", "KY": "Kentucky", "OR": "Oregon",
                            "OK": "Oklahoma", "CT": "Connecticut", "IA": "Iowa",
                            "MS": "Mississippi", "AR": "Arkansas", "KS": "Kansas",
                            "NV": "Nevada", "UT": "Utah", "NE": "Nebraska",
                            "NM": "New Mexico", "WV": "West Virginia", "ID": "Idaho",
                            "HI": "Hawaii", "ME": "Maine", "NH": "New Hampshire",
                            "RI": "Rhode Island", "MT": "Montana", "DE": "Delaware",
                            "SD": "South Dakota", "ND": "North Dakota", "AK": "Alaska",
                            "VT": "Vermont", "WY": "Wyoming", "DC": "District of Columbia",
                        }
                        full_name = state_names.get(card.state, card.state)
                        try:
                            await state_el.select_option(label=full_name, timeout=3000)
                        except Exception:
                            pass
                else:
                    await state_el.fill(card.state, timeout=3000)
        except Exception:
            pass

    # 勾选条款
    try:
        cb = page.locator('input[type="checkbox"], [role="checkbox"]').first
        if await cb.is_visible(timeout=2000) and not await cb.is_checked():
            await cb.click(force=True)
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    # 关闭可能弹出的地址建议下拉框（Google 地址自动补全）
    # 按 Escape 关闭下拉，再点击页面空白处确保焦点离开输入框
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
        # 点击页面标题区域，确保没有下拉遮挡
        await page.locator('body').click(position={"x": 10, "y": 10}, force=True)
        await page.wait_for_timeout(500)
    except Exception:
        pass

    # Subscribe / 订阅 - 多种选择器兜底
    log("[Stripe] 点击订阅按钮...")
    subscribe_clicked = False
    subscribe_selectors = [
        'button:has-text("Subscribe")',
        'button:has-text("订阅")',
        'button.SubmitButton',
        'button.SubmitButton--complete',
        '[data-testid="hosted-payment-submit-button"]',
        'button[type="submit"]',
    ]
    for sel in subscribe_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                # 确保按钮没被禁用
                is_disabled = await btn.is_disabled()
                if is_disabled:
                    log(f"[Stripe] 按钮被禁用: {sel}，等待...")
                    await page.wait_for_timeout(3000)
                await btn.click(timeout=10000)
                subscribe_clicked = True
                log(f"[Stripe] 订阅按钮已点击 (选择器: {sel})")
                break
        except Exception:
            continue

    if not subscribe_clicked:
        # 终极兜底：点击页面上最后一个可见的 submit 按钮
        log("[Stripe] 常规选择器未命中，尝试 JS 点击...")
        await page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            for (const btn of buttons.reverse()) {
                const text = (btn.textContent || '').trim();
                const rect = btn.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 && /Subscribe|订阅|submit/i.test(text + btn.type)) {
                    btn.click();
                    return;
                }
            }
        }""")

    # 等跳转 PayPal - 如果 10 秒没跳转，再点一次订阅
    jumped = False
    for attempt in range(2):
        for _ in range(30 if attempt == 0 else 30):
            if "paypal.com" in page.url:
                jumped = True
                break
            await page.wait_for_timeout(1000)
        if jumped:
            break
        if attempt == 0:
            log("[Stripe] 30s 未跳转 PayPal，检查是否有表单错误并重试点击...")
            # 检查是否有错误提示
            has_error = await page.evaluate("""() => {
                const text = (document.body?.innerText || '');
                return /This is required|必填|invalid|错误|error/i.test(text);
            }""")
            if has_error:
                log("[Stripe] 检测到表单错误，可能地址未填完整")
            # 再点一次
            for sel in subscribe_selectors[:3]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click(timeout=5000)
                        log(f"[Stripe] 重试点击订阅按钮: {sel}")
                        break
                except Exception:
                    continue

    if not jumped:
        raise RuntimeError(f"60s 内未跳转 PayPal，URL: {page.url}")
    await page.wait_for_timeout(3000)


async def fill_paypal(
    page,
    email: str,
    card: CardInfo,
    phone: PhoneInfo,
    paypal_password: str,
    proxy: str | None = None,
    *,
    country_code: str = "US",
) -> None:
    """PayPal 页面：注册 + 绑卡。"""
    await page.wait_for_timeout(5000)

    # 第一步：填邮箱（确保所有可见的 email 字段都被填写）
    log("[PayPal] 填写邮箱...")
    email_filled = False
    # PayPal 中文页面的 placeholder 是 "电子邮箱地址或手机号码"
    email_selectors = 'input[name="email"], input[type="email"], input[placeholder*="邮箱" i], input[placeholder*="email" i], input[placeholder*="手机号" i]'
    email_fields = page.locator(email_selectors)
    count = await email_fields.count()
    for i in range(count):
        field = email_fields.nth(i)
        try:
            if await field.is_visible(timeout=2000):
                await field.fill("", timeout=2000)
                await field.fill(email, timeout=3000)
                email_filled = True
                break
        except Exception:
            pass
    if not email_filled:
        log("[PayPal] ⚠️ 未找到可见的邮箱输入框，尝试备用选择器")
        try:
            await page.locator('input[aria-label*="email" i], input[aria-label*="邮箱" i]').first.fill(email, timeout=5000)
        except Exception:
            pass

    # 点击"继续付款"优先（PayPal 注册页面的实际按钮文字），然后是其他变体
    clicked_next = False
    next_selectors = [
        'button:has-text("继续付款")',
        'button:has-text("Continue")',
        'button:has-text("继续")',
        'button:has-text("Next")',
        'button:has-text("下一页")',
        'button:has-text("次へ")',
        'button[id="btnNext"]',
        'button[type="submit"]',
    ]
    for sel in next_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=5000)
                clicked_next = True
                log(f"[PayPal] 点击了按钮 (选择器: {sel})")
                break
        except Exception:
            continue

    if not clicked_next:
        log("[PayPal] ⚠️ 未找到可点击的下一步按钮")

    # 等待 PayPal 页面加载完成（按钮转圈结束，新页面元素出现）
    log("[PayPal] 等待页面跳转/加载...")
    for _ in range(30):
        await page.wait_for_timeout(2000)
        # 检查是否已经进入了注册表单（有国家选择框或手机号输入框）
        try:
            has_form = await page.evaluate("""() => {
                const selects = document.querySelectorAll('select');
                const hasCountrySelect = Array.from(selects).some(s => s.options.length > 50 || /country/i.test(s.name + s.id));
                const hasPhoneInput = !!document.querySelector('input[name*="phone" i], input[id*="phone" i], input[placeholder*="Phone" i], input[placeholder*="手机" i]');
                const hasCardInput = !!document.querySelector('input[name*="card" i], input[id*="card" i], input[placeholder*="Card" i], input[placeholder*="卡号" i]');
                return hasCountrySelect || hasPhoneInput || hasCardInput;
            }""")
        except Exception:
            # 页面正在导航中，等一下再试
            continue
        if has_form:
            log("[PayPal] 注册表单已加载")
            break
    else:
        log("[PayPal] ⚠️ 等待 60 秒后仍未检测到注册表单，继续尝试...")

    await page.wait_for_timeout(2000)

    desired_country = "JP" if str(country_code or "").upper() == "JP" else "US"
    desired_labels = ["Japan", "日本"] if desired_country == "JP" else ["United States", "美国"]

    # 第二步：进入注册表单后，先切国家
    # 等待国家下拉框出现并可交互
    log("[PayPal] 等待国家选择框加载...")
    try:
        await page.locator('select').first.wait_for(state="attached", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)

    async def _switch_paypal_country(target_country: str, labels: list[str]) -> bool:
        # 先尝试 select_option（值 / 文本）
        try:
            country_sel = page.locator('select[name*="country" i], select[id*="country" i]').first
            if await country_sel.is_visible(timeout=2000):
                try:
                    await country_sel.select_option(target_country, timeout=2500)
                    return True
                except Exception:
                    pass
                for lbl in labels:
                    try:
                        await country_sel.select_option(label=lbl, timeout=2500)
                        return True
                    except Exception:
                        continue
        except Exception:
            pass

        # 兜底：遍历所有 select，按 value 或 option 文本匹配国家
        result = await page.evaluate("""(targetCountry, labels) => {
            const keys = (labels || []).map(x => String(x || '').toLowerCase()).filter(Boolean);
            const selects = Array.from(document.querySelectorAll('select'));
            const candidates = selects.filter((sel) => {
                const name = String(sel.name || '').toLowerCase();
                const id = String(sel.id || '').toLowerCase();
                return name.includes('country') || id.includes('country') || sel.options.length > 50;
            });
            for (const sel of candidates) {
                const opts = Array.from(sel.options || []);
                let hit = opts.find(o => String(o.value || '').toUpperCase() === String(targetCountry || '').toUpperCase());
                if (!hit) {
                    hit = opts.find(o => {
                        const t = String(o.text || o.label || '').toLowerCase();
                        return keys.some(k => t.includes(k));
                    });
                }
                if (!hit) continue;
                sel.value = String(hit.value || '');
                sel.dispatchEvent(new Event('input', {bubbles: true}));
                sel.dispatchEvent(new Event('change', {bubbles: true}));
                const text = String(hit.text || hit.label || '').trim();
                return {ok: true, value: sel.value || '', text};
            }
            return {ok: false, value: '', text: ''};
        }""", target_country, labels)
        return bool((result or {}).get("ok"))

    log(f"[PayPal] 切换国家到 {desired_country} ...")
    country_switched = await _switch_paypal_country(desired_country, desired_labels)
    if not country_switched:
        log("[PayPal] ⚠️ 国家切换首轮未命中，将继续进入复检重试")

    # 国家切换后页面会重新渲染表单字段，必须等待足够时间
    log("[PayPal] 等待页面根据新国家重新加载表单...")
    await page.wait_for_timeout(5000)

    # 等待表单字段重新出现（国家切换后地址字段会重新渲染）
    try:
        await page.locator('input[name*="phone" i], input[id*="phone" i], input[placeholder*="Phone" i]').first.wait_for(state="visible", timeout=10000)
    except Exception:
        await page.wait_for_timeout(3000)

    # 验证国家是否切换成功
    current_country = await page.evaluate("""() => {
        const selects = Array.from(document.querySelectorAll('select'));
        for (const sel of selects) {
            if (sel.name.toLowerCase().includes('country') || sel.id.toLowerCase().includes('country') || sel.options.length > 50) {
                const idx = sel.selectedIndex || 0;
                const opt = sel.options && sel.options[idx];
                const text = opt ? String(opt.text || opt.label || '').trim() : '';
                return { value: sel.value || '', text };
            }
        }
        return { value: '', text: '' };
    }""")
    current_value = str((current_country or {}).get("value") or "")
    current_text = str((current_country or {}).get("text") or "").strip().lower()
    if desired_country == "JP":
        country_ok = current_value.upper() == "JP" or ("japan" in current_text) or ("日本" in current_text)
    else:
        country_ok = current_value.upper() == "US" or ("united states" in current_text) or ("美国" in current_text)

    if not country_ok:
        log(f"[PayPal] ⚠️ 国家仍为 value={current_value}, text={current_text}，再次尝试切换到 {desired_country}...")
        await _switch_paypal_country(desired_country, desired_labels)
        await page.wait_for_timeout(5000)
    else:
        log(f"[PayPal] ✓ 国家已确认切换为 {desired_country} (value={current_value}, text={current_text})")

    # 再次确认邮箱（国家切换后页面可能重置了邮箱字段）
    try:
        email_fields_after = page.locator('input[name="email"], input[type="email"]')
        count_after = await email_fields_after.count()
        for i in range(count_after):
            field = email_fields_after.nth(i)
            if await field.is_visible(timeout=1000):
                val = (await field.input_value()).strip()
                if not val:
                    log("[PayPal] 邮箱字段为空，重新填写...")
                    await field.fill(email, timeout=3000)
                break
    except Exception:
        pass

    # 手机号
    phone_local = phone.number.lstrip("+1") if phone.number.startswith("+1") else phone.number.lstrip("+")
    try:
        phone_input = page.locator('input[name*="phone" i], input[id*="phone" i], input[placeholder*="Phone" i]').first
        await phone_input.fill("", timeout=2000)
        await phone_input.fill(phone_local, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # 卡号
    try:
        card_input = page.locator('input[name="cardnumber"], input[id*="card" i], input[placeholder*="Card" i]').first
        await card_input.fill("", timeout=2000)
        await card_input.fill(card.number, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # 有效期
    try:
        exp_input = page.locator('input[name="exp-date"], input[id*="exp" i], input[placeholder*="Expir" i]').first
        await exp_input.fill("", timeout=2000)
        await exp_input.fill(f"{card.exp_month}/{card.exp_year}", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # CVV
    try:
        cvv_input = page.locator('input[name="cvv"], input[id*="cvv" i], input[placeholder*="CVV" i]').first
        await cvv_input.fill("", timeout=2000)
        await cvv_input.fill(card.cvv, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # 地址（先清空再填写，避免残留旧值）
    # PayPal 页面字段可能用 placeholder 而非 name 属性，需要多种选择器兜底
    log("[PayPal] 填写地址信息...")
    jp_birth, jp_first_kana, jp_last_kana, jp_first_kanji, jp_last_kanji = _jp_identity_values(card, email)
    jp_first_kata = _hiragana_to_katakana(jp_first_kana)
    jp_last_kata = _hiragana_to_katakana(jp_last_kana)
    first_name_value = jp_first_kata if desired_country == "JP" else card.first_name
    last_name_value = jp_last_kata if desired_country == "JP" else card.last_name

    # First name
    try:
        loc = page.locator('input[name="fname"], input[id*="first" i], input[placeholder*="First" i], input[autocomplete="given-name"]').first
        if await loc.is_visible(timeout=2000):
            await loc.fill("", timeout=2000)
            await loc.fill(first_name_value, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(300)

    # Last name
    try:
        loc = page.locator('input[name="lname"], input[id*="last" i], input[placeholder*="Last" i], input[autocomplete="family-name"]').first
        if await loc.is_visible(timeout=2000):
            await loc.fill("", timeout=2000)
            await loc.fill(last_name_value, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(300)

    # Street address - PayPal 页面可能用浮动 label 而非 placeholder
    # 尝试多种方式定位 street 字段
    street_filled = False
    street_selectors = [
        'input[name*="street" i]',
        'input[name*="address" i]:not([name*="email" i])',
        'input[autocomplete="address-line1"]',
        'input[placeholder*="Street" i]',
        'input[placeholder*="地址" i]',
        'input[placeholder*="Address" i]',
        'input[aria-label*="Street" i]',
        'input[aria-label*="address" i]:not([aria-label*="email" i])',
        'input[id*="street" i]',
        'input[id*="address" i]:not([id*="email" i])',
    ]
    for sel in street_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.fill("", timeout=2000)
                await loc.fill(card.street, timeout=5000)
                street_filled = True
                log(f"[PayPal] Street 已填 (选择器: {sel})")
                break
        except Exception:
            continue

    if not street_filled:
        # 最后兜底：通过 label 文本找到对应的 input
        log("[PayPal] Street 常规选择器均未命中，尝试通过 label 文本定位...")
        try:
            loc = page.get_by_label("Street address", exact=False).first
            if await loc.is_visible(timeout=2000):
                await loc.fill(card.street, timeout=5000)
                street_filled = True
        except Exception:
            pass
        if not street_filled:
            try:
                loc = page.get_by_label("地址", exact=False).first
                if await loc.is_visible(timeout=2000):
                    await loc.fill(card.street, timeout=5000)
                    street_filled = True
            except Exception:
                pass
        if not street_filled:
            # 终极兜底：找 Billing address 区域下第一个空的 text input（排除 first/last name）
            try:
                street_filled = await page.evaluate("""(street) => {
                    const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'));
                    for (const inp of inputs) {
                        const rect = inp.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const label = (inp.placeholder || inp.getAttribute('aria-label') || inp.name || inp.id || '').toLowerCase();
                        // 跳过已知字段
                        if (/first|last|city|zip|postal|phone|email|apt|suite|bldg/i.test(label)) continue;
                        // 跳过已有值的
                        if ((inp.value || '').trim()) continue;
                        // 这个可能就是 street
                        const proto = HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        desc?.set?.call(inp, street);
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    return false;
                }""", card.street)
            except Exception:
                pass
    if not street_filled:
        log("[PayPal] ⚠️ Street address 填写失败，所有选择器均未命中")
    await page.wait_for_timeout(300)

    # City
    try:
        loc = page.locator('input[name="city"], input[name*="city" i], input[autocomplete="address-level2"], input[placeholder*="City" i], input[placeholder*="城市" i]').first
        if await loc.is_visible(timeout=3000):
            await loc.fill("", timeout=2000)
            await loc.fill(card.city, timeout=5000)
        else:
            raise Exception("not visible")
    except Exception:
        try:
            loc = page.get_by_placeholder("City").first
            await loc.fill(card.city, timeout=5000)
        except Exception:
            pass
    await page.wait_for_timeout(300)

    # ZIP code
    try:
        loc = page.locator('input[name*="zip" i], input[name*="postal" i], input[autocomplete="postal-code"], input[placeholder*="ZIP" i], input[placeholder*="邮编" i], input[placeholder*="Postal" i]').first
        if await loc.is_visible(timeout=2000):
            await loc.fill("", timeout=2000)
            await loc.fill(card.zip_code, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(300)

    async def _fill_paypal_jp_prefecture() -> tuple[bool, str]:
        pref_label = _JP_PREFECTURE_LABELS.get(card.state, card.state)
        pref_code = _JP_PAYPAL_PREFECTURE_CODES.get(card.state, "")
        pref_code_base = pref_code.rsplit("-", 1)[0] if "-" in pref_code else pref_code
        pref_keys = [pref_label, card.state, pref_code, pref_code_base, card.state.upper()]
        pref_keys = [x for x in pref_keys if x]

        # 1) 原生 select_option
        try:
            sel = page.locator(
                '#state, #province, '
                'select[name="state"], select[name*="state" i], select[name*="prefecture" i], '
                'select[name*="region" i], select[name*="province" i], '
                'select[id*="state" i], select[id*="prefecture" i], select[id*="region" i], select[id*="province" i], '
                'select[autocomplete="address-level1"], select[aria-label*="都道府県"], select[aria-label*="Prefecture" i]'
            ).first
            if await sel.is_visible(timeout=1400):
                for key in pref_keys:
                    try:
                        await sel.select_option(value=key, timeout=1500)
                        return True, f"select:value:{key}"
                    except Exception:
                        pass
                    try:
                        await sel.select_option(label=key, timeout=1500)
                        return True, f"select:label:{key}"
                    except Exception:
                        continue
        except Exception:
            pass

        # 2) 组合下拉（都道府県）
        try:
            result = await page.evaluate(
                """(payload) => {
                    const keys = (payload.keys || []).map(x => String(x || '').trim()).filter(Boolean);
                    const keysN = keys.map(x => x.toLowerCase().replace(/[\\s\\-ー—‐－]/g, ''));
                    const isVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        if (r.width < 8 || r.height < 8) return false;
                        const st = window.getComputedStyle(el);
                        if (!st) return false;
                        return st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity || '1') > 0.05;
                    };
                    const norm = (s) => String(s || '').toLowerCase().replace(/[\\s\\-ー—‐－]/g, '');
                    const textOf = (el) => String(el?.innerText || el?.textContent || '').trim();
                    const hasKey = (s) => {
                        const t = norm(s);
                        return keysN.some(k => t.includes(k));
                    };
                    const clickEl = (el) => {
                        if (!el) return false;
                        try { el.click(); return true; } catch {}
                        return false;
                    };

                    const controls = Array.from(document.querySelectorAll(
                        'select,input,button,[role="combobox"],[aria-haspopup="listbox"],div'
                    )).filter(isVisible).filter(el => {
                        const hint = [
                            el.id || '', el.name || '', el.getAttribute('aria-label') || '',
                            el.getAttribute('placeholder') || '', el.getAttribute('data-testid') || '', textOf(el),
                        ].join(' ').toLowerCase();
                        return /都道府県|prefecture|state|province|region|administrative/.test(hint);
                    });
                    if (!controls.length) return { ok: false, reason: 'no_control' };

                    for (const ctl of controls) {
                        const tag = String(ctl.tagName || '').toLowerCase();
                        if (tag === 'select') {
                            const opts = Array.from(ctl.options || []);
                            let hit = opts.find(o => hasKey(o.value || ''));
                            if (!hit) hit = opts.find(o => hasKey(o.text || o.label || ''));
                            if (!hit) continue;
                            ctl.value = String(hit.value || '');
                            ctl.dispatchEvent(new Event('input', { bubbles: true }));
                            ctl.dispatchEvent(new Event('change', { bubbles: true }));
                            return { ok: true, reason: 'select_set' };
                        }

                        clickEl(ctl);
                        const options = Array.from(document.querySelectorAll(
                            '[role="option"], [role="listbox"] li, li[role="option"], div[role="option"], ul li, button'
                        )).filter(isVisible).filter(el => {
                            const t = textOf(el);
                            if (!t || t.length > 40) return false;
                            return hasKey(t);
                        });
                        if (options.length) {
                            clickEl(options[0]);
                            return { ok: true, reason: 'option_click' };
                        }
                    }
                    return { ok: false, reason: 'no_option' };
                }""",
                {"keys": pref_keys},
            )
            if bool((result or {}).get("ok")):
                return True, str((result or {}).get("reason") or "combo_ok")
            return False, str((result or {}).get("reason") or "combo_fail")
        except Exception as exc:
            return False, f"exception:{exc}"

    # State/Prefecture（JP 下优先填都道府県）
    if desired_country == "JP":
        ok, why = await _fill_paypal_jp_prefecture()
        log(f"[PayPal][JP] 都道府県填充: ok={ok} reason={why} target={_JP_PREFECTURE_LABELS.get(card.state, card.state)}")
    else:
        try:
            state_sel = page.locator('select[name="state"], select[name*="state" i], select[autocomplete="address-level1"], select[aria-label*="State" i]').first
            if await state_sel.is_visible(timeout=3000):
                try:
                    await state_sel.select_option(value=card.state, timeout=5000)
                except Exception:
                    try:
                        await state_sel.select_option(label=card.state, timeout=3000)
                    except Exception:
                        pass
            else:
                state_input = page.locator('input[name*="state" i], input[placeholder*="State" i], input[autocomplete="address-level1"]').first
                if await state_input.is_visible(timeout=2000):
                    await state_input.fill("", timeout=2000)
                    await state_input.fill(card.state, timeout=5000)
        except Exception:
            pass
    await page.wait_for_timeout(500)

    # 密码
    try:
        pwd_input = page.locator('input[name="password"], input[type="password"]').first
        if await pwd_input.is_visible(timeout=3000):
            await pwd_input.fill("", timeout=2000)
            await pwd_input.fill(paypal_password, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(1000)

    # 日本实名页会额外要求生日 + かな + 漢字姓名
    if desired_country == "JP":
        await _fill_paypal_jp_identity(page, email=email, card=card)
        await page.wait_for_timeout(600)
        # 日本页经常要求“名/姓”为假名；若页面报错则强制改写一次
        try:
            kana_name_invalid = await page.evaluate("""() => {
                const text = (document.body?.innerText || '');
                return /ひらがなまたはカタカナ/.test(text);
            }""")
            if kana_name_invalid:
                log("[PayPal][JP] 检测到姓名假名校验错误，强制重填 名/姓 为假名")
                await page.evaluate(
                    """(payload) => {
                        const first = String(payload.first || '');
                        const last = String(payload.last || '');
                        const isVisible = (el) => {
                            if (!el) return false;
                            const r = el.getBoundingClientRect();
                            if (r.width < 8 || r.height < 8) return false;
                            const st = window.getComputedStyle(el);
                            if (!st) return false;
                            return st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity || '1') > 0.05;
                        };
                        const setVal = (el, v) => {
                            if (!el) return false;
                            const proto = HTMLInputElement.prototype;
                            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                            if (desc && typeof desc.set === 'function') desc.set.call(el, v);
                            else el.value = v;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new Event('blur', { bubbles: true }));
                            return true;
                        };
                        const sig = (el) => {
                            const p = String(el.placeholder || '').toLowerCase();
                            const a = String(el.getAttribute('aria-label') || '').toLowerCase();
                            const n = String(el.name || '').toLowerCase();
                            const i = String(el.id || '').toLowerCase();
                            const t = String(el.closest('label')?.innerText || el.parentElement?.innerText || '').toLowerCase();
                            return `${p} ${a} ${n} ${i} ${t}`;
                        };
                        const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible);
                        let firstDone = false;
                        let lastDone = false;
                        for (const el of inputs) {
                            const s = sig(el);
                            if (!firstDone && /(\\b名\\b|first|given)/.test(s)) firstDone = setVal(el, first) || firstDone;
                            if (!lastDone && /(\\b姓\\b|last|family)/.test(s)) lastDone = setVal(el, last) || lastDone;
                        }
                        return { firstDone, lastDone };
                    }""",
                    {"first": jp_first_kata, "last": jp_last_kata},
                )
                await page.wait_for_timeout(500)
        except Exception:
            pass

    # 最终检查：确认所有关键字段已填写
    log("[PayPal] 检查表单完整性...")
    empty_fields = await page.evaluate("""() => {
        const checks = [
            {name: 'email', selectors: 'input[name="email"], input[type="email"]'},
            {name: 'phone', selectors: 'input[name*="phone" i], input[id*="phone" i]'},
            {name: 'card', selectors: 'input[name="cardnumber"], input[id*="card" i]'},
            {name: 'firstName', selectors: 'input[name="fname"], input[id*="first" i], input[autocomplete="given-name"], input[placeholder*="First" i]'},
            {name: 'lastName', selectors: 'input[name="lname"], input[id*="last" i], input[autocomplete="family-name"], input[placeholder*="Last" i]'},
            {name: 'street', selectors: 'input[name*="street" i], input[name*="address" i], input[autocomplete="address-line1"], input[placeholder*="Street" i]'},
            {name: 'city', selectors: 'input[name="city"], input[name*="city" i], input[autocomplete="address-level2"], input[placeholder*="City" i]'},
            {name: 'zip', selectors: 'input[name*="zip" i], input[name*="postal" i], input[autocomplete="postal-code"], input[placeholder*="ZIP" i]'},
            {name: 'prefecture', selectors: 'select[name*="state" i], select[name*="prefecture" i], select[name*="region" i], input[name*="state" i], input[aria-label*="都道府県"], [role="combobox"][aria-label*="都道府県"]'},
        ];
        const empty = [];
        for (const {name, selectors} of checks) {
            const els = document.querySelectorAll(selectors);
            let found = false;
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    if ((el.value || '').trim()) {
                        found = true;
                    }
                    break;
                }
            }
            if (!found) empty.push(name);
        }
        return empty;
    }""")
    if empty_fields:
        log(f"[PayPal] ⚠️ 以下字段仍为空: {empty_fields}，尝试补填...")
        # 补填邮箱
        if "email" in empty_fields:
            try:
                await page.locator('input[name="email"], input[type="email"]').first.fill(email, timeout=3000)
            except Exception:
                pass
        # 补填手机号
        if "phone" in empty_fields:
            try:
                await page.locator('input[name*="phone" i], input[id*="phone" i]').first.fill(phone_local, timeout=3000)
            except Exception:
                pass
        # 补填 street
        if "street" in empty_fields:
            try:
                loc = page.locator('input[placeholder*="Street" i], input[name*="street" i], input[name*="address" i]:not([name*="email" i]), input[aria-label*="Street" i], input[id*="street" i], input[id*="address" i]:not([id*="email" i])').first
                if await loc.is_visible(timeout=2000):
                    await loc.fill(card.street, timeout=3000)
                else:
                    loc = page.get_by_label("Street address", exact=False).first
                    await loc.fill(card.street, timeout=3000)
            except Exception:
                # 终极兜底
                await page.evaluate("""(street) => {
                    const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'));
                    for (const inp of inputs) {
                        const rect = inp.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const label = (inp.placeholder || inp.getAttribute('aria-label') || inp.name || inp.id || '').toLowerCase();
                        if (/first|last|city|zip|postal|phone|email|apt|suite|bldg/i.test(label)) continue;
                        if ((inp.value || '').trim()) continue;
                        const proto = HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        desc?.set?.call(inp, street);
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return;
                    }
                }""", card.street)
        # 补填 city
        if "city" in empty_fields:
            try:
                loc = page.locator('input[placeholder*="City" i], input[name*="city" i]').first
                await loc.fill(card.city, timeout=3000)
            except Exception:
                pass
        if "prefecture" in empty_fields and desired_country == "JP":
            try:
                ok, why = await _fill_paypal_jp_prefecture()
                log(f"[PayPal][JP] 都道府県补填: ok={ok} reason={why}")
            except Exception:
                pass
        await page.wait_for_timeout(1000)

    async def _refill_card_fields(c: CardInfo) -> None:
        try:
            card_input = page.locator('input[name="cardnumber"], input[id*="card" i], input[placeholder*="Card" i]').first
            await card_input.fill("", timeout=2000)
            await card_input.fill(c.number, timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(300)
        try:
            exp_input = page.locator('input[name="exp-date"], input[id*="exp" i], input[placeholder*="Expir" i]').first
            await exp_input.fill("", timeout=2000)
            await exp_input.fill(f"{c.exp_month}/{c.exp_year}", timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(300)
        try:
            cvv_input = page.locator('input[name="cvv"], input[id*="cvv" i], input[placeholder*="CVV" i]').first
            await cvv_input.fill("", timeout=2000)
            await cvv_input.fill(c.cvv, timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(300)

    async def _is_card_rejected() -> bool:
        try:
            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=2500)
            except Exception:
                body_text = ""
            normalized = (body_text or "").replace("’", "'").lower()
            if any(
                k in normalized
                for k in (
                    "we weren't able to add this card",
                    "check all the details are correct",
                    "try a different card",
                    "unable to add this card",
                    "无法添加此卡",
                    "添加此卡失败",
                    "请尝试其他卡",
                )
            ):
                return True

            # 文本抓取失败或页面分段渲染时，补一层可见错误提示定位。
            err = page.locator(
                "text=We weren’t able to add this card, "
                "text=We weren't able to add this card, "
                "text=try a different card, "
                "text=无法添加此卡, "
                "text=请尝试其他卡"
            ).first
            if await err.is_visible(timeout=600):
                return True
            return False
        except Exception:
            return False

    # Agree & Create Account；如遇卡被拒，自动生成新卡并重试一次
    env = load_env(".env")
    max_regen = 1
    try:
        max_regen = max(0, int((env.get("PAYPAL_CARD_REGEN_ON_DECLINE") or "1").strip() or "1"))
    except Exception:
        max_regen = 1
    working_card = card
    for regen_idx in range(max_regen + 1):
        create_btn = page.locator('button:has-text("Agree"), button:has-text("Create Account"), button[type="submit"]').first
        try:
            await create_btn.click(timeout=10000)
        except Exception as exc:
            if _looks_like_captcha_pointer_block(exc):
                log("[PayPal] 创建账号按钮被 CAPTCHA 遮挡，处理后重试点击")
                await handle_paypal_captcha(page, solver_proxy=proxy, force=True)
                await _wait_captcha_cleared(page, timeout_seconds=30)
                await create_btn.click(timeout=10000)
            else:
                raise
        await page.wait_for_timeout(2500)

        # 检测并处理人机验证码（PayPal 安全问题 / CAPTCHA）
        await handle_paypal_captcha(page, solver_proxy=proxy)
        await page.wait_for_timeout(1200)

        rejected = False
        for _ in range(6):
            if await _is_card_rejected():
                rejected = True
                break
            await page.wait_for_timeout(800)
        if rejected:
            if regen_idx >= max_regen:
                raise RuntimeError("银行卡被拒（We weren’t able to add this card），且已达到自动换卡上限")
            new_card = _generate_local_random_card(
                int(time.time() * 1000) + regen_idx + random.randint(1, 9999),
                email,
                env,
                region_mode="jp" if str(country_code or "").upper() == "JP" else "default",
            )
            log(f"[PayPal] 检测到卡被拒，自动生成新卡重试 ({regen_idx + 1}/{max_regen})")
            working_card = new_card
            await _refill_card_fields(working_card)
            continue
        break


async def handle_paypal_captcha(page, timeout_seconds: int = 180, solver_proxy: str | None = None, force: bool = False) -> None:
    """检测 PayPal 人机验证码并处理。

    当前只保留人工处理逻辑；检测到 hosted checkout 的遮挡层时，先按
    GuJumpgate 的做法清理页面上的 captcha artifact，再等待人工完成。
    """
    await _remove_hosted_captcha_artifacts(page)
    # 检测是否有验证码弹窗（避免 v3 eval 误判）
    has_captcha, reason = await _detect_captcha_signal(page)
    if not has_captcha and not force:
        return
    if not has_captcha and force:
        has_any_frame = await _has_any_captcha_frame(page)
        if not has_any_frame:
            return
        reason = "force_by_timeout"
        has_captcha = True
    # 连续两次确认，避免“挑战未完全加载”就误触发打码
    await page.wait_for_timeout(1200)
    has_captcha_2, reason_2 = await _detect_captcha_signal(page)
    if not has_captcha_2:
        # force 模式下，如果 iframe 仍在，继续解码而不是直接跳过
        if force and await _has_any_captcha_frame(page):
            has_captcha_2 = True
            reason_2 = "force_frame_present"
        else:
            log(f"[PayPal] CAPTCHA 预检未稳定（首次={reason}, 二次={reason_2}），跳过本次自动解码")
            return

    log(f"[PayPal] ⚠️ 检测到人机验证码（CAPTCHA），需要处理... reason={reason_2 or reason}")

    removed = await _cleanup_hosted_captcha_artifacts(page, timeout_ms=15000)
    if removed:
        log(f"[PayPal] 已清理 hosted captcha 遮挡元素 {removed} 个，继续检测页面状态")
        await page.wait_for_timeout(800)
        if not await _detect_captcha(page):
            log("[PayPal] CAPTCHA 遮挡元素已移除，继续流程")
            return

    await _wait_captcha_manual(page, timeout_seconds)


async def _detect_captcha(page) -> bool:
    """检测页面是否出现了验证码弹窗。"""
    await _remove_hosted_captcha_artifacts(page)
    ok, _ = await _detect_captcha_signal(page)
    return ok


async def _remove_hosted_captcha_artifacts(page) -> int:
    """移除 PayPal hosted checkout 上会遮挡按钮的 captcha 容器。"""
    script = """() => {
        let removed = 0;
        const selectors = [
            '#captcha-standalone',
            '.captcha-overlay',
            '.captcha-container',
        ];
        for (const selector of selectors) {
            for (const node of Array.from(document.querySelectorAll(selector))) {
                try {
                    node.remove();
                    removed += 1;
                } catch {}
            }
        }
        return removed;
    }"""
    total = 0
    targets = [page]
    try:
        targets.extend([fr for fr in page.frames if fr is not page.main_frame])
    except Exception:
        pass
    for target in targets:
        try:
            total += int(await target.evaluate(script) or 0)
        except Exception:
            continue
    return total


async def _cleanup_hosted_captcha_artifacts(page, timeout_ms: int = 15000) -> int:
    """短时间持续清理新插入的 hosted captcha artifact。"""
    deadline = time.monotonic() + max(1.0, timeout_ms / 1000)
    total = 0
    while time.monotonic() < deadline:
        total += await _remove_hosted_captcha_artifacts(page)
        await page.wait_for_timeout(300)
        if not await _has_hosted_captcha_artifact(page):
            break
    return total


async def _has_hosted_captcha_artifact(page) -> bool:
    script = """() => !!document.querySelector('#captcha-standalone, .captcha-overlay, .captcha-container')"""
    targets = [page]
    try:
        targets.extend([fr for fr in page.frames if fr is not page.main_frame])
    except Exception:
        pass
    for target in targets:
        try:
            if await target.evaluate(script):
                return True
        except Exception:
            continue
    return False


async def _detect_captcha_signal(page) -> tuple[bool, str]:
    """返回 (是否应触发解码, 原因)。"""
    try:
        result = await page.evaluate("""() => {
            const isVisible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width < 20 || rect.height < 20) return false;
                const style = window.getComputedStyle(el);
                if (!style) return false;
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                if (Number(style.opacity || '1') < 0.05) return false;
                return true;
            };

            const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
            // PayPal 验证码特征
            const hasCaptchaText = /安全问题|security challenge|请选择包含|select all images|人行横道|crosswalk|traffic light|bus|bicycle|i'?m not a robot|robot check/i.test(text);

            const frames = Array.from(document.querySelectorAll('iframe'));
            const frameInfos = frames.map((f) => {
                const src = (f.getAttribute('src') || '').toLowerCase();
                const title = (f.getAttribute('title') || '').toLowerCase();
                const rect = f.getBoundingClientRect();
                return { src, title, w: rect.width, h: rect.height, visible: isVisible(f) };
            });

            const hasVisibleV2OrChallengeFrame = frameInfos.some((x) => {
                if (!x.visible) return false;
                const isV3Eval = /recaptcha_v3|source=recaptchav3eval/.test(x.src);
                if (isV3Eval) return false;
                return /api2\\/bframe|api2\\/anchor|recaptcha_v2|hcaptcha|challenge/.test(x.src + ' ' + x.title) && x.w >= 140 && x.h >= 60;
            });

            const hasVisiblePaypalChallenge = Array.from(
                document.querySelectorAll('[data-testid="captcha"], .captcha-container, #captcha, [class*="challenge" i], [class*="captcha" i]')
            ).some((el) => isVisible(el));

            const recaptchaFrames = frameInfos.filter((x) => /recaptcha/.test(x.src));
            const onlyV3EvalFrames =
                recaptchaFrames.length > 0 &&
                recaptchaFrames.every((x) => /recaptcha_v3|source=recaptchav3eval/.test(x.src));

            let reason = 'none';
            if (hasVisibleV2OrChallengeFrame) reason = 'visible_challenge_frame';
            else if (hasVisiblePaypalChallenge) reason = 'visible_paypal_challenge';
            else if (hasCaptchaText && !onlyV3EvalFrames) reason = 'captcha_text';
            else if (onlyV3EvalFrames) reason = 'v3_eval_only';

            const shouldSolve =
                hasVisibleV2OrChallengeFrame ||
                hasVisiblePaypalChallenge ||
                (hasCaptchaText && !onlyV3EvalFrames);

            return { shouldSolve, reason };
        }""")
        if isinstance(result, dict):
            return bool(result.get("shouldSolve")), str(result.get("reason") or "")
        return bool(result), ""
    except Exception:
        return False, "detect_error"


async def _has_any_captcha_frame(page) -> bool:
    """宽松检测：只要页面存在 captcha 相关 iframe 就返回 True（用于超时兜底）。"""
    try:
        return bool(await page.evaluate("""() => {
            return !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="captcha"], iframe[title*="challenge" i]');
        }"""))
    except Exception:
        return False


def _looks_like_captcha_pointer_block(exc: Exception | str) -> bool:
    text = str(exc or "").lower()
    return ("intercepts pointer events" in text) and (
        "recaptcha" in text or "hcaptcha" in text or "captcha" in text
    )


async def _fill_visible_tel_inputs_direct(target, code: str) -> bool:
    """不依赖 click，直接向可见 tel 输入框写入验证码。target 可是 page 或 frame。"""
    try:
        mode = await target.evaluate(
            """(code) => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (Number(st.opacity || '1') < 0.05) return false;
                    return true;
                };
                const setVal = (el, v) => {
                    const proto = HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && typeof desc.set === 'function') desc.set.call(el, v);
                    else el.value = v;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                };
                const inputs = Array.from(document.querySelectorAll('input[type="tel"]')).filter(isVisible);
                if (!inputs.length) return "";

                // 单输入框：整串写入
                if (inputs.length === 1) {
                    setVal(inputs[0], String(code));
                    inputs[0].focus();
                    return "single_direct";
                }
                // 多输入格：逐位写入
                const digits = String(code).replace(/\\D/g, "").slice(0, inputs.length).split("");
                if (!digits.length) return "";
                for (let i = 0; i < digits.length; i++) {
                    setVal(inputs[i], digits[i]);
                }
                inputs[Math.min(digits.length - 1, inputs.length - 1)].focus();
                return "multi_direct";
            }""",
            str(code),
        )
        return bool(mode)
    except Exception:
        return False


async def _fill_paypal_otp_inputs_direct(target, code: str) -> bool:
    """匹配 PayPal OTP 输入框并直填；支持 ciBasic 与几何分组兜底。"""
    try:
        mode = await target.evaluate(
            """(rawCode) => {
                const code = String(rawCode || '').replace(/\\D/g, '');
                if (!code) return '';
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (Number(st.opacity || '1') < 0.05) return false;
                    return true;
                };
                const setVal = (el, v) => {
                    const proto = HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && typeof desc.set === 'function') desc.set.call(el, v);
                    else el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: v }));
                };
                const idx = (el) => {
                    const id = String(el.id || '');
                    const name = String(el.name || '');
                    const m = id.match(/ciBasic-(\\d+)$/i) || name.match(/ciBasic-(\\d+)$/i);
                    return m ? Number(m[1]) : 9999;
                };
                const looksOtp = (el) => {
                    const id = String(el.id || '').toLowerCase();
                    const name = String(el.name || '').toLowerCase();
                    const cls = String(el.className || '').toLowerCase();
                    const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
                    const ac = String(el.getAttribute('autocomplete') || '').toLowerCase();
                    return id.includes('cibasic') || name.includes('cibasic') ||
                        cls.includes('code_input') || ac.includes('one-time-code') ||
                        /\\b1\\s*-\\s*6\\b/.test(aria) || aria.includes('code');
                };
                let inputs = Array.from(
                    document.querySelectorAll('input[type="tel"][id*="ciBasic" i], input[type="tel"][name*="ciBasic" i]')
                ).filter(isVisible);
                if (inputs.length < 6) {
                    const all = Array.from(document.querySelectorAll('input[type="tel"], input[inputmode="numeric"], input[autocomplete="one-time-code"]'))
                        .filter(isVisible)
                        .map((el) => ({ el, r: el.getBoundingClientRect(), otp: looksOtp(el) }))
                        .filter((x) => x.r.width >= 20 && x.r.width <= 120 && x.r.height >= 25 && x.r.height <= 90);

                    const groups = new Map();
                    for (const item of all) {
                        const key = String(Math.round(item.r.top / 12) * 12);
                        if (!groups.has(key)) groups.set(key, []);
                        groups.get(key).push(item);
                    }

                    let best = [];
                    for (const group of groups.values()) {
                        const sorted = group.sort((a, b) => a.r.left - b.r.left);
                        const otpScore = sorted.filter((x) => x.otp).length;
                        if (sorted.length >= 6 && (otpScore >= 2 || sorted.every((x) => x.r.width <= 80))) {
                            if (sorted.length > best.length || otpScore > best.filter((x) => x.otp).length) {
                                best = sorted;
                            }
                        }
                    }
                    inputs = best.slice(0, 6).map((x) => x.el);
                }
                inputs.sort((a, b) => idx(a) - idx(b));
                // 只在 OTP 输入框数量足够时启用，避免误命中少量非 OTP tel 框导致“部分填入”
                if (inputs.length < 6) return '';
                if (idx(inputs[0]) === 9999) {
                    inputs.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                }
                const digits = code.slice(0, 6).split('');
                if (digits.length < 6) return '';
                for (let i = 0; i < 6; i++) setVal(inputs[i], digits[i]);

                // 回读校验：必须 6 位都写入，才算成功
                const compact = inputs.slice(0, 6).map((el) => String(el.value || '')).join('');
                if (compact.length < 6) return '';
                return `paypal_otp_6`;
            }""",
            str(code),
        )
        return bool(mode)
    except Exception:
        return False


async def _wait_captcha_manual(page, timeout_seconds: int = 180) -> None:
    """方案1：暂停等待手动处理验证码。

    检测到验证码后每 3 秒检查一次是否已消失，最多等待 timeout_seconds 秒。
    用户手动完成验证码后脚本自动继续。
    """
    log(f"[PayPal] 🖐️ 请手动完成验证码！等待最多 {timeout_seconds} 秒...")
    log("[PayPal] 完成验证码后脚本会自动继续")

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_seconds:
        removed = await _remove_hosted_captcha_artifacts(page)
        if removed:
            log(f"[PayPal] 人工等待期间已清理 hosted captcha 遮挡元素 {removed} 个")
        await page.wait_for_timeout(3000)
        # 检查验证码是否已消失
        still_has = await _detect_captcha(page)
        if not still_has:
            log("[PayPal] ✓ 验证码已完成，继续流程")
            await page.wait_for_timeout(2000)
            return
        # 检查是否已经跳转到下一页（验证码通过后可能直接跳转）
        try:
            body_text = await page.evaluate("() => (document.body?.innerText || '').slice(0, 500)")
            if "code" in body_text.lower() or "验证码" in body_text or "verify" in body_text.lower():
                log("[PayPal] ✓ 页面已跳转到验证码/下一步页面，继续流程")
                return
        except Exception:
            # 页面可能在导航
            await page.wait_for_timeout(2000)
            return

    log("[PayPal] ⚠️ 验证码等待超时，继续尝试...")


async def _inject_recaptcha_token(page, token: str) -> None:
    """向页面注入 reCAPTCHA token，并尽可能触发回调。"""
    inject_js = """(token) => {
        const touch = (el) => {
            if (!el) return;
            el.value = token;
            el.innerHTML = token;
            try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch {}
            try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch {}
        };

        const ensureField = (selector, id, name) => {
            let el = document.querySelector(selector);
            if (!el) {
                el = document.createElement('textarea');
                if (id) el.id = id;
                if (name) el.name = name;
                el.style.display = 'none';
                document.body.appendChild(el);
            }
            touch(el);
            return el;
        };

        ensureField('#g-recaptcha-response', 'g-recaptcha-response', 'g-recaptcha-response');
        ensureField('textarea[name="g-recaptcha-response"]', '', 'g-recaptcha-response');
        ensureField('textarea[name="g-recaptcha-response-100000"]', '', 'g-recaptcha-response-100000');

        for (const el of Array.from(document.querySelectorAll('[name*="captcha-response" i], textarea[id*="captcha" i]'))) {
            touch(el);
        }

        for (const cbEl of Array.from(document.querySelectorAll('[data-callback]'))) {
            const cb = cbEl.getAttribute('data-callback');
            if (!cb) continue;
            const fn = window[cb];
            if (typeof fn === 'function') {
                try { fn(token); } catch {}
            }
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
                            try { v(token); } catch {}
                        } else if (v && typeof v === 'object') {
                            stack.push(v);
                        }
                    }
                }
            }
        }
    }"""

    await page.evaluate(inject_js, token)
    for frame in page.frames:
        try:
            u = (frame.url or "").lower()
            if "recaptcha" in u or "paypal.com" in u:
                await frame.evaluate(inject_js, token)
        except Exception:
            pass


async def _inject_hcaptcha_token(page, token: str) -> None:
    """向页面注入 hCaptcha token，并尽可能触发回调。"""
    inject_js = """(token) => {
        const touch = (el) => {
            if (!el) return;
            el.value = token;
            el.innerHTML = token;
            try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch {}
            try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch {}
        };

        const ensureField = (selector, name) => {
            let el = document.querySelector(selector);
            if (!el) {
                el = document.createElement('textarea');
                if (name) el.name = name;
                el.style.display = 'none';
                document.body.appendChild(el);
            }
            touch(el);
        };

        ensureField('textarea[name="h-captcha-response"]', 'h-captcha-response');
        ensureField('textarea[name="g-recaptcha-response"]', 'g-recaptcha-response');
        for (const el of Array.from(document.querySelectorAll('[name*="captcha-response" i], textarea[id*="captcha" i]'))) {
            touch(el);
        }
        for (const cbEl of Array.from(document.querySelectorAll('[data-callback]'))) {
            const cb = cbEl.getAttribute('data-callback');
            if (!cb) continue;
            const fn = window[cb];
            if (typeof fn === 'function') {
                try { fn(token); } catch {}
            }
        }
        if (window.hcaptcha && typeof window.hcaptcha.execute === 'function') {
            try { window.hcaptcha.execute(); } catch {}
        }
    }"""

    await page.evaluate(inject_js, token)
    for frame in page.frames:
        try:
            u = (frame.url or "").lower()
            if "hcaptcha" in u or "paypal.com" in u:
                await frame.evaluate(inject_js, token)
        except Exception:
            pass


async def _wait_captcha_cleared(page, timeout_seconds: int = 45) -> bool:
    """等待验证码真正消失，或页面进入下一步（短信/验证页）。"""
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_seconds:
        await _remove_hosted_captcha_artifacts(page)
        await page.wait_for_timeout(1500)
        if not await _detect_captcha(page):
            return True
        try:
            body = await page.locator("body").inner_text(timeout=1500)
            low = body.lower()
            if ("verification code" in low) or ("enter code" in low) or ("验证码" in body):
                return True
        except Exception:
            # 页面导航瞬间读取失败也视作可能前进，继续下一轮
            pass
    return False


async def _is_paypal_verification_stage(page) -> bool:
    """判断是否已进入 PayPal 短信验证码阶段（用于避免残留 iframe 误判）。"""
    try:
        body = await page.locator("body").inner_text(timeout=1500)
        low = body.lower()
        markers = (
            "verification code",
            "enter code",
            "security code",
            "one-time code",
            "text message",
            "sms code",
            "验证码",
            "認証コード",
            "コードを入力",
            "コードを入力する",
            "送信しました",
            "再送",
        )
        if any(m in low for m in markers):
            return True
    except Exception:
        pass

    try:
        if await page.locator('input[type="tel"]:visible').count() > 0:
            return True
    except Exception:
        pass

    try:
        for fr in page.frames:
            if fr is page.main_frame:
                continue
            try:
                if await fr.locator('input[type="tel"]:visible').count() > 0:
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _post_captcha_nudge(page) -> None:
    """token 注入后，尝试触发页面继续动作（部分站点需要 callback 后再点按钮）。"""
    selectors = [
        'button:has-text("Continue")',
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button:has-text("Next")',
        'button:has-text("下一步")',
        'button:has-text("继续")',
        'button[type="submit"]',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1200):
                await btn.click(timeout=2000)
                log(f"[PayPal] 验证码后触发按钮: {sel}")
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


async def _solve_captcha_via_api(page, env: dict[str, str], solver_proxy: str | None = None) -> None:
    """兼容旧调用：打码平台逻辑已移除，只保留人工处理。"""
    await _cleanup_hosted_captcha_artifacts(page, timeout_ms=15000)
    await _wait_captcha_manual(page)

async def fill_sms_code(
    page,
    api_url: str,
    solver_proxy: str | None = None,
    *,
    prefix: str = "[PayPal]",
) -> bool:
    """等待并填入 PayPal 手机验证码（复刻 source4 逻辑）。"""
    await page.wait_for_timeout(800)
    if not await _is_paypal_verification_stage(page):
        log(f"{prefix} 未检测到短信验证码页，跳过自动填码")
        return False

    # 二次风控常发生在短信页刚加载时，先做一次预处理。
    has_captcha_pre, reason_pre = await _detect_captcha_signal(page)
    if has_captcha_pre:
        log(f"[PayPal] 短信验证码页检测到 CAPTCHA，先处理... reason={reason_pre}")
        await handle_paypal_captcha(page, solver_proxy=solver_proxy)
        await _wait_captcha_cleared(page, timeout_seconds=30)

    code = poll_sms_code(api_url, timeout=120, interval=5)
    log(f"[PayPal] 验证码: {code}")

    # 优先使用直填，避免 click 被遮挡导致输入中断。
    typed = False
    if await _fill_paypal_otp_inputs_direct(page, code):
        typed = True

    if not typed:
        all_tel = page.locator('input[type="tel"]:visible')
        tel_count = await all_tel.count()
        for i in range(tel_count):
            val = await all_tel.nth(i).input_value()
            if not val:
                try:
                    await all_tel.nth(i).click(timeout=2000)
                except Exception as exc:
                    if _looks_like_captcha_pointer_block(exc):
                        log("[PayPal] 验证码输入框被 CAPTCHA 遮挡，处理后重试点击")
                        await handle_paypal_captcha(page, solver_proxy=solver_proxy, force=True)
                        await _wait_captcha_cleared(page, timeout_seconds=30)
                        await all_tel.nth(i).click(timeout=3000)
                    else:
                        raise
                await page.wait_for_timeout(300)
                for digit in code:
                    await page.keyboard.press(digit)
                    await page.wait_for_timeout(200)
                typed = True
                break
    if not typed:
        raise RuntimeError("短信验证码输入失败：未找到可填写的 OTP 输入框")

    # 快速路径：验证码写入后先短暂等待，尽快进入提交/确认点击
    await page.wait_for_timeout(500)
    # 输入后再次出现验证码时，先处理再点提交。
    has_captcha_post, reason_post = await _detect_captcha_signal(page)
    if has_captcha_post:
        log(f"[PayPal] 验证码输入后检测到 CAPTCHA，处理后再提交... reason={reason_post}")
        await handle_paypal_captcha(page, solver_proxy=solver_proxy)
        await _wait_captcha_cleared(page, timeout_seconds=30)

    try:
        btn = page.locator('button:has-text("Confirm"), button:has-text("Submit"), button:has-text("Verify"), button[type="submit"]').first
        if await btn.is_visible(timeout=3000):
            try:
                await btn.click()
            except Exception as exc:
                    if _looks_like_captcha_pointer_block(exc):
                        log("[PayPal] 提交按钮被 CAPTCHA 遮挡，处理后重试提交")
                        await handle_paypal_captcha(page, solver_proxy=solver_proxy, force=True)
                        await _wait_captcha_cleared(page, timeout_seconds=30)
                        await btn.click(timeout=3000)
                    else:
                        raise
    except Exception:
        pass

    # 提交后立刻尝试点击确认，缩短“收到验证码 -> 点击 Agree and Continue”耗时。
    await _click_paypal_agree_and_continue_if_present(page, solver_proxy=solver_proxy, prefix=prefix)
    await page.wait_for_timeout(900)
    still_verify = await _is_paypal_verification_stage(page)
    if still_verify:
        log(f"{prefix} 短信验证码阶段仍在，判定未完成提交")
        return False
    log(f"{prefix} 短信验证码已提交并通过")
    return True


async def check_phone_rejected(page) -> bool:
    """检查是否出现 'Try a different phone number' 弹窗。"""
    try:
        body = await page.locator("body").inner_text(timeout=2000)
        if "different phone" in body.lower() or "Try a different" in body:
            # 点 OK 关闭弹窗
            try:
                await page.locator('button:has-text("OK")').first.click(timeout=3000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False


async def _click_paypal_agree_and_continue_if_present(
    page,
    *,
    solver_proxy: str | None = None,
    prefix: str = "[PayPal]",
) -> bool:
    """若出现 PayPal review 页的 Agree and Continue，则自动点击。"""
    selectors = [
        'button:has-text("同意して続行")',
        'button:has-text("同意して続ける")',
        'button:has-text("続行")',
        'button:has-text("Agree and Continue")',
        'button:has-text("Agree & Continue")',
        'button:has-text("同意并继续")',
        'button:has-text("继续并同意")',
        'button:has-text("继续")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1200):
                # 页面常在底部，先滚动到按钮再点击
                try:
                    await btn.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
                try:
                    await btn.click(timeout=5000)
                except Exception as exc:
                    if _looks_like_captcha_pointer_block(exc):
                        log(f"{prefix} Agree and Continue 按钮被 CAPTCHA 遮挡，处理后重试")
                        await handle_paypal_captcha(page, solver_proxy=solver_proxy, force=True)
                        await _wait_captcha_cleared(page, timeout_seconds=30)
                        await btn.click(timeout=5000)
                    else:
                        raise
                log(f"{prefix} 已点击支付确认按钮: {sel}")
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    # JS 文本兜底：处理包裹在 span/div 的日语大蓝按钮
    try:
        js_clicked = await page.evaluate(
            """() => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 20 || r.height < 20) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    return st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity || '1') > 0.05;
                };
                const textOf = (el) => String(el?.innerText || el?.textContent || '').trim();
                const targets = Array.from(document.querySelectorAll('button, a, div[role="button"], span'))
                    .filter(isVisible);
                const hit = targets.find(el => /同意して続行|同意して続ける|Agree\\s*&?\\s*Continue/i.test(textOf(el)));
                if (!hit) return false;
                const btn = hit.closest('button, a, div[role="button"]') || hit;
                btn.scrollIntoView({block: 'center'});
                btn.click();
                return true;
            }"""
        )
        if js_clicked:
            log(f"{prefix} 已点击支付确认按钮: js_fallback_jp_agree")
            await page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False


async def pay_one(
    item: dict[str, str],
    card: CardInfo,
    phone_pool: PhonePool,
    cfg: dict[str, Any],
    worker_id: int = 1,
    max_phone_retries: int = 3,
    proxy: str | None = None,
    flow2_region_mode: str = "default",
) -> bool:
    """执行一次 PayPal 支付。"""
    email = item["email"]
    query_code = item["query_code"]
    payment_link = item["payment_link"]
    prefix = f"[paypal-pay-{worker_id:02d}][{email}]"
    paypal_password = generate_paypal_password(email)
    region_mode = _normalize_flow2_region_mode(flow2_region_mode)
    billing_country = _billing_country_code(region_mode)

    browser_cfg = cfg.get("browser", {})
    profile_dir = resolve_path("profiles") / f"paypal_pay_{safe_filename(email)}"

    if proxy:
        log(f"{prefix} 使用代理: {_display_proxy(proxy)}")

    session = BrowserSession(
        profile_dir=profile_dir,
        headless=bool(browser_cfg.get("headless", False)),
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
        fingerprint_seed=email,
    )

    phone: PhoneInfo | None = None
    try:
        flow_env = load_env(".env")
        working_card = card
        if region_mode == "jp":
            jp_billing = _generate_local_random_card(worker_id, email, flow_env, region_mode="jp")
            working_card = _with_billing_profile(card, jp_billing)
            log(
                f"{prefix} 日本代理模式: 使用日本账单地址 "
                f"{working_card.city}, {working_card.state}, {working_card.zip_code}"
            )

        await session.__aenter__()
        page = await session.current_page()

        # 打开长链接
        log(f"{prefix} 打开支付链接...")
        await page.goto(payment_link, wait_until="domcontentloaded")
        try:
            await page.locator(
                'input[autocomplete="cc-number"], iframe[name*="__privateStripeFrame"], text=支付方式'
            ).first.wait_for(timeout=2500)
        except Exception:
            pass
        await page.wait_for_timeout(1200)

        # Stripe
        log(f"{prefix} Stripe 填充...")
        await fill_stripe(page, email, working_card, country_code=billing_country)

        # PayPal - 可能需要换手机号重试
        for attempt in range(1, max_phone_retries + 1):
            phone = phone_pool.acquire(worker_id)
            if not phone:
                raise RuntimeError("手机号池已耗尽")
            log(f"{prefix} PayPal 注册 (手机: {phone.number}, 尝试 {attempt}/{max_phone_retries})...")

            await fill_paypal(
                page,
                email,
                working_card,
                phone,
                paypal_password,
                proxy=proxy,
                country_code=billing_country,
            )

            # 检查手机号是否被拒
            await page.wait_for_timeout(1200)
            # CAPTCHA 未通过时，不应误判手机号拒绝；先做一次有 reason 的复检与兜底处理
            has_captcha_after, reason_after = await _detect_captcha_signal(page)
            if has_captcha_after:
                log(f"{prefix} 检测到 CAPTCHA 残留（reason={reason_after}），尝试再处理一次...")
                await handle_paypal_captcha(page, solver_proxy=proxy)
                # 关键修复：不要仅凭 challenge iframe 残留直接判失败；
                # 若页面已推进到短信验证码阶段，也视为 CAPTCHA 已通过。
                cleared = await _wait_captcha_cleared(page, timeout_seconds=20)
                if not cleared:
                    has_captcha_after2, reason_after2 = await _detect_captcha_signal(page)
                    if has_captcha_after2:
                        # 兼容场景：CAPTCHA 已通过但 challenge iframe 短暂残留。
                        if reason_after2 == "visible_challenge_frame" and await _is_paypal_verification_stage(page):
                            log(f"{prefix} CAPTCHA 残留 iframe，但已进入短信验证码阶段，继续流程")
                        else:
                            raise RuntimeError(f"CAPTCHA 未通过（reason={reason_after2}），需人工处理或更换打码策略")
            if await check_phone_rejected(page):
                log(f"{prefix} 手机号 {phone.number} 被拒，换号重试...")
                phone_pool.mark_failed(phone.number)
                phone = None
                continue

            # 验证码
            log(f"{prefix} 等待验证码...")
            sms_done = await fill_sms_code(page, phone.api_url, solver_proxy=proxy, prefix=prefix)
            if not sms_done:
                if await _is_paypal_verification_stage(page):
                    raise RuntimeError("短信验证码未完成，停止后续支付等待")
                log(f"{prefix} 当前未处于短信验证码页，继续后续流程")
            await _click_paypal_agree_and_continue_if_present(page, solver_proxy=proxy, prefix=prefix)
            phone_pool.release(phone.number, success=True)
            break
        else:
            raise RuntimeError(f"手机号重试 {max_phone_retries} 次均被拒")

        # 等待回到 chatgpt.com
        log(f"{prefix} 等待支付完成...")
        for i in range(60):
            if "chatgpt.com" in page.url or "success" in page.url.lower():
                break
            if "paypal.com" in page.url and i % 2 == 0:
                await _click_paypal_agree_and_continue_if_present(page, solver_proxy=proxy, prefix=prefix)
            # review 页额外强制点击一次（该页常停留在“同意して続行”）
            if "paypal.com/webapps/hermes" in page.url and "billingweb/review" in page.url:
                await _click_paypal_agree_and_continue_if_present(page, solver_proxy=proxy, prefix=prefix)
            await page.wait_for_timeout(1000)

        final_url = page.url
        if "chatgpt.com" in final_url or "success" in final_url.lower():
            log(f"{prefix} ✅ 支付成功！")
            save_pending_auth(email, query_code)
            remove_from_link_pool(email)
            return True
        else:
            log(f"{prefix} ⚠️ 最终 URL: {final_url}")
            if "paypal.com/webapps/hermes" in final_url and "billingweb/review" in final_url:
                raise RuntimeError("仍停留在 PayPal review 页（同意并继续未完成）")
            raise RuntimeError("支付流程未返回成功页面")

    except Exception as exc:
        log(f"{prefix} ❌ 失败: {exc}")
        traceback.print_exc()
        if phone:
            phone_pool.release(phone.number, success=False)
        return False
    finally:
        await session.__aexit__(None, None, None)


async def run_paypal_pay(
    cfg: dict[str, Any],
    count: int = 1,
    workers: int = 1,
    card_source_mode: str | None = None,
    flow2_region_mode: str | None = None,
) -> int:
    """批量执行流程2。返回成功数。"""
    log(f"PayPal flow2 code version: {PAYPAL_FLOW2_CODE_VERSION} file={Path(__file__).resolve()}")
    env = load_env(".env")
    resolved_region_mode = _normalize_flow2_region_mode(flow2_region_mode)
    pool = load_link_pool()
    if not pool:
        log("PayPal 流程2：长链接池为空，请先运行流程1")
        return 0

    cards_file = env.get("PAYPAL_CARDS_FILE") or "data/paypal/cards.txt"
    phones_file = env.get("PAYPAL_PHONES_FILE") or "data/paypal/phones.txt"
    max_uses = int(env.get("PAYPAL_PHONE_MAX_USES") or 5)
    max_retries = int(env.get("PAYPAL_PHONE_RETRY_ON_REJECT") or 3)

    card_pool = CardPool(cards_file)
    phone_pool = PhonePool(phones_file, max_uses=max_uses)
    if card_source_mode:
        local_random_mode = card_source_mode.strip().lower() in {"local_random", "random_local", "local"}
    else:
        local_random_mode = is_local_random_card_mode(env)

    # 代理池（通过 PAYPAL_USE_PROXY 开关控制）
    from .proxy_pool import ProxyPool
    if resolved_region_mode == "jp":
        use_proxy = True
    else:
        use_proxy = (env.get("PAYPAL_USE_PROXY") or "").strip().lower() in ("true", "1", "yes")
    proxy_pool: ProxyPool | None = None
    if use_proxy:
        if resolved_region_mode == "jp":
            proxy_file = (
                env.get("PAYPAL_PROXY_FILE_JP")
                or env.get("PAYPAL_PROXY_FILE")
                or env.get("PROXY_FILE")
                or "data/proxies/proxies_jp.txt"
            )
        else:
            proxy_file = env.get("PAYPAL_PROXY_FILE") or env.get("PROXY_FILE") or "data/proxies/proxies.txt"
        proxy_pool = ProxyPool(proxy_file)
        if proxy_pool.count() <= 0:
            log(f"PayPal 流程2：PAYPAL_USE_PROXY 已开启但代理池为空: {proxy_file}")
            return 0
        if resolved_region_mode == "jp":
            log(f"PayPal 流程2：日本代理模式已启用，代理池={proxy_file}，代理数={proxy_pool.count()}")
        else:
            log(f"PayPal 流程2：代理已启用，代理数={proxy_pool.count()}")

    if not local_random_mode:
        desired_cards = min(count, len(pool), phone_pool.count())
        if desired_cards > 0 and card_pool.count() < desired_cards:
            from .paypal_card_redeem import ensure_card_supply

            ensure_card_supply(env, desired_cards, log_prefix="PayPal 流程2")

        if card_pool.count() <= 0:
            log("PayPal 流程2：卡池为空")
            return 0
    else:
        mode_label = card_source_mode or "PAYPAL_CARD_SOURCE=local_random"
        log(f"PayPal 流程2：已启用本地随机卡资料模式（{mode_label}）")
    if phone_pool.count() <= 0:
        log("PayPal 流程2：手机号池为空")
        return 0

    target = min(count, len(pool), phone_pool.count()) if local_random_mode else min(count, len(pool), card_pool.count())
    card_desc = "本地随机" if local_random_mode else str(card_pool.count())
    log(f"PayPal 流程2：长链接 {len(pool)} 个，卡 {card_desc} 张，手机号 {phone_pool.count()} 个，本次目标 {target}，并发 {workers}")

    success = 0
    sem = asyncio.Semaphore(workers)

    async def worker(index: int, item: dict[str, str]) -> None:
        nonlocal success
        async with sem:
            if local_random_mode:
                card = _generate_local_random_card(index, item["email"], env, region_mode=resolved_region_mode)
            else:
                card = card_pool.take_one()
                if not card:
                    log(f"[paypal-pay-{index:02d}] 卡池已空")
                    return
            proxy = proxy_pool.pick(index) if proxy_pool else None
            ok = await pay_one(
                item,
                card,
                phone_pool,
                cfg,
                worker_id=index,
                max_phone_retries=max_retries,
                proxy=proxy,
                flow2_region_mode=resolved_region_mode,
            )
            if ok and not local_random_mode:
                card_pool.remove(card)
            if ok:
                success += 1

    tasks = [asyncio.create_task(worker(i + 1, item)) for i, item in enumerate(pool[:target])]
    await asyncio.gather(*tasks)
    log(f"PayPal 流程2 完成：成功 {success}/{target}")
    return success


