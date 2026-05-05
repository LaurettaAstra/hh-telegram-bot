"""
Interactive step-by-step search flow for "Поиск вакансий".
Collects filters, optionally saves to Сохраненные фильтры, then runs search on "Начать поиск".
"""

import logging
from types import SimpleNamespace

from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from app.hh_api import (
    HHApiError,
    HHAppTokenConfigurationError,
    HHVacanciesForbiddenError,
    _build_search_text,
    search_vacancies_page,
    vacancy_hh_error_user_message,
)
from app.hh_auth import respond_to_vacancies_forbidden
from app.user_repository import (
    USER_FRIENDLY_ERROR,
    get_user_filter_by_id,
    save_user_filter,
    update_user_filter,
)
from app.vacancy_results import _store_search_state, fetch_and_show_page

logger = logging.getLogger(__name__)

# Name for ConversationHandler so we can end this conversation when switching flows.
SEARCH_CONVERSATION_NAME = "search_flow"


async def reset_search_conversation(application, update: Update) -> None:
    """End search wizard state so other menus do not see stale wizard steps (PTB ConversationHandler)."""
    for handlers in application.handlers.values():
        for h in handlers:
            if isinstance(h, ConversationHandler) and getattr(h, "name", None) == SEARCH_CONVERSATION_NAME:
                key = h._get_key(update)
                async with h._timeout_jobs_lock:
                    job = h.timeout_jobs.pop(key, None)
                    if job is not None:
                        job.schedule_removal()
                h._update_state(ConversationHandler.END, key)
                return


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
EDIT_SAVE_OPTIONS = [
    ("save", "Сохранить"),
    ("cancel", "Отменить"),
]
BACK_OPTION = ("back", "Назад")
NAV_PREFIX = "nav"


def _get_search_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    key = "search_data"
    if key not in context.user_data:
        context.user_data[key] = {}
    return context.user_data[key]


def _clear_search_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("search_data", None)


def _clear_edit_mode(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("edit_filter_id", None)


def _is_edit_mode(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("edit_filter_id"))


def _clear_wizard_nav_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("search_current_state", None)
    context.user_data.pop("run_entry_state", None)


def _build_inline_keyboard(
    options: list[tuple[str, str]],
    prefix: str,
    include_back: bool = True,
) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:{val}")]
        for val, label in options
    ]
    if include_back:
        buttons.append([InlineKeyboardButton(BACK_OPTION[1], callback_data=f"{NAV_PREFIX}:{BACK_OPTION[0]}")])
    return InlineKeyboardMarkup(buttons)


def _value_or_dash(value) -> str:
    if value is None:
        return "не задано"
    if isinstance(value, str) and not value.strip():
        return "не задано"
    return str(value)


def _schedule_label(schedule_code: str | None) -> str:
    for val, label in SCHEDULE_OPTIONS:
        if val == schedule_code:
            return label
    return "не задано"


def _period_label(period_value: str | int | None) -> str:
    if period_value is None:
        return "за месяц"
    period_str = str(period_value)
    for val, label in PERIOD_OPTIONS:
        if val == period_str:
            return label
    return "за месяц"


def _prompt_with_current(prompt: str, current_value) -> str:
    return f"Текущее значение: {_value_or_dash(current_value)}\n{prompt}"


def _format_filter_detail(f) -> str:
    lines = [f"<b>{f.name}</b>"]
    if f.title_keywords:
        lines.append(f"Название: {f.title_keywords}")
    if f.title_exclude_keywords:
        lines.append(f"Исключить в названии: {f.title_exclude_keywords}")
    if f.description_keywords:
        lines.append(f"Тело: {f.description_keywords}")
    if f.description_exclude_keywords:
        lines.append(f"Исключить в теле: {f.description_exclude_keywords}")
    if f.city:
        lines.append(f"Город: {f.city}")
    if f.salary_from:
        lines.append(f"Зарплата от: {f.salary_from}")
    if f.work_format:
        lines.append(f"Формат: {f.work_format}")
    mon_status = "включены" if f.monitoring_enabled else "выключены"
    lines.append(f"Уведомления о новых вакансиях: {mon_status}")
    return "\n".join(lines)


