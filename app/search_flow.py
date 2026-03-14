"""
Interactive step-by-step search flow for "Поиск вакансий".
Collects filters, optionally saves to Сохраненные фильтры, then runs search on "Начать поиск".
"""

import logging
from types import SimpleNamespace

from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from app.hh_api import _build_search_text, search_vacancies_page
from app.user_repository import save_user_filter
from app.vacancy_results import _store_search_state, fetch_and_show_page

logger = logging.getLogger(__name__)

# Conversation states
(
    TITLE_KEYWORDS,
    TITLE_EXCLUDE,
    DESC_KEYWORDS,
    DESC_EXCLUDE,
    CITY,
    SALARY,
    SCHEDULE,
    PERIOD,
    SAVE_FILTER,
    MONITORING,
    RUN_SEARCH,
) = range(11)

# Button options - labels EXACTLY as specified
TEXT_SKIP = [("skip", "Пропустить")]
SALARY_SKIP = [("skip", "Не указывать")]
SCHEDULE_OPTIONS = [
    ("remote", "удаленка"),
    ("flyInFlyOut", "гибрид"),
    ("fullDay", "офис"),
    ("skip", "не важно"),
]
PERIOD_OPTIONS = [
    ("1", "за день"),
    ("3", "за последние три дня"),
    ("7", "за неделю"),
    ("30", "за месяц"),
]
SAVE_FILTER_OPTIONS = [
    ("save", "Сохранить"),
    ("skip", "Пропустить"),
]
MONITORING_OPTIONS = [
    ("filter_monitoring_yes", "Да, получать уведомления"),
    ("filter_monitoring_no", "Нет, пропустить"),
]
RUN_SEARCH_BUTTON = [("run", "Начать поиск")]


def _get_search_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    key = "search_data"
    if key not in context.user_data:
        context.user_data[key] = {}
    return context.user_data[key]


def _clear_search_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("search_data", None)


def _build_inline_keyboard(options: list[tuple[str, str]], prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:{val}")]
        for val, label in options
    ]
    return InlineKeyboardMarkup(buttons)


def _data_to_search_params(data: dict) -> tuple[dict, SimpleNamespace]:
    """Build HH API search_params and filter_obj from collected data.
    Uses strict search: search_field for title/description, AND/NOT operators."""
    text, search_field = _build_search_text(
        title_keywords=data.get("title_keywords"),
        title_exclude_keywords=data.get("title_exclude_keywords"),
        description_keywords=data.get("description_keywords"),
        description_exclude_keywords=data.get("description_exclude_keywords"),
        city=data.get("city"),
    )
    params = {"text": text, "search_field": search_field}
    if data.get("work_format"):
        params["schedule"] = data["work_format"]
    if data.get("salary_from"):
        params["salary_from"] = data["salary_from"]
    if data.get("period"):
        params["period"] = int(data["period"])
    elif "period" not in params:
        params["period"] = 30

    filter_obj = SimpleNamespace(
        title_keywords=data.get("title_keywords"),
        title_exclude_keywords=data.get("title_exclude_keywords"),
        description_keywords=data.get("description_keywords"),
        description_exclude_keywords=data.get("description_exclude_keywords"),
        work_format=data.get("work_format"),
    )
    return params, filter_obj


