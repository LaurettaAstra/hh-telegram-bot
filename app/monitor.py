"""
Scheduled monitoring of HH vacancies.

Per-user monitoring: each active user receives only vacancies matching their
saved filters. Delivery is tracked per filter via filter_vacancy_matches.

First-run baseline: when last_monitoring_at is NULL, we fetch vacancies,
mark them as baseline (sent) without sending, then set last_monitoring_at.
This prevents spamming users with historical backlog.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from telegram import Bot

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
    user_id: int  # internal user id for filter_vacancy_matches
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
        logger.info("Monitoring run: no active users (is_active=True), skipping")
        logger.info("[MONITOR_TRACE] early_return run_monitoring_check reason=no_active_users")
        return []

    logger.info("Monitoring run: checking %d active user(s) for new vacancies", len(users))

    results = []
    for user in users:
        try:
            filters_list = get_user_monitoring_filters(user.id)
            logger.info(
                "Monitoring user telegram_id=%s internal_id=%s: %d filter(s) with monitoring_enabled=True",
                user.telegram_id,
                user.id,
                len(filters_list),
            )
            if not filters_list:
                logger.info(
                    "Monitoring user telegram_id=%s: no monitoring filters, skip",
                    user.telegram_id,
                )
                logger.info(
                    "[MONITOR_TRACE] continue run_monitoring_check reason=no_monitoring_filters "
                    "user_id=%s telegram_id=%s final_user_items_count=n/a",
                    user.id,
                    user.telegram_id,
                )
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
                    logger.info(
                        "[MONITOR_TRACE] skip process_filter_for_user exception user_id=%s "
                        "filter_id=%s monitoring_enabled=%s last_monitoring_at=%s "
                        "reason=exception_items_not_added",
                        user.id,
                        f.id,
                        getattr(f, "monitoring_enabled", None),
                        getattr(f, "last_monitoring_at", None),
                    )

            if user_items:
                results.append(
                    MonitoringResult(
                        user_telegram_id=user.telegram_id,
                        user_id=user.id,
                        items_to_send=user_items,
                    )
                )
                logger.info(
                    "Monitoring user telegram_id=%s: queued %d vacancy send(s) across filters",
                    user.telegram_id,
                    len(user_items),
                )
                logger.info(
                    "[MONITOR_TRACE] final_user_items user_id=%s telegram_id=%s final_user_items_count=%d",
                    user.id,
                    user.telegram_id,
                    len(user_items),
                )
            else:
                logger.info(
                    "Monitoring user telegram_id=%s: no vacancies to send after processing %d filter(s)",
                    user.telegram_id,
                    len(filters_list),
                )
                logger.info(
                    "[MONITOR_TRACE] skip_user_no_queued_items user_id=%s telegram_id=%s "
                    "final_user_items_count=0 filters_processed=%d",
                    user.id,
                    user.telegram_id,
                    len(filters_list),
                )
        except Exception as e:
            logger.exception("Monitoring failed for user %s: %s", user.telegram_id, e)
            logger.info(
                "[MONITOR_TRACE] skip_user run_monitoring_check exception user_id=%s telegram_id=%s",
                getattr(user, "id", None),
                user.telegram_id,
            )

    total_queued = sum(len(r.items_to_send) for r in results)
    logger.info(
        "Monitoring run finished: %d user(s) with items to send, %d total queued vacancy message(s)",
        len(results),
        total_queued,
    )
    return results


def process_filter_for_user(user, filter_obj) -> list[tuple[dict, int]]:
    """
    Fetch vacancies for filter, return list of (vacancy_dict, filter_id) that have not
    yet been sent to this user for this filter. Deduplication by HH vacancy_id (string).

    First-run baseline: if last_monitoring_at is NULL, mark all as sent without sending.

    Does NOT send or mark as sent - caller does that after successful send.
    """
    _lma_at_start = getattr(filter_obj, "last_monitoring_at", None)
    _mon_en = getattr(filter_obj, "monitoring_enabled", None)
    logger.info(
        "[MONITOR_TRACE] process_filter_for_user start user_id=%s filter_id=%s "
        "monitoring_enabled=%s last_monitoring_at=%s",
        user.id,
        filter_obj.id,
        _mon_en,
        _lma_at_start,
    )
    search_params = _search_params_from_filter(filter_obj)
    search_params["period"] = 1  # Only last day's vacancies for monitoring
    api_vacancies = search_vacancies(search_params=search_params, filter_obj=filter_obj)
    logger.info(
        "Filter '%s' (id=%s): HH API returned %d vacancy/vacancies (after client-side filters, period=1)",
        filter_obj.name,
        filter_obj.id,
        len(api_vacancies),
    )
    logger.info(
        "[MONITOR_FLOW] after search_vacancies (HH fetch + _process_vacancy_items) "
        "count=%s filter_id=%s user_id=%s",
        len(api_vacancies),
        filter_obj.id,
        user.id,
    )
    if not api_vacancies:
        update_filter_last_monitoring(filter_obj.id, datetime.now(timezone.utc))
        logger.info(
            "Filter '%s' (id=%s): no vacancies from API, updated last_monitoring_at, nothing to send",
            filter_obj.name,
            filter_obj.id,
        )
        logger.info(
            "[MONITOR_TRACE] early_return process_filter_for_user reason=no_hh_vacancies "
            "user_id=%s filter_id=%s monitoring_enabled=%s last_monitoring_at=%s "
            "hh_vacancies=0 baseline_branch=False save_vacancies_to_db=n/a "
            "skipped_no_id=0 skipped_already_sent=0 to_send=0",
            user.id,
            filter_obj.id,
            _mon_en,
            _lma_at_start,
        )
        logger.info(
            "[MONITOR_SKIP] reason=no_results_after_search_vacancies filter_id=%s user_id=%s — "
            "vacancies dropped here (nothing to dedupe or send)",
            filter_obj.id,
            user.id,
        )
        return []

    # First-run baseline: do not send historical backlog, just mark as baseline
    if getattr(filter_obj, "last_monitoring_at", None) is None:
        logger.info(
            "Filter '%s' (id=%s): first-run baseline (last_monitoring_at is NULL), "
            "marking as sent without Telegram delivery",
            filter_obj.name,
            filter_obj.id,
        )
        _nv_count = 0
        _save_baseline = "skipped_no_new_rows"
        baseline_skipped_no_id = 0
        try:
            new_vacancies = filter_new_vacancies(api_vacancies)
            _nv_count = len(new_vacancies)
            if new_vacancies:
                save_vacancies_to_db(new_vacancies)
                _save_baseline = "ok"
            _baseline_no_id_logged = 0
            for v in api_vacancies:
                hh_id = str(v.get("id", ""))
                if not hh_id:
                    baseline_skipped_no_id += 1
                    if _baseline_no_id_logged < 3:
                        _baseline_no_id_logged += 1
                        logger.info(
                            "[MONITOR_SKIP] reason=no_vacancy_id (baseline mark loop) filter_id=%s "
                            "vacancy_keys=%s",
                            filter_obj.id,
                            list(v.keys())[:12] if isinstance(v, dict) else type(v).__name__,
                        )
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
            _save_baseline = "failed"
        logger.info(
            "[MONITOR_SKIP] reason=first_run_baseline (last_monitoring_at was NULL) "
            "filter_id=%s user_id=%s count_after_process=%s baseline_skipped_no_id=%s — "
            "all Telegram sends dropped for this filter run; rows marked in DB where possible",
            filter_obj.id,
            user.id,
            len(api_vacancies),
            baseline_skipped_no_id,
        )
        logger.info(
            "[MONITOR_TRACE] early_return process_filter_for_user reason=baseline_branch "
            "user_id=%s filter_id=%s monitoring_enabled=%s last_monitoring_at=%s "
            "hh_vacancies=%d baseline_branch=True save_vacancies_to_db=%s new_to_db=%d "
            "skipped_no_id=n/a skipped_already_sent=n/a to_send=0",
            user.id,
            filter_obj.id,
            _mon_en,
            _lma_at_start,
            len(api_vacancies),
            _save_baseline,
            _nv_count,
        )
        return []

    # Save new vacancies to DB (for search/filters; dedup uses hh_id)
    new_vacancies = filter_new_vacancies(api_vacancies)
    logger.info(
        "Filter '%s' (id=%s): %d vacancy/vacancies new to DB (not yet in vacancies table)",
        filter_obj.name,
        filter_obj.id,
        len(new_vacancies),
    )
    _save_normal = "skipped_no_new_rows"
    if new_vacancies:
        try:
            save_vacancies_to_db(new_vacancies)
            _save_normal = "ok"
        except Exception as e:
            logger.exception("Failed to save vacancies for filter %s: %s", filter_obj.id, e)
            _save_normal = "failed"
            logger.info(
                "[MONITOR_TRACE] early_return process_filter_for_user reason=save_vacancies_to_db_failed "
                "user_id=%s filter_id=%s monitoring_enabled=%s last_monitoring_at=%s "
                "hh_vacancies=%d baseline_branch=False save_vacancies_to_db=failed new_to_db=%d "
                "skipped_no_id=n/a skipped_already_sent=n/a to_send=0",
                user.id,
                filter_obj.id,
                _mon_en,
                _lma_at_start,
                len(api_vacancies),
                len(new_vacancies),
            )
            logger.info(
                "[MONITOR_SKIP] reason=save_vacancies_to_db_failed filter_id=%s user_id=%s — "
                "all vacancies dropped for this filter run",
                filter_obj.id,
                user.id,
            )
            return []

    # Deduplicate by HH vacancy_id: only send if not already_sent
    # Note: repository.already_sent returns True for empty id (treated as "skip");
    # we count missing id separately via skipped_no_id before calling already_sent.
    to_send = []
    skipped_no_id = 0
    skipped_already_sent = 0
    sample_already_sent_ids: list[str] = []
    _dedup_no_id_logged = 0
    for v in api_vacancies:
        hh_id = str(v.get("id", ""))
        if not hh_id:
            skipped_no_id += 1
            if _dedup_no_id_logged < 3:
                _dedup_no_id_logged += 1
                logger.info(
                    "[MONITOR_SKIP] reason=no_vacancy_id (dedup) filter_id=%s user_id=%s",
                    filter_obj.id,
                    user.id,
                )
            continue
        if already_sent(hh_id, user.id, filter_obj.id):
            skipped_already_sent += 1
            if len(sample_already_sent_ids) < 5:
                sample_already_sent_ids.append(hh_id)
            continue
        to_send.append((v, filter_obj.id))

    update_filter_last_monitoring(filter_obj.id, datetime.now(timezone.utc))
    logger.info(
        "Filter '%s' (id=%s): dedup — from API %d, skipped (no id) %d, skipped (already sent) %d, to_send %d",
        filter_obj.name,
        filter_obj.id,
        len(api_vacancies),
        skipped_no_id,
        skipped_already_sent,
        len(to_send),
    )
    if skipped_no_id:
        logger.info(
            "[MONITOR_SKIP] summary reason=no_vacancy_id total=%s filter_id=%s user_id=%s",
            skipped_no_id,
            filter_obj.id,
            user.id,
        )
    if skipped_already_sent:
        logger.info(
            "[MONITOR_SKIP] summary reason=already_sent total=%s sample_hh_ids=%s filter_id=%s user_id=%s",
            skipped_already_sent,
            sample_already_sent_ids,
            filter_obj.id,
            user.id,
        )
    logger.info(
        "[MONITOR_FLOW] to_send final count=%s filter_id=%s user_id=%s (after dedup)",
        len(to_send),
        filter_obj.id,
        user.id,
    )
    if to_send:
        logger.info(
            "Filter '%s' (id=%s): %d new vacancies to send to user %s",
            filter_obj.name,
            filter_obj.id,
            len(to_send),
            user.telegram_id,
        )
    else:
        logger.info(
            "Filter '%s' (id=%s): nothing to send (all duplicates or empty after dedup)",
            filter_obj.name,
            filter_obj.id,
        )
    logger.info(
        "[MONITOR_TRACE] return process_filter_for_user user_id=%s filter_id=%s "
        "monitoring_enabled=%s last_monitoring_at=%s hh_vacancies=%d baseline_branch=False "
        "save_vacancies_to_db=%s new_to_db=%d skipped_no_id=%d skipped_already_sent=%d to_send=%d",
        user.id,
        filter_obj.id,
        _mon_en,
        _lma_at_start,
        len(api_vacancies),
        _save_normal,
        len(new_vacancies),
        skipped_no_id,
        skipped_already_sent,
        len(to_send),
    )
    return to_send


async def monitoring_loop(
    bot: "Bot",
    interval_minutes: int,
    send_fn,
    mark_sent_fn,
) -> None:
    """
    Continuous background loop: periodically runs monitoring check, sends
    notifications, updates last_monitoring_at. Runs in parallel with bot polling.

    Args:
        bot: Telegram Bot instance for sending messages
        interval_minutes: Minutes between monitoring runs
        send_fn: Async callable(bot, chat_id, vacancies) to send notifications
        mark_sent_fn: Sync callable(hh_id, user_id, filter_id) to mark vacancy sent
    """
    interval_seconds = interval_minutes * 60
    first_run_delay = min(60, interval_seconds)  # First run after 1 min or interval
    logger.info(
        "Monitoring loop started: first run in %ds, then every %d minutes",
        first_run_delay,
        interval_minutes,
    )

    while True:
        try:
            await asyncio.sleep(first_run_delay)
            first_run_delay = interval_seconds  # Subsequent runs use full interval
            logger.info("MONITOR WORKING")
            logger.info("Monitoring loop: starting check run")
            results = await asyncio.to_thread(run_monitoring_check)

            n_results = len(results)
            n_items = sum(len(r.items_to_send) for r in results)
            logger.info(
                "Monitoring loop: run_monitoring_check returned %d user result(s), %d total vacancy item(s)",
                n_results,
                n_items,
            )

            total_sent = 0
            for result in results:
                if not result.items_to_send:
                    logger.info(
                        "[MONITOR_TRACE] continue monitoring_loop reason=empty_items_to_send "
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
                            "Monitoring loop send attempt: telegram_id=%s filter_id=%s hh_vacancy_id=%s",
                            result.user_telegram_id,
                            filter_id,
                            hh_id,
                        )
                        await send_fn(bot, result.user_telegram_id, [vacancy_dict])
                        if hh_id and filter_id:
                            await asyncio.to_thread(
                                mark_sent_fn, hh_id, result.user_id, filter_id
                            )
                        total_sent += 1
                        logger.info(
                            "Monitoring loop send ok: telegram_id=%s filter_id=%s hh_vacancy_id=%s",
                            result.user_telegram_id,
                            filter_id,
                            hh_id,
                        )
                    except Exception as e:
                        logger.exception(
                            "Failed to send vacancy %s to user %s: %s",
                            hh_id,
                            result.user_telegram_id,
                            e,
                        )

            if total_sent > 0:
                logger.info("Monitoring loop: sent %d vacancy notifications", total_sent)
            else:
                logger.info(
                    "Monitoring loop: check complete, no new vacancies to send (results=%d, items=%d)",
                    n_results,
                    n_items,
                )

        except asyncio.CancelledError:
            logger.info("Monitoring loop cancelled (bot shutting down)")
            raise
        except Exception as e:
            logger.exception("Monitoring loop error (will retry): %s", e)
