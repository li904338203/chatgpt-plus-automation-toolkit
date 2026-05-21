from __future__ import annotations


SUCCESS = "success"
UNKNOWN = "unknown"


def classify_error(text: str) -> str:
    value = " ".join(str(text or "").lower().split())
    if not value:
        return UNKNOWN
    if "no_valid_organizations" in value:
        return "no_valid_organizations"
    if "invalid_state" in value or "验证过程中出错" in value:
        return "invalid_state"
    if "cloudflare" in value or "risk" in value or "verify you are human" in value:
        return "cloudflare_or_risk"
    if "wrong password" in value or "incorrect password" in value or "密码错误" in value:
        return "wrong_password"
    if "验证码无效" in value or "验证码错误" in value or "invalid code" in value or "wrong_email_otp_code" in value:
        return "otp_invalid"
    if "未找到验证码" in value or "未收到验证码" in value or "等待验证码" in value or "otp timeout" in value:
        return "otp_timeout"
    if "邮箱" in value and ("失败" in value or "异常" in value or "未命中" in value):
        return "mail_adapter_failed"
    if "oauth consent" in value or "oauth_consent_callback_missing" in value:
        return "oauth_consent_callback_missing"
    if "未捕获到 oauth authorization code" in value or "oauth_callback_missing" in value:
        return "oauth_callback_missing"
    if "token 交换失败" in value or "exchange token" in value:
        return "token_exchange_failed"
    if "服务器上传" in value or "server upload" in value:
        return "server_upload_failed"
    if "已写入" in value and "失败" in value:
        return "local_write_failed"
    if "locator" in value or "selector" in value or "页面结构" in value or "element" in value:
        return "page_changed"
    return UNKNOWN


def classify_exit(code: int, text: str = "") -> str:
    if int(code or 0) == 0:
        return SUCCESS
    return classify_error(text)
