"""
Мои сохраненные фильтры - list as buttons, filter detail with Начать поиск / Удалить фильтр.
NO plain text list. NO /delete_filter. Inline keyboards only.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from app.hh_api import _search_params_from_filter, search_vacancies_page
from app.vacancy_results import _store_search_state, fetch_and_show_page, handle_vacancy_page_callback
from app.user_repository import (
    delete_user_filter,
    get_user_filter_by_id,
    get_user_filters,
    update_filter_monitoring,
)

logger = logging.getLogger(__name__)

# Callback pattern as specified
CB_FILTER = "saved_filter"
CB_SEARCH = "saved_filter_search"
CB_DELETE = "saved_filter_delete"
CB_MONITOR_ON = "saved_filter_mon_on"
CB_MONITOR_OFF = "saved_filter_mon_off"
CB_VACANCY_PAGE = "vacancy_page"

MAX_BUTTON_LABEL = 40


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


def _build_filter_list_keyboard(filters_list: list) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            (f.name[:MAX_BUTTON_LABEL] + "…" if len(f.name) > MAX_BUTTON_LABEL else f.name),
            callback_data=f"{CB_FILTER}:{f.id}",
        )]
        for f in filters_list
    ]
    return InlineKeyboardMarkup(buttons)


def _build_filter_detail_keyboard(filter_id: int, monitoring_enabled: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("Начать поиск", callback_data=f"{CB_SEARCH}:{filter_id}")],
    ]
    if monitoring_enabled:
        buttons.append([InlineKeyboardButton("Отключить уведомления о новых вакансиях", callback_data=f"{CB_MONITOR_OFF}:{filter_id}")])
    else:
        buttons.append([InlineKeyboardButton("Включить уведомления о новых вакансиях", callback_data=f"{CB_MONITOR_ON}:{filter_id}")])
    buttons.append([InlineKeyboardButton("Удалить фильтр", callback_data=f"{CB_DELETE}:{filter_id}")])
    return InlineKeyboardMarkup(buttons)


async def filters_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, ensure_user_fn):
    """Show Мои сохраненные фильтры - ONLY inline keyboard with filter buttons."""
    logger.info("[FILTERS] Opening saved filters list")
    user, err = ensure_user_fn(update)
    if err:
        await update.message.reply_text(err)
        return

    try:
        filters_list = get_user_filters(user.id)
    except Exception as e:
        logger.exception("get_user_filters failed: %s", e)
        await update.message.reply_text(f"Ошибка при загрузке фильтров: {e}")
        return

    if not filters_list:
        await update.message.reply_text(
            "У вас пока нет сохранённых фильтров.\n\n"
            "Используйте /search для создания фильтра."
        )
        return

    keyboard = _build_filter_list_keyboard(filters_list)
    await update.message.reply_text(
        "💾 Мои сохраненные фильтры",
        reply_markup=keyboard,
    )


async def callback_filter_select(update: Update, context: ContextTypes.DEFAULT_TYPE, ensure_user_fn):
    """User tapped filter button - show filter detail with Начать поиск and Удалить фильтр."""
    query = update.callback_query
    logger.info("[FILTERS] Opening filter detail, callback_data=%s", query.data)
    await query.answer()

    user, err = ensure_user_fn(update)
    if err:
        await query.edit_message_text(err)
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        await query.edit_message_text("Ошибка.")
        return
    try:
        filter_id = int(parts[1])
    except ValueError:
        await query.edit_message_text("Ошибка.")
        return

    f = get_user_filter_by_id(filter_id, user.id)
    if not f:
        await query.edit_message_text("Фильтр не найден.")
        return

    text = _format_filter_detail(f)
    keyboard = _build_filter_detail_keyboard(filter_id, f.monitoring_enabled)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def callback_filter_search(update: Update, context: ContextTypes.DEFAULT_TYPE, ensure_user_fn):
    """User tapped Начать поиск - run vacancy search with this filter."""
    query = update.callback_query
    await query.answer()

    user, err = ensure_user_fn(update)
    if err:
        await query.edit_message_text(err)
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        await query.edit_message_text("Ошибка.")
        return
    try:
        filter_id = int(parts[1])
    except ValueError:
        await query.edit_message_text("Ошибка.")
        return

    f = get_user_filter_by_id(filter_id, user.id)
    if not f:
        await query.edit_message_text("Фильтр не найден.")
        return

    await query.edit_message_text("Ищу вакансии...")

    try:
        search_params = _search_params_from_filter(f)
        search_params["period"] = 30
        found, vacancies = search_vacancies_page(0, search_params, filter_obj=f)
    except Exception as e:
        logger.exception("Search failed: %s", e)
        await query.edit_message_text(f"Ошибка при запросе к HH: {e}")
        return

    if found == 0:
        await query.edit_message_text("По вашему запросу вакансии не найдены.")
        return

    period = 30
    _store_search_state(context, search_params, f, period)
    success = await fetch_and_show_page(
        context, 0, search_params, f, period,
        query.message.chat_id, query.message.message_id,
    )
    if not success:
        await query.edit_message_text("По вашему запросу вакансии не найдены.")


async def callback_filter_monitor_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, ensure_user_fn, enable: bool):
    """User tapped Включить/Отключить мониторинг."""
    query = update.callback_query
    await query.answer()

    user, err = ensure_user_fn(update)
    if err:
        await query.edit_message_text(err)
        return

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        await query.edit_message_text("Ошибка.")
        return
    try:
        filter_id = int(parts[1])
    except ValueError:
        await query.edit_message_text("Ошибка.")
        return

    updated = update_filter_monitoring(filter_id, user.id, enable)
    if not updated:
        await query.edit_message_text("Фильтр не найден.")
        return

    f = get_user_filter_by_id(filter_id, user.id)
    if not f:
        await query.edit_message_text("Фильтр не найден.")
        return

    status = "включены" if enable else "выключены"
    text = _format_filter_detail(f)
    keyboard = _build_filter_detail_keyboard(filter_id, f.monitoring_enabled)
    await query.edit_message_text(
        f"Уведомления о новых вакансиях {status}.\n\n{text}",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def callback_filter_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, ensure_user_fn):
    """User tapped Удалить фильтр - delete from DB, show confirmation, return to list."""
    query = update.callback_query
    logger.info("[FILTERS] Delete button pressed, callback_data=%s", query.data)
    await query.answer()

    try:
        user, err = ensure_user_fn(update)
        if err:
            logger.warning("[FILTERS] ensure_user failed: %s", err)
            await query.edit_message_text(err)
            return

        parts = query.data.split(":", 1)
        if len(parts) != 2:
            logger.warning("[FILTERS] Invalid callback_data: %s", query.data)
            await query.edit_message_text("Ошибка.")
            return
        try:
            filter_id = int(parts[1])
        except ValueError:
            logger.warning("[FILTERS] Invalid filter_id in: %s", query.data)
            await query.edit_message_text("Ошибка.")
            return

        logger.info("[FILTERS] Deleting filter_id=%s for user_id=%s", filter_id, user.id)
        deleted = delete_user_filter(filter_id, user.id)
        if not deleted:
            logger.warning("[FILTERS] Filter %s not found or not owned", filter_id)
            await query.edit_message_text("Фильтр не найден.")
            return

        logger.info("[FILTERS] Filter %s deleted, reloading list", filter_id)
        filters_list = get_user_filters(user.id)
        if not filters_list:
            await query.edit_message_text("Фильтр удалён\n\nСохранённых фильтров нет")
            logger.info("[FILTERS] No filters left, showing empty state")
            return

        keyboard = _build_filter_list_keyboard(filters_list)
        await query.edit_message_text(
            "Фильтр удалён\n\n💾 Мои сохраненные фильтры",
            reply_markup=keyboard,
        )
        logger.info("[FILTERS] Showing updated list with %d filters", len(filters_list))
    except Exception as e:
        logger.exception("[FILTERS] Delete failed: %s", e)
        await query.edit_message_text(f"Ошибка: {e}")


def build_filters_handlers(ensure_user_fn):
    async def wrap_filters(u, c):
        return await filters_cmd(u, c, ensure_user_fn)

    async def wrap_select(u, c):
        return await callback_filter_select(u, c, ensure_user_fn)

    async def wrap_search(u, c):
        return await callback_filter_search(u, c, ensure_user_fn)

    async def wrap_delete(u, c):
        return await callback_filter_delete(u, c, ensure_user_fn)

    async def wrap_mon_on(u, c):
        return await callback_filter_monitor_toggle(u, c, ensure_user_fn, enable=True)

    async def wrap_mon_off(u, c):
        return await callback_filter_monitor_toggle(u, c, ensure_user_fn, enable=False)

    async def wrap_vacancy_page(u, c):
        user, err = ensure_user_fn(u)
        if err:
            if u.callback_query:
                await u.callback_query.answer()
                await u.callback_query.edit_message_text(err)
            return
        await handle_vacancy_page_callback(u, c)

    return [
        CommandHandler("filters", wrap_filters),
        MessageHandler(filters.Regex("^💾 Мои фильтры$"), wrap_filters),
        CallbackQueryHandler(wrap_select, pattern=rf"^{CB_FILTER}:"),
        CallbackQueryHandler(wrap_search, pattern=rf"^{CB_SEARCH}:"),
        CallbackQueryHandler(wrap_mon_on, pattern=rf"^{CB_MONITOR_ON}:"),
        CallbackQueryHandler(wrap_mon_off, pattern=rf"^{CB_MONITOR_OFF}:"),
        CallbackQueryHandler(wrap_delete, pattern=rf"^{CB_DELETE}:"),
        CallbackQueryHandler(wrap_vacancy_page, pattern=rf"^{CB_VACANCY_PAGE}:"),
    ]
