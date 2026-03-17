"""
Repository layer for users and saved filters.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import SavedFilter, User

logger = logging.getLogger(__name__)

USER_FRIENDLY_ERROR = "Не удалось обработать запрос. Попробуйте ещё раз позже."


def get_or_create_user(
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    """
    Get existing user by telegram_id or create a new one.

    Handles race condition: if two concurrent requests try to create the same
    new user, one may get IntegrityError (unique violation). We retry with SELECT
    to fetch the user created by the other request.

    Returns:
        User instance (existing or newly created).
    """
    session = SessionLocal()
    try:
        result = session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalars().first()
        if user:
            # Update profile if provided
            if username is not None:
                user.username = username
            if first_name is not None:
                user.first_name = first_name
            if last_name is not None:
                user.last_name = last_name
            session.commit()
            session.refresh(user)
            return user

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user
    except IntegrityError as e:
        session.rollback()
        # Race condition: another request created the user. Fetch it.
        logger.info("get_or_create_user race condition for telegram_id=%s, retrying SELECT: %s", telegram_id, e)
        session2 = SessionLocal()
        try:
            result = session2.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalars().first()
            if user:
                return user
        finally:
            session2.close()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def save_user_filter(
    user_id: int,
    name: str,
    *,
    title_keywords: str | None = None,
    title_exclude_keywords: str | None = None,
    description_keywords: str | None = None,
    description_exclude_keywords: str | None = None,
    city: str | None = None,
    work_format: str | None = None,
    experience: str | None = None,
    employment: str | None = None,
    salary_from: int | None = None,
    monitoring_enabled: bool = False,
) -> SavedFilter:
    """
    Create a new saved filter for a user.

    Returns:
        The created SavedFilter instance.
    """
    session = SessionLocal()
    try:
        now = datetime.now(timezone.utc) if monitoring_enabled else None
        f = SavedFilter(
            user_id=user_id,
            name=name,
            title_keywords=title_keywords,
            title_exclude_keywords=title_exclude_keywords,
            description_keywords=description_keywords,
            description_exclude_keywords=description_exclude_keywords,
            city=city,
            work_format=work_format,
            experience=experience,
            employment=employment,
            salary_from=salary_from,
            monitoring_enabled=monitoring_enabled,
            monitoring_started_at=now if monitoring_enabled else None,
        )
        session.add(f)
        session.commit()
        session.refresh(f)
        return f
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_user_filters(user_id: int) -> list[SavedFilter]:
    """Return all saved filters for a user."""
    session = SessionLocal()
    try:
        result = session.execute(
            select(SavedFilter).where(SavedFilter.user_id == user_id).order_by(SavedFilter.id)
        )
        return list(result.scalars().all())
    finally:
        session.close()


def get_active_users() -> list[User]:
    """Return all users with is_active=True."""
    session = SessionLocal()
    try:
        result = session.execute(
            select(User).where(User.is_active == True).order_by(User.id)
        )
        return list(result.scalars().all())
    finally:
        session.close()


def get_user_monitoring_filters(user_id: int) -> list[SavedFilter]:
    """Return saved filters with monitoring_enabled=True for a user."""
    session = SessionLocal()
    try:
        result = session.execute(
            select(SavedFilter)
            .where(
                SavedFilter.user_id == user_id,
                SavedFilter.monitoring_enabled == True,
            )
            .order_by(SavedFilter.id)
        )
        return list(result.scalars().all())
    finally:
        session.close()


def get_filter_by_id(filter_id: int) -> SavedFilter | None:
    """Get a saved filter by id."""
    session = SessionLocal()
    try:
        result = session.execute(select(SavedFilter).where(SavedFilter.id == filter_id))
        return result.scalars().first()
    finally:
        session.close()


def get_user_filter_by_id(filter_id: int, user_id: int) -> SavedFilter | None:
    """Get a saved filter by id only if it belongs to the user."""
    session = SessionLocal()
    try:
        result = session.execute(
            select(SavedFilter).where(
                SavedFilter.id == filter_id,
                SavedFilter.user_id == user_id,
            )
        )
        return result.scalars().first()
    finally:
        session.close()


def update_filter_monitoring(filter_id: int, user_id: int, enabled: bool) -> bool:
    """
    Enable or disable monitoring for a filter. Returns True if updated.
    When enabling: sets monitoring_started_at if not already set,
    resets last_monitoring_at to NULL so next run does a baseline (no historical spam).
    """
    session = SessionLocal()
    try:
        result = session.execute(
            select(SavedFilter).where(
                SavedFilter.id == filter_id,
                SavedFilter.user_id == user_id,
            )
        )
        f = result.scalars().first()
        if not f:
            return False
        f.monitoring_enabled = enabled
        if enabled:
            if f.monitoring_started_at is None:
                f.monitoring_started_at = datetime.now(timezone.utc)
            f.last_monitoring_at = None  # Baseline on next run
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def update_filter_last_monitoring(filter_id: int, last_at: datetime) -> None:
    """Update last_monitoring_at for a filter (used by monitoring job)."""
    session = SessionLocal()
    try:
        result = session.execute(select(SavedFilter).where(SavedFilter.id == filter_id))
        f = result.scalars().first()
        if f:
            f.last_monitoring_at = last_at
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_user_filter(filter_id: int, user_id: int) -> bool:
    """
    Delete a filter. Returns True if deleted, False if not found or not owned by user.
    """
    session = SessionLocal()
    try:
        result = session.execute(
            select(SavedFilter).where(
                SavedFilter.id == filter_id,
                SavedFilter.user_id == user_id,
            )
        )
        f = result.scalars().first()
        if not f:
            return False
        session.delete(f)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