# --- Entry: go directly to Step 1 (title keywords) ---
async def search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: /search or 🔍 Поиск вакансий - go directly to first step."""
    _clear_search_data(context)
    keyboard = _build_inline_keyboard(TEXT_SKIP, "title_kw")
    await update.message.reply_text(
        "Введите ключевые слова в названии вакансии:",
        reply_markup=keyboard,
    )
    return TITLE_KEYWORDS


# --- Step 1: title keywords ---
async def receive_title_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["title_keywords"] = (update.message.text or "").strip() or None
    keyboard = _build_inline_keyboard(TEXT_SKIP, "title_ex")
    await update.message.reply_text(
        "Исключить слова в названии вакансии:",
        reply_markup=keyboard,
    )
    return TITLE_EXCLUDE


async def receive_title_keywords_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    data["title_keywords"] = None
    keyboard = _build_inline_keyboard(TEXT_SKIP, "title_ex")
    await query.edit_message_text(
        "Исключить слова в названии вакансии:",
        reply_markup=keyboard,
    )
    return TITLE_EXCLUDE


# --- Step 2: title exclude ---
async def receive_title_exclude(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["title_exclude_keywords"] = (update.message.text or "").strip() or None
    keyboard = _build_inline_keyboard(TEXT_SKIP, "desc_kw")
    await update.message.reply_text(
        "Ключевые слова в ТЕЛЕ вакансии:",
        reply_markup=keyboard,
    )
    return DESC_KEYWORDS


async def receive_title_exclude_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    data["title_exclude_keywords"] = None
    keyboard = _build_inline_keyboard(TEXT_SKIP, "desc_kw")
    await query.edit_message_text(
        "Ключевые слова в ТЕЛЕ вакансии:",
        reply_markup=keyboard,
    )
    return DESC_KEYWORDS


# --- Step 3: desc keywords ---
async def receive_desc_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["description_keywords"] = (update.message.text or "").strip() or None
    keyboard = _build_inline_keyboard(TEXT_SKIP, "desc_ex")
    await update.message.reply_text(
        "Исключить слова в ТЕЛЕ  вакансии:",
        reply_markup=keyboard,
    )
    return DESC_EXCLUDE


async def receive_desc_keywords_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    data["description_keywords"] = None
    keyboard = _build_inline_keyboard(TEXT_SKIP, "desc_ex")
    await query.edit_message_text(
        "Исключить слова в ТЕЛЕ  вакансии:",
        reply_markup=keyboard,
    )
    return DESC_EXCLUDE


# --- Step 4: desc exclude ---
async def receive_desc_exclude(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["description_exclude_keywords"] = (update.message.text or "").strip() or None
    keyboard = _build_inline_keyboard(TEXT_SKIP, "city")
    await update.message.reply_text(
        "Введите город или регион:",
        reply_markup=keyboard,
    )
    return CITY


async def receive_desc_exclude_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    data["description_exclude_keywords"] = None
    keyboard = _build_inline_keyboard(TEXT_SKIP, "city")
    await query.edit_message_text(
        "Введите город или регион:",
        reply_markup=keyboard,
    )
    return CITY


# --- Step 5: city (text or skip only, no "Ручной ввод") ---
async def receive_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["city"] = (update.message.text or "").strip() or None
    keyboard = _build_inline_keyboard(SALARY_SKIP, "salary")
    await update.message.reply_text(
        "Желаемая зарплата:",
        reply_markup=keyboard,
    )
    return SALARY


async def receive_city_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    data["city"] = None
    keyboard = _build_inline_keyboard(SALARY_SKIP, "salary")
    await query.edit_message_text(
        "Желаемая зарплата:",
        reply_markup=keyboard,
    )
    return SALARY


# --- Step 6: salary ---
async def receive_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    text = (update.message.text or "").strip()
    try:
        data["salary_from"] = int(text.replace(" ", ""))
    except (ValueError, AttributeError):
        await update.message.reply_text("Введите число:")
        return SALARY
    keyboard = _build_inline_keyboard(SCHEDULE_OPTIONS, "sched")
    await update.message.reply_text(
        "Формат работы:",
        reply_markup=keyboard,
    )
    return SCHEDULE


async def receive_salary_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    data["salary_from"] = None
    keyboard = _build_inline_keyboard(SCHEDULE_OPTIONS, "sched")
    await query.edit_message_text(
        "Формат работы:",
        reply_markup=keyboard,
    )
    return SCHEDULE


# --- Step 7: schedule ---
async def receive_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    _, val = query.data.split(":", 1)
    data["work_format"] = None if val == "skip" else val
    keyboard = _build_inline_keyboard(PERIOD_OPTIONS, "period")
    await query.edit_message_text(
        "Период:",
        reply_markup=keyboard,
    )
    return PERIOD


# --- Step 8: period ---
async def receive_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    _, val = query.data.split(":", 1)
    data["period"] = val
    keyboard = _build_inline_keyboard(SAVE_FILTER_OPTIONS, "save")
    await query.edit_message_text(
        "Сохранить фильтр для последующего использования?",
        reply_markup=keyboard,
    )
    return SAVE_FILTER


# --- Step 9: save filter ---
async def receive_save_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    _, val = query.data.split(":", 1)

    if val == "skip":
        keyboard = _build_inline_keyboard(RUN_SEARCH_BUTTON, "run")
        await query.edit_message_text(
            "Всё готово! Нажмите кнопку для начала поиска вакансий:",
            reply_markup=keyboard,
        )
        return RUN_SEARCH

    if val == "save":
        keyboard = _build_inline_keyboard(MONITORING_OPTIONS, "mon")
        await query.edit_message_text(
            "Хотите получать уведомления по сохраненным параметрам поиска?\n"
            "Вы будете получать новые вакансии, как только они опубликованы на HH.",
            reply_markup=keyboard,
        )
        return MONITORING

    keyboard = _build_inline_keyboard(RUN_SEARCH_BUTTON, "run")
    await query.edit_message_text(
        "Всё готово! Нажмите кнопку для начала поиска вакансий:",
        reply_markup=keyboard,
    )
    return RUN_SEARCH


# --- Step 10: monitoring (only when user chose to save filter) ---
async def receive_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    _, val = query.data.split(":", 1)

    prefix = ""
    user = context.user_data.get("search_user")
    if user:
        try:
            name_parts = []
            if data.get("title_keywords"):
                name_parts.append(data["title_keywords"][:30])
            if data.get("city"):
                name_parts.append(data["city"])
            filter_name = " ".join(name_parts) if name_parts else f"Поиск {datetime.now().strftime('%d.%m.%Y')}"
            monitoring_enabled = val == "filter_monitoring_yes"
            save_user_filter(
                user.id,
                filter_name,
                title_keywords=data.get("title_keywords"),
                title_exclude_keywords=data.get("title_exclude_keywords"),
                description_keywords=data.get("description_keywords"),
                description_exclude_keywords=data.get("description_exclude_keywords"),
                city=data.get("city"),
                work_format=data.get("work_format"),
                salary_from=data.get("salary_from"),
                monitoring_enabled=monitoring_enabled,
            )
            prefix = "Фильтр сохранён в Сохраненные фильтры.\n\n"
        except Exception as e:
            logger.exception("Failed to save filter: %s", e)
            prefix = f"Ошибка при сохранении: {e}\n\n"

    keyboard = _build_inline_keyboard(RUN_SEARCH_BUTTON, "run")
    await query.edit_message_text(
        f"{prefix}Всё готово! Нажмите кнопку для начала поиска вакансий:",
        reply_markup=keyboard,
    )
    return RUN_SEARCH


# --- Final: run search ---
async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute search and show first page with pagination."""
    query = update.callback_query
    await query.answer()

    data = _get_search_data(context)
    search_params, filter_obj = _data_to_search_params(data)
    period = search_params.get("period", 30)

    await query.edit_message_text("Ищу вакансии...")

    try:
        found, vacancies = search_vacancies_page(0, search_params, filter_obj)
    except Exception as e:
        logger.exception("Search failed: %s", e)
        await query.edit_message_text(f"Ошибка при запросе к HH: {e}")
        _clear_search_data(context)
        return ConversationHandler.END

    if found == 0:
        await query.edit_message_text("По вашему запросу вакансии не найдены.")
        _clear_search_data(context)
        return ConversationHandler.END

    _store_search_state(context, search_params, filter_obj, period)
    success = await fetch_and_show_page(
        context, 0, search_params, filter_obj, period,
        query.message.chat_id, query.message.message_id,
    )
    if not success:
        await query.edit_message_text("По вашему запросу вакансии не найдены.")
        _clear_search_data(context)
        return ConversationHandler.END

    _clear_search_data(context)
    return ConversationHandler.END


