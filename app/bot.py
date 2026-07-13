"""Telegram handlers and the periodic availability check."""

import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from app import storage
from app.config import (
    MAX_DAN,
    MAX_SUBSCRIPTIONS_PER_CHAT,
    MAX_TITLE_CACHE,
    STORE_SEARCH_RADIUS_KM,
)
from app.dm_api import Availability, DmApi

logger = logging.getLogger(__name__)

# dm store IDs look like "D357"; validate before interpolating into a URL path.
STORE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")


def parse_dan(text: str) -> int | None:
    """Parse a dm article number from untrusted text, or None if invalid.

    Guards against non-numeric input, Unicode digits that ``int`` rejects,
    and values large enough to overflow SQLite's 64-bit INTEGER.
    """
    if not text.isdecimal():
        return None
    dan = int(text)
    return dan if 0 < dan < MAX_DAN else None

HELP_TEXT = (
    "I watch product availability at your local dm-drogerie markt and "
    "notify you when something comes back in stock or sells out.\n\n"
    "Setup:\n"
    "1. /store <PLZ or city> — pick your dm store (or just send me a location)\n"
    "2. /search <product> — find products and subscribe with one tap\n\n"
    "Commands:\n"
    "/store <PLZ|city> — choose your dm store\n"
    "/search <query> — search dm products\n"
    "/subscribe <DAN> — subscribe to a product by its dm article number\n"
    "/unsubscribe <DAN> — stop watching a product\n"
    "/list — your subscriptions and their current status\n"
    "/check — check availability right now\n"
    "/help — this message"
)


def get_api(context: ContextTypes.DEFAULT_TYPE) -> DmApi:
    return context.application.bot_data["dm_api"]


def transition(last_available: int | None, current: bool) -> str | None:
    """Compare stored state with the current one.

    Returns "available" / "unavailable" on a change, None otherwise.
    The first check after subscribing (or switching store) only seeds the
    state and never notifies.
    """
    if last_available is None:
        return None
    if bool(last_available) == current:
        return None
    return "available" if current else "unavailable"