def _build_filter_detail_keyboard(filter_id: int, monitoring_enabled: bool) -> InlineKeyboardMarkup:
    from app.filters_handlers import CB_DELETE, CB_EDIT, CB_MONITOR_OFF, CB_MONITOR_ON, CB_SEARCH

    buttons = [
        [InlineKeyboardButton("Начать поиск", callback_data=f"{CB_SEARCH}:{filter_id}")],
        [InlineKeyboardButton("Редактировать", callback_data=f"{CB_EDIT}:{filter_id}")],
    ]
    if monitoring_enabled:
        buttons.append([InlineKeyboardButton("Отключить уведомления", callback_data=f"{CB_MONITOR_OFF}:{filter_id}")])
    else:
        buttons.append([InlineKeyboardButton("Уведомления", callback_data=f"{CB_MONITOR_ON}:{filter_id}")])
    buttons.append([InlineKeyboardButton("Удалить фильтр", callback_data=f"{CB_DELETE}:{filter_id}")])
    return InlineKeyboardMarkup(buttons)


def _step_text_and_keyboard(context: ContextTypes.DEFAULT_TYPE, state: int) -> tuple[str, InlineKeyboardMarkup]:
    data = _get_search_data(context)

    if state == TITLE_KEYWORDS:
        text = _prompt_with_current(
            "Введите ключевые слова в названии вакансии или нажмите «Пропустить»:",
            data.get("title_keywords"),
        ) if _is_edit_mode(context) else "Введите ключевые слова в названии вакансии:"
        return text, _build_inline_keyboard(TEXT_SKIP, "title_kw")
    if state == TITLE_EXCLUDE:
        text = _prompt_with_current(
            "Исключить слова в названии вакансии или нажмите «Пропустить»:",
            data.get("title_exclude_keywords"),
        ) if _is_edit_mode(context) else "Исключить слова в названии вакансии:"
        return text, _build_inline_keyboard(TEXT_SKIP, "title_ex")
    if state == DESC_KEYWORDS:
        text = _prompt_with_current(
            "Ключевые слова в ТЕЛЕ вакансии или нажмите «Пропустить»:",
            data.get("description_keywords"),
        ) if _is_edit_mode(context) else "Ключевые слова в ТЕЛЕ вакансии:"
        return text, _build_inline_keyboard(TEXT_SKIP, "desc_kw")
    if state == DESC_EXCLUDE:
        text = _prompt_with_current(
            "Исключить слова в ТЕЛЕ вакансии или нажмите «Пропустить»:",
            data.get("description_exclude_keywords"),
        ) if _is_edit_mode(context) else "Исключить слова в ТЕЛЕ  вакансии:"
        return text, _build_inline_keyboard(TEXT_SKIP, "desc_ex")
    if state == CITY:
        text = _prompt_with_current(
            "Введите город или регион или нажмите «Пропустить»:",
            data.get("city"),
        ) if _is_edit_mode(context) else "Введите город или регион:"
        return text, _build_inline_keyboard(TEXT_SKIP, "city")
    if state == SALARY:
        text = _prompt_with_current(
            "Желаемая зарплата (числом) или нажмите «Не указывать»:",
            data.get("salary_from"),
        ) if _is_edit_mode(context) else "Желаемая зарплата:"
        return text, _build_inline_keyboard(SALARY_SKIP, "salary")
    if state == SCHEDULE:
        text = _prompt_with_current(
            "Формат работы:",
            _schedule_label(data.get("work_format")),
        ) if _is_edit_mode(context) else "Формат работы:"
        return text, _build_inline_keyboard(SCHEDULE_OPTIONS, "sched")
    if state == PERIOD:
        text = _prompt_with_current(
            "Период:",
            _period_label(data.get("period")),
        ) if _is_edit_mode(context) else "Период:"
        return text, _build_inline_keyboard(PERIOD_OPTIONS, "period")
    if state == SAVE_FILTER:
        if _is_edit_mode(context):
            return "Сохранить изменения фильтра?", _build_inline_keyboard(EDIT_SAVE_OPTIONS, "edit")
        return "Сохранить фильтр для последующего использования?", _build_inline_keyboard(SAVE_FILTER_OPTIONS, "save")
    if state == MONITORING:
        text = (
            "Хотите получать уведомления по сохраненным параметрам поиска?\n"
            "Вы будете получать новые вакансии, как только они опубликованы на HH."
        )
        return text, _build_inline_keyboard(MONITORING_OPTIONS, "mon")
    if state == RUN_SEARCH:
        return "Всё готово! Нажмите кнопку для начала поиска вакансий:", _build_inline_keyboard(RUN_SEARCH_BUTTON, "run")
    raise ValueError(f"Unsupported state for render: {state}")


