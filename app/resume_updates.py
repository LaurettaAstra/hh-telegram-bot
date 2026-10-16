"""Persistent, opt-in resume updating every five hours."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app.db import SessionLocal, engine
from app.hh_api import HHApiError, HHAuthorizationError
from app.hh_auth import require_hh_auth
from app.models import ResumeAutoUpdate, User
from app.resume_api import get_resume_status, list_my_resumes, publish_resume
from app.search_flow import reset_search_conversation

logger = logging.getLogger(__name__)
INTERVAL = timedelta(hours=5)
ENABLE_TEXT = "Включить автообновление резюме"
DISABLE_TEXT = "Отключить автообновление резюме"


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def init_resume_updates():
    ResumeAutoUpdate.__table__.create(bind=engine, checkfirst=True)


def set_enabled(user_id, resume_id=None):
    with SessionLocal.begin() as session:
        row = session.execute(select(ResumeAutoUpdate).where(
            ResumeAutoUpdate.user_id == user_id).with_for_update()).scalar_one_or_none()
        if resume_id is None:
            if row:
                row.enabled = False
            return
        now = datetime.now(timezone.utc)
        if row is None:
            row = ResumeAutoUpdate(user_id=user_id, resume_id=resume_id, next_run_at=now)
            session.add(row)
        if not row.enabled:
            # Preserve the cooldown even if the user toggles off/on.
            row.next_run_at = max(now, utc(row.next_run_at))
        row.resume_id = resume_id
        row.enabled = True
        row.last_error = None


def update_one(user_id):
    """Lock and recheck the schedule so overlapping jobs cannot publish twice."""
    with SessionLocal.begin() as session:
        row = session.execute(select(ResumeAutoUpdate).where(
            ResumeAutoUpdate.user_id == user_id).with_for_update()).scalar_one_or_none()
        user = session.get(User, user_id)
        now = datetime.now(timezone.utc)
        if not row or not row.enabled or not user or not user.is_active or utc(row.next_run_at) > now:
            return None
        previous_error = row.last_error
        # Also back off on timeouts: the POST may have succeeded at HH.
        row.next_run_at = now + INTERVAL
        try:
            status = get_resume_status(user_id, row.resume_id)
            if status.get("next_publish_at"):
                allowed_at = utc(status["next_publish_at"])
                if allowed_at > now:
                    row.next_run_at = max(row.next_run_at, allowed_at)
                    return None
            if status.get("blocked") or status.get("status", {}).get("id") != "published":
                row.enabled = False
                raise HHApiError("Резюме скрыто, заблокировано или не опубликовано. Автообновление отключено.")
            if status.get("can_publish_or_update") is not True:
                raise HHApiError("HH пока не разрешает обновление. Следующая попытка через 5 часов.")
            publish_resume(user_id, row.resume_id)
            row.last_success_at = datetime.now(timezone.utc)
            row.next_run_at = row.last_success_at + INTERVAL
            row.last_error = None
        except HHAuthorizationError:
            row.enabled = False
            row.last_error = "Подключите аккаунт HH через /connect_hh и снова включите автообновление."
        except HHApiError as exc:
            if exc.status_code in (401, 403, 404):
                row.enabled = False
                row.last_error = "Нет доступа к резюме. Автообновление отключено. Проверьте аккаунт HH и включите снова."
            else:
                row.last_error = str(exc)
        if row.last_error and row.last_error != previous_error:
            return user.telegram_id, row.last_error
    return None


async def resume_update_pass(bot):
    def due_users():
        with SessionLocal() as session:
            return list(session.scalars(select(ResumeAutoUpdate.user_id).where(
                ResumeAutoUpdate.enabled.is_(True),
                ResumeAutoUpdate.next_run_at <= datetime.now(timezone.utc))))
    for user_id in await asyncio.to_thread(due_users):
        try:
            notification = await asyncio.to_thread(update_one, user_id)
            if notification:
                await bot.send_message(chat_id=notification[0], text=notification[1])
        except Exception:
            logger.exception("Resume update failed user_id=%s", user_id)


async def resume_update_job(context):
    await resume_update_pass(context.bot)


async def resume_update_loop(bot):
    while True:
        try:
            await resume_update_pass(bot)
        except Exception:
            logger.exception("Resume update pass failed")
        await asyncio.sleep(60)


def build_resume_handlers(ensure_user):
    async def enable(update, context):
        await reset_search_conversation(context.application, update)
        user, error = ensure_user(update)
        if error:
            await update.effective_message.reply_text(error)
            return
        if not await require_hh_auth(update, context, user):
            return
        try:
            resumes = await asyncio.to_thread(list_my_resumes, user.id)
        except (HHApiError, HHAuthorizationError):
            await update.effective_message.reply_text("Не удалось получить резюме. Проверьте подключение HH через /connect_hh.")
            return
        resumes = [r for r in resumes if r.get("status", {}).get("id") == "published" and not r.get("blocked")]
        if not resumes:
            await update.effective_message.reply_text("Нет опубликованных резюме. Сначала опубликуйте резюме на HH.")
            return
        # Callback data carries only a short index; never trust a client-provided resume ID.
        context.user_data["resume_choices"] = {str(i): str(r["id"]) for i, r in enumerate(resumes)}
        keyboard = [[InlineKeyboardButton(r.get("title") or "Резюме", callback_data=f"resume_enable:{i}")]
                    for i, r in enumerate(resumes)]
        await update.effective_message.reply_text(
            "Выберите резюме. Бот будет поднимать его в поиске HH раз в 5 часов, когда HH разрешает обновление. "
            "Первая попытка — в течение минуты. Текст резюме не меняется.",
            reply_markup=InlineKeyboardMarkup(keyboard))

    async def choose(update, context):
        query = update.callback_query
        await query.answer()
        user, error = ensure_user(update)
        if error:
            await query.message.reply_text(error)
            return
        resume_id = context.user_data.get("resume_choices", {}).get(query.data.split(":", 1)[1])
        if not resume_id:
            await query.message.reply_text("Выбор устарел. Нажмите «Включить автообновление резюме» ещё раз.")
            return
        try:
            # Recheck ownership and publication after the selection.
            resumes = await asyncio.to_thread(list_my_resumes, user.id)
            if not any(str(r["id"]) == resume_id and r.get("status", {}).get("id") == "published"
                       and not r.get("blocked") for r in resumes):
                await query.message.reply_text("Это резюме больше недоступно для автообновления.")
                return
            await asyncio.to_thread(set_enabled, user.id, resume_id)
        except (HHApiError, HHAuthorizationError):
            await query.message.reply_text("Не удалось включить автообновление. Проверьте подключение HH.")
            return
        context.user_data.pop("resume_choices", None)
        await query.edit_message_text("Автообновление резюме включено: раз в 5 часов. Отключить можно кнопкой в меню или /resume_off.")

    async def disable(update, context):
        await reset_search_conversation(context.application, update)
        user, error = ensure_user(update)
        if error:
            await update.effective_message.reply_text(error)
            return
        await asyncio.to_thread(set_enabled, user.id)
        context.user_data.pop("resume_choices", None)
        await update.effective_message.reply_text("Автообновление резюме отключено.")

    return [CommandHandler("resume_on", enable), CommandHandler("resume_off", disable),
            MessageHandler(filters.Regex(f"^{ENABLE_TEXT}$"), enable),
            MessageHandler(filters.Regex(f"^{DISABLE_TEXT}$"), disable),
            CallbackQueryHandler(choose, pattern=r"^resume_enable:\d+$")]
