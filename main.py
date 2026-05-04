import asyncio
import logging
import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.error import TimedOut, NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import HH_REAUTH_CHECK_INTERVAL_MINUTES, MONITOR_INTERVAL_MINUTES
from app.config import HH_REDIRECT_URI
from app.hh_api import HHAuthorizationError, get_hh_authorize_url
from app.hh_auth import run_hh_reauth_notification_pass
from app.hh_oauth_callback_server import start_hh_callback_server
from app.monitor import monitoring_loop, run_monitoring_check
from app.notifier import send_vacancies_to_telegram
from app.repository import mark_vacancy_sent
from app.search_flow import build_search_conversation_handler, reset_search_conversation
from app.filters_handlers import build_filters_handlers
from app.user_repository import get_or_create_user, USER_FRIENDLY_ERROR

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

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 Поиск вакансий"), KeyboardButton("💾 Мои фильтры")],
        [KeyboardButton("ℹ️ О боте")],
    ],
    resize_keyboard=True,
    is_persistent=True,
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
        return None, USER_FRIENDLY_ERROR


async def _refresh_bot_commands(bot):
    """Register visible slash commands (search / filters / info only). Clears stale menu first."""
    await bot.delete_my_commands()
    await bot.set_my_commands(
        [
            BotCommand("search", "🔍 Поиск вакансий"),
            BotCommand("filters", "💾 Мои фильтры"),
            BotCommand("info", "ℹ️ О боте"),
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reset_search_conversation(context.application, update)
    user, err = _ensure_user(update)
    if err:
        await update.message.reply_text(err)
        return

    await _refresh_bot_commands(context.bot)
    await update.message.reply_text(
        "Добро пожаловать! Для навигации по чат-боту воспользуйтесь меню ниже:",
        reply_markup=MAIN_KEYBOARD,
    )


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reset_search_conversation(context.application, update)
    user, err = _ensure_user(update)
    if err:
        await update.message.reply_text(err)
        return

    await update.message.reply_text(
        "Привет! Я бот для поиска вакансий на HH.ru\n\n"
        "Моя ключевая фишка: я умею присылать уведомления о только что опубликованных "
        "вакансиях по твоим сохранённым фильтрам.\n\n"
        "Для этого:\n"
        "1. Нужно зайти в меню «Поиск вакансий»\n"
        "2. Настроить фильтры поиска и сохранить фильтр\n"
        "3. В конце нажать «Получать уведомления о новых вакансиях»",
        reply_markup=MAIN_KEYBOARD,
    )


async def connect_hh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reset_search_conversation(context.application, update)
    user, err = _ensure_user(update)
    if err:
        await update.message.reply_text(err)
        return
    try:
        auth_url = get_hh_authorize_url(user.id)
    except HHAuthorizationError as e:
        logger.exception("HH auth URL generation failed: %s", e)
        await update.message.reply_text("Не удалось сформировать ссылку авторизации HH.")
        return
    await update.message.reply_text(
        "Поиск вакансий на HH.ru в боте идёт от имени приложения (OAuth client_credentials на сервере). "
        "Личный вход соискателя для поиска не нужен.\n\n"
        "Ссылка ниже — опционально, на будущее, если появятся функции, привязанные к вашему аккаунту HH.\n\n"
        f"{auth_url}"
    )


async def hh_reauth_notification_job_callback(context: ContextTypes.DEFAULT_TYPE):
    """Periodic passive reminder for expired HH tokens (once per expiration cycle)."""
    try:
        await run_hh_reauth_notification_pass(context.bot)
    except Exception as e:
        logger.exception("HH_REAUTH job failed: %s", e)


async def monitoring_job_callback(context: ContextTypes.DEFAULT_TYPE):
    """job_queue callback: per-user monitoring, send only unsent vacancies, track delivery."""
    logger.info("MONITOR WORKING")
    try:
        results = run_monitoring_check()
        n_results = len(results)
        n_items = sum(len(r.items_to_send) for r in results)
        logger.info(
            "MONITOR job: run_monitoring_check returned %d user result(s), %d total vacancy item(s) to send",
            n_results,
            n_items,
        )
        for result in results:
            if not result.items_to_send:
                logger.info(
                    "[MONITOR_TRACE] continue monitoring_job_callback reason=empty_items_to_send "
                    "user_id=%s telegram_id=%s",
                    result.user_id,
                    result.user_telegram_id,
                )
                continue

            logger.info(
                "About to send %d vacancies to user telegram_id=%s (internal_user_id=%s)",
                len(result.items_to_send),
                result.user_telegram_id,
                result.user_id,
            )
            for vacancy_dict, filter_id in result.items_to_send:
                hh_id = str(vacancy_dict.get("id", ""))
                try:
                    logger.info(
                        "MONITOR send attempt: telegram_id=%s filter_id=%s hh_vacancy_id=%s",
                        result.user_telegram_id,
                        filter_id,
                        hh_id,
                    )
                    await send_vacancies_to_telegram(
                        context.bot,
                        result.user_telegram_id,
                        [vacancy_dict],
                    )
                    logger.info(
                        "MONITOR send ok: telegram_id=%s filter_id=%s hh_vacancy_id=%s",
                        result.user_telegram_id,
                        filter_id,
                        hh_id,
                    )
                    if hh_id and filter_id:
                        mark_vacancy_sent(hh_id, result.user_id, filter_id)
                except Exception as e:
                    logger.exception(
                        "MONITOR FAILED: send vacancy %s to user %s: %s",
                        hh_id,
                        result.user_telegram_id,
                        e,
                    )
    except Exception as e:
        logger.exception("MONITOR FAILED: %s", e)


async def post_init(application: Application):
    await _refresh_bot_commands(application.bot)
    if HH_REDIRECT_URI:
        parsed = urlparse(HH_REDIRECT_URI)
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        loop = asyncio.get_running_loop()
        try:
            start_hh_callback_server(application, loop, host=host, port=port)
        except Exception:
            logger.exception("Failed to start HH callback server")

    if not application.job_queue:
        task = application.create_task(
            monitoring_loop(
                bot=application.bot,
                interval_minutes=MONITOR_INTERVAL_MINUTES,
                send_fn=send_vacancies_to_telegram,
                mark_sent_fn=mark_vacancy_sent,
            )
        )

        def _on_monitoring_done(t):
            try:
                t.result()
            except Exception:
                logger.exception("Monitoring loop stopped with error")

        task.add_done_callback(_on_monitoring_done)

        logger.info(
            "Monitoring loop: job_queue not available, using asyncio fallback every %d min",
            MONITOR_INTERVAL_MINUTES,
        )

        async def hh_reauth_fallback_loop():
            interval_sec = HH_REAUTH_CHECK_INTERVAL_MINUTES * 60
            first_delay = min(90, interval_sec)
            while True:
                try:
                    await asyncio.sleep(first_delay)
                    first_delay = interval_sec
                    await run_hh_reauth_notification_pass(application.bot)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("HH re-auth fallback loop error")

        rtask = application.create_task(hh_reauth_fallback_loop())

        def _on_reauth_done(t):
            try:
                t.result()
            except Exception:
                logger.exception("HH re-auth fallback loop stopped with error")

        rtask.add_done_callback(_on_reauth_done)
        logger.info(
            "HH re-auth reminders: asyncio fallback every %d min",
            HH_REAUTH_CHECK_INTERVAL_MINUTES,
        )


def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    if app.job_queue:
        interval_seconds = MONITOR_INTERVAL_MINUTES * 60
        app.job_queue.run_repeating(
            monitoring_job_callback,
            interval=interval_seconds,
            first=1,
            name="hh_monitor",
        )
        logger.info(
            "Scheduled monitoring (job_queue): every %d minutes",
            MONITOR_INTERVAL_MINUTES,
        )
        reauth_seconds = HH_REAUTH_CHECK_INTERVAL_MINUTES * 60
        app.job_queue.run_repeating(
            hh_reauth_notification_job_callback,
            interval=reauth_seconds,
            first=90,
            name="hh_reauth_notify",
        )
        logger.info(
            "Scheduled HH re-auth reminders (job_queue): every %d minutes",
            HH_REAUTH_CHECK_INTERVAL_MINUTES,
        )
    else:
        logger.warning(
            "Job queue not available; monitoring uses asyncio fallback in post_init"
        )

    for handler in build_filters_handlers(_ensure_user):
        app.add_handler(handler, group=-1)

    app.add_handler(
        MessageHandler(filters.Regex("^ℹ️ О боте$"), info),
        group=-1,
    )

    app.add_handler(build_search_conversation_handler(_ensure_user))
    app.add_handler(CommandHandler("start", start))
    # Hidden from Telegram command menu and reply keyboard; available for debugging / future use.
    app.add_handler(CommandHandler("connect_hh", connect_hh))
    app.add_handler(CommandHandler("info", info))

    return app


def main():
    print("Запускаем бота...")

    app = build_application()

    print("Бот запущен")

    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            poll_interval=2.0,
            timeout=30,
            bootstrap_retries=5,
        )
    except TimedOut:
        logger.exception("Telegram polling timed out")
        raise
    except NetworkError:
        logger.exception("Telegram network error")
        raise
    except Exception:
        logger.exception("Bot crashed")
        raise


if __name__ == "__main__":
    main()