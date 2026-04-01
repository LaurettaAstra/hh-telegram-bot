"""
Shared logic for vacancy search results + pagination.
Used by wizard search and saved filters search.
"""

import logging
import math

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.hh_api import search_vacancies_page
from app.notifier import format_vacancies_page_header, format_vacancy_single_message

logger = logging.getLogger(__name__)

CB_VACANCY_PAGE = "vacancy_page"
PER_PAGE = 10


def _build_pagination_keyboard(total: int, current_page: int) -> InlineKeyboardMarkup:
    """Build ◀ Назад | Стр. X / Y | Вперёд ▶ keyboard."""
    pages = max(1, math.ceil(total / PER_PAGE))
    buttons = []
    row = []
    if current_page > 0:
        row.append(InlineKeyboardButton("◀ Назад", callback_data=f"{CB_VACANCY_PAGE}:{current_page - 1}"))
    row.append(InlineKeyboardButton(
        f"Стр. {current_page + 1} / {pages}",
        callback_data=f"{CB_VACANCY_PAGE}:{current_page}",  # no-op, same page
    ))
    if current_page < pages - 1:
        row.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"{CB_VACANCY_PAGE}:{current_page + 1}"))
    buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def _get_search_state(context) -> tuple[dict, object, int] | None:
    """Get stored search state from user_data. Returns (search_params, filter_obj, period) or None."""
    params = context.user_data.get("vacancy_search_params")
    period = context.user_data.get("vacancy_period")
    if params is None or period is None:
        return None
    filter_obj = context.user_data.get("vacancy_filter_obj")
    return params, filter_obj, period


def _store_search_state(context, search_params: dict, filter_obj, period: int) -> None:
    """Store search state for pagination callbacks."""
    context.user_data["vacancy_search_params"] = search_params
    context.user_data["vacancy_filter_obj"] = filter_obj
    context.user_data["vacancy_period"] = period


async def fetch_and_show_page(
    context,
    page: int,
    search_params: dict,
    filter_obj,
    period: int,
    chat_id: int,
    message_id: int,
) -> bool:
    """
    Fetch page from HH API, edit header with pagination, send each vacancy as separate message,
    send footer with pagination. Buttons appear both above and below the list.
    Returns True on success, False if no vacancies.
    """
    try:
        found, vacancies = search_vacancies_page(page, search_params, filter_obj)
    except Exception as e:
        logger.exception("search_vacancies_page failed: %s", e)
        return False

    logger.info(
        "[VACANCY_UI] fetch_and_show_page page=%s found=%s len_vacancies=%s",
        page,
        found,
        len(vacancies),
    )
    if not vacancies:
        logger.info(
            "[VACANCY_UI] fetch_and_show_page: empty vacancies list — nothing to send (found=%s)",
            found,
        )
        return False

    header_text = format_vacancies_page_header(found, period, page)
    keyboard = _build_pagination_keyboard(found, page)

    header_id = context.user_data.get("vacancy_header_message_id") or message_id
    footer_id = context.user_data.get("vacancy_footer_message_id")

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=header_id,
            text=header_text,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.exception("edit_message_text failed: %s", e)
        return False

    for vacancy in vacancies[:10]:
        text = format_vacancy_single_message(vacancy)
        await context.bot.send_message(chat_id=chat_id, text=text)

    if footer_id:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=footer_id,
            text=header_text,
            reply_markup=keyboard,
        )
    else:
        footer_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=header_text,
            reply_markup=keyboard,
        )
        context.user_data["vacancy_header_message_id"] = header_id
        context.user_data["vacancy_footer_message_id"] = footer_msg.message_id

    return True


async def handle_vacancy_page_callback(update, context):
    """Handle vacancy_page:N callback - switch to page N."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        return
    try:
        page = int(parts[1])
    except ValueError:
        return

    if page < 0:
        return

    state = _get_search_state(context)
    if not state:
        await query.edit_message_text("Сессия поиска истекла. Запустите поиск заново.")
        return

    search_params, filter_obj, period = state

    success = await fetch_and_show_page(
        context,
        page,
        search_params,
        filter_obj,
        period,
        query.message.chat_id,
        query.message.message_id,
    )

    if not success:
        await query.edit_message_text("Не удалось загрузить страницу. Попробуйте поиск заново.")
