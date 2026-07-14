"""Tests for the SQLite persistence layer."""

import sqlite3

from app import config, storage


class TestStores:
    def test_add_and_get(self):
        assert storage.add_store(1, "D357", "Hauptstraße 241, 44649 Herne") is True
        rows = storage.get_stores(1)
        assert [(r["store_id"], r["store_name"]) for r in rows] == [
            ("D357", "Hauptstraße 241, 44649 Herne")
        ]

    def test_add_duplicate(self):
        storage.add_store(1, "D357", "Herne")
        assert storage.add_store(1, "D357", "Herne") is False
        assert storage.count_stores(1) == 1

    def test_multiple_stores(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_store(1, "D123", "Bochum")
        assert storage.count_stores(1) == 2
        assert storage.has_store(1, "D123") is True
        assert storage.has_store(1, "D999") is False

    def test_get_unknown_chat(self):
        assert storage.get_stores(999) == []

    def test_remove(self):
        storage.add_store(1, "D357", "Herne")
        assert storage.remove_store(1, "D357") is True
        assert storage.remove_store(1, "D357") is False
        assert storage.get_stores(1) == []

    def test_add_store_creates_empty_status_rows(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.update_status(1, 100, "D357", True, 5)
        storage.add_store(1, "D123", "Bochum")
        by_store = {(r["store_id"], r["dan"]): r for r in storage.list_statuses(1)}
        assert by_store[("D357", 100)]["last_available"] == 1  # untouched
        assert by_store[("D123", 100)]["last_available"] is None  # seeds silently

    def test_remove_store_drops_its_status(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_store(1, "D123", "Bochum")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.update_status(1, 100, "D123", True, 5)
        storage.remove_store(1, "D123")
        assert {r["store_id"] for r in storage.list_statuses(1)} == {"D357"}


class TestSubscriptions:
    def test_add_and_list(self):
        storage.add_store(1, "D357", "Herne")
        assert storage.add_subscription(1, 100, "Zahnpasta") is True
        assert storage.add_subscription(1, 100, "Zahnpasta") is False  # duplicate
        rows = storage.list_subscriptions(1)
        assert len(rows) == 1
        assert rows[0]["dan"] == 100

    def test_add_creates_status_row_per_store(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_store(1, "D123", "Bochum")
        storage.add_subscription(1, 100, "Zahnpasta")
        rows = storage.list_statuses(1)
        assert {r["store_id"] for r in rows} == {"D357", "D123"}
        assert all(r["last_available"] is None for r in rows)

    def test_remove(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        assert storage.remove_subscription(1, 100) is True
        assert storage.remove_subscription(1, 100) is False
        assert storage.list_subscriptions(1) == []
        assert storage.list_statuses(1) == []

    def test_has_and_count(self):
        assert storage.count_subscriptions(1) == 0
        assert storage.has_subscription(1, 100) is False
        storage.add_subscription(1, 100, "A")
        storage.add_subscription(1, 200, "B")
        assert storage.count_subscriptions(1) == 2
        assert storage.has_subscription(1, 100) is True
        assert storage.has_subscription(1, 999) is False

    def test_delete_chat(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "A")
        storage.delete_chat(1)
        assert storage.get_stores(1) == []
        assert storage.list_subscriptions(1) == []
        assert storage.list_statuses(1) == []

    def test_update_status(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.update_status(1, 100, "D357", True, 7)
        row = storage.list_statuses(1)[0]
        assert row["last_available"] == 1
        assert row["last_stock"] == 7
        assert row["updated_at"] is not None

    def test_update_status_cas(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")  # last_available starts NULL

        # Matching expected (NULL) writes.
        assert storage.update_status_cas(1, 100, "D357", True, 3, expected=None) is True
        assert storage.list_statuses(1)[0]["last_available"] == 1

        # Stale expected (still None, but row is now 1) is skipped.
        assert storage.update_status_cas(1, 100, "D357", False, 0, expected=None) is False
        assert storage.list_statuses(1)[0]["last_available"] == 1

        # Correct expected swaps.
        assert storage.update_status_cas(1, 100, "D357", False, 0, expected=1) is True
        assert storage.list_statuses(1)[0]["last_available"] == 0

    def test_update_status_cas_other_store_untouched(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_store(1, "D123", "Bochum")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.update_status_cas(1, 100, "D357", True, 3, expected=None)
        by_store = {r["store_id"]: r for r in storage.list_statuses(1)}
        assert by_store["D357"]["last_available"] == 1
        assert by_store["D123"]["last_available"] is None

    def test_grouped_by_store(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_store(2, "D357", "Herne")
        storage.add_store(3, "D999", "Bochum")
        storage.add_subscription(1, 100, "A")
        storage.add_subscription(2, 100, "A")
        storage.add_subscription(2, 200, "B")
        storage.add_subscription(3, 300, "C")
        storage.add_subscription(4, 400, "D")  # chat without a store: excluded

        grouped = storage.subscriptions_by_store()
        assert set(grouped) == {"D357", "D999"}
        assert len(grouped["D357"]) == 3
        assert {r["dan"] for r in grouped["D999"]} == {300}
        assert grouped["D357"][0]["store_name"] == "Herne"

    def test_grouped_multi_store_chat_appears_per_store(self):
        storage.add_store(1, "D357", "Herne")
        storage.add_store(1, "D999", "Bochum")
        storage.add_subscription(1, 100, "A")
        storage.update_status(1, 100, "D999", True, 2)

        grouped = storage.subscriptions_by_store()
        assert set(grouped) == {"D357", "D999"}
        assert grouped["D357"][0]["last_available"] is None
        assert grouped["D999"][0]["last_available"] == 1


class TestMigration:
    def test_single_store_layout_is_migrated(self, monkeypatch, tmp_path):
        monkeypatch.setattr("app.config.DB_PATH", str(tmp_path / "legacy.db"))
        with sqlite3.connect(config.DB_PATH) as conn:
            conn.executescript(
                """
                CREATE TABLE chats (
                    chat_id INTEGER PRIMARY KEY,
                    store_id TEXT NOT NULL,
                    store_name TEXT NOT NULL
                );
                CREATE TABLE subscriptions (
                    chat_id INTEGER NOT NULL,
                    dan INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    last_available INTEGER,
                    last_stock INTEGER,
                    updated_at TEXT,
                    PRIMARY KEY (chat_id, dan)
                );
                INSERT INTO chats VALUES (1, 'D357', 'Herne');
                INSERT INTO subscriptions VALUES
                    (1, 100, 'Zahnpasta', 1, 3, '2026-01-01T00:00:00+00:00'),
                    (1, 200, 'Windeln', NULL, NULL, NULL);
                """
            )

        storage.init_db()

        assert [(r["store_id"], r["store_name"]) for r in storage.get_stores(1)] == [("D357", "Herne")]
        assert storage.has_subscription(1, 100) and storage.has_subscription(1, 200)
        by_dan = {r["dan"]: r for r in storage.list_statuses(1)}
        # State carried over: no spurious "back in stock" message after the update.
        assert by_dan[100]["last_available"] == 1
        assert by_dan[100]["last_stock"] == 3
        assert by_dan[200]["last_available"] is None

        storage.init_db()  # idempotent: the legacy tables are gone
        assert storage.count_subscriptions(1) == 2

    def test_fresh_db_untouched(self):
        storage.add_store(1, "D357", "Herne")
        storage.init_db()  # must not raise or alter anything
        assert storage.has_store(1, "D357")