async def cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_search_data(context)
    msg = update.message or (update.callback_query and update.callback_query.message)
    if msg:
        await msg.reply_text("Поиск отменён.")
    return ConversationHandler.END


def build_search_conversation_handler(ensure_user_fn):
    """Build ConversationHandler for /search - interactive filter setup then one-time search."""

    async def wrapped_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user, err = ensure_user_fn(update)
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END
        context.user_data["search_user"] = user
        return await search_entry(update, context)

    return ConversationHandler(
        entry_points=[
            CommandHandler("search", wrapped_entry),
            CommandHandler("jobs", wrapped_entry),
            MessageHandler(filters.Regex("^🔍 Поиск вакансий$"), wrapped_entry),
        ],
        states={
            TITLE_KEYWORDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title_keywords),
                CallbackQueryHandler(receive_title_keywords_skip, pattern=r"^title_kw:skip"),
            ],
            TITLE_EXCLUDE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title_exclude),
                CallbackQueryHandler(receive_title_exclude_skip, pattern=r"^title_ex:skip"),
            ],
            DESC_KEYWORDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc_keywords),
                CallbackQueryHandler(receive_desc_keywords_skip, pattern=r"^desc_kw:skip"),
            ],
            DESC_EXCLUDE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc_exclude),
                CallbackQueryHandler(receive_desc_exclude_skip, pattern=r"^desc_ex:skip"),
            ],
            CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_city),
                CallbackQueryHandler(receive_city_skip, pattern=r"^city:skip"),
            ],
            SALARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_salary),
                CallbackQueryHandler(receive_salary_skip, pattern=r"^salary:skip"),
            ],
            SCHEDULE: [CallbackQueryHandler(receive_schedule, pattern=r"^sched:")],
            PERIOD: [CallbackQueryHandler(receive_period, pattern=r"^period:")],
            SAVE_FILTER: [CallbackQueryHandler(receive_save_filter, pattern=r"^save:")],
            MONITORING: [CallbackQueryHandler(receive_monitoring, pattern=r"^mon:")],
            RUN_SEARCH: [CallbackQueryHandler(run_search, pattern=r"^run:run")],
        },
        fallbacks=[CommandHandler("cancel", cancel_search)],
        allow_reentry=True,
        conversation_timeout=300,
    )
