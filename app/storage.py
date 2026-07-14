"""SQLite persistence: stores per chat, product subscriptions, per-store state."""

import contextlib
import os
import sqlite3
from datetime import datetime, timezone

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_stores (
    chat_id INTEGER NOT NULL,
    store_id TEXT NOT NULL,
    store_name TEXT NOT NULL,
    PRIMARY KEY (chat_id, store_id)
);
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id INTEGER NOT NULL,
    dan INTEGER NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (chat_id, dan)
);
CREATE TABLE IF NOT EXISTS subscription_status (
    chat_id INTEGER NOT NULL,
    dan INTEGER NOT NULL,
    store_id TEXT NOT NULL,
    last_available INTEGER,
    last_stock INTEGER,
    updated_at TEXT,
    PRIMARY KEY (chat_id, dan, store_id)
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with contextlib.closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        with conn:
            _migrate_legacy(conn)


def _migrate_legacy(conn: sqlite3.Connection):
    """One-time migration from the single-store layout.

    The old layout kept one store per chat in ``chats`` and the availability
    state directly on ``subscriptions``. The presence of ``chats`` is the
    marker for an unmigrated database; the table is dropped afterwards, so
    this can never run twice. Existing state carries over to the chat's one
    store, so nobody gets spurious "back in stock" messages after the update.
    """
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chats'"
    ).fetchone()
    if legacy is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO chat_stores (chat_id, store_id, store_name) "
        "SELECT chat_id, store_id, store_name FROM chats"
    )
    conn.execute(
        "INSERT OR IGNORE INTO subscription_status "
        "(chat_id, dan, store_id, last_available, last_stock, updated_at) "
        "SELECT s.chat_id, s.dan, c.store_id, s.last_available, s.last_stock, s.updated_at "
        "FROM subscriptions s JOIN chats c ON c.chat_id = s.chat_id"
    )
    # The old subscriptions table carried the state columns; rebuild it slim.
    conn.execute(
        "CREATE TABLE subscriptions_new ("
        "chat_id INTEGER NOT NULL, dan INTEGER NOT NULL, name TEXT NOT NULL, "
        "PRIMARY KEY (chat_id, dan))"
    )
    conn.execute("INSERT INTO subscriptions_new SELECT chat_id, dan, name FROM subscriptions")
    conn.execute("DROP TABLE subscriptions")
    conn.execute("ALTER TABLE subscriptions_new RENAME TO subscriptions")
    conn.execute("DROP TABLE chats")


def add_store(chat_id: int, store_id: str, store_name: str) -> bool:
    """Add a store to the chat. Returns False if it was already configured.

    Empty status rows are created for the chat's existing subscriptions, so
    the first check at the new store seeds silently instead of notifying.
    """
    with contextlib.closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO chat_stores (chat_id, store_id, store_name) VALUES (?, ?, ?)",
            (chat_id, store_id, store_name),
        )
        if cursor.rowcount == 0:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO subscription_status (chat_id, dan, store_id) "
            "SELECT chat_id, dan, ? FROM subscriptions WHERE chat_id=?",
            (store_id, chat_id),
        )
        return True


def remove_store(chat_id: int, store_id: str) -> bool:
    with contextlib.closing(_connect()) as conn, conn:
        conn.execute(
            "DELETE FROM subscription_status WHERE chat_id=? AND store_id=?",
            (chat_id, store_id),
        )
        cursor = conn.execute(
            "DELETE FROM chat_stores WHERE chat_id=? AND store_id=?", (chat_id, store_id)
        )
        return cursor.rowcount > 0


def get_stores(chat_id: int) -> list[sqlite3.Row]:
    with contextlib.closing(_connect()) as conn:
        return conn.execute(
            "SELECT store_id, store_name FROM chat_stores WHERE chat_id=? ORDER BY store_name",
            (chat_id,),
        ).fetchall()


def has_store(chat_id: int, store_id: str) -> bool:
    with contextlib.closing(_connect()) as conn:
        row = conn.execute(
            "SELECT 1 FROM chat_stores WHERE chat_id=? AND store_id=?", (chat_id, store_id)
        ).fetchone()
    return row is not None


