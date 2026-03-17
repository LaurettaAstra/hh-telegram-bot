"""
Test that the bot is publicly accessible to any Telegram user.

Simulates a new user (NOT in any whitelist) and verifies:
- _ensure_user does NOT return "Доступ запрещен"
- Commands work without access errors
- New user record is created in DB
"""

import asyncio
import os
import sys
from types import SimpleNamespace

# Set dummy BOT_TOKEN before importing main (required at module load)
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN-for-unit-tests")

# Simulate a Telegram user ID that would NEVER be in a whitelist
TEST_TELEGRAM_ID = 999888777666

ACCESS_DENIED = "Доступ запрещен"
USER_FRIENDLY_ERROR = "Не удалось обработать запрос. Попробуйте ещё раз позже."

# Raw error patterns that must NOT be shown to users
RAW_ERROR_PATTERNS = [
    "Ошибка БД",
    "IntegrityError",
    "OperationalError",
    "Connection",
    "traceback",
    "Exception:",
]


def make_mock_update(telegram_id: int, username: str = "test_new_user", first_name: str = "Test", last_name: str = "User"):
    """Create a minimal mock Update with effective_user."""
    user = SimpleNamespace(
        id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
    )
    return SimpleNamespace(effective_user=user)


def make_mock_message_update(telegram_id: int):
    """Create mock Update with message for command handlers."""
    replies = []

    async def mock_reply_text(text, **kwargs):
        replies.append(text)

    msg = SimpleNamespace(reply_text=mock_reply_text)
    update = make_mock_update(telegram_id)
    update.message = msg
    update.message.reply_text = mock_reply_text
    return update, replies


def test_ensure_user_accepts_any_user():
    """Test that _ensure_user accepts a new user and does NOT return 'Доступ запрещен'."""
    from main import _ensure_user

    mock_update = make_mock_update(TEST_TELEGRAM_ID)
    user, err = _ensure_user(mock_update)

    assert err is None, f"Expected no error, got: {err!r}"
    assert ACCESS_DENIED not in (err or ""), "Bot must NOT block with 'Доступ запрещен'"
    assert user is not None, "User should be returned"
    assert user.telegram_id == TEST_TELEGRAM_ID, f"Expected telegram_id={TEST_TELEGRAM_ID}, got {user.telegram_id}"
    print("  [OK] _ensure_user accepts new user, no 'Доступ запрещен'")


def test_user_created_in_db():
    """Verify that a new user record was created in the database."""
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import User

    session = SessionLocal()
    try:
        result = session.execute(select(User).where(User.telegram_id == TEST_TELEGRAM_ID))
        user = result.scalars().first()
        assert user is not None, f"User with telegram_id={TEST_TELEGRAM_ID} should exist in DB"
        print(f"  [OK] User record created: id={user.id}, telegram_id={user.telegram_id}")
    finally:
        session.close()


def test_handlers_receive_user():
    """Test that start/info/filters handlers would get a valid user (no access block)."""
    from main import _ensure_user

    for tid in [TEST_TELEGRAM_ID, 111222333444]:  # Two different "new" users
        mock_update = make_mock_update(tid, username=f"user_{tid}")
        user, err = _ensure_user(mock_update)
        assert err is None, f"User {tid}: expected no error, got {err!r}"
        assert ACCESS_DENIED not in (err or "")
    print("  [OK] Multiple new users can pass _ensure_user")


def _assert_no_raw_error(text: str, context: str = ""):
    """Assert reply does not contain raw DB/technical errors."""
    if not text:
        return
    text_lower = text.lower()
    for pattern in RAW_ERROR_PATTERNS:
        assert pattern.lower() not in text_lower, (
            f"{context}: must not show raw error '{pattern}', got: {text!r}"
        )


