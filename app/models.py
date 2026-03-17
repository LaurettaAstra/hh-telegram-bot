"""
SQLAlchemy ORM models matching existing PostgreSQL tables.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False)
    username = Column(Text, nullable=True)
    first_name = Column(Text, nullable=True)
    last_name = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    saved_filters = relationship("SavedFilter", back_populates="user")
    search_sessions = relationship("SearchSession", back_populates="user")


class SavedFilter(Base):
    __tablename__ = "saved_filters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(Text, nullable=False)
    title_keywords = Column(Text, nullable=True)
    title_exclude_keywords = Column(Text, nullable=True)
    description_keywords = Column(Text, nullable=True)
    description_exclude_keywords = Column(Text, nullable=True)
    city = Column(Text, nullable=True)
    work_format = Column(Text, nullable=True)  # schedule: remote, fullDay, etc.
    experience = Column(Text, nullable=True)  # noExperience, between1And3, etc.
    employment = Column(Text, nullable=True)  # full, part, project, etc.
    salary_from = Column(Integer, nullable=True)
    monitor_interval_minutes = Column(Integer, nullable=True)
    monitoring_enabled = Column(Boolean, default=False, nullable=False)
    last_monitoring_at = Column(DateTime(timezone=True), nullable=True)
    monitoring_started_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="saved_filters")
    filter_vacancy_matches = relationship(
        "FilterVacancyMatch",
        back_populates="filter",
        cascade="all, delete-orphan",
    )


class SearchSession(Base):
    __tablename__ = "search_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    title_keywords = Column(Text, nullable=True)
    title_exclude_keywords = Column(Text, nullable=True)
    description_keywords = Column(Text, nullable=True)
    city = Column(Text, nullable=True)
    work_format = Column(Text, nullable=True)
    salary_from = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="search_sessions")


class Vacancy(Base):
    __tablename__ = "vacancies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    hh_id = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    company = Column(Text, nullable=True)
    city = Column(Text, nullable=True)
    salary_from = Column(Integer, nullable=True)
    salary_to = Column(Integer, nullable=True)
    currency = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    raw_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    filter_vacancy_matches = relationship("FilterVacancyMatch", back_populates="vacancy")


class FilterVacancyMatch(Base):
    __tablename__ = "filter_vacancy_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filter_id = Column(Integer, ForeignKey("saved_filters.id"), nullable=False)
    vacancy_id = Column(Integer, ForeignKey("vacancies.id"), nullable=False)
    matched_at = Column(DateTime(timezone=True), server_default=func.now())
    sent_to_user = Column(Boolean, default=False, nullable=False)

    filter = relationship("SavedFilter", back_populates="filter_vacancy_matches")
    vacancy = relationship("Vacancy", back_populates="filter_vacancy_matches")


class VacancySentLog(Base):
    """
    Deduplication: track vacancies sent per user per filter by HH vacancy_id (string).
    Ensures each vacancy is sent only once, even if HH re-indexes or updates it.
    """
    __tablename__ = "vacancy_sent_log"
    __table_args__ = (UniqueConstraint("user_id", "filter_id", "vacancy_id", name="uq_vacancy_sent_user_filter"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    filter_id = Column(Integer, ForeignKey("saved_filters.id"), nullable=False)
    vacancy_id = Column(Text, nullable=False)  # HH API vacancy id (string)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    sent_at = Column(DateTime(timezone=True), server_default=func.now())
