from __future__ import annotations

from typing import Any

from playwright.async_api import Page


async def get_chatgpt_session(page: Page) -> dict[str, Any]:
    data = await page.evaluate(
        """async () => {
            const r = await fetch('/api/auth/session', { credentials: 'include' });
            return await r.json();
        }"""
    )
    if not isinstance(data, dict):
        raise RuntimeError("无法获取 ChatGPT session，当前页面可能未登录 ChatGPT")
    return data


async def get_access_token(page: Page) -> str:
    data = await get_chatgpt_session(page)
    token = data.get("accessToken")
    if not token:
        raise RuntimeError("无法获取 accessToken，当前页面可能未登录 ChatGPT")
    return str(token)


async def create_plus_checkout_link(page: Page, access_token: str, cfg: dict[str, Any]) -> str:
    payload = {
        "plan_name": cfg["plan_name"],
        "billing_details": {
            "country": cfg["billing_country"],
            "currency": cfg["currency"],
        },
        "cancel_url": cfg["cancel_url"],
        "promo_campaign": {
            "promo_campaign_id": cfg["promo_campaign_id"],
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": cfg["checkout_ui_mode"],
    }
    data = await page.evaluate(
        """async ({ accessToken, payload }) => {
            const r = await fetch('https://chatgpt.com/backend-api/payments/checkout', {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${accessToken}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });
            const text = await r.text();
            let data = null;
            try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
            return { ok: r.ok, status: r.status, data };
        }""",
        {"accessToken": access_token, "payload": payload},
    )
    if not data.get("ok"):
        raise RuntimeError(f"生成长链接失败: HTTP {data.get('status')} {data.get('data')}")
    body = data.get("data") or {}
    link = body.get("url") or body.get("stripe_hosted_url") or body.get("checkout_url")
    if not link:
        raise RuntimeError(f"生成长链接响应里没有 url: {body}")
    return link