async def _show_state(update: Update, context: ContextTypes.DEFAULT_TYPE, state: int, from_callback: bool) -> int:
    text, keyboard = _step_text_and_keyboard(context, state)
    if from_callback:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)
    context.user_data["search_current_state"] = state
    return state


def _previous_state(current_state: int, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if current_state == TITLE_KEYWORDS:
        return None
    if current_state == TITLE_EXCLUDE:
        return TITLE_KEYWORDS
    if current_state == DESC_KEYWORDS:
        return TITLE_EXCLUDE
    if current_state == DESC_EXCLUDE:
        return DESC_KEYWORDS
    if current_state == CITY:
        return DESC_EXCLUDE
    if current_state == SALARY:
        return CITY
    if current_state == SCHEDULE:
        return SALARY
    if current_state == PERIOD:
        return SCHEDULE
    if current_state == SAVE_FILTER:
        if _is_edit_mode(context):
            return SCHEDULE
        return PERIOD
    if current_state == MONITORING:
        return SAVE_FILTER
    if current_state == RUN_SEARCH:
        return context.user_data.get("run_entry_state", SAVE_FILTER)
    return None


def _data_to_search_params(data: dict) -> tuple[dict, SimpleNamespace]:
    """Build HH API search_params and filter_obj from collected data."""
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
    _clear_edit_mode(context)
    _clear_wizard_nav_data(context)
    return await _show_state(update, context, TITLE_KEYWORDS, from_callback=False)


def _set_search_conversation_state(application, update: Update, state: int) -> None:
    for handlers in application.handlers.values():
        for h in handlers:
            if isinstance(h, ConversationHandler) and getattr(h, "name", None) == SEARCH_CONVERSATION_NAME:
                key = h._get_key(update)
                h._update_state(state, key)
                return


async def begin_filter_edit_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, user, saved_filter) -> int:
    """Start /search wizard in edit mode from saved-filter card callback."""
    _clear_search_data(context)
    _clear_wizard_nav_data(context)
    context.user_data["search_user"] = user
    context.user_data["edit_filter_id"] = saved_filter.id

    data = _get_search_data(context)
    data["title_keywords"] = saved_filter.title_keywords
    data["title_exclude_keywords"] = saved_filter.title_exclude_keywords
    data["description_keywords"] = saved_filter.description_keywords
    data["description_exclude_keywords"] = saved_filter.description_exclude_keywords
    data["city"] = saved_filter.city
    data["salary_from"] = saved_filter.salary_from
    data["work_format"] = saved_filter.work_format

    _set_search_conversation_state(context.application, update, TITLE_KEYWORDS)
    return await _show_state(update, context, TITLE_KEYWORDS, from_callback=True)


# --- Step 1: title keywords ---
async def receive_title_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["title_keywords"] = (update.message.text or "").strip() or None
    return await _show_state(update, context, TITLE_EXCLUDE, from_callback=False)


async def receive_title_keywords_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    if not _is_edit_mode(context):
        data["title_keywords"] = None
    return await _show_state(update, context, TITLE_EXCLUDE, from_callback=True)