def status_line(name: str, availability: Availability | None) -> str:
    if availability is None or availability.store_available is None:
        return f"❓ {name} — no store data"
    if availability.store_available:
        stock = f" ({availability.store_stock}x)" if availability.store_stock is not None else ""
        return f"✅ {name} — in stock{stock}"
    return f"❌ {name} — not available"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hi! " + HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def _reply_store_choices(update: Update, context: ContextTypes.DEFAULT_TYPE, lat: float, lon: float):
    stores = await get_api(context).find_stores(lat, lon, STORE_SEARCH_RADIUS_KM)
    if not stores:
        await update.message.reply_text(
            f"No dm store found within {STORE_SEARCH_RADIUS_KM:.0f} km. Try a different location."
        )
        return
    keyboard = [
        [InlineKeyboardButton(f"{s.name} ({s.distance_km:.1f} km)", callback_data=f"store:{s.store_id}")]
        for s in stores
    ]
    await update.message.reply_text("Pick your dm store:", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text(
            "Usage: /store <PLZ or city>, e.g. /store 76133 — or send me a location."
        )
        return
    place = await get_api(context).geocode(query)
    if place is None:
        await update.message.reply_text(f"Could not find '{query}'. Try a 5-digit PLZ or a city name.")
        return
    lat, lon, _ = place
    await _reply_store_choices(update, context, lat, lon)


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location = update.message.location
    await _reply_store_choices(update, context, location.latitude, location.longitude)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /search <product>, e.g. /search elmex zahnpasta")
        return
    products = await get_api(context).search_products(query)
    if not products:
        await update.message.reply_text(f"No products found for '{query}'.")
        return
    titles = context.application.bot_data.setdefault("titles", {})
    lines = []
    keyboard = []
    for i, product in enumerate(products, start=1):
        titles.pop(product.dan, None)  # re-insert at the end (bounded LRU-ish cache)
        titles[product.dan] = product.name
        while len(titles) > MAX_TITLE_CACHE:
            titles.pop(next(iter(titles)))
        lines.append(f"{i}. {product.name} (DAN {product.dan})")
        keyboard.append([InlineKeyboardButton(f"🔔 Watch #{i}", callback_data=f"sub:{product.dan}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def _subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, dan: int):
    reply = update.effective_message.reply_text
    store = storage.get_store(chat_id)
    if store is None:
        await reply("Please choose your dm store first: /store <PLZ or city>")
        return
    store_id, store_name = store

    if not storage.has_subscription(chat_id, dan) and (
        storage.count_subscriptions(chat_id) >= MAX_SUBSCRIPTIONS_PER_CHAT
    ):
        await reply(
            f"You can watch at most {MAX_SUBSCRIPTIONS_PER_CHAT} products. "
            "Unsubscribe from something first with /list."
        )
        return

    name = context.application.bot_data.get("titles", {}).get(dan)
    if name is None:
        results = await get_api(context).search_products(str(dan), page_size=3)
        matches = [p for p in results if p.dan == dan]
        name = matches[0].name if matches else f"DAN {dan}"

    if not storage.add_subscription(chat_id, dan, name):
        await reply(f"You are already watching {name}.")
        return

    # Seed the current state so the first poll doesn't notify
    try:
        availability = (await get_api(context).get_availability(store_id, [dan])).get(dan)
    except Exception:
        logger.exception("Initial availability check failed for DAN %s", dan)
        availability = None
    if availability is not None and availability.store_available is not None:
        storage.update_status(chat_id, dan, availability.store_available, availability.store_stock)
    await reply(f"Watching {name} at {store_name}.\nCurrent status: {status_line(name, availability)}")


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dan = parse_dan(context.args[0]) if len(context.args) == 1 else None
    if dan is None:
        await update.message.reply_text("Usage: /subscribe <DAN> (the number shown by /search)")
        return
    await _subscribe(update, context, update.effective_chat.id, dan)


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dan = parse_dan(context.args[0]) if len(context.args) == 1 else None
    if dan is None:
        await update.message.reply_text("Usage: /unsubscribe <DAN>")
        return
    if storage.remove_subscription(update.effective_chat.id, dan):
        await update.message.reply_text(f"Stopped watching DAN {dan}.")
    else:
        await update.message.reply_text(f"You are not watching DAN {dan}.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    store = storage.get_store(chat_id)
    subscriptions = storage.list_subscriptions(chat_id)
    if not subscriptions:
        await update.message.reply_text("No subscriptions yet. Use /search to find products.")
        return
    lines = [f"Your store: {store[1]}" if store else "No store chosen!", ""]
    keyboard = []
    for row in subscriptions:
        if row["last_available"] is None:
            status = "❓ not checked yet"
        elif row["last_available"]:
            stock = f" ({row['last_stock']}x)" if row["last_stock"] is not None else ""
            status = f"✅ in stock{stock}"
        else:
            status = "❌ not available"
        lines.append(f"{row['name']} — {status}")
        keyboard.append(
            [InlineKeyboardButton(f"🗑 Stop watching {row['name'][:32]}", callback_data=f"unsub:{row['dan']}")]
        )
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    store = storage.get_store(chat_id)
    subscriptions = storage.list_subscriptions(chat_id)
    if store is None or not subscriptions:
        await update.message.reply_text("Nothing to check — set a /store and /subscribe to products first.")
        return
    store_id, store_name = store
    dans = [row["dan"] for row in subscriptions]
    availability = await get_api(context).get_availability(store_id, dans)
    lines = [f"{store_name}:"]
    for row in subscriptions:
        current = availability.get(row["dan"])
        lines.append(status_line(row["name"], current))
        if current is not None and current.store_available is not None:
            storage.update_status(chat_id, row["dan"], current.store_available, current.store_stock)
    await update.message.reply_text("\n".join(lines))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback_data is attacker-controllable (a modified client can send anything);
    # validate every value before using it.
    if not query.data:
        return
    action, _, value = query.data.partition(":")
    chat_id = query.message.chat.id

    if action == "store":
        if not STORE_ID_RE.fullmatch(value):
            await query.edit_message_text("Invalid store selection.")
            return
        store = await get_api(context).get_store(value)
        if store is None:
            await query.edit_message_text("Sorry, that store could not be found.")
            return
        storage.set_store(chat_id, store.store_id, store.name)
        await query.edit_message_text(
            f"Your dm store is now: {store.name}\nUse /search to find and watch products."
        )
    elif action == "sub":
        dan = parse_dan(value)
        if dan is not None:
            await _subscribe(update, context, chat_id, dan)
    elif action == "unsub":
        dan = parse_dan(value)
        if dan is None:
            return
        removed = storage.remove_subscription(chat_id, dan)
        await query.edit_message_text(
            f"Stopped watching DAN {dan}." if removed else f"You were not watching DAN {dan}."
        )


async def check_all_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job: poll availability per store and notify on changes."""
    api = get_api(context)
    grouped = storage.subscriptions_by_store()
    for store_id, subscriptions in grouped.items():
        dans = sorted({row["dan"] for row in subscriptions})
        try:
            availability = await api.get_availability(store_id, dans)
        except Exception:
            logger.exception("Availability check failed for store %s", store_id)
            continue
        for row in subscriptions:
            current = availability.get(row["dan"])
            if current is None or current.store_available is None:
                continue
            change = transition(row["last_available"], current.store_available)
            storage.update_status(row["chat_id"], row["dan"], current.store_available, current.store_stock)
            if change is None:
                continue
            if change == "available":
                stock = f" ({current.store_stock}x)" if current.store_stock is not None else ""
                text = f"✅ {row['name']} is back in stock{stock} at {row['store_name']}!"
            else:
                text = f"❌ {row['name']} is no longer available at {row['store_name']}."
            try:
                await context.bot.send_message(chat_id=row["chat_id"], text=text)
            except Forbidden:
                # User blocked the bot (or deleted the chat) — stop polling for them.
                logger.info("Chat %s blocked the bot; removing its data", row["chat_id"])
                storage.delete_chat(row["chat_id"])
            except TelegramError:
                logger.exception("Failed to notify chat %s", row["chat_id"])


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all so a handler failure replies gracefully instead of only logging.

    Registering this also prevents PTB's default handler from logging the entire
    update (which contains user message content) at ERROR level.
    """
    logger.error("Error handling update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text("Sorry, something went wrong. Please try again.")
        except TelegramError:
            pass
