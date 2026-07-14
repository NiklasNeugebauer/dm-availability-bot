"""Telegram handlers and the periodic availability check."""

import logging
import re
from datetime import datetime, timezone

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app import config, storage
from app.config import (
    MAX_DAN,
    MAX_STORES_PER_CHAT,
    MAX_SUBSCRIPTIONS_PER_CHAT,
    MAX_TITLE_CACHE,
    STORE_SEARCH_RADIUS_KM,
)
from app.dm_api import Availability, DmApi

logger = logging.getLogger(__name__)

# dm store IDs look like "D357"; validate before interpolating into a URL path.
STORE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")

# Shown when dm/geocoding is unreachable, so users can tell it apart from a bot bug.
SERVICE_UNREACHABLE = "Der Dienst ist gerade nicht erreichbar. Bitte versuche es später erneut."

def _numbered_buttons(prefix: str, action: str, items: list[tuple]) -> list[list[InlineKeyboardButton]]:
    """One full-width '<prefix> <n> · <label>' button per (value, label) item.

    Telegram clients truncate labels that don't fit the screen, but the leading
    action + number always stays visible and the complete text lives in the
    numbered message above the keyboard — so nothing is ever lost.
    """
    return [
        [InlineKeyboardButton(f"{prefix} {i} · {label}", callback_data=f"{action}:{value}")]
        for i, (value, label) in enumerate(items, start=1)
    ]


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
    "Ich beobachte die Produktverfügbarkeit in deinen dm-Märkten und "
    "benachrichtige dich, wenn etwas wieder verfügbar ist oder ausverkauft wird.\n\n"
    "Einrichtung:\n"
    "1. /store <PLZ oder Stadt> — füge einen oder mehrere dm-Märkte hinzu "
    "(oder sende mir einfach einen Standort)\n"
    "2. /search <Produkt> — finde Produkte und abonniere mit einem Tippen\n\n"
    "Jedes beobachtete Produkt wird an allen deinen Märkten geprüft.\n\n"
    "Befehle:\n"
    "/store <PLZ|Stadt> — einen dm-Markt hinzufügen\n"
    "/store — deine Märkte anzeigen und entfernen\n"
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
    The first check after subscribing (or at a newly added store) only
    seeds the state and never notifies.
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
        line = f"✅ {name} — verfügbar{stock}"
    else:
        line = f"❌ {name} — nicht verfügbar"
    if availability.online_available:
        line += " · online verfügbar"
    return line


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hallo! " + HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def _reply_store_choices(update: Update, context: ContextTypes.DEFAULT_TYPE, lat: float, lon: float):
    try:
        stores = await get_api(context).find_stores(lat, lon, STORE_SEARCH_RADIUS_KM)
    except httpx.HTTPError:
        await update.message.reply_text(SERVICE_UNREACHABLE)
        return
    if not stores:
        await update.message.reply_text(
            f"Kein dm-Markt im Umkreis von {STORE_SEARCH_RADIUS_KM:.0f} km gefunden. "
            "Versuche einen anderen Ort."
        )
        return
    lines = ["Wähle einen dm-Markt zum Hinzufügen:", ""]
    lines += [f"{i}. {s.name} ({s.distance_km:.1f} km)" for i, s in enumerate(stores, start=1)]
    keyboard = _numbered_buttons("➕", "store", [(s.store_id, s.name) for s in stores])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


STORE_USAGE = "Verwendung: /store <PLZ oder Stadt>, z. B. /store 76133 — oder sende mir einen Standort."