# --- Step 2: title exclude ---
async def receive_title_exclude(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["title_exclude_keywords"] = (update.message.text or "").strip() or None
    return await _show_state(update, context, DESC_KEYWORDS, from_callback=False)


async def receive_title_exclude_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    if not _is_edit_mode(context):
        data["title_exclude_keywords"] = None
    return await _show_state(update, context, DESC_KEYWORDS, from_callback=True)


# --- Step 3: desc keywords ---
async def receive_desc_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["description_keywords"] = (update.message.text or "").strip() or None
    return await _show_state(update, context, DESC_EXCLUDE, from_callback=False)


async def receive_desc_keywords_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    if not _is_edit_mode(context):
        data["description_keywords"] = None
    return await _show_state(update, context, DESC_EXCLUDE, from_callback=True)


# --- Step 4: desc exclude ---
async def receive_desc_exclude(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["description_exclude_keywords"] = (update.message.text or "").strip() or None
    return await _show_state(update, context, CITY, from_callback=False)


async def receive_desc_exclude_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    if not _is_edit_mode(context):
        data["description_exclude_keywords"] = None
    return await _show_state(update, context, CITY, from_callback=True)


# --- Step 5: city (text or skip only, no "Ручной ввод") ---
async def receive_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    data["city"] = (update.message.text or "").strip() or None
    return await _show_state(update, context, SALARY, from_callback=False)


async def receive_city_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    if not _is_edit_mode(context):
        data["city"] = None
    return await _show_state(update, context, SALARY, from_callback=True)


# --- Step 6: salary ---
async def receive_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = _get_search_data(context)
    text = (update.message.text or "").strip()
    try:
        data["salary_from"] = int(text.replace(" ", ""))
    except (ValueError, AttributeError):
        await update.message.reply_text("Введите число:")
        return SALARY
    return await _show_state(update, context, SCHEDULE, from_callback=False)


async def receive_salary_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    if not _is_edit_mode(context):
        data["salary_from"] = None
    return await _show_state(update, context, SCHEDULE, from_callback=True)


# --- Step 7: schedule ---
async def receive_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    _, val = query.data.split(":", 1)
    data["work_format"] = None if val == "skip" else val
    if _is_edit_mode(context):
        return await _show_state(update, context, SAVE_FILTER, from_callback=True)
    return await _show_state(update, context, PERIOD, from_callback=True)


# --- Step 8: period ---
async def receive_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    _, val = query.data.split(":", 1)
    data["period"] = val
    return await _show_state(update, context, SAVE_FILTER, from_callback=True)


# --- Step 9: save filter ---
async def receive_save_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = _get_search_data(context)
    _, val = query.data.split(":", 1)

    if _is_edit_mode(context):
        user = context.user_data.get("search_user")
        filter_id = context.user_data.get("edit_filter_id")
        if not user or not filter_id:
            await query.edit_message_text("Сессия редактирования истекла. Попробуйте снова.")
            _clear_search_data(context)
            _clear_edit_mode(context)
            _clear_wizard_nav_data(context)
            return ConversationHandler.END

        if val == "cancel":
            f = get_user_filter_by_id(filter_id, user.id)
            _clear_search_data(context)
            _clear_edit_mode(context)
            _clear_wizard_nav_data(context)
            if not f:
                await query.edit_message_text("Фильтр не найден.")
                return ConversationHandler.END
            await query.edit_message_text(
                _format_filter_detail(f),
                reply_markup=_build_filter_detail_keyboard(f.id, f.monitoring_enabled),
                parse_mode="HTML",
            )
            return ConversationHandler.END

        if val == "save":
            try:
                updated = update_user_filter(
                    filter_id,
                    user.id,
                    title_keywords=data.get("title_keywords"),
                    title_exclude_keywords=data.get("title_exclude_keywords"),
                    description_keywords=data.get("description_keywords"),
                    description_exclude_keywords=data.get("description_exclude_keywords"),
                    city=data.get("city"),
                    work_format=data.get("work_format"),
                    salary_from=data.get("salary_from"),
                )
            except Exception as e:
                logger.exception("Failed to update filter: %s", e)
                _clear_search_data(context)
                _clear_edit_mode(context)
                _clear_wizard_nav_data(context)
                await query.edit_message_text(USER_FRIENDLY_ERROR)
                return ConversationHandler.END

            _clear_search_data(context)
            _clear_edit_mode(context)
            _clear_wizard_nav_data(context)
            if not updated:
                await query.edit_message_text("Фильтр не найден.")
                return ConversationHandler.END
            await query.edit_message_text(
                f"Фильтр обновлён.\n\n{_format_filter_detail(updated)}",
                reply_markup=_build_filter_detail_keyboard(updated.id, updated.monitoring_enabled),
                parse_mode="HTML",
            )
            return ConversationHandler.END

    if val == "skip":
        context.user_data["run_entry_state"] = SAVE_FILTER
        return await _show_state(update, context, RUN_SEARCH, from_callback=True)

    if val == "save":
        return await _show_state(update, context, MONITORING, from_callback=True)

    context.user_data["run_entry_state"] = SAVE_FILTER
    return await _show_state(update, context, RUN_SEARCH, from_callback=True)


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
            prefix = f"{USER_FRIENDLY_ERROR}\n\n"

    context.user_data["run_entry_state"] = MONITORING
    text, keyboard = _step_text_and_keyboard(context, RUN_SEARCH)
    await query.edit_message_text(f"{prefix}{text}", reply_markup=keyboard)
    context.user_data["search_current_state"] = RUN_SEARCH
    return RUN_SEARCH


async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    current_state = context.user_data.get("search_current_state")
    if current_state is None:
        await query.edit_message_text("Сессия поиска истекла. Запустите поиск заново.")
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        return ConversationHandler.END

    prev_state = _previous_state(current_state, context)
    if prev_state is None:
        user = context.user_data.get("search_user")
        filter_id = context.user_data.get("edit_filter_id")
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        if user and filter_id:
            f = get_user_filter_by_id(filter_id, user.id)
            if not f:
                await query.edit_message_text("Фильтр не найден.")
                return ConversationHandler.END
            await query.edit_message_text(
                _format_filter_detail(f),
                reply_markup=_build_filter_detail_keyboard(f.id, f.monitoring_enabled),
                parse_mode="HTML",
            )
            return ConversationHandler.END

        await query.edit_message_text("Поиск отменён.")
        return ConversationHandler.END

    _set_search_conversation_state(context.application, update, prev_state)
    return await _show_state(update, context, prev_state, from_callback=True)


# --- Final: run search ---
async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute search and show first page with pagination."""
    query = update.callback_query

    user = context.user_data.get("search_user")
    if not user:
        await query.answer()
        await query.edit_message_text("Сессия поиска истекла. Запустите поиск заново.")
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        return ConversationHandler.END

    await query.answer()

    data = _get_search_data(context)
    search_params, filter_obj = _data_to_search_params(data)
    period = search_params.get("period", 30)

    await query.edit_message_text("Ищу вакансии...")

    try:
        found, vacancies = search_vacancies_page(
            0,
            search_params,
            filter_obj,
            user_id=user.id,
            source="search_flow.run_search",
        )
    except HHAppTokenConfigurationError as e:
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        await query.edit_message_text(str(e))
        return ConversationHandler.END
    except HHVacanciesForbiddenError as e:
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        await respond_to_vacancies_forbidden(
            update, context, user.id, e, answer_callback=False
        )
        return ConversationHandler.END
    except HHApiError as e:
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        await query.edit_message_text(vacancy_hh_error_user_message(e))
        return ConversationHandler.END
    except Exception as e:
        logger.exception("Search failed: %s", e)
        await query.edit_message_text(USER_FRIENDLY_ERROR)
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        return ConversationHandler.END

    logger.info(
        "[SEARCH_FLOW] run_search first page: found=%s len_vacancies=%s",
        found,
        len(vacancies),
    )
    if not vacancies:
        await query.edit_message_text("По вашему запросу вакансии не найдены.")
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        return ConversationHandler.END

    _store_search_state(context, search_params, filter_obj, period, user_id=user.id)
    try:
        success = await fetch_and_show_page(
            context,
            0,
            search_params,
            filter_obj,
            period,
            query.message.chat_id,
            query.message.message_id,
            user_id=user.id,
            source="search_flow.fetch_page",
        )
    except HHAppTokenConfigurationError as e:
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        await query.edit_message_text(str(e))
        return ConversationHandler.END
    except HHVacanciesForbiddenError as e:
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        await respond_to_vacancies_forbidden(
            update, context, user.id, e, answer_callback=False
        )
        return ConversationHandler.END
    except HHApiError as e:
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        await query.edit_message_text(vacancy_hh_error_user_message(e))
        return ConversationHandler.END
    if not success:
        await query.edit_message_text("По вашему запросу вакансии не найдены.")
        _clear_search_data(context)
        _clear_edit_mode(context)
        _clear_wizard_nav_data(context)
        return ConversationHandler.END

    _clear_search_data(context)
    _clear_edit_mode(context)
    _clear_wizard_nav_data(context)
    return ConversationHandler.END


async def cancel_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = context.user_data.get("search_user")
    filter_id = context.user_data.get("edit_filter_id")
    _clear_search_data(context)
    _clear_edit_mode(context)
    _clear_wizard_nav_data(context)

    if user and filter_id:
        f = get_user_filter_by_id(filter_id, user.id)
        msg = update.message or (update.callback_query and update.callback_query.message)
        if msg and f:
            await msg.reply_text(
                _format_filter_detail(f),
                reply_markup=_build_filter_detail_keyboard(f.id, f.monitoring_enabled),
                parse_mode="HTML",
            )
        elif msg:
            await msg.reply_text("Фильтр не найден.")
        return ConversationHandler.END

    msg = update.message or (update.callback_query and update.callback_query.message)
    if msg:
        await msg.reply_text("Поиск отменён.")
    return ConversationHandler.END


def build_search_conversation_handler(ensure_user_fn):
    """Build ConversationHandler for /search - interactive filter setup then one-time search."""

    async def wrapped_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await reset_search_conversation(context.application, update)
        user, err = ensure_user_fn(update)
        if err:
            await update.message.reply_text(err)
            return ConversationHandler.END
        context.user_data["search_user"] = user
        return await search_entry(update, context)

    return ConversationHandler(
        name=SEARCH_CONVERSATION_NAME,
        entry_points=[
            CommandHandler("search", wrapped_entry),
            CommandHandler("jobs", wrapped_entry),
            MessageHandler(filters.Regex("^🔍 Поиск вакансий$"), wrapped_entry),
        ],
        states={
            TITLE_KEYWORDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title_keywords),
                CallbackQueryHandler(receive_title_keywords_skip, pattern=r"^title_kw:skip"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            TITLE_EXCLUDE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title_exclude),
                CallbackQueryHandler(receive_title_exclude_skip, pattern=r"^title_ex:skip"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            DESC_KEYWORDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc_keywords),
                CallbackQueryHandler(receive_desc_keywords_skip, pattern=r"^desc_kw:skip"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            DESC_EXCLUDE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc_exclude),
                CallbackQueryHandler(receive_desc_exclude_skip, pattern=r"^desc_ex:skip"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_city),
                CallbackQueryHandler(receive_city_skip, pattern=r"^city:skip"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            SALARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_salary),
                CallbackQueryHandler(receive_salary_skip, pattern=r"^salary:skip"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            SCHEDULE: [
                CallbackQueryHandler(receive_schedule, pattern=r"^sched:"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            PERIOD: [
                CallbackQueryHandler(receive_period, pattern=r"^period:"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            SAVE_FILTER: [
                CallbackQueryHandler(receive_save_filter, pattern=r"^(save|edit):"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            MONITORING: [
                CallbackQueryHandler(receive_monitoring, pattern=r"^mon:"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
            RUN_SEARCH: [
                CallbackQueryHandler(run_search, pattern=r"^run:run"),
                CallbackQueryHandler(handle_back, pattern=r"^nav:back$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_search)],
        allow_reentry=True,
        conversation_timeout=300,
    )
