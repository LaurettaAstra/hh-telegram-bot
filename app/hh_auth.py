"""
HH OAuth token checks and Telegram prompts for re-authorization.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.hh_api import HHAuthorizationError, HHVacanciesForbiddenError, get_hh_authorize_url, refresh_user_hh_token
from app.user_repository import (
    clear_user_hh_tokens,
    get_user_by_id,
    list_users_expired_hh_pending_notification,
    set_hh_reauth_notified,
)

logger = logging.getLogger(__name__)

# GET /vacancies uses application OAuth; 403 means HH blocked this app for vacancy search.
HH_APP_BLOCKED_VACANCY_SEARCH_MSG = (
    "HH.ru blocked vacancy search for the application. Please check HH application access/settings."
)


def is_hh_token_valid(user) -> bool:
    """
    Return False if access token is missing or expiration time has passed.
    """
    if user is None or not getattr(user, "hh_access_token", None):
        return False
    expires_at = getattr(user, "hh_expires_at", None)
    if expires_at is None:
        return True
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now < expires_at


def ensure_hh_api_access_sync(user_id: int) -> bool:
    """
    Return True if HH API may be called for this user: valid access token,
    or successfully refreshed via refresh_token.
    """
    user = get_user_by_id(user_id)
    if user is None:
        return False
    if is_hh_token_valid(user):
        return True
    if not user.hh_refresh_token:
        return False
    try:
        refresh_user_hh_token(user_id)
    except HHAuthorizationError as e:
        logger.info("[HH_AUTH] refresh failed user_id=%s: %s", user_id, e)
        return False
    user = get_user_by_id(user_id)
    return bool(user and is_hh_token_valid(user))


async def send_hh_authorization_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user,
    *,
    answer_callback: bool = True,
) -> None:
    """Send Telegram message with HH OAuth link (same UX as require_hh_auth denial path)."""
    try:
        auth_url = get_hh_authorize_url(user.id)
    except HHAuthorizationError as e:
        logger.exception("[HH_AUTH] authorize URL failed: %s", e)
        text = "Не удалось сформировать ссылку авторизации HH."
        if answer_callback and update.callback_query:
            await update.callback_query.answer()
        if update.callback_query:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
        elif update.message:
            await update.message.reply_text(text)
        elif update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text)
        return

    text = "To continue using the bot, please authorize via HH.ru"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔐 Authorize HH", url=auth_url)]]
    )

    if answer_callback and update.callback_query:
        await update.callback_query.answer()
    if update.callback_query:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=keyboard,
        )
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    else:
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
            )


async def notify_hh_access_revoked(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    *,
    answer_callback: bool = True,
) -> None:
    """Clear stored HH tokens and prompt OAuth (e.g. after HTTP 403 from HH API)."""
    clear_user_hh_tokens(user_id)
    fresh = get_user_by_id(user_id)
    if fresh:
        await send_hh_authorization_prompt(
            update, context, fresh, answer_callback=answer_callback
        )


async def respond_to_vacancies_forbidden(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    exc: HHVacanciesForbiddenError,
    *,
    answer_callback: bool = True,
) -> None:
    """
    Generic vacancy 403 without oauth hints: keep tokens, explain dev.hh.ru.
    OAuth/token errors: clear tokens and offer re-authorization.
    """
    if exc.prompt_reauthorize:
        logger.info("[HH_AUTH_UI] user_id=%s vacancies_403 reprompt_reauthorize=true", user_id)
        await notify_hh_access_revoked(
            update, context, user_id, answer_callback=answer_callback
        )
        return
    logger.info("[HH_AUTH_UI] user_id=%s vacancies_403 reprompt_reauthorize=false app_policy_message", user_id)
    text = HH_APP_BLOCKED_VACANCY_SEARCH_MSG
    if answer_callback and update.callback_query:
        await update.callback_query.answer()
    chat = update.effective_chat
    if chat:
        await context.bot.send_message(chat_id=chat.id, text=text)
    elif update.message:
        await update.message.reply_text(text)


async def require_hh_auth(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user,
) -> bool:
    """
    If token is valid (optionally after silent refresh), return True.
    Otherwise send authorization prompt with OAuth link and return False.
    """
    fresh = get_user_by_id(user.id)
    if fresh is None:
        return False

    if not is_hh_token_valid(fresh) and fresh.hh_refresh_token:
        try:
            await asyncio.to_thread(refresh_user_hh_token, user.id)
        except HHAuthorizationError:
            pass
        fresh = get_user_by_id(user.id)

    if fresh and is_hh_token_valid(fresh):
        return True

    await send_hh_authorization_prompt(update, context, fresh or user)
    return False


async def run_hh_reauth_notification_pass(bot) -> None:
    """
    For users with expired HH tokens, try silent refresh; if still invalid, send a one-time
    Telegram message with the OAuth link (guarded by hh_reauth_notified in the DB).
    """
    try:
        candidates = list_users_expired_hh_pending_notification()
    except Exception as e:
        logger.exception("[HH_REAUTH] list candidates failed: %s", e)
        return

    for row in candidates:
        if ensure_hh_api_access_sync(row.id):
            continue
        fresh = get_user_by_id(row.id)
        if not fresh or fresh.hh_reauth_notified:
            continue
        try:
            auth_url = get_hh_authorize_url(fresh.id)
        except HHAuthorizationError as e:
            logger.warning("[HH_REAUTH] skip notify user_id=%s: %s", fresh.id, e)
            continue
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔐 Authorize", url=auth_url)]]
        )
        try:
            await bot.send_message(
                chat_id=fresh.telegram_id,
                text=(
                    "Опциональное подключение HH.ru (личный аккаунт соискателя) истекло. "
                    "Поиск вакансий в боте от этого **не зависит**. "
                    "Подключите снова только если пользуетесь или планируете функции, связанные с вашим профилем HH."
                ),
                reply_markup=keyboard,
            )
            set_hh_reauth_notified(fresh.id, True)
        except Exception as e:
            logger.exception(
                "[HH_REAUTH] notify failed telegram_id=%s: %s", fresh.telegram_id, e
            )
