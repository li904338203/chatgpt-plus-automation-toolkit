"""PayPal 手机号池管理。

线程安全，支持：
- 每个号最多使用 max_uses 次
- 同一时刻每个号只分配给一个 worker（避免验证码混乱）
- 手机号被 PayPal 拒绝时标记为不可用
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from .utils import resolve_path, load_env


@dataclass
class PhoneInfo:
    number: str
    api_url: str


@dataclass
class PhoneState:
    phone: PhoneInfo
    use_count: int = 0
    failed: bool = False
    in_use_by: int | None = None  # worker_id or None


class PhonePool:
    def __init__(self, phones_file: str | Path = "data/paypal/phones.txt", max_uses: int = 5):
        self.phones_file = resolve_path(phones_file)
        self.max_uses = max_uses
        self._lock = threading.Lock()
        self._states: list[PhoneState] = []
        self._load()

    def _load(self) -> None:
        if not self.phones_file.exists():
            return
        for line in self.phones_file.read_text(encoding="utf-8").splitlines():
            line = line.strip().lstrip("\ufeff\u200b\u2060")
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                phone = PhoneInfo(number=parts[0].strip(), api_url=parts[1].strip())
                self._states.append(PhoneState(phone=phone))

    def count(self) -> int:
        with self._lock:
            return len([s for s in self._states if not s.failed and s.use_count < self.max_uses])

    def acquire(self, worker_id: int) -> PhoneInfo | None:
        """分配一个当前没被占用、未用满、未失败的手机号给 worker。"""
        with self._lock:
            for state in self._states:
                if state.failed:
                    continue
                if state.use_count >= self.max_uses:
                    continue
                if state.in_use_by is not None:
                    continue
                state.in_use_by = worker_id
                return state.phone
        return None

    def release(self, phone_number: str, success: bool) -> None:
        """worker 用完后释放。success=True 则 use_count+1。"""
        with self._lock:
            for state in self._states:
                if state.phone.number == phone_number:
                    state.in_use_by = None
                    if success:
                        state.use_count += 1
                    return

    def mark_failed(self, phone_number: str) -> None:
        """手机号被 PayPal 拒绝，标记为不可用。"""
        with self._lock:
            for state in self._states:
                if state.phone.number == phone_number:
                    state.in_use_by = None
                    state.failed = True
                    return

    def status(self) -> list[dict]:
        """返回所有手机号状态（用于调试/显示）。"""
        with self._lock:
            return [
                {
                    "number": s.phone.number,
                    "uses": s.use_count,
                    "max": self.max_uses,
                    "failed": s.failed,
                    "in_use": s.in_use_by is not None,
                }
                for s in self._states
            ]
