"""SQLite persistence for chats (chosen store) and product subscriptions."""

import contextlib
import os
import sqlite3
from datetime import datetime, timezone

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    store_id TEXT NOT NULL,
    store_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id INTEGER NOT NULL,
    dan INTEGER NOT NULL,
    name TEXT NOT NULL,
    last_available INTEGER,
    last_stock INTEGER,
    updated_at TEXT,
    PRIMARY KEY (chat_id, dan)
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with contextlib.closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)


def set_store(chat_id: int, store_id: str, store_name: str):
    """Set the chat's store and reset known availability states (they are per-store)."""
    with contextlib.closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO chats (chat_id, store_id, store_name) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET store_id=excluded.store_id, store_name=excluded.store_name",
            (chat_id, store_id, store_name),
        )
        conn.execute(
            "UPDATE subscriptions SET last_available=NULL, last_stock=NULL WHERE chat_id=?",
            (chat_id,),
        )


def get_store(chat_id: int) -> tuple[str, str] | None:
    with contextlib.closing(_connect()) as conn:
        row = conn.execute("SELECT store_id, store_name FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    return (row["store_id"], row["store_name"]) if row else None


def add_subscription(chat_id: int, dan: int, name: str) -> bool:
    """Returns False if the subscription already existed."""
    with contextlib.closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, dan, name) VALUES (?, ?, ?)",
            (chat_id, dan, name),
        )
        return cursor.rowcount > 0


def has_subscription(chat_id: int, dan: int) -> bool:
    with contextlib.closing(_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM subscriptions WHERE chat_id=? AND dan=?", (chat_id, dan)
        ).fetchone()
    return row is not None


def count_subscriptions(chat_id: int) -> int:
    with contextlib.closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM subscriptions WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["n"]


def delete_chat(chat_id: int):
    """Remove a chat and all its subscriptions (e.g. after the user blocks the bot)."""
    with contextlib.closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))


def remove_subscription(chat_id: int, dan: int) -> bool:
    with contextlib.closing(_connect()) as conn, conn:
        cursor = conn.execute("DELETE FROM subscriptions WHERE chat_id=? AND dan=?", (chat_id, dan))
        return cursor.rowcount > 0


def list_subscriptions(chat_id: int) -> list[sqlite3.Row]:
    with contextlib.closing(_connect()) as conn:
        return conn.execute(
            "SELECT dan, name, last_available, last_stock, updated_at "
            "FROM subscriptions WHERE chat_id=? ORDER BY name",
            (chat_id,),
        ).fetchall()


def subscriptions_by_store() -> dict[str, list[sqlite3.Row]]:
    """All subscriptions grouped by the subscribing chat's store."""
    with contextlib.closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT c.store_id, c.store_name, s.chat_id, s.dan, s.name, s.last_available "
            "FROM subscriptions s JOIN chats c ON c.chat_id = s.chat_id"
        ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["store_id"], []).append(row)
    return grouped


def update_status(chat_id: int, dan: int, available: bool | None, stock: int | None):
    with contextlib.closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE subscriptions SET last_available=?, last_stock=?, updated_at=? "
            "WHERE chat_id=? AND dan=?",
            (
                None if available is None else int(available),
                stock,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                chat_id,
                dan,
            ),
        )


def update_status_cas(
    chat_id: int, dan: int, available: bool | None, stock: int | None, expected: int | None
) -> bool:
    """Compare-and-swap: write only if last_available still equals ``expected``.

    Guards against a stale-snapshot write: the periodic check reads a row, then
    awaits dm for seconds; if the user switches store (resetting to NULL) or runs
    /check in between, the row changed underneath and this write is skipped — the
    row is simply reseeded next cycle. ``IS`` gives NULL-safe comparison in SQLite.
    Returns True if a row was updated.
    """
    with contextlib.closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "UPDATE subscriptions SET last_available=?, last_stock=?, updated_at=? "
            "WHERE chat_id=? AND dan=? AND last_available IS ?",
            (
                None if available is None else int(available),
                stock,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                chat_id,
                dan,
                expected,
            ),
        )
        return cursor.rowcount > 0
