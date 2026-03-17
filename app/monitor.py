"""
Scheduled monitoring of HH vacancies.

Per-user monitoring: each active user receives only vacancies matching their
saved filters. Delivery is tracked per filter via filter_vacancy_matches.

First-run baseline: when last_monitoring_at is NULL, we fetch vacancies,
mark them as baseline (sent) without sending, then set last_monitoring_at.
This prevents spamming users with historical backlog.
"""

import logging
from datetime import datetime, timezone
from typing import NamedTuple

from app.hh_api import _search_params_from_filter, search_vacancies
from app.repository import (
    already_sent,
    filter_new_vacancies,
    mark_vacancy_sent,
    save_vacancies_to_db,
)
from app.user_repository import get_active_users, get_user_monitoring_filters, update_filter_last_monitoring

logger = logging.getLogger(__name__)


class MonitoringResult(NamedTuple):
    """Result of monitoring for one user."""

    user_telegram_id: int
    user_id: int  # internal user id for VacancySentLog
    items_to_send: list[tuple[dict, int]]  # (vacancy_dict, filter_id)


def run_monitoring_check() -> list[MonitoringResult]:
    """
    Run per-user monitoring: load active users, their filters, fetch vacancies,
    detect unsent ones, return items to send per user.

    Flow:
    1. get_active_users()
    2. For each user: get_user_monitoring_filters(user_id)
    3. For each filter: process_filter_for_user(user, filter)
    4. Collect (user_telegram_id, items_to_send) per user

    Returns:
        List of MonitoringResult.
    """
    users = get_active_users()
    if not users:
        logger.info("Monitoring run: no active users")
        return []

    results = []
    for user in users:
        try:
            filters_list = get_user_monitoring_filters(user.id)
            if not filters_list:
                continue

            user_items = []
            for f in filters_list:
                try:
                    items = process_filter_for_user(user, f)
                    user_items.extend(items)
                except Exception as e:
                    logger.exception(
                        "Monitoring failed for user %s filter '%s' (id=%s): %s",
                        user.telegram_id,
                        f.name,
                        f.id,
                        e,
                    )

            if user_items:
                results.append(
                    MonitoringResult(
                        user_telegram_id=user.telegram_id,
                        user_id=user.id,
                        items_to_send=user_items,
                    )
                )
        except Exception as e:
            logger.exception("Monitoring failed for user %s: %s", user.telegram_id, e)

    return results


def process_filter_for_user(user, filter_obj) -> list[tuple[dict, int]]:
    """
    Fetch vacancies for filter, return list of (vacancy_dict, filter_id) that have not
    yet been sent to this user for this filter. Deduplication by HH vacancy_id (string).

    First-run baseline: if last_monitoring_at is NULL, mark all as sent without sending.

    Does NOT send or mark as sent - caller does that after successful send.
    """
    search_params = _search_params_from_filter(filter_obj)
    search_params["period"] = 1  # Only last day's vacancies for monitoring
    api_vacancies = search_vacancies(search_params=search_params, filter_obj=filter_obj)
    if not api_vacancies:
        update_filter_last_monitoring(filter_obj.id, datetime.now(timezone.utc))
        return []

    # First-run baseline: do not send historical backlog, just mark as baseline
    if getattr(filter_obj, "last_monitoring_at", None) is None:
        try:
            new_vacancies = filter_new_vacancies(api_vacancies)
            if new_vacancies:
                save_vacancies_to_db(new_vacancies)
            for v in api_vacancies:
                hh_id = str(v.get("id", ""))
                if not hh_id:
                    continue
                if not already_sent(hh_id, user.id, filter_obj.id):
                    mark_vacancy_sent(hh_id, user.id, filter_obj.id)
            update_filter_last_monitoring(filter_obj.id, datetime.now(timezone.utc))
            logger.info(
                "Filter '%s' (id=%s): first-run baseline, %d vacancies marked, no send",
                filter_obj.name,
                filter_obj.id,
                len(api_vacancies),
            )
        except Exception as e:
            logger.exception("First-run baseline failed for filter %s: %s", filter_obj.id, e)
        return []

    # Save new vacancies to DB (for search/filters; dedup uses hh_id)
    new_vacancies = filter_new_vacancies(api_vacancies)
    if new_vacancies:
        try:
            save_vacancies_to_db(new_vacancies)
        except Exception as e:
            logger.exception("Failed to save vacancies for filter %s: %s", filter_obj.id, e)
            return []

    # Deduplicate by HH vacancy_id: only send if not already_sent
    to_send = []
    for v in api_vacancies:
        hh_id = str(v.get("id", ""))
        if not hh_id:
            continue
        if already_sent(hh_id, user.id, filter_obj.id):
            continue
        to_send.append((v, filter_obj.id))

    update_filter_last_monitoring(filter_obj.id, datetime.now(timezone.utc))
    if to_send:
        logger.info(
            "Filter '%s' (id=%s): %d new vacancies to send to user %s",
            filter_obj.name,
            filter_obj.id,
            len(to_send),
            user.telegram_id,
        )
    return to_send


