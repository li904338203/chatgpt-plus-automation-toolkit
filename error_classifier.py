from __future__ import annotations


SUCCESS = "success"
UNKNOWN = "unknown"


def classify_error(text: str) -> str:
    value = " ".join(str(text or "").lower().split())
    if not value:
        return UNKNOWN
    if "no_valid_organizations" in value:
        return "no_valid_organizations"
    if "invalid_state" in value or "楠岃瘉杩囩▼涓嚭閿?" in value:
        return "invalid_state"
    if "cloudflare" in value or "risk" in value or "verify you are human" in value:
        return "cloudflare_or_risk"
    if "wrong password" in value or "incorrect password" in value or "瀵嗙爜閿欒" in value:
        return "wrong_password"
    if "楠岃瘉鐮佹棤鏁?" in value or "楠岃瘉鐮侀敊璇?" in value or "invalid code" in value or "wrong_email_otp_code" in value:
        return "otp_invalid"
    if "鏈壘鍒伴獙璇佺爜" in value or "鏈敹鍒伴獙璇佺爜" in value or "绛夊緟楠岃瘉鐮?" in value or "otp timeout" in value:
        return "otp_timeout"
    if "閭" in value and ("澶辫触" in value or "寮傚父" in value or "鏈懡涓?" in value):
        return "mail_adapter_failed"
    if "oauth consent" in value or "oauth_consent_callback_missing" in value:
        return "oauth_consent_callback_missing"
    if "鏈崟鑾峰埌 oauth authorization code" in value or "oauth_callback_missing" in value:
        return "oauth_callback_missing"
    if "auth_phone_link_limit" in value or "phone_link_limit" in value or "最大账户" in value:
        return "auth_phone_link_limit"
    if "token 浜ゆ崲澶辫触" in value or "exchange token" in value:
        return "token_exchange_failed"
    if "鏈嶅姟鍣ㄤ笂浼?" in value or "server upload" in value:
        return "server_upload_failed"
    if "宸插啓鍏?" in value and "澶辫触" in value:
        return "local_write_failed"
    if "locator" in value or "selector" in value or "椤甸潰缁撴瀯" in value or "element" in value:
        return "page_changed"
    return UNKNOWN


def classify_exit(code: int, text: str = "") -> str:
    if int(code or 0) == 0:
        return SUCCESS
    return classify_error(text)