def count_stores(chat_id: int) -> int:
    with contextlib.closing(_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_stores WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["n"]


def add_subscription(chat_id: int, dan: int, name: str) -> bool:
    """Returns False if the subscription already existed.

    Empty status rows are created for each of the chat's stores; they are
    filled by the initial check or seeded silently by the next poll.
    """
    with contextlib.closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, dan, name) VALUES (?, ?, ?)",
            (chat_id, dan, name),
        )
        if cursor.rowcount == 0:
            return False
        conn.execute(
            "INSERT OR IGNORE INTO subscription_status (chat_id, dan, store_id) "
            "SELECT ?, ?, store_id FROM chat_stores WHERE chat_id=?",
            (chat_id, dan, chat_id),
        )
        return True


def subscription_name(chat_id: int, dan: int) -> str | None:
    with contextlib.closing(_connect()) as conn:
        row = conn.execute(
            "SELECT name FROM subscriptions WHERE chat_id=? AND dan=?", (chat_id, dan)
        ).fetchone()
    return row["name"] if row else None


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
    """Remove a chat and all its data (e.g. after the user blocks the bot)."""
    with contextlib.closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM subscription_status WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))
        conn.execute("DELETE FROM chat_stores WHERE chat_id=?", (chat_id,))


def remove_subscription(chat_id: int, dan: int) -> bool:
    with contextlib.closing(_connect()) as conn, conn:
        conn.execute(
            "DELETE FROM subscription_status WHERE chat_id=? AND dan=?", (chat_id, dan)
        )
        cursor = conn.execute(
            "DELETE FROM subscriptions WHERE chat_id=? AND dan=?", (chat_id, dan)
        )
        return cursor.rowcount > 0


def list_subscriptions(chat_id: int) -> list[sqlite3.Row]:
    with contextlib.closing(_connect()) as conn:
        return conn.execute(
            "SELECT dan, name FROM subscriptions WHERE chat_id=? ORDER BY name",
            (chat_id,),
        ).fetchall()


def list_statuses(chat_id: int) -> list[sqlite3.Row]:
    """Every (store, subscription) pair of the chat with its last known state."""
    with contextlib.closing(_connect()) as conn:
        return conn.execute(
            "SELECT cs.store_id, cs.store_name, s.dan, s.name, "
            "       st.last_available, st.last_stock, st.updated_at "
            "FROM subscriptions s "
            "JOIN chat_stores cs ON cs.chat_id = s.chat_id "
            "LEFT JOIN subscription_status st "
            "  ON st.chat_id = s.chat_id AND st.dan = s.dan AND st.store_id = cs.store_id "
            "WHERE s.chat_id=? ORDER BY cs.store_name, s.name",
            (chat_id,),
        ).fetchall()


def subscriptions_by_store() -> dict[str, list[sqlite3.Row]]:
    """All (chat, store, subscription) rows grouped by store.

    A chat with several stores contributes one row per store, so one
    availability request per store still covers every subscriber.
    """
    with contextlib.closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT cs.store_id, cs.store_name, s.chat_id, s.dan, s.name, st.last_available "
            "FROM subscriptions s "
            "JOIN chat_stores cs ON cs.chat_id = s.chat_id "
            "LEFT JOIN subscription_status st "
            "  ON st.chat_id = s.chat_id AND st.dan = s.dan AND st.store_id = cs.store_id"
        ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["store_id"], []).append(row)
    return grouped


def update_status(chat_id: int, dan: int, store_id: str, available: bool | None, stock: int | None):
    with contextlib.closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE subscription_status SET last_available=?, last_stock=?, updated_at=? "
            "WHERE chat_id=? AND dan=? AND store_id=?",
            (
                None if available is None else int(available),
                stock,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                chat_id,
                dan,
                store_id,
            ),
        )


def update_status_cas(
    chat_id: int,
    dan: int,
    store_id: str,
    available: bool | None,
    stock: int | None,
    expected: int | None,
) -> bool:
    """Compare-and-swap: write only if last_available still equals ``expected``.

    Guards against a stale-snapshot write: the periodic check reads a row, then
    awaits dm for seconds; if the user removes/re-adds the store (resetting the
    row) or runs /check in between, the row changed underneath and this write
    is skipped — the row is simply reseeded next cycle. ``IS`` gives NULL-safe
    comparison in SQLite. Returns True if a row was updated.
    """
    with contextlib.closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "UPDATE subscription_status SET last_available=?, last_stock=?, updated_at=? "
            "WHERE chat_id=? AND dan=? AND store_id=? AND last_available IS ?",
            (
                None if available is None else int(available),
                stock,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                chat_id,
                dan,
                store_id,
                expected,
            ),
        )
        return cursor.rowcount > 0
