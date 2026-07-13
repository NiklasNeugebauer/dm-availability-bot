"""Tests for the Telegram-facing handlers and the allowlist gate."""

from types import SimpleNamespace

import httpx
import pytest
from telegram.ext import ApplicationHandlerStop

from app import bot, storage
from app.dm_api import Product, Store


class Recorder:
    """Captures reply/edit text and markup from a handler."""

    def __init__(self):
        self.texts: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, reply_markup=None):
        self.texts.append(text)
        self.markups.append(reply_markup)

    edit_message_text = reply_text

    async def answer(self):
        pass


class HandlerApi:
    def __init__(self, *, products=None, stores=None, store=None, availability=None, geo=None):
        self._products = products or []
        self._stores = stores or []
        self._store = store
        self._availability = availability or {}
        self._geo = geo

    async def search_products(self, query, page_size=8):
        return self._products

    async def find_stores(self, lat, lon, radius):
        return self._stores

    async def get_store(self, store_id):
        return self._store

    async def get_availability(self, store_id, dans):
        return self._availability

    async def geocode(self, query):
        return self._geo


def make_ctx(api, args=None):
    return SimpleNamespace(args=args or [], application=SimpleNamespace(bot_data={"dm_api": api}))


def make_message_update(chat_id=1):
    rec = Recorder()
    msg = SimpleNamespace(reply_text=rec.reply_text, location=None)
    update = SimpleNamespace(
        message=msg,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=chat_id),
    )
    return update, rec


def make_callback_update(data, chat_id=1):
    rec = Recorder()
    query = SimpleNamespace(
        data=data,
        answer=rec.answer,
        edit_message_text=rec.edit_message_text,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id)),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_message=SimpleNamespace(reply_text=rec.reply_text),
        effective_chat=SimpleNamespace(id=chat_id),
    )
    return update, rec


STORE = Store(store_id="D357", street="Hauptstr. 1", zip="44649", city="Herne", lat=51.5, lon=7.2)


