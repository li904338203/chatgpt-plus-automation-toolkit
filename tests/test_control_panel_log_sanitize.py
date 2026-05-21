from control_panel_app import strip_ansi_for_display


def test_strip_ansi_for_display_removes_color_codes() -> None:
    raw = "\x1b[91m\x1b[1m[FAIL]\x1b[0m bad\n"

    assert strip_ansi_for_display(raw) == "[FAIL] bad\n"
