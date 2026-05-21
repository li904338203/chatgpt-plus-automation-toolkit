from pathlib import Path

from modules.paypal_card_pool import CardPool, parse_card_line


def test_parse_card_line_ignores_bom_prefixed_comment_header() -> None:
    line = "\ufeff# 格式：KW-ID----卡号----有效期----CVV----手机号----持卡人----地址----API地址"

    assert parse_card_line(line) is None


def test_parse_card_line_rejects_header_like_invalid_card_number() -> None:
    line = "KW-ID----卡号----有效期----CVV----手机号----持卡人----地址----API地址"

    assert parse_card_line(line) is None


def test_card_pool_does_not_count_bom_header_as_card(tmp_path: Path) -> None:
    cards_file = tmp_path / "cards.txt"
    cards_file.write_text(
        "\ufeff# 格式：KW-ID----卡号----有效期----CVV----手机号----持卡人----地址----API地址\n",
        encoding="utf-8",
    )

    assert CardPool(cards_file).count() == 0
