"""
Repository layer for saving vacancies to the database.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import FilterVacancyMatch, Vacancy, VacancySentLog


def filter_new_vacancies(vacancies: list) -> list:
    """
    Return only vacancies that do not yet exist in the database (by hh_id).

    Args:
        vacancies: List of vacancy dicts from HH API (each must have "id" key).

    Returns:
        List of vacancy dicts that are new (not in vacancies table).
    """
    if not vacancies:
        return []

    hh_ids = [str(v.get("id", "")) for v in vacancies if v.get("id")]
    if not hh_ids:
        return []

    session = SessionLocal()
    try:
        result = session.execute(select(Vacancy.hh_id).where(Vacancy.hh_id.in_(hh_ids)))
        existing_ids = {row[0] for row in result.fetchall()}
        return [v for v in vacancies if str(v.get("id", "")) not in existing_ids]
    finally:
        session.close()


def _parse_published_at(value):
    """Parse HH API published_at string to datetime or None."""
    if not value:
        return None
    try:
        ts = value.replace("+0300", "+03:00").replace("+0000", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _parse_int(value):
    """Parse value to int or None."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def save_vacancies_to_db(vacancies: list) -> dict[str, int]:
    """
    Save vacancies to the database. Assumes vacancies are new (no duplicate check).

    Args:
        vacancies: List of vacancy dicts from HH API (with keys: id, name, company,
                  salary_from, salary_to, currency, url, published_at, raw_json, etc.)

    Returns:
        Dict mapping hh_id -> vacancy_id for each saved vacancy.

    Raises:
        Re-raises any database exception after rollback.
    """
    if not vacancies:
        return {}

    session = SessionLocal()
    saved_ids = {}
    try:
        for v in vacancies:
            hh_id = str(v.get("id", ""))
            if not hh_id:
                continue

            published_at = _parse_published_at(v.get("published_at"))
            salary_from = _parse_int(v.get("salary_from"))
            salary_to = _parse_int(v.get("salary_to"))

            vacancy = Vacancy(
                hh_id=hh_id,
                title=v.get("name", ""),
                company=v.get("company") or None,
                city=v.get("area") or None,
                salary_from=salary_from,
                salary_to=salary_to,
                currency=v.get("currency") or None,
                url=v.get("url") or None,
                published_at=published_at,
                raw_json=v.get("raw_json") or None,
            )
            session.add(vacancy)
            session.flush()
            saved_ids[hh_id] = vacancy.id

        session.commit()
        return saved_ids

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def get_vacancy_by_hh_id(hh_id: str) -> Vacancy | None:
    """Get vacancy by hh_id or None if not found."""
    session = SessionLocal()
    try:
        result = session.execute(select(Vacancy).where(Vacancy.hh_id == hh_id))
        return result.scalars().first()
    finally:
        session.close()


def was_vacancy_sent_to_filter(filter_id: int, vacancy_id: int) -> bool:
    """Check if vacancy was already sent to user for this filter."""
    session = SessionLocal()
    try:
        result = session.execute(
            select(FilterVacancyMatch).where(
                FilterVacancyMatch.filter_id == filter_id,
                FilterVacancyMatch.vacancy_id == vacancy_id,
                FilterVacancyMatch.sent_to_user == True,
            )
        )
        return result.scalars().first() is not None
    finally:
        session.close()


def mark_vacancy_sent_to_filter(filter_id: int, vacancy_id: int) -> None:
    """Record that vacancy was sent to user for this filter. Idempotent."""
    if was_vacancy_sent_to_filter(filter_id, vacancy_id):
        return
    session = SessionLocal()
    try:
        match = FilterVacancyMatch(
            filter_id=filter_id,
            vacancy_id=vacancy_id,
            sent_to_user=True,
        )
        session.add(match)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- VacancySentLog: deduplication by HH vacancy_id (string) ---


def already_sent(vacancy_id: str, user_id: int, filter_id: int) -> bool:
    """
    Check if vacancy (HH id string) was already sent to this user for this filter.
    Deduplication is based ONLY on vacancy_id - ignores published_at or HH updates.
    """
    if not vacancy_id or not str(vacancy_id).strip():
        return True
    session = SessionLocal()
    try:
        result = session.execute(
            select(VacancySentLog).where(
                VacancySentLog.user_id == user_id,
                VacancySentLog.filter_id == filter_id,
                VacancySentLog.vacancy_id == str(vacancy_id),
            )
        )
        return result.scalars().first() is not None
    finally:
        session.close()


def mark_vacancy_sent(vacancy_id: str, user_id: int, filter_id: int) -> None:
    """
    Record that vacancy (HH id string) was sent to user for this filter.
    Idempotent: handles race condition via try/except on unique violation.
    """
    if not vacancy_id or not str(vacancy_id).strip():
        return
    session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        log = VacancySentLog(
            user_id=user_id,
            filter_id=filter_id,
            vacancy_id=str(vacancy_id),
            first_seen_at=now,
            sent_at=now,
        )
        session.add(log)
        session.commit()
    except IntegrityError:
        session.rollback()
        # Race: another process already inserted (unique violation) - treat as success
        pass
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
