"""Applicant-only resume API. Never use the application vacancy token here."""
from urllib.parse import quote
import logging
import re

import requests

from app.config import HH_API_HH_USER_AGENT
from app.hh_api import HHApiError, HHAuthorizationError, _access_token_for_user, refresh_user_hh_token, _hh_drop_duplicate_user_agent

logger = logging.getLogger(__name__)


def resume_request(user_id, method, path, **kwargs):
    url = "https://api.hh.ru" + path
    for attempt in range(2):
        token = _access_token_for_user(user_id)
        if not token:
            raise HHAuthorizationError("Подключите личный аккаунт HH через /connect_hh.")
        try:
            # Match the existing HH client: requests' default User-Agent must
            # not accompany HH-User-Agent.
            request = requests.Request(
                method, url,
                headers={"Authorization": f"Bearer {token}",
                         "HH-User-Agent": HH_API_HH_USER_AGENT}, **kwargs,
            )
            with requests.Session() as session:
                prepared = session.prepare_request(request)
                _hh_drop_duplicate_user_agent(prepared)
                response = session.send(prepared, timeout=25)
        except requests.RequestException as exc:
            # Do not retry POST: HH may have accepted it before the connection failed.
            raise HHApiError("Нет ответа от HH. Попробуем позже.") from exc
        if response.status_code == 401 and attempt == 0:
            refresh_user_hh_token(user_id)
            continue
        if response.status_code not in (200, 204):
            # Log only documented error codes, never raw bodies or headers.
            codes = []
            try:
                for error in response.json().get("errors", []):
                    for key in ("type", "value"):
                        value = str(error.get(key, ""))
                        if re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", value):
                            codes.append(value)
            except (ValueError, AttributeError, TypeError):
                pass
            detail = ", ".join(codes) or "unknown"
            logger.warning("HH_RESUME_API method=%s path=%s user_id=%s status=%s codes=%s",
                           method, path, user_id, response.status_code, detail)
            raise HHApiError(f"HH: HTTP {response.status_code} ({detail}).", status_code=response.status_code)
        return response.json() if response.status_code == 200 else None


def list_my_resumes(user_id):
    items, page = [], 0
    while True:
        data = resume_request(user_id, "GET", "/resumes/mine", params={"page": page, "per_page": 100})
        items.extend(data.get("items", []))
        page += 1
        if page >= data.get("pages", 1):
            return items


def get_resume_status(user_id, resume_id):
    return resume_request(user_id, "GET", f"/resumes/{quote(resume_id, safe='')}/status")


def publish_resume(user_id, resume_id):
    return resume_request(user_id, "POST", f"/resumes/{quote(resume_id, safe='')}/publish")
