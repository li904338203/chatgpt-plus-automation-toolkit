from pathlib import Path

from control_panel.env_service import get_known_env_fields, read_env, update_env


def test_read_env_parses_key_value_pairs(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("# comment\nA=1\nB = two\nEMPTY=\n", encoding="utf-8")

    values = read_env(target)

    assert values == {"A": "1", "B": "two", "EMPTY": ""}


def test_read_missing_env_returns_empty_dict(tmp_path: Path) -> None:
    assert read_env(tmp_path / ".env") == {}


def test_update_env_preserves_comments_and_unknown_keys(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("# keep\nA=1\nUNKNOWN=value\n", encoding="utf-8")

    update_env(target, {"A": "changed"})

    assert target.read_text(encoding="utf-8") == "# keep\nA=changed\nUNKNOWN=value\n"


def test_update_env_adds_missing_keys_at_end(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text("A=1\n", encoding="utf-8")

    update_env(target, {"NEW_KEY": "new-value"})

    assert target.read_text(encoding="utf-8") == "A=1\nNEW_KEY=new-value\n"


def test_known_env_fields_include_api_keys() -> None:
    fields = get_known_env_fields()

    assert "HERO_SMS_API_KEY" in fields
    assert "PAYPAL_CARD_REDEEM_API_KEY" in fields
    assert "PAYPAL_USE_PROXY" in fields
