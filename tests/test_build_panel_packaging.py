from pathlib import Path


def test_build_panel_copies_oauth_authorization_script() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = project_root / "build_panel.ps1"

    text = script.read_text(encoding="utf-8")

    assert "get_oauth_rt.py" in text
