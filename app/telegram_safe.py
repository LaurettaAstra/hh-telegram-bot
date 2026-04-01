"""
Safe wrappers for Telegram API calls: catch TimedOut and other errors so the bot keeps running.
"""

import logging

from telegram.error import TimedOut

logger = logging.getLogger(__name__)


async def safe_query_answer(query, **kwargs) -> None:
    """Answer callback query; never raises on Telegram errors."""
    try:
        logger.debug("telegram safe_query_answer")
        await query.answer(**kwargs)
    except TimedOut:
        logger.warning("Telegram timeout on query.answer")
    except Exception as e:
        logger.exception("Telegram error on query.answer: %s", e)


async def safe_reply_text(message, text: str, **kwargs) -> None:
    """reply_text on a Message; never raises on Telegram errors."""
    if message is None:
        logger.warning("safe_reply_text: message is None, skip")
        return
    try:
        logger.debug("telegram safe_reply_text (len=%s)", len(text) if text else 0)
        await message.reply_text(text, **kwargs)
    except TimedOut:
        logger.warning("Telegram timeout on message.reply_text")
    except Exception as e:
        logger.exception("Telegram error on message.reply_text: %s", e)


async def safe_edit_message_text(query, text: str, *, timeout_reply: str | None = None, **kwargs) -> None:
    """
    query.edit_message_text with TimedOut / error handling.
    On TimedOut: log warning, then reply_text with timeout_reply or text if query.message exists.
    On other errors: log exception, same fallback.
    """
    try:
        logger.debug("telegram safe_edit_message_text")
        await query.edit_message_text(text, **kwargs)
    except TimedOut:
        logger.warning("Telegram timeout, retrying...")
        if query.message:
            try:
                await query.message.reply_text(timeout_reply if timeout_reply is not None else text)
            except Exception as e2:
                logger.exception("Telegram error on fallback reply_text after timeout: %s", e2)
    except Exception as e:
        logger.exception("Telegram error: %s", e)
        if query.message:
            try:
                await query.message.reply_text(timeout_reply if timeout_reply is not None else text)
            except Exception as e2:
                logger.exception("Telegram error on fallback reply_text: %s", e2)


async def safe_bot_edit_message_text(bot, *, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
    """bot.edit_message_text; returns False on failure. Does not raise."""
    try:
        logger.debug("telegram safe_bot_edit_message_text chat_id=%s message_id=%s", chat_id, message_id)
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)
        return True
    except TimedOut:
        logger.warning("Telegram timeout on bot.edit_message_text (chat_id=%s message_id=%s)", chat_id, message_id)
        return False
    except Exception as e:
        logger.exception("Telegram error on bot.edit_message_text: %s", e)
        return False


async def safe_bot_send_message(bot, *, chat_id: int, text: str, **kwargs):
    """bot.send_message; returns message or None. Does not raise."""
    try:
        logger.debug("telegram safe_bot_send_message chat_id=%s", chat_id)
        return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TimedOut:
        logger.warning("Telegram timeout on bot.send_message (chat_id=%s)", chat_id)
        return None
    except Exception as e:
        logger.exception("Telegram error on bot.send_message: %s", e)
        return None
