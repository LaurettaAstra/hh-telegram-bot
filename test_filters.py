"""Test filters functionality: DB schema and get_user_filters."""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

def test_schema():
    """Verify saved_filters has all columns expected by filters_cmd."""
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set")
        return False
    engine = create_engine(url)
    with engine.connect() as conn:
        r = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'saved_filters' ORDER BY ordinal_position
        """))
        cols = [row[0] for row in r]
    expected = [
        "id", "user_id", "name", "title_keywords", "title_exclude_keywords",
        "description_keywords", "description_exclude_keywords", "city",
        "work_format", "experience", "employment", "salary_from",
        "monitoring_enabled",
    ]
    missing = [c for c in expected if c not in cols]
    if missing:
        print("MISSING columns:", missing)
        return False
    print("OK: All expected columns exist in saved_filters")
    return True


def test_get_user_filters_empty():
    """Test get_user_filters when user has no filters."""
    from app.user_repository import get_user_filters, get_or_create_user

    # Create a user with a unique telegram_id (no filters)
    user = get_or_create_user(telegram_id=999999999)
    filters_list = get_user_filters(user.id)
    assert len(filters_list) == 0, "New user should have 0 filters"
    print("OK: Empty filters case - returns []")
    return True


def test_get_user_filters_with_data():
    """Test get_user_filters - with saved filters."""
    from app.user_repository import get_user_filters, get_or_create_user

    telegram_id = int(os.getenv("TELEGRAM_USER_ID", "0"))
    if not telegram_id:
        print("TELEGRAM_USER_ID not set, skipping get_user_filters test")
        return True

    user = get_or_create_user(telegram_id=telegram_id)
    filters_list = get_user_filters(user.id)
    print(f"OK: get_user_filters returned {len(filters_list)} filter(s)")

    # Simulate filters_cmd display logic (verify all fields readable)
    for f in filters_list:
        monitor = "monitoring" if f.monitoring_enabled else ""
        parts = [f"{f.id}: {f.name}"]
        if f.title_keywords:
            parts.append(f"title: {f.title_keywords}")
        if f.title_exclude_keywords:
            parts.append(f"-{f.title_exclude_keywords}")
        if f.description_keywords:
            parts.append(f"desc: {f.description_keywords}")
        if f.description_exclude_keywords:
            parts.append(f"-desc: {f.description_exclude_keywords}")
        if f.city:
            parts.append(f"city: {f.city}")
        if f.salary_from:
            parts.append(f"salary>={f.salary_from}")
        if f.work_format:
            parts.append(f"format: {f.work_format}")
        if monitor:
            parts.append(monitor)
        print("  Filter:", " ".join(parts))
    return True


if __name__ == "__main__":
    ok = test_schema()
    if ok:
        try:
            test_get_user_filters_empty()
            test_get_user_filters_with_data()
        except Exception as e:
            print("ERROR:", e)
            import traceback
            traceback.print_exc()
            ok = False
    exit(0 if ok else 1)
