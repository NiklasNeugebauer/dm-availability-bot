"""Tests for transition logic and the periodic check job."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import storage
from app.bot import check_all_subscriptions, parse_dan, stale_note, status_line, transition
from app.dm_api import Availability


class TestStaleNote:
    def test_none_and_empty(self):
        assert stale_note(None) == ""
        assert stale_note("") == ""
        assert stale_note("not-a-date") == ""

    def test_recent_is_blank(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        assert stale_note(now) == ""

    def test_old_shows_days(self):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        note = stale_note(old)
        assert "vor 3 Tag" in note


class TestParseDan:
    def test_valid(self):
        assert parse_dan("123456") == 123456

    def test_non_numeric(self):
        assert parse_dan("abc") is None
        assert parse_dan("") is None
        assert parse_dan("12a") is None

    def test_unicode_digit_rejected(self):
        # "²" passes str.isdigit() but int() would raise — must be rejected
        assert parse_dan("²") is None

    def test_zero_and_negative_like(self):
        assert parse_dan("0") is None
        assert parse_dan("-5") is None  # "-" is not a decimal

    def test_overflow_guard(self):
        assert parse_dan("9" * 30) is None


class TestTransition:
    def test_first_check_never_notifies(self):
        assert transition(None, True) is None
        assert transition(None, False) is None

    def test_no_change(self):
        assert transition(1, True) is None
        assert transition(0, False) is None

    def test_became_available(self):
        assert transition(0, True) == "available"

    def test_became_unavailable(self):
        assert transition(1, False) == "unavailable"


class TestStatusLine:
    def test_in_stock(self):
        line = status_line("Zahnpasta", Availability(1, True, 11, True))
        assert "✅" in line and "11x" in line

    def test_out_of_stock(self):
        assert "❌" in status_line("Zahnpasta", Availability(1, False, None, True))

    def test_unknown(self):
        assert "❓" in status_line("Zahnpasta", None)

    def test_online_suffix(self):
        assert "online verfügbar" in status_line("Z", Availability(1, False, None, True))
        assert "online verfügbar" not in status_line("Z", Availability(1, False, None, False))


class FakeApi:
    def __init__(self, availability: dict[int, Availability]):
        self.availability = availability
        self.calls: list[tuple[str, list[int]]] = []

    async def get_availability(self, store_id, dans):
        self.calls.append((store_id, list(dans)))
        return {dan: self.availability[dan] for dan in dans if dan in self.availability}


class FakeBot:
    def __init__(self):
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


def make_context(api: FakeApi) -> SimpleNamespace:
    bot = FakeBot()
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"dm_api": api}),
        bot=bot,
    )
    return context


class TestSubscriptionCap:
    async def test_new_subscription_blocked_beyond_limit(self):
        from app.bot import _subscribe
        from app.config import MAX_SUBSCRIPTIONS_PER_CHAT

        storage.set_store(1, "D357", "Herne")
        for dan in range(1000, 1000 + MAX_SUBSCRIPTIONS_PER_CHAT):
            storage.add_subscription(1, dan, f"P{dan}")

        sent = []

        async def reply(text):
            sent.append(text)

        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        ctx = make_context(FakeApi({}))
        ctx.application.bot_data["titles"] = {999999: "X"}  # cached name -> no API lookup
        await _subscribe(update, ctx, 1, 999999)

        assert storage.count_subscriptions(1) == MAX_SUBSCRIPTIONS_PER_CHAT
        assert storage.has_subscription(1, 999999) is False
        assert any("höchstens" in t for t in sent)


class TestCheckAllSubscriptions:
    async def test_notifies_on_change_only(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.add_subscription(1, 200, "Windeln")
        storage.update_status(1, 100, False, 0)  # previously out of stock
        storage.update_status(1, 200, True, 5)  # previously in stock, unchanged

        api = FakeApi({
            100: Availability(100, True, 11, True),
            200: Availability(200, True, 4, True),
        })
        context = make_context(api)
        await check_all_subscriptions(context)

        assert len(context.bot.messages) == 1
        chat_id, text = context.bot.messages[0]
        assert chat_id == 1
        assert "Zahnpasta" in text and "wieder verfügbar" in text and "11x" in text

        # State was persisted
        rows = {r["dan"]: r for r in storage.list_subscriptions(1)}
        assert rows[100]["last_available"] == 1
        assert rows[200]["last_stock"] == 4

    async def test_notifies_on_becoming_unavailable(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.update_status(1, 100, True, 3)

        context = make_context(FakeApi({100: Availability(100, False, None, True)}))
        await check_all_subscriptions(context)

        assert len(context.bot.messages) == 1
        assert "nicht mehr verfügbar" in context.bot.messages[0][1]

    async def test_first_check_seeds_silently(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")

        context = make_context(FakeApi({100: Availability(100, True, 2, True)}))
        await check_all_subscriptions(context)

        assert context.bot.messages == []
        assert storage.list_subscriptions(1)[0]["last_available"] == 1

    async def test_one_request_per_store(self):
        storage.set_store(1, "D357", "Herne")
        storage.set_store(2, "D357", "Herne")
        storage.add_subscription(1, 100, "A")
        storage.add_subscription(2, 100, "A")
        storage.add_subscription(2, 200, "B")

        api = FakeApi({
            100: Availability(100, True, 1, True),
            200: Availability(200, True, 1, True),
        })
        await check_all_subscriptions(make_context(api))

        assert api.calls == [("D357", [100, 200])]

    async def test_api_failure_does_not_crash(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "A")
        storage.update_status(1, 100, False, 0)

        class BrokenApi:
            async def get_availability(self, store_id, dans):
                raise RuntimeError("dm is down")

        context = make_context(BrokenApi())
        await check_all_subscriptions(context)  # must not raise

        assert context.bot.messages == []
        # State unchanged
        assert storage.list_subscriptions(1)[0]["last_available"] == 0

    async def test_schema_drift_canary_warns(self, caplog):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")

        # API responds but with no usable store availability for any DAN.
        context = make_context(FakeApi({100: Availability(100, None, None, True)}))
        with caplog.at_level(logging.WARNING, logger="app.bot"):
            await check_all_subscriptions(context)

        assert any("schema may have changed" in r.message for r in caplog.records)

    async def test_missing_store_data_keeps_state(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "A")
        storage.update_status(1, 100, True, 5)

        # API responds, but without store info for this DAN
        context = make_context(FakeApi({100: Availability(100, None, None, True)}))
        await check_all_subscriptions(context)

        assert context.bot.messages == []
        assert storage.list_subscriptions(1)[0]["last_available"] == 1

    async def test_send_failure_does_not_advance_state(self):
        from telegram.error import TelegramError

        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.update_status(1, 100, False, 0)  # was out of stock -> would notify "available"

        class FloodBot:
            async def send_message(self, chat_id, text):
                raise TelegramError("flood control exceeded")

        context = make_context(FakeApi({100: Availability(100, True, 3, True)}))
        context.bot = FloodBot()
        await check_all_subscriptions(context)

        # State must NOT advance, so the transition is retried next cycle.
        row = storage.list_subscriptions(1)[0]
        assert row["last_available"] == 0
        assert row["last_stock"] == 0

    async def test_blocked_chat_is_pruned(self):
        from telegram.error import Forbidden

        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        storage.add_subscription(1, 200, "Windeln")
        storage.update_status(1, 100, False, 0)  # both were out of stock -> would notify
        storage.update_status(1, 200, False, 0)

        class BlockingBot:
            def __init__(self):
                self.attempts = 0

            async def send_message(self, chat_id, text):
                self.attempts += 1
                raise Forbidden("bot was blocked by the user")

        context = make_context(FakeApi({
            100: Availability(100, True, 3, True),
            200: Availability(200, True, 1, True),
        }))
        context.bot = BlockingBot()
        await check_all_subscriptions(context)

        # First Forbidden prunes the chat; the second row is skipped (one attempt only).
        assert context.bot.attempts == 1
        assert storage.get_store(1) is None
        assert storage.list_subscriptions(1) == []
