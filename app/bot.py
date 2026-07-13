"""Telegram handlers and the periodic availability check."""

import logging
import re
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app import config, storage
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
    "Ich beobachte die Produktverfügbarkeit in deinem dm-Markt und "
    "benachrichtige dich, wenn etwas wieder verfügbar ist oder ausverkauft wird.\n\n"
    "Einrichtung:\n"
    "1. /store <PLZ oder Stadt> — wähle deinen dm-Markt (oder sende mir einfach einen Standort)\n"
    "2. /search <Produkt> — finde Produkte und abonniere mit einem Tippen\n\n"
    "Befehle:\n"
    "/store <PLZ|Stadt> — deinen dm-Markt wählen\n"
    "/search <Suchbegriff> — dm-Produkte suchen\n"
    "/subscribe <DAN> — ein Produkt über seine dm-Artikelnummer abonnieren\n"
    "/unsubscribe <DAN> — ein Produkt nicht mehr beobachten\n"
    "/list — deine Abos und ihr aktueller Status\n"
    "/check — Verfügbarkeit jetzt prüfen\n"
    "/help — diese Nachricht"
)


def get_api(context: ContextTypes.DEFAULT_TYPE) -> DmApi:
    return context.application.bot_data["dm_api"]


async def enforce_allowlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1 gate: drop updates from chats not on the allowlist (when one is set)."""
    if config.ALLOWED_CHAT_IDS and (
        update.effective_chat is None or update.effective_chat.id not in config.ALLOWED_CHAT_IDS
    ):
        raise ApplicationHandlerStop


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


def stale_note(updated_at: str | None) -> str:
    """A hint for /list when a subscription hasn't been refreshed in ≥1 day.

    Active products refresh every poll cycle, so a stale timestamp means dm
    stopped returning the DAN (e.g. delisted) and the shown state is old.
    """
    if not updated_at:
        return ""
    try:
        ts = datetime.fromisoformat(updated_at)
    except ValueError:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - ts).days
    return f" · zuletzt aktualisiert vor {days} Tag(en)" if days >= 1 else ""


def status_line(name: str, availability: Availability | None) -> str:
    if availability is None or availability.store_available is None:
        return f"❓ {name} — keine Marktdaten"
    if availability.store_available:
        stock = f" ({availability.store_stock}x)" if availability.store_stock is not None else ""
        return f"✅ {name} — verfügbar{stock}"
    return f"❌ {name} — nicht verfügbar"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hallo! " + HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def _reply_store_choices(update: Update, context: ContextTypes.DEFAULT_TYPE, lat: float, lon: float):
    stores = await get_api(context).find_stores(lat, lon, STORE_SEARCH_RADIUS_KM)
    if not stores:
        await update.message.reply_text(
            f"Kein dm-Markt im Umkreis von {STORE_SEARCH_RADIUS_KM:.0f} km gefunden. "
            "Versuche einen anderen Ort."
        )
        return
    keyboard = [
        [InlineKeyboardButton(f"{s.name} ({s.distance_km:.1f} km)", callback_data=f"store:{s.store_id}")]
        for s in stores
    ]
    await update.message.reply_text("Wähle deinen dm-Markt:", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text(
            "Verwendung: /store <PLZ oder Stadt>, z. B. /store 76133 — oder sende mir einen Standort."
        )
        return
    place = await get_api(context).geocode(query)
    if place is None:
        await update.message.reply_text(
            f"'{query}' konnte nicht gefunden werden. Versuche eine 5-stellige PLZ oder einen Stadtnamen."
        )
        return
    lat, lon, _ = place
    await _reply_store_choices(update, context, lat, lon)


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    location = update.message.location
    await _reply_store_choices(update, context, location.latitude, location.longitude)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Verwendung: /search <Produkt>, z. B. /search elmex zahnpasta")
        return
    products = await get_api(context).search_products(query)
    if not products:
        await update.message.reply_text(f"Keine Produkte für '{query}' gefunden.")
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
        keyboard.append([InlineKeyboardButton(f"🔔 #{i} beobachten", callback_data=f"sub:{product.dan}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def _subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, dan: int):
    reply = update.effective_message.reply_text
    store = storage.get_store(chat_id)
    if store is None:
        await reply("Bitte wähle zuerst deinen dm-Markt: /store <PLZ oder Stadt>")
        return
    store_id, store_name = store

    if not storage.has_subscription(chat_id, dan) and (
        storage.count_subscriptions(chat_id) >= MAX_SUBSCRIPTIONS_PER_CHAT
    ):
        await reply(
            f"Du kannst höchstens {MAX_SUBSCRIPTIONS_PER_CHAT} Produkte beobachten. "
            "Beende zuerst eine Beobachtung mit /list."
        )
        return

    name = context.application.bot_data.get("titles", {}).get(dan)
    if name is None:
        results = await get_api(context).search_products(str(dan), page_size=3)
        matches = [p for p in results if p.dan == dan]
        name = matches[0].name if matches else f"DAN {dan}"

    if not storage.add_subscription(chat_id, dan, name):
        await reply(f"Du beobachtest {name} bereits.")
        return

    # Seed the current state so the first poll doesn't notify
    try:
        availability = (await get_api(context).get_availability(store_id, [dan])).get(dan)
    except Exception:
        logger.exception("Initial availability check failed for DAN %s", dan)
        availability = None
    if availability is not None and availability.store_available is not None:
        storage.update_status(chat_id, dan, availability.store_available, availability.store_stock)
    await reply(f"Beobachte {name} in {store_name}.\nAktueller Status: {status_line(name, availability)}")


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dan = parse_dan(context.args[0]) if len(context.args) == 1 else None
    if dan is None:
        await update.message.reply_text("Verwendung: /subscribe <DAN> (die von /search angezeigte Nummer)")
        return
    await _subscribe(update, context, update.effective_chat.id, dan)


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dan = parse_dan(context.args[0]) if len(context.args) == 1 else None
    if dan is None:
        await update.message.reply_text("Verwendung: /unsubscribe <DAN>")
        return
    if storage.remove_subscription(update.effective_chat.id, dan):
        await update.message.reply_text(f"Beobachtung von DAN {dan} beendet.")
    else:
        await update.message.reply_text(f"Du beobachtest DAN {dan} nicht.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    store = storage.get_store(chat_id)
    subscriptions = storage.list_subscriptions(chat_id)
    if not subscriptions:
        await update.message.reply_text("Noch keine Abos. Nutze /search, um Produkte zu finden.")
        return
    lines = [f"Dein Markt: {store[1]}" if store else "Kein Markt gewählt!", ""]
    keyboard = []
    for row in subscriptions:
        if row["last_available"] is None:
            status = "❓ noch nicht geprüft"
        elif row["last_available"]:
            stock = f" ({row['last_stock']}x)" if row["last_stock"] is not None else ""
            status = f"✅ verfügbar{stock}"
        else:
            status = "❌ nicht verfügbar"
        lines.append(f"{row['name']} — {status}{stale_note(row['updated_at'])}")
        keyboard.append(
            [InlineKeyboardButton(f"🗑 {row['name'][:32]} nicht mehr beobachten", callback_data=f"unsub:{row['dan']}")]
        )
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    store = storage.get_store(chat_id)
    subscriptions = storage.list_subscriptions(chat_id)
    if store is None or not subscriptions:
        await update.message.reply_text(
            "Nichts zu prüfen — lege zuerst mit /store einen Markt fest und abonniere mit /subscribe Produkte."
        )
        return
    store_id, store_name = store
    dans = [row["dan"] for row in subscriptions]
    availability = await get_api(context).get_availability(store_id, dans)
    lines = [f"{store_name}:"]
    for row in subscriptions:
        current = availability.get(row["dan"])
        lines.append(status_line(row["name"], current))
        if current is not None and current.store_available is not None:
            # CAS on the snapshot value so a concurrent /store reset isn't clobbered.
            storage.update_status_cas(
                chat_id, row["dan"], current.store_available, current.store_stock,
                expected=row["last_available"],
            )
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
            await query.edit_message_text("Ungültige Marktauswahl.")
            return
        store = await get_api(context).get_store(value)
        if store is None:
            await query.edit_message_text("Dieser Markt konnte leider nicht gefunden werden.")
            return
        storage.set_store(chat_id, store.store_id, store.name)
        await query.edit_message_text(
            f"Dein dm-Markt ist jetzt: {store.name}\nNutze /search, um Produkte zu finden und zu beobachten."
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
            f"Beobachtung von DAN {dan} beendet." if removed else f"Du hast DAN {dan} nicht beobachtet."
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
        # Schema-drift canary: if we asked about DANs but got zero usable store
        # rows back, dm likely changed the tile format and we'd silently go quiet.
        if dans and not any(a.store_available is not None for a in availability.values()):
            logger.warning(
                "Store %s: no usable store availability for %d DAN(s) — dm schema may have changed",
                store_id,
                len(dans),
            )
        for row in subscriptions:
            current = availability.get(row["dan"])
            if current is None or current.store_available is None:
                continue
            change = transition(row["last_available"], current.store_available)
            if change is None:
                # Seeding or unchanged: no message to send, so persist immediately.
                storage.update_status_cas(
                    row["chat_id"], row["dan"], current.store_available, current.store_stock,
                    expected=row["last_available"],
                )
                continue
            if change == "available":
                stock = f" ({current.store_stock}x)" if current.store_stock is not None else ""
                text = f"✅ {row['name']} ist wieder verfügbar{stock} in {row['store_name']}!"
            else:
                text = f"❌ {row['name']} ist nicht mehr verfügbar in {row['store_name']}."
            # Persist the transition only after a successful send: a transient send
            # failure must not consume the change (the user would silently miss it).
            try:
                await context.bot.send_message(chat_id=row["chat_id"], text=text)
            except Forbidden:
                # User blocked the bot (or deleted the chat) — stop polling for them.
                logger.info("Chat %s blocked the bot; removing its data", row["chat_id"])
                storage.delete_chat(row["chat_id"])
                continue
            except TelegramError:
                # Transient (flood control, network): leave state so we retry next cycle.
                logger.exception("Failed to notify chat %s", row["chat_id"])
                continue
            storage.update_status_cas(
                row["chat_id"], row["dan"], current.store_available, current.store_stock,
                expected=row["last_available"],
            )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all so a handler failure replies gracefully instead of only logging.

    Registering this also prevents PTB's default handler from logging the entire
    update (which contains user message content) at ERROR level.
    """
    logger.error("Error handling update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Entschuldigung, etwas ist schiefgelaufen. Bitte versuche es erneut."
            )
        except TelegramError:
            pass
