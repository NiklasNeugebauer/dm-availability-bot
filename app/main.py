"""dm availability bot — entrypoint.

Telegram bot that watches product availability at your local
dm-drogerie markt store and notifies you on changes.
"""

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from app import bot, storage
from app.config import ALLOWED_CHAT_IDS, CHECK_INTERVAL_MINUTES, TELEGRAM_BOT_TOKEN
from app.dm_api import DmApi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


BOT_COMMANDS = [
    BotCommand("store", "deinen dm-Markt wählen (PLZ oder Stadt)"),
    BotCommand("search", "dm-Produkte suchen"),
    BotCommand("subscribe", "ein Produkt per DAN abonnieren"),
    BotCommand("unsubscribe", "ein Produkt nicht mehr beobachten"),
    BotCommand("list", "deine Abos und ihr Status"),
    BotCommand("check", "Verfügbarkeit jetzt prüfen"),
    BotCommand("help", "Hilfe anzeigen"),
]


async def _post_init(application: Application):
    application.bot_data["dm_api"] = DmApi()
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Bot started, checking availability every %d min", CHECK_INTERVAL_MINUTES)


async def _post_shutdown(application: Application):
    api = application.bot_data.get("dm_api")
    if api is not None:
        await api.aclose()
    logger.info("Bot stopped.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Get a token from @BotFather and export it.")

    storage.init_db()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    if ALLOWED_CHAT_IDS:
        application.add_handler(TypeHandler(Update, bot.enforce_allowlist), group=-1)
        logger.info("Restricting access to %d allowed chat(s)", len(ALLOWED_CHAT_IDS))

    application.add_handler(CommandHandler("start", bot.cmd_start))
    application.add_handler(CommandHandler("help", bot.cmd_help))
    application.add_handler(CommandHandler("store", bot.cmd_store))
    application.add_handler(CommandHandler("search", bot.cmd_search))
    application.add_handler(CommandHandler("subscribe", bot.cmd_subscribe))
    application.add_handler(CommandHandler("unsubscribe", bot.cmd_unsubscribe))
    application.add_handler(CommandHandler("list", bot.cmd_list))
    application.add_handler(CommandHandler("check", bot.cmd_check))
    application.add_handler(CallbackQueryHandler(bot.on_callback))
    application.add_handler(MessageHandler(filters.LOCATION, bot.on_location))
    application.add_error_handler(bot.on_error)

    application.job_queue.run_repeating(
        bot.check_all_subscriptions,
        interval=CHECK_INTERVAL_MINUTES * 60,
        first=30,
    )

    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