async def cmd_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        stores = storage.get_stores(update.effective_chat.id)
        if not stores:
            await update.message.reply_text(STORE_USAGE)
            return
        lines = ["Deine Märkte — tippe 🗑 zum Entfernen:", ""]
        lines += [f"{i}. {s['store_name']}" for i, s in enumerate(stores, start=1)]
        lines += ["", STORE_USAGE]
        keyboard = _numbered_buttons("🗑", "unstore", [(s["store_id"], s["store_name"]) for s in stores])
        await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
        return
    try:
        place = await get_api(context).geocode(query)
    except httpx.HTTPError:
        await update.message.reply_text(SERVICE_UNREACHABLE)
        return
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
    try:
        products = await get_api(context).search_products(query)
    except httpx.HTTPError:
        await update.message.reply_text(SERVICE_UNREACHABLE)
        return
    if not products:
        await update.message.reply_text(f"Keine Produkte für '{query}' gefunden.")
        return
    titles = context.application.bot_data.setdefault("titles", {})
    lines = [f"Ergebnisse für '{query}' — tippe 🔔 zum Beobachten:", ""]
    for i, product in enumerate(products, start=1):
        titles.pop(product.dan, None)  # re-insert at the end (bounded LRU-ish cache)
        titles[product.dan] = product.name
        while len(titles) > MAX_TITLE_CACHE:
            titles.pop(next(iter(titles)))
        lines.append(f"{i}. {product.name} (DAN {product.dan})")
    keyboard = _numbered_buttons("🔔", "sub", [(p.dan, p.name) for p in products])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def _subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, dan: int):
    reply = update.effective_message.reply_text
    stores = storage.get_stores(chat_id)
    if not stores:
        await reply("Bitte füge zuerst einen dm-Markt hinzu: /store <PLZ oder Stadt>")
        return

    name = context.application.bot_data.get("titles", {}).get(dan)
    if name is None:
        try:
            results = await get_api(context).search_products(str(dan), page_size=3)
        except httpx.HTTPError:
            results = []  # fall back to a plain DAN label; still let the user subscribe
        matches = [p for p in results if p.dan == dan]
        name = matches[0].name if matches else f"DAN {dan}"

    # Cap check and insert run with no await between them, so on the single-threaded
    # event loop a double-tap can't slip past the limit.
    if not storage.has_subscription(chat_id, dan) and (
        storage.count_subscriptions(chat_id) >= MAX_SUBSCRIPTIONS_PER_CHAT
    ):
        await reply(
            f"Du kannst höchstens {MAX_SUBSCRIPTIONS_PER_CHAT} Produkte beobachten. "
            "Beende zuerst eine Beobachtung mit /list."
        )
        return

    if not storage.add_subscription(chat_id, dan, name):
        await reply(f"Du beobachtest {name} bereits.")
        return

    # Seed the current state at every store so the first poll doesn't notify
    lines = [f"Beobachte {name}.", "Aktueller Status:"]
    for store in stores:
        try:
            availability = (await get_api(context).get_availability(store["store_id"], [dan])).get(dan)
        except Exception:
            logger.exception("Initial availability check failed for DAN %s", dan)
            availability = None
        if availability is not None and availability.store_available is not None:
            storage.update_status(
                chat_id, dan, store["store_id"], availability.store_available, availability.store_stock
            )
        lines.append(status_line(store["store_name"], availability))
    await reply("\n".join(lines))


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
    name = storage.subscription_name(update.effective_chat.id, dan)
    if storage.remove_subscription(update.effective_chat.id, dan):
        await update.message.reply_text(f"Beobachtung von {name or f'DAN {dan}'} beendet.")
    else:
        await update.message.reply_text(f"Du beobachtest DAN {dan} nicht.")


