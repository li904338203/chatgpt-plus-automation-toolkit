from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .utils import log


MOEMAIL_ALLOWED_DOMAINS = [
    "intereloucer.com",
    "interestedloucer.com",
    "interestloucered.com",
]


@dataclass(frozen=True)
class CreatedMailAccount:
    email: str
    mail_line: str
    inbox_id: str
    domain: str


def split_domains(value: str | None) -> list[str]:
    raw = (value or "").replace(";", ",")
    domains = [item.strip().lower().lstrip("@") for item in raw.split(",") if item.strip()]
    clean = [item for item in domains if item in MOEMAIL_ALLOWED_DOMAINS]
    return list(dict.fromkeys(clean)) or MOEMAIL_ALLOWED_DOMAINS.copy()


def moemail_api_enabled(cfg: dict, env: dict[str, str]) -> bool:
    mode = (env.get("MAIL_ACCOUNT_MODE") or cfg.get("mail", {}).get("account_mode") or "").strip().lower()
    if mode in {"api", "auto", "moemail_api"}:
        return True
    if mode in {"pool", "file", "txt"}:
        return False
    active_source = (cfg.get("mail", {}).get("active_source") or cfg.get("mail", {}).get("source") or "").strip().lower()
    return active_source == "moemail" and bool(
        (env.get("MOEMAIL_BASE_URL") or "").strip() and (env.get("MOEMAIL_API_KEY") or "").strip()
    )


async def create_moemail_accounts(
    *,
    base_url: str,
    api_key: str,
    count: int,
    domains: list[str],
    prefix: str = "openai",
    mode: str = "human",
    batch_name: str = "",
    expiry_time_ms: int = 0,
) -> list[CreatedMailAccount]:
    if count <= 0:
        return []
    base_url = base_url.rstrip("/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    created: list[CreatedMailAccount] = []
    errors: list[str] = []
    domain_index = 0

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        while len(created) < count:
            domain = domains[domain_index % len(domains)]
            domain_index += 1
            need = min(50, count - len(created))
            payload = {
                "count": need,
                "domain": domain,
                "prefix": prefix,
                "mode": mode,
                "batch_name": batch_name or f"gopay-{int(time.time())}",
                "tags": ["gopay", "register"],
                "expiry_time_ms": int(expiry_time_ms),
            }
            try:
                response = await client.post(f"{base_url}/api/v1/otp/create", headers=headers, json=payload)
                data = response.json() if response.text else {}
                if response.status_code >= 400 or not data.get("success"):
                    message = ((data.get("error") or {}).get("message") if isinstance(data, dict) else "") or response.text[:160]
                    errors.append(f"{domain}: HTTP {response.status_code} {message}")
                    if domain_index >= len(domains) and not created:
                        raise RuntimeError("MoeMail 创建邮箱失败: " + " | ".join(errors))
                    if domain_index >= len(domains) * 2 and not created:
                        raise RuntimeError("MoeMail 创建邮箱失败: " + " | ".join(errors))
                    continue
                items = (data.get("data") or {}).get("emails") or []
                ids = [str(item.get("id") or "") for item in items if item.get("id")]
                export_content = await export_moemail_lines(client, base_url, headers, ids)
                lines = [line.strip() for line in export_content.splitlines() if line.strip()]
                for item, line in zip(items, lines):
                    email = str(item.get("email") or item.get("address") or "").strip()
                    inbox_id = str(item.get("id") or "").strip()
                    if email and line:
                        created.append(CreatedMailAccount(email=email, mail_line=line, inbox_id=inbox_id, domain=domain))
                    if len(created) >= count:
                        break
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{domain}: {exc}")
                if domain_index >= len(domains) * 2 and not created:
                    raise RuntimeError("MoeMail 创建邮箱失败: " + " | ".join(errors)) from exc
            if domain_index >= len(domains) * 4 and not created:
                raise RuntimeError("MoeMail 创建邮箱失败: " + " | ".join(errors))

    if len(created) < count:
        log(f"MoeMail 只创建到 {len(created)}/{count} 个邮箱；错误: {' | '.join(errors[-3:])}")
    return created[:count]


async def export_moemail_lines(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    ids: list[str],
) -> str:
    response = await client.post(
        f"{base_url}/api/v1/otp/export",
        headers=headers,
        json={"ids": ids, "locale": "zh-CN"},
    )
    data: dict[str, Any] = response.json() if response.text else {}
    if response.status_code >= 400 or not data.get("success"):
        message = ((data.get("error") or {}).get("message") if isinstance(data, dict) else "") or response.text[:160]
        raise RuntimeError(f"MoeMail 导出接码地址失败: HTTP {response.status_code} {message}")
    return str((data.get("data") or {}).get("content") or "")
