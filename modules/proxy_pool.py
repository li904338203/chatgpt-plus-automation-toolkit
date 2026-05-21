from __future__ import annotations

from pathlib import Path

from .utils import resolve_path


class ProxyPool:
    def __init__(self, proxy_file: str | Path):
        self.proxy_file = resolve_path(proxy_file)
        self.proxy_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.proxy_file.exists():
            self.proxy_file.write_text("", encoding="utf-8")
        self.proxies = self._load()

    def _load(self) -> list[str]:
        values = []
        for raw_line in self.proxy_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            # Tolerate UTF-8 BOM / zero-width chars at line start.
            line = line.lstrip("\ufeff\u200b\u2060")
            if not line or line.startswith("#"):
                continue
            values.append(line)
        return values

    def pick(self, worker_id: int) -> str | None:
        if not self.proxies:
            return None
        return self.proxies[(worker_id - 1) % len(self.proxies)]

    def count(self) -> int:
        return len(self.proxies)
