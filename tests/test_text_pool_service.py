from pathlib import Path

from control_panel.text_pool_service import (
    clear_file,
    dedupe_lines,
    export_txt,
    import_txt,
    read_text,
    save_text,
)


def test_read_missing_file_returns_empty_string(tmp_path: Path) -> None:
    assert read_text(tmp_path / "missing.txt") == ""


def test_save_and_export_text(tmp_path: Path) -> None:
    target = tmp_path / "pool.txt"

    save_text(target, "a\nb")

    assert read_text(target) == "a\nb"
    assert export_txt(target) == "a\nb"


def test_import_txt_appends_non_empty_lines(tmp_path: Path) -> None:
    target = tmp_path / "pool.txt"
    save_text(target, "a\n")

    added = import_txt(target, "\nb\nc\n", append=True)

    assert added == 2
    assert read_text(target) == "a\nb\nc\n"


def test_import_txt_replace_mode(tmp_path: Path) -> None:
    target = tmp_path / "pool.txt"
    save_text(target, "old\n")

    added = import_txt(target, "new\n\nnext\n", append=False)

    assert added == 2
    assert read_text(target) == "new\nnext\n"


def test_dedupe_lines_preserves_order(tmp_path: Path) -> None:
    target = tmp_path / "pool.txt"
    save_text(target, "a\nb\na\n\nc\nb\n")

    removed = dedupe_lines(target)

    assert removed == 2
    assert read_text(target) == "a\nb\nc\n"


def test_clear_file_creates_empty_file(tmp_path: Path) -> None:
    target = tmp_path / "pool.txt"
    save_text(target, "a\n")

    clear_file(target)

    assert target.exists()
    assert read_text(target) == ""
