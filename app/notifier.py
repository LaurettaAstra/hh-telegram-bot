"""
Telegram notification helpers for sending vacancy messages.
"""

import logging
import math

logger = logging.getLogger(__name__)

MAX_VACANCIES_PER_BATCH = 15


def format_vacancy_single_message(vacancy: dict) -> str:
    """Format one vacancy as a separate message (for link preview card).
    Each vacancy = one message so Telegram shows HH preview.
    """
    name = vacancy.get("name") or "не указано"
    company = vacancy.get("company") or "не указано"
    salary = vacancy.get("salary") or "не указана"
    area = vacancy.get("area") or "не указано"
    schedule = vacancy.get("schedule") or "не указан"
    experience = vacancy.get("experience") or "не указан"
    employment = vacancy.get("employment") or "не указана"
    url = vacancy.get("url") or ""
    return (
        f"{name}\n\n"
        f"Компания: {company}\n"
        f"Зарплата: {salary}\n"
        f"Город: {area}\n"
        f"Формат работы: {schedule}\n"
        f"Опыт: {experience}\n"
        f"Занятость: {employment}\n\n"
        f"Ссылка: {url}"
    )


def format_vacancy_message(vacancy: dict) -> str:
    """Format a single vacancy dict as a Telegram message (for monitoring notifications)."""
    name = vacancy.get("name") or "не указано"
    company = vacancy.get("company") or "не указано"
    salary = vacancy.get("salary") or "не указана"
    area = vacancy.get("area") or "не указано"
    schedule = vacancy.get("schedule") or "не указан"
    experience = vacancy.get("experience") or "не указан"
    employment = vacancy.get("employment") or "не указана"
    url = vacancy.get("url") or ""

    return (
        f"<b>{name}</b>\n\n"
        f"Компания: {company}\n"
        f"Зарплата: {salary}\n"
        f"Город: {area}\n"
        f"Формат работы: {schedule}\n"
        f"Опыт: {experience}\n"
        f"Занятость: {employment}\n\n"
        f"Ссылка: {url}"
    )


def format_vacancies_page_header(total: int, period: int, page: int) -> str:
    """Build header message for pagination (vacancies sent as separate messages)."""
    pages = max(1, math.ceil(total / 10))
    return (
        f"Найдено {total} вакансий за последние {period} дней\n"
        f"Стр. {page + 1} / {pages}"
    )


async def send_vacancies_to_telegram(bot, chat_id: int, vacancies: list) -> None:
    """
    Send vacancy messages to a Telegram chat. Limits to MAX_VACANCIES_PER_BATCH.

    Args:
        bot: Telegram Bot instance (e.g. context.bot)
        chat_id: Target chat ID
        vacancies: List of vacancy dicts
    """
    batch = vacancies[:MAX_VACANCIES_PER_BATCH]
    logger.info(
        "send_vacancies_to_telegram: chat_id=%s sending %d message(s) (batch max=%s)",
        chat_id,
        len(batch),
        MAX_VACANCIES_PER_BATCH,
    )
    for vacancy in batch:
        text = format_vacancy_message(vacancy)
        hh_id = str(vacancy.get("id", ""))
        logger.info(
            "send_vacancies_to_telegram: calling bot.send_message chat_id=%s hh_vacancy_id=%s",
            chat_id,
            hh_id,
        )
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        logger.info(
            "send_vacancies_to_telegram: bot.send_message completed chat_id=%s hh_vacancy_id=%s",
            chat_id,
            hh_id,
        )