def _stored_status(row) -> str:
    if row["last_available"] is None:
        return "❓ noch nicht geprüft"
    if row["last_available"]:
        stock = f" ({row['last_stock']}x)" if row["last_stock"] is not None else ""
        return f"✅ verfügbar{stock}"
    return "❌ nicht verfügbar"


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscriptions = storage.list_subscriptions(chat_id)
    if not subscriptions:
        await update.message.reply_text("Noch keine Abos. Nutze /search, um Produkte zu finden.")
        return
    numbers = {row["dan"]: i for i, row in enumerate(subscriptions, start=1)}
    statuses = storage.list_statuses(chat_id)  # ordered by store, then product
    lines = []
    if not statuses:
        lines = ["Kein Markt gewählt!", ""]
        lines += [f"{numbers[row['dan']]}. {row['name']}" for row in subscriptions]
    current_store = None
    for row in statuses:
        if row["store_name"] != current_store:
            if current_store is not None:
                lines.append("")
            lines.append(f"📍 {row['store_name']}")
            current_store = row["store_name"]
        lines.append(
            f"{numbers[row['dan']]}. {row['name']} — {_stored_status(row)}{stale_note(row['updated_at'])}"
        )
    lines += ["", "Tippe 🗑, um eine Beobachtung zu beenden."]
    keyboard = _numbered_buttons("🗑", "unsub", [(row["dan"], row["name"]) for row in subscriptions])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    stores = storage.get_stores(chat_id)
    subscriptions = storage.list_subscriptions(chat_id)
    if not stores or not subscriptions:
        await update.message.reply_text(
            "Nichts zu prüfen — lege zuerst mit /store einen Markt fest und abonniere mit /subscribe Produkte."
        )
        return
    dans = [row["dan"] for row in subscriptions]
    # Snapshot of the stored states, used as the CAS expectation below.
    snapshot = {
        (row["store_id"], row["dan"]): row["last_available"] for row in storage.list_statuses(chat_id)
    }
    lines = []
    for store in stores:
        try:
            availability = await get_api(context).get_availability(store["store_id"], dans)
        except httpx.HTTPError:
            await update.message.reply_text(SERVICE_UNREACHABLE)
            return
        if lines:
            lines.append("")
        lines.append(f"{store['store_name']}:")
        for row in subscriptions:
            current = availability.get(row["dan"])
            lines.append(status_line(row["name"], current))
            if current is not None and current.store_available is not None:
                # CAS on the snapshot value so a concurrent store removal/re-add isn't clobbered.
                storage.update_status_cas(
                    chat_id, row["dan"], store["store_id"],
                    current.store_available, current.store_stock,
                    expected=snapshot.get((store["store_id"], row["dan"])),
                )
    await update.message.reply_text("\n".join(lines))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # callback_data is attacker-controllable (a modified client can send anything);
    # validate every value before using it.
    if not query.data or query.message is None:  # message is None for very old buttons
        return
    action, _, value = query.data.partition(":")
    chat_id = query.message.chat.id

    if action == "store":
        if not STORE_ID_RE.fullmatch(value):
            await query.edit_message_text("Ungültige Marktauswahl.")
            return
        try:
            store = await get_api(context).get_store(value)
        except httpx.HTTPError:
            await query.edit_message_text(SERVICE_UNREACHABLE)
            return
        if store is None:
            await query.edit_message_text("Dieser Markt konnte leider nicht gefunden werden.")
            return
        # Cap check and insert run with no await between them (double-tap safe).
        if storage.has_store(chat_id, store.store_id):
            await query.edit_message_text(f"{store.name} ist bereits einer deiner Märkte.")
            return
        if storage.count_stores(chat_id) >= MAX_STORES_PER_CHAT:
            await query.edit_message_text(
                f"Du kannst höchstens {MAX_STORES_PER_CHAT} Märkte beobachten. "
                "Entferne zuerst einen mit /store."
            )
            return
        storage.add_store(chat_id, store.store_id, store.name)
        await query.edit_message_text(
            f"Hinzugefügt: {store.name}\n"
            "Deine beobachteten Produkte werden dort ab jetzt mitgeprüft. "
            "Nutze /search, um Produkte zu finden, oder /store, um deine Märkte zu verwalten."
        )
    elif action == "unstore":
        if not STORE_ID_RE.fullmatch(value):
            await query.edit_message_text("Ungültige Marktauswahl.")
            return
        names = {s["store_id"]: s["store_name"] for s in storage.get_stores(chat_id)}
        removed = storage.remove_store(chat_id, value)
        await query.edit_message_text(
            f"{names.get(value, 'Der Markt')} wurde entfernt."
            if removed
            else "Dieser Markt war nicht in deiner Liste."
        )
    elif action == "sub":
        dan = parse_dan(value)
        if dan is not None:
            await _subscribe(update, context, chat_id, dan)
    elif action == "unsub":
        dan = parse_dan(value)
        if dan is None:
            return
        name = storage.subscription_name(chat_id, dan)
        removed = storage.remove_subscription(chat_id, dan)
        await query.edit_message_text(
            f"Beobachtung von {name or f'DAN {dan}'} beendet."
            if removed
            else f"Du hast DAN {dan} nicht beobachtet."
        )


async def check_all_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    """Periodic job: poll availability per store and notify on changes."""
    api = get_api(context)
    grouped = storage.subscriptions_by_store()
    pruned: set[int] = set()  # chats deleted mid-run (blocked the bot) — skip their other rows
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
            if row["chat_id"] in pruned:
                continue
            current = availability.get(row["dan"])
            if current is None or current.store_available is None:
                continue
            change = transition(row["last_available"], current.store_available)
            if change is None:
                # Seeding or unchanged: no message to send, so persist immediately.
                storage.update_status_cas(
                    row["chat_id"], row["dan"], store_id,
                    current.store_available, current.store_stock,
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
                pruned.add(row["chat_id"])
                continue
            except TelegramError:
                # Transient (flood control, network): leave state so we retry next cycle.
                logger.exception("Failed to notify chat %s", row["chat_id"])
                continue
            storage.update_status_cas(
                row["chat_id"], row["dan"], store_id,
                current.store_available, current.store_stock,
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