async def test_start_handler():
    """Test /start handler: no access denial, no raw DB error, works for new user."""
    from main import start

    async def noop_set_commands(commands):
        pass

    update, replies = make_mock_message_update(TEST_TELEGRAM_ID)
    context = SimpleNamespace(
        bot=SimpleNamespace(set_my_commands=noop_set_commands),
    )
    await start(update, context)
    assert len(replies) >= 1, "Handler should have replied"
    for text in replies:
        assert ACCESS_DENIED not in text, f"/start must not return access denied, got: {text!r}"
        _assert_no_raw_error(text, "/start")
    assert USER_FRIENDLY_ERROR not in replies[0], (
        "/start should succeed for new user, not show error message"
    )
    print("  [OK] /start handler responds correctly (no access denied, no raw error)")


async def test_info_handler():
    """Test /info handler: no access denial, no raw DB error."""
    from main import info

    update, replies = make_mock_message_update(TEST_TELEGRAM_ID)
    context = SimpleNamespace()
    await info(update, context)
    for text in replies:
        assert ACCESS_DENIED not in text, f"/info must not return access denied, got: {text!r}"
        _assert_no_raw_error(text, "/info")
    assert len(replies) >= 1
    print("  [OK] /info handler responds correctly (no access denied, no raw error)")


async def test_filters_handler():
    """Test /filters handler: no access denial, no raw DB error, new user accepted."""
    from main import _ensure_user
    from app.filters_handlers import filters_cmd

    update, replies = make_mock_message_update(TEST_TELEGRAM_ID)
    context = SimpleNamespace()
    await filters_cmd(update, context, _ensure_user)
    for text in replies:
        assert ACCESS_DENIED not in text, f"/filters must not return access denied, got: {text!r}"
        _assert_no_raw_error(text, "/filters")
    assert len(replies) >= 1
    # New user with no filters gets empty state message, not generic error
    assert USER_FRIENDLY_ERROR not in replies[0], (
        "/filters should work for new user, not show generic error"
    )
    print("  [OK] /filters handler responds correctly (no access denied, no raw error)")


def cleanup_test_user():
    """Remove test user from DB to avoid polluting data."""
    try:
        from sqlalchemy import delete
        from app.db import SessionLocal
        from app.models import User

        session = SessionLocal()
        try:
            session.execute(delete(User).where(User.telegram_id == TEST_TELEGRAM_ID))
            session.commit()
            print("  [OK] Test user cleaned up from DB")
        finally:
            session.close()
    except Exception as e:
        print(f"  [WARN] Cleanup failed (non-fatal): {e}")


def main():
    print("=" * 60)
    print("Testing: Bot is publicly accessible")
    print("=" * 60)

    results = []
    try:
        print("\n1. _ensure_user accepts new user (no whitelist check)...")
        test_ensure_user_accepts_any_user()
        results.append(("_ensure_user", True))
    except Exception as e:
        print(f"  [FAIL] {e}")
        results.append(("_ensure_user", False))

    try:
        print("\n2. New user record created in database...")
        test_user_created_in_db()
        results.append(("DB user creation", True))
    except Exception as e:
        print(f"  [FAIL] {e}")
        results.append(("DB user creation", False))

    try:
        print("\n3. Multiple new users can interact...")
        test_handlers_receive_user()
        results.append(("Multiple users", True))
    except Exception as e:
        print(f"  [FAIL] {e}")
        results.append(("Multiple users", False))

    async def run_async_tests():
        for name, coro in [
            ("/start handler", test_start_handler()),
            ("/info handler", test_info_handler()),
            ("/filters handler", test_filters_handler()),
        ]:
            try:
                print(f"\n4. {name}...")
                await coro
                results.append((name, True))
            except Exception as e:
                print(f"  [FAIL] {e}")
                results.append((name, False))

    asyncio.run(run_async_tests())

    print("\n5. Cleanup...")
    cleanup_test_user()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    if passed == total:
        print(f"ALL {total} TESTS PASSED")
        print("Any Telegram user can now use the bot.")
        return 0
    else:
        print(f"FAILED: {total - passed}/{total} tests")
        return 1


if __name__ == "__main__":
    sys.exit(main())
