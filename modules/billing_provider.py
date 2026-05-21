from __future__ import annotations

from dataclasses import dataclass
import asyncio
import re

import httpx


MEIGUODIZHI_API = "https://www.meiguodizhi.com/api/v1/dz"


@dataclass(frozen=True)
class BillingAddress:
    name: str
    country: str
    address_line1: str
    city: str
    state: str
    state_full: str
    postal_code: str
    phone: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "country": self.country,
            "address_line1": self.address_line1,
            "city": self.city,
            "state": self.state,
            "state_full": self.state_full,
            "postal_code": self.postal_code,
            "phone": self.phone,
        }


async def fetch_meiguodizhi_us_address(city: str = "", retries: int = 3) -> BillingAddress:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await _fetch_meiguodizhi_us_address_once(city)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                await asyncio.sleep(0.8 * attempt)
    raise RuntimeError(f"拉取美国地址失败，已重试 {retries} 次: {last_error}")


async def _fetch_meiguodizhi_us_address_once(city: str = "") -> BillingAddress:
    payload = {"city": city, "path": "/", "method": "refresh"}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.meiguodizhi.com/",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        response = await client.post(MEIGUODIZHI_API, json=payload)
    if response.status_code >= 400:
        raise RuntimeError(f"拉取美国地址失败: HTTP {response.status_code} {response.text[:200]}")
    data = response.json()
    if data.get("status") != "ok" or not isinstance(data.get("address"), dict):
        raise RuntimeError(f"拉取美国地址响应异常: {data}")
    item = data["address"]
    address = BillingAddress(
        name=clean(item.get("Full_Name")) or clean(item.get("Username")) or "John Smith",
        country="US",
        address_line1=clean(item.get("Address")),
        city=clean(item.get("City")),
        state=clean(item.get("State")),
        state_full=clean(item.get("State_Full")),
        postal_code=clean(item.get("Zip_Code")),
        phone=clean(item.get("Telephone")),
    )
    missing = [key for key, value in address.as_dict().items() if key not in {"phone"} and not value]
    if missing:
        raise RuntimeError(f"拉取美国地址缺少字段: {missing} | raw={item}")
    return address


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
