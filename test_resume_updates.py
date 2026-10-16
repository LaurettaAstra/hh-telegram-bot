"""Isolated tests: no real HH requests or Telegram messages."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock

# Run with DATABASE_URL=sqlite:///:memory:; never use the deployment database.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("BOT_TOKEN", "123456:local-test-token")

from app.db import Base, engine, SessionLocal
from app.models import User, ResumeAutoUpdate
from app import resume_updates as ru
from app import resume_api
from app.hh_api import HHApiError, HHAuthorizationError


class ResumeUpdatesTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(engine)
        with SessionLocal.begin() as session:
            session.add(User(id=1, telegram_id=123, is_active=True))
        ru.set_enabled(1, "my-resume")
        self.status = patch.object(ru, "get_resume_status", return_value={
            "status": {"id": "published"}, "can_publish_or_update": True})
        self.publish = patch.object(ru, "publish_resume")
        self.status_mock = self.status.start()
        self.publish_mock = self.publish.start()
        self.addCleanup(self.status.stop)
        self.addCleanup(self.publish.stop)

    def tearDown(self):
        Base.metadata.drop_all(engine)

    def row(self):
        with SessionLocal() as session:
            return session.get(ResumeAutoUpdate, 1)

    def test_success_is_persisted_and_not_repeated(self):
        ru.update_one(1)
        self.publish_mock.assert_called_once_with(1, "my-resume")
        row = self.row()
        self.assertEqual(ru.utc(row.next_run_at) - ru.utc(row.last_success_at), timedelta(hours=5))
        ru.update_one(1)
        self.assertEqual(self.publish_mock.call_count, 1)

    def test_disable_and_reenable_preserves_cooldown(self):
        ru.update_one(1)
        next_run = self.row().next_run_at
        ru.set_enabled(1)
        ru.set_enabled(1, "my-resume")
        self.assertEqual(self.row().next_run_at, next_run)
        ru.update_one(1)
        self.assertEqual(self.publish_mock.call_count, 1)

    def test_disabled_does_not_request_hh(self):
        ru.set_enabled(1)
        ru.update_one(1)
        self.status_mock.assert_not_called()

    def test_hidden_resume_is_not_published(self):
        self.status_mock.return_value = {"status": {"id": "not_published"}}
        self.assertIsNotNone(ru.update_one(1))
        self.publish_mock.assert_not_called()
        self.assertFalse(self.row().enabled)

    def test_hh_cooldown_is_respected(self):
        allowed = datetime.now(timezone.utc) + timedelta(hours=8)
        self.status_mock.return_value["next_publish_at"] = allowed.isoformat()
        ru.update_one(1)
        self.publish_mock.assert_not_called()
        self.assertEqual(ru.utc(self.row().next_run_at), allowed)

    def test_rate_limit_backs_off(self):
        self.publish_mock.side_effect = HHApiError("Лимит HH", status_code=429)
        ru.update_one(1)
        self.assertTrue(self.row().enabled)
        ru.update_one(1)
        self.assertEqual(self.publish_mock.call_count, 1)

    def test_auth_error_disables_job(self):
        self.status_mock.side_effect = HHAuthorizationError("expired")
        self.assertIsNotNone(ru.update_one(1))
        self.assertFalse(self.row().enabled)

    def test_network_error_does_not_immediately_repeat_post(self):
        self.publish_mock.side_effect = HHApiError("timeout")
        ru.update_one(1)
        ru.update_one(1)
        self.assertEqual(self.publish_mock.call_count, 1)

    def test_another_user_cannot_disable_schedule(self):
        ru.set_enabled(999)
        self.assertTrue(self.row().enabled)


class ResumeApiTest(unittest.TestCase):
    @patch.object(resume_api, "refresh_user_hh_token")
    @patch.object(resume_api, "_access_token_for_user", return_value="applicant-token")
    @patch.object(resume_api.requests, "request")
    def test_publish_refreshes_401_and_accepts_204(self, request, token, refresh):
        request.side_effect = [Mock(status_code=401), Mock(status_code=204)]
        resume_api.publish_resume(1, "abc")
        refresh.assert_called_once_with(1)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args, ("POST", "https://api.hh.ru/resumes/abc/publish"))
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer applicant-token")

    @patch.object(resume_api, "_access_token_for_user", return_value="applicant-token")
    @patch.object(resume_api.requests, "request", side_effect=resume_api.requests.Timeout)
    def test_post_timeout_is_not_retried(self, request, token):
        with self.assertRaises(HHApiError):
            resume_api.publish_resume(1, "abc")
        request.assert_called_once()

    def test_menu_contains_both_buttons(self):
        import main
        labels = [button.text for row in main.MAIN_KEYBOARD.keyboard for button in row]
        self.assertIn(ru.ENABLE_TEXT, labels)
        self.assertIn(ru.DISABLE_TEXT, labels)
        app = main.build_application()
        commands = [command for handler in app.handlers[-1] for command in getattr(handler, "commands", [])]
        self.assertIn("resume_on", commands)
        self.assertIn("resume_off", commands)


if __name__ == "__main__":
    unittest.main()
