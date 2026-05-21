from __future__ import annotations

from pathlib import Path


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _normalize_lines(content: str) -> list[str]:
    return [line.strip() for line in content.splitlines() if line.strip()]


def _join_lines(lines: list[str]) -> str:
    return "\n".join(lines) + ("\n" if lines else "")


def read_text(path: Path | str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return ""
    return file_path.read_text(encoding="utf-8-sig")


def save_text(path: Path | str, content: str) -> None:
    file_path = Path(path)
    _ensure_parent(file_path)
    file_path.write_text(content, encoding="utf-8")


def import_txt(path: Path | str, imported: str, append: bool = True) -> int:
    file_path = Path(path)
    new_lines = _normalize_lines(imported)
    if append:
        existing_lines = _normalize_lines(read_text(file_path))
        lines = existing_lines + new_lines
    else:
        lines = new_lines
    save_text(file_path, _join_lines(lines))
    return len(new_lines)


def export_txt(path: Path | str) -> str:
    return read_text(path)


def clear_file(path: Path | str) -> None:
    save_text(path, "")


def dedupe_lines(path: Path | str) -> int:
    file_path = Path(path)
    original = _normalize_lines(read_text(file_path))
    seen: set[str] = set()
    deduped: list[str] = []
    for line in original:
        if line in seen:
            continue
        seen.add(line)
        deduped.append(line)
    save_text(file_path, _join_lines(deduped))
    return len(original) - len(deduped)
