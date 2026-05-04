import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app.hh_api import HHAuthorizationError, exchange_code_and_save_tokens, resolve_user_id_from_state
from app.user_repository import get_user_by_id

logger = logging.getLogger(__name__)


def _html(body: str) -> bytes:
    return f"<html><body><h3>{body}</h3></body></html>".encode("utf-8")


def _build_handler(application, loop):
    class HHOAuthCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/hh/callback":
                self.send_response(404)
                self.end_headers()
                return

            qs = parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            state = (qs.get("state") or [None])[0]

            if not code or not state:
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_html("Ошибка: отсутствует code/state"))
                return

            user_id = resolve_user_id_from_state(state)
            if not user_id:
                logger.error("[HH_OAUTH] invalid state received state=%r", state)
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_html("Ошибка авторизации: invalid state"))
                return

            try:
                exchange_code_and_save_tokens(user_id, code)
                ucheck = get_user_by_id(user_id)
                logger.info(
                    "[HH_OAUTH_CALLBACK_OK] tokens_saved internal_user_id=%s telegram_id=%s",
                    user_id,
                    ucheck.telegram_id if ucheck else None,
                )
            except HHAuthorizationError as e:
                logger.exception("[HH_OAUTH] token exchange failed user_id=%s: %s", user_id, e)
                self.send_response(502)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_html("Ошибка: не удалось получить токен HH"))
                return
            except Exception as e:
                logger.exception("[HH_OAUTH] callback failed user_id=%s: %s", user_id, e)
                self.send_response(500)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_html("Внутренняя ошибка"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_html("HH успешно подключен. Можно возвращаться в Telegram."))

            telegram_id = None
            user = get_user_by_id(user_id)
            if user is not None:
                telegram_id = user.telegram_id

            if telegram_id:
                future = asyncio.run_coroutine_threadsafe(
                    application.bot.send_message(
                        chat_id=telegram_id,
                        text="You have successfully authorized on HH.ru ✅",
                    ),
                    loop,
                )
                future.add_done_callback(lambda _: None)

        def log_message(self, format, *args):  # noqa: A003
            logger.info("[HH_OAUTH_HTTP] " + format, *args)

    return HHOAuthCallbackHandler


def start_hh_callback_server(application, loop, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _build_handler(application, loop))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("[HH_OAUTH] callback server started at http://%s:%s/hh/callback", host, port)
    return server
