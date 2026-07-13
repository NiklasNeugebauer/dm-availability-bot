"""Tests for the SQLite persistence layer."""

from app import storage


class TestStore:
    def test_set_and_get(self):
        storage.set_store(1, "D357", "Hauptstraße 241, 44649 Herne")
        assert storage.get_store(1) == ("D357", "Hauptstraße 241, 44649 Herne")

    def test_get_unknown_chat(self):
        assert storage.get_store(999) is None

    def test_change_store_resets_states(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.update_status(1, 100, True, 5)
        storage.set_store(1, "D123", "Bochum")
        row = storage.list_subscriptions(1)[0]
        assert row["last_available"] is None
        assert row["last_stock"] is None


class TestSubscriptions:
    def test_add_and_list(self):
        storage.set_store(1, "D357", "Herne")
        assert storage.add_subscription(1, 100, "Zahnpasta") is True
        assert storage.add_subscription(1, 100, "Zahnpasta") is False  # duplicate
        rows = storage.list_subscriptions(1)
        assert len(rows) == 1
        assert rows[0]["dan"] == 100
        assert rows[0]["last_available"] is None

    def test_remove(self):
        storage.add_subscription(1, 100, "Zahnpasta")
        assert storage.remove_subscription(1, 100) is True
        assert storage.remove_subscription(1, 100) is False
        assert storage.list_subscriptions(1) == []

    def test_has_and_count(self):
        assert storage.count_subscriptions(1) == 0
        assert storage.has_subscription(1, 100) is False
        storage.add_subscription(1, 100, "A")
        storage.add_subscription(1, 200, "B")
        assert storage.count_subscriptions(1) == 2
        assert storage.has_subscription(1, 100) is True
        assert storage.has_subscription(1, 999) is False

    def test_delete_chat(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "A")
        storage.delete_chat(1)
        assert storage.get_store(1) is None
        assert storage.list_subscriptions(1) == []

    def test_update_status(self):
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.update_status(1, 100, True, 7)
        row = storage.list_subscriptions(1)[0]
        assert row["last_available"] == 1
        assert row["last_stock"] == 7
        assert row["updated_at"] is not None

    def test_grouped_by_store(self):
        storage.set_store(1, "D357", "Herne")
        storage.set_store(2, "D357", "Herne")
        storage.set_store(3, "D999", "Bochum")
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
