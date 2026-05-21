from pathlib import Path

import recaptcha_solver


ROOT = Path(__file__).resolve().parents[1]


def test_recaptcha_solver_exposes_hcaptcha_solver() -> None:
    assert callable(recaptcha_solver.solve_hcaptcha)


def test_paypal_captchaai_no_longer_recaptcha_only() -> None:
    source = (ROOT / "modules" / "paypal_pay.py").read_text(encoding="utf-8")

    assert "CaptchaAI 当前仅自动处理 reCAPTCHA" not in source
    assert 'provider not in {"recaptcha", "hcaptcha"}' in source


def test_paypal_captchaai_uses_parent_pageurl_for_hcaptcha() -> None:
    source = (ROOT / "modules" / "paypal_pay.py").read_text(encoding="utf-8")

    assert 'if provider == "hcaptcha":\n        page_url = page.url' in source
