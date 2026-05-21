from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    account_type TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    error_type TEXT NOT NULL DEFAULT '',
    output_json TEXT NOT NULL DEFAULT '',
    rt_saved INTEGER NOT NULL DEFAULT 0,
    store_saved INTEGER NOT NULL DEFAULT 0,
    server_uploaded INTEGER NOT NULL DEFAULT 0,
    server_skipped INTEGER NOT NULL DEFAULT 0,
    removed_from_input INTEGER NOT NULL DEFAULT 0,
    invalid_state_count INTEGER NOT NULL DEFAULT 0,
    headless INTEGER NOT NULL DEFAULT 0,
    current_stage TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT NOT NULL DEFAULT '',
    UNIQUE(email, account_type, source_path)
);
CREATE INDEX IF NOT EXISTS idx_auth_tasks_status ON auth_tasks(status);
CREATE INDEX IF NOT EXISTS idx_auth_tasks_updated_at ON auth_tasks(updated_at);
"""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    ensure_columns(conn)
    return conn


def ensure_columns(conn: sqlite3.Connection) -> None:
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(auth_tasks)").fetchall()}
    if "current_stage" not in columns:
        conn.execute("ALTER TABLE auth_tasks ADD COLUMN current_stage TEXT NOT NULL DEFAULT ''")
    if "invalid_state_count" not in columns:
        conn.execute("ALTER TABLE auth_tasks ADD COLUMN invalid_state_count INTEGER NOT NULL DEFAULT 0")


@contextmanager
def open_db(db_path: str | Path):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str | Path) -> None:
    with open_db(db_path):
        return


def start_task(db_path: str | Path, *, email: str, account_type: str, source_type: str, source_path: str, headless: bool) -> None:
    now = utc_now()
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT attempt_count, created_at FROM auth_tasks WHERE email = ? AND account_type = ? AND source_path = ?",
            (email, account_type, source_path or ""),
        ).fetchone()
        attempt_count = int(row["attempt_count"] or 0) + 1 if row else 1
        created_at = row["created_at"] if row else now
        conn.execute(
            """
            INSERT INTO auth_tasks (
                email, account_type, source_type, source_path, status, attempt_count,
                last_error, error_type, headless, created_at, updated_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, 'running', ?, '', '', ?, ?, ?, ?, '')
            ON CONFLICT(email, account_type, source_path) DO UPDATE SET
                source_type = excluded.source_type,
                status = 'running',
                attempt_count = excluded.attempt_count,
                last_error = '',
                error_type = '',
                output_json = '',
                current_stage = '',
                rt_saved = 0,
                store_saved = 0,
                server_uploaded = 0,
                server_skipped = 0,
                removed_from_input = 0,
                invalid_state_count = invalid_state_count,
                headless = excluded.headless,
                updated_at = excluded.updated_at,
                started_at = excluded.started_at,
                finished_at = '',
                next_retry_at = ''
            """,
            (email, account_type, source_type or "", source_path or "", attempt_count, int(bool(headless)), created_at, now, now),
        )


def finish_task(
    db_path: str | Path,
    *,
    email: str,
    account_type: str,
    source_path: str,
    status: str,
    error_type: str = "",
    last_error: str = "",
    output_json: str = "",
    rt_saved: bool = False,
    store_saved: bool = False,
    server_uploaded: bool = False,
    server_skipped: bool = False,
    removed_from_input: bool = False,
    next_retry_at: str = "",
) -> None:
    now = utc_now()
    with open_db(db_path) as conn:
        conn.execute(
            """
            UPDATE auth_tasks SET
                status = ?,
                error_type = ?,
                last_error = ?,
                output_json = ?,
                current_stage = '',
                rt_saved = ?,
                store_saved = ?,
                server_uploaded = ?,
                server_skipped = ?,
                removed_from_input = ?,
                invalid_state_count = invalid_state_count + CASE WHEN ? IN ('invalid_state', 'no_valid_organizations') THEN 1 ELSE 0 END,
                updated_at = ?,
                finished_at = ?,
                next_retry_at = ?
            WHERE email = ? AND account_type = ? AND source_path = ?
            """,
            (
                status,
                error_type or "",
                last_error or "",
                output_json or "",
                int(bool(rt_saved)),
                int(bool(store_saved)),
                int(bool(server_uploaded)),
                int(bool(server_skipped)),
                int(bool(removed_from_input)),
                error_type or "",
                now,
                now,
                next_retry_at or "",
                email,
                account_type,
                source_path or "",
            ),
        )


def update_stage(db_path: str | Path, *, email: str, account_type: str, source_path: str, stage: str) -> None:
    now = utc_now()
    with open_db(db_path) as conn:
        conn.execute(
            """
            UPDATE auth_tasks SET current_stage = ?, updated_at = ?
            WHERE email = ? AND account_type = ? AND source_path = ? AND status = 'running'
            """,
            (stage or "", now, email, account_type, source_path or ""),
        )


def ensure_task(db_path: str | Path, *, email: str, account_type: str, source_type: str = "", source_path: str = "", headless: bool = False) -> None:
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM auth_tasks WHERE email = ? AND account_type = ? AND source_path = ?",
            (email, account_type, source_path or ""),
        ).fetchone()
    if not row:
        start_task(
            db_path,
            email=email,
            account_type=account_type,
            source_type=source_type,
            source_path=source_path,
            headless=headless,
        )


def mark_stale_running_failed(db_path: str | Path, *, older_than_seconds: int = 600) -> int:
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max(1, int(older_than_seconds))))
    now = utc_now()
    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE auth_tasks SET
                status = 'failed',
                error_type = 'account_timeout',
                last_error = '历史 running 任务超时，已自动标记失败',
                updated_at = ?,
                finished_at = ?
            WHERE status = 'running' AND started_at < ?
            """,
            (now, now, cutoff),
        )
        return int(cur.rowcount or 0)


def count_by_status(db_path: str | Path) -> dict[str, int]:
    with open_db(db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM auth_tasks GROUP BY status").fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def latest_tasks(db_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM auth_tasks ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, int(limit or 20)),),
        ).fetchall()
    return [dict(row) for row in rows]