class TestOnCallback:
    async def test_store_valid_sets_store(self):
        update, rec = make_callback_update("store:D357")
        await bot.on_callback(update, make_ctx(HandlerApi(store=STORE)))
        assert storage.get_store(1) == ("D357", STORE.name)
        assert any("Dein dm-Markt ist jetzt" in t for t in rec.texts)

    async def test_store_invalid_id_rejected(self):
        update, rec = make_callback_update("store:not/a/valid id")
        await bot.on_callback(update, make_ctx(HandlerApi(store=STORE)))
        assert storage.get_store(1) is None
        assert any("Ungültige Marktauswahl" in t for t in rec.texts)

    async def test_store_not_found(self):
        update, rec = make_callback_update("store:D999")
        await bot.on_callback(update, make_ctx(HandlerApi(store=None)))
        assert storage.get_store(1) is None
        assert any("nicht gefunden" in t for t in rec.texts)

    async def test_sub_adds_subscription(self):
        storage.set_store(1, "D357", "Herne")
        ctx = make_ctx(HandlerApi(availability={}))
        ctx.application.bot_data["titles"] = {100: "Zahnpasta"}
        update, rec = make_callback_update("sub:100")
        await bot.on_callback(update, ctx)
        assert storage.has_subscription(1, 100) is True

    async def test_sub_invalid_dan_noop(self):
        storage.set_store(1, "D357", "Herne")
        update, rec = make_callback_update("sub:abc")
        await bot.on_callback(update, make_ctx(HandlerApi()))
        assert storage.count_subscriptions(1) == 0

    async def test_message_none_returns_cleanly(self):
        rec = Recorder()
        query = SimpleNamespace(data="store:D357", answer=rec.answer, message=None)
        update = SimpleNamespace(callback_query=query)
        await bot.on_callback(update, make_ctx(HandlerApi(store=STORE)))  # must not raise
        assert rec.texts == []

    async def test_unsub_removes(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        update, rec = make_callback_update("unsub:100")
        await bot.on_callback(update, make_ctx(HandlerApi()))
        assert storage.has_subscription(1, 100) is False
        assert any("beendet" in t for t in rec.texts)


class TestCommands:
    async def test_search_populates_title_cache(self):
        products = [Product(dan=100, brand="dontodent", title="Zahnpasta")]
        ctx = make_ctx(HandlerApi(products=products))
        update, rec = make_message_update()
        ctx.args = ["zahnpasta"]
        await bot.cmd_search(update, ctx)
        assert ctx.application.bot_data["titles"][100] == "dontodent Zahnpasta"
        assert any("DAN 100" in t for t in rec.texts)

    async def test_search_empty_query_usage(self):
        update, rec = make_message_update()
        await bot.cmd_search(update, make_ctx(HandlerApi(), args=[]))
        assert any("Verwendung" in t for t in rec.texts)

    async def test_store_lists_choices(self):
        api = HandlerApi(geo=(51.5, 7.2, "Herne"), stores=[STORE])
        update, rec = make_message_update()
        await bot.cmd_store(update, make_ctx(api, args=["44649"]))
        assert any("Wähle deinen dm-Markt" in t for t in rec.texts)
        assert rec.markups[-1] is not None  # inline keyboard present

    async def test_store_not_geocodable(self):
        update, rec = make_message_update()
        await bot.cmd_store(update, make_ctx(HandlerApi(geo=None), args=["nirgendwo"]))
        assert any("konnte nicht gefunden werden" in t for t in rec.texts)

    async def test_check_without_setup(self):
        update, rec = make_message_update()
        await bot.cmd_check(update, make_ctx(HandlerApi()))
        assert any("Nichts zu prüfen" in t for t in rec.texts)

    async def test_list_empty(self):
        update, rec = make_message_update()
        await bot.cmd_list(update, make_ctx(HandlerApi()))
        assert any("Noch keine Abos" in t for t in rec.texts)


class RaisingApi:
    async def search_products(self, query, page_size=8):
        raise httpx.ConnectError("boom")

    async def find_stores(self, lat, lon, radius):
        raise httpx.ConnectError("boom")

    async def get_store(self, store_id):
        raise httpx.ConnectError("boom")

    async def get_availability(self, store_id, dans):
        raise httpx.ConnectError("boom")

    async def geocode(self, query):
        raise httpx.ConnectError("boom")


class TestServiceUnreachable:
    async def test_search(self):
        update, rec = make_message_update()
        await bot.cmd_search(update, make_ctx(RaisingApi(), args=["x"]))
        assert bot.SERVICE_UNREACHABLE in rec.texts

    async def test_store_geocode(self):
        update, rec = make_message_update()
        await bot.cmd_store(update, make_ctx(RaisingApi(), args=["76133"]))
        assert bot.SERVICE_UNREACHABLE in rec.texts

    async def test_check(self):
        storage.set_store(1, "D357", "Herne")
        storage.add_subscription(1, 100, "Zahnpasta")
        update, rec = make_message_update()
        await bot.cmd_check(update, make_ctx(RaisingApi()))
        assert bot.SERVICE_UNREACHABLE in rec.texts

    async def test_callback_store(self):
        update, rec = make_callback_update("store:D357")
        await bot.on_callback(update, make_ctx(RaisingApi()))
        assert bot.SERVICE_UNREACHABLE in rec.texts


class TestEnforceAllowlist:
    async def test_blocks_when_not_listed(self, monkeypatch):
        monkeypatch.setattr("app.config.ALLOWED_CHAT_IDS", frozenset({1}))
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=2))
        with pytest.raises(ApplicationHandlerStop):
            await bot.enforce_allowlist(update, None)

    async def test_allows_when_listed(self, monkeypatch):
        monkeypatch.setattr("app.config.ALLOWED_CHAT_IDS", frozenset({1}))
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=1))
        await bot.enforce_allowlist(update, None)  # must not raise

    async def test_open_when_empty(self, monkeypatch):
        monkeypatch.setattr("app.config.ALLOWED_CHAT_IDS", frozenset())
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=999))
        await bot.enforce_allowlist(update, None)  # must not raise
