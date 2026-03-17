import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import MONITOR_INTERVAL_MINUTES
from app.monitor import run_monitoring_check
from app.notifier import send_vacancies_to_telegram
from app.repository import mark_vacancy_sent_to_filter
from app.search_flow import build_search_conversation_handler
from app.filters_handlers import build_filters_handlers
from app.user_repository import get_or_create_user

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

print("BOT_TOKEN loaded:", bool(BOT_TOKEN))

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в .env")

# Main reply keyboard with emoji buttons
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 Поиск вакансий"), KeyboardButton("💾 Мои фильтры")],
        [KeyboardButton("ℹ️ О боте")],
    ],
    resize_keyboard=True,
)


def _ensure_user(update: Update):
    """
    Ensure user exists in DB. Creates a new user if they don't exist.
    Does not perform authorization — any Telegram user is allowed.
    Returns (user, None) on success, (None, error_msg) on DB error only.
    """
    try:
        u = update.effective_user
        user = get_or_create_user(
            telegram_id=u.id,
            username=u.username,
            first_name=u.first_name,
            last_name=u.last_name,
        )
        return user, None
    except Exception as e:
        logger.exception("get_or_create_user failed: %s", e)
        return None, f"Ошибка БД: {e}"


async def _refresh_bot_commands(bot):
    """Set bot commands with emoji (called on init and /start for cache refresh)."""
    await bot.set_my_commands([
        BotCommand("search", "🔍 Поиск вакансий"),
        BotCommand("filters", "💾 Мои фильтры"),
        BotCommand("info", "ℹ️ О боте"),
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, err = _ensure_user(update)
    if err:
        await update.message.reply_text(err)
        return

    await _refresh_bot_commands(context.bot)
    await update.message.reply_text(
        "Бот работает.\n\n"
        "Текущий фильтр по умолчанию:\n"
        "• только Системный / Бизнес аналитик\n"
        "• удаленка\n"
        "• без слов: 1С, Битрикс, DWH, Lead, Senior\n"
        "• выборка: 300 последних вакансий\n\n"
        "Используйте кнопки меню ниже:",
        reply_markup=MAIN_KEYBOARD,
    )


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /info - show help/about message."""
    user, err = _ensure_user(update)
    if err:
        await update.message.reply_text(err)
        return

    await update.message.reply_text(
        "Привет! Я бот для поиска вакансий на HH.ru\n\n"
        "Моя ключевая фишка: я умею присылать уведомления о только что опубликованных "
        "вакансиях по твоим сохранённым фильтрам!\n\n"
        "Для этого:\n"
        "1. Нужно зайти в меню «Поиск вакансий»\n"
        "2. Настроить фильтры поиска и сохранить фильтр\n"
        "3. В конце нажать «Получать уведомления о новых вакансиях»",
        reply_markup=MAIN_KEYBOARD,
    )


async def monitoring_job_callback(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: per-user monitoring, send only unsent vacancies, track delivery.
    Mark as sent only after successful Telegram delivery (idempotent).
    """
    try:
        results = run_monitoring_check()
        for result in results:
            if not result.items_to_send:
                continue
            for vacancy_dict, vacancy_id, filter_id in result.items_to_send:
                try:
                    await send_vacancies_to_telegram(
                        context.bot, result.user_telegram_id, [vacancy_dict]
                    )
                    if filter_id:
                        mark_vacancy_sent_to_filter(filter_id, vacancy_id)
                except Exception as e:
                    logger.exception(
                        "Failed to send vacancy %s to user %s: %s",
                        vacancy_id,
                        result.user_telegram_id,
                        e,
                    )
    except Exception as e:
        logger.exception("Monitoring job failed: %s", e)


def main():
    print("Запускаем бота...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def post_init(application):
        await _refresh_bot_commands(application.bot)

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Filters handlers in group -1 so they run BEFORE ConversationHandlers
    for handler in build_filters_handlers(_ensure_user):
        app.add_handler(handler, group=-1)

    # Reply keyboard button handler for "ℹ️ О боте" (group -1)
    app.add_handler(
        MessageHandler(filters.Regex("^ℹ️ О боте$"), info),
        group=-1,
    )

    app.add_handler(build_search_conversation_handler(_ensure_user))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("info", info))

    # Start scheduled monitoring (every N minutes)
    if app.job_queue:
        interval_seconds = MONITOR_INTERVAL_MINUTES * 60
        app.job_queue.run_repeating(
            monitoring_job_callback,
            interval=interval_seconds,
            first=interval_seconds,
            name="hh_monitor",
        )
        logger.info(
            "Scheduled monitoring started: every %d minutes",
            MONITOR_INTERVAL_MINUTES,
        )
    else:
        logger.warning("Job queue not available; scheduled monitoring disabled")

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
