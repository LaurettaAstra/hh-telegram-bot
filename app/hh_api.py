import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from app.config import (
    HH_API_HH_USER_AGENT,
    HH_CLIENT_ID,
    HH_CLIENT_SECRET,
    HH_REDIRECT_URI,
)
from app.user_repository import get_user_by_id, get_user_hh_tokens, save_user_hh_tokens

logger = logging.getLogger(__name__)

# Do not log these request header names (secrets / credentials).
_HH_HEADER_LOG_DENYLIST = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "x-api-key",
    "api-key",
})

# Max chars of response body to log on errors (and preview on success).
_HH_LOG_BODY_MAX = 1000

# Delay before one retry on 403, 5xx, or timeout (seconds).
_HH_RETRY_DELAY_SEC = 2.5

# GET /vacancies (HH OpenAPI) requires **application** OAuth (client_credentials) or **employer**
# user authorization. Applicant (соискатель) user tokens and unauthenticated calls return 403.
# Vacancy search uses application Bearer from ``get_hh_app_access_token``. Applicant OAuth remains
# for future user-specific HH features (e.g. responses, resumes) and ``/me`` diagnostics.
HH_API_VACANCIES_URL = "https://api.hh.ru/vacancies"
HH_API_ME_URL = "https://api.hh.ru/me"
HH_OAUTH_AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
HH_OAUTH_TOKEN_URL = "https://hh.ru/oauth/token"

# Lazily populated on first call to _hh_http_session — single shared client for all api.hh.ru GETs.
_HH_HTTP_CLIENT: requests.Session | None = None
_HH_OAUTH_STATE: dict[str, int] = {}
_HH_OAUTH_STATE_LOCK = threading.Lock()

# In-memory application access token (client_credentials). Not persisted to DB or ``users``.
_hh_app_access_token: str | None = None
_hh_app_access_token_expires_at: datetime | None = None
_HH_APP_TOKEN_LOCK = threading.Lock()


def _hh_full_url(base_url: str, params: dict) -> str:
    """Full URL string for logging (matches requests encoding, including list params)."""
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params, doseq=True)}"


def _hh_http_session() -> requests.Session:
    """Create (once) and return the shared Session; default headers only on the client."""
    global _HH_HTTP_CLIENT
    if _HH_HTTP_CLIENT is None:
        client = requests.Session()
        client.headers.pop("User-Agent", None)
        client.headers.update(
            {
                "HH-User-Agent": HH_API_HH_USER_AGENT,
                "Accept": "application/json",
            }
        )
        _HH_HTTP_CLIENT = client
    return _HH_HTTP_CLIENT


def _hh_drop_duplicate_user_agent(prepared: requests.PreparedRequest) -> None:
    """HH API: use HH-User-Agent only; drop library User-Agent if both would be sent."""
    h = prepared.headers
    if h.get("HH-User-Agent"):
        h.pop("User-Agent", None)


def _hh_headers_for_log(headers) -> dict[str, str]:
    """Copy headers to a dict safe for logs (redact secrets)."""
    safe: dict[str, str] = {}
    for key, value in headers.items():
        lk = key.lower()
        if lk in _HH_HEADER_LOG_DENYLIST:
            safe[key] = "<redacted>"
        else:
            safe[key] = value
    return safe


def _log_hh_outgoing_headers(prepared: requests.PreparedRequest) -> None:
    """Log final merged headers for this GET (no secrets)."""
    safe = _hh_headers_for_log(prepared.headers)
    ua = prepared.headers.get("User-Agent")
    hh_ua = prepared.headers.get("HH-User-Agent")
    logger.info(
        "[HH_API] outgoing GET headers (final): %s | HH-User-Agent=%r User-Agent=%r",
        safe,
        hh_ua,
        ua,
    )


def _log_hh_round_trip_diagnostics(
    response: requests.Response,
    full_url: str,
    params: dict,
    attempt: int,
) -> None:
    """
    Temporary: full snapshot after each HH response (compare 403 vs 200, filter vs minimal).
    """
    req_headers = _hh_headers_for_log(response.request.headers)
    body = response.text[:_HH_LOG_BODY_MAX]
    effective_url = getattr(response, "url", None) or full_url
    logger.info(
        "[HH_API_DIAG] attempt=%s full_url=%s effective_url=%s params=%s status=%s "
        "outgoing_headers=%s body_trunc_%s_chars=%r",
        attempt,
        full_url,
        effective_url,
        params,
        response.status_code,
        req_headers,
        _HH_LOG_BODY_MAX,
        body,
    )


def _hh_send_get(
    session: requests.Session,
    url: str,
    params: dict,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    req = requests.Request("GET", url, params=params, headers=headers)
    prepared = session.prepare_request(req)
    _hh_drop_duplicate_user_agent(prepared)
    _log_hh_outgoing_headers(prepared)
    return session.send(prepared, timeout=timeout)


def _log_hh_probe_summary(
    log_tag: str,
    response: requests.Response,
    full_url: str,
    params: dict,
) -> None:
    """Final probe snapshot: status, body, outgoing headers, effective URL (any HTTP status)."""
    body = response.text[:_HH_LOG_BODY_MAX]
    effective_url = getattr(response, "url", None) or full_url
    outgoing = _hh_headers_for_log(response.request.headers)
    logger.warning(
        "%s status=%s effective_url=%s full_url=%s params=%s "
        "outgoing_headers=%s body_trunc_%s_chars=%r",
        log_tag,
        response.status_code,
        effective_url,
        full_url,
        params,
        outgoing,
        _HH_LOG_BODY_MAX,
        body,
    )


class HHApiError(Exception):
    """HH API HTTP layer failure (after retries). Not used for empty search results."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        response_body_preview: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.response_body_preview = response_body_preview


class HHVacanciesForbiddenError(HHApiError):
    """HH returned 403 for a vacancy-list/search GET (Bearer or anonymous)."""

    def __init__(
        self,
        message: str,
        *,
        user_id: int | None = None,
        status_code: int | None = None,
        url: str | None = None,
        response_body_preview: str | None = None,
        prompt_reauthorize: bool = True,
    ):
        super().__init__(
            message,
            status_code=status_code,
            url=url,
            response_body_preview=response_body_preview,
        )
        self.user_id = user_id
        self.prompt_reauthorize = prompt_reauthorize


class HHAuthorizationError(Exception):
    """HH OAuth2 authorization flow error."""


class HHAppTokenConfigurationError(Exception):
    """Missing or empty env vars required for HH application token (client_credentials) / vacancy search."""


def vacancy_hh_error_user_message(exc: Exception, *, max_detail: int = 900) -> str:
    """
    User-visible text for vacancy GET failures (token endpoint or /vacancies), without implying
    applicant OAuth is required for search.
    """
    detail = (str(exc) or type(exc).__name__).strip()
    if len(detail) > max_detail:
        detail = detail[: max_detail - 3] + "..."
    return (
        "HH.ru vacancy request failed (application authorization). "
        "Applicant HH login is not required for vacancy search and will not fix this. "
        "Technical detail:\n\n"
        f"{detail}"
    )


def _require_hh_app_oauth_env() -> None:
    missing: list[str] = []
    if not (HH_CLIENT_ID or "").strip():
        missing.append("HH_CLIENT_ID")
    if not (HH_CLIENT_SECRET or "").strip():
        missing.append("HH_CLIENT_SECRET")
    if not (HH_API_HH_USER_AGENT or "").strip():
        missing.append("HH_API_HH_USER_AGENT")
    if missing:
        raise HHAppTokenConfigurationError(
            "Vacancy search requires HH application OAuth. Set these environment variables: "
            + ", ".join(missing)
        )


def _invalidate_hh_app_access_token_cache() -> None:
    global _hh_app_access_token, _hh_app_access_token_expires_at
    with _HH_APP_TOKEN_LOCK:
        _hh_app_access_token = None
        _hh_app_access_token_expires_at = None


def get_hh_app_access_token() -> str:
    """
    Return OAuth access token for the registered HH **application** (grant client_credentials).

    Cached in memory until shortly before HH ``expires_in`` (refresh 60 seconds early).
    """
    _require_hh_app_oauth_env()
    global _hh_app_access_token, _hh_app_access_token_expires_at
    now = datetime.now(timezone.utc)
    margin = timedelta(seconds=60)
    with _HH_APP_TOKEN_LOCK:
        if (
            _hh_app_access_token
            and _hh_app_access_token_expires_at
            and now < _hh_app_access_token_expires_at - margin
        ):
            logger.info(
                "[HH_APP_TOKEN] reused=%s expires_at=%s",
                True,
                _hh_app_access_token_expires_at.isoformat(),
            )
            return _hh_app_access_token

    data = _request_hh_token(
        {
            "grant_type": "client_credentials",
            "client_id": HH_CLIENT_ID.strip(),
            "client_secret": HH_CLIENT_SECRET.strip(),
        }
    )
    raw = data["access_token"]
    exp_in = data.get("expires_in")
    try:
        sec = int(exp_in) if exp_in is not None else 3600
    except (TypeError, ValueError):
        sec = 3600
    wall = datetime.now(timezone.utc) + timedelta(seconds=sec)
    logger.info(
        "[HH_APP_TOKEN] requested_new expires_at=%s",
        wall.isoformat(),
    )
    with _HH_APP_TOKEN_LOCK:
        _hh_app_access_token = raw
        _hh_app_access_token_expires_at = wall
        return raw


def _log_access_token_used(user_id: int, raw_token: str, attempt: int, note: str) -> None:
    """Safe token fingerprint for debugging (no full secret)."""
    user = get_user_by_id(user_id)
    telegram_id = user.telegram_id if user else None
    tl = len(raw_token)
    prefix = raw_token[:6] if tl >= 6 else raw_token
    bearer_ok = raw_token.strip() == raw_token and tl > 0
    logger.info(
        "[HH_TOKEN_USE] user_id=%s telegram_id=%s attempt=%s token_len=%s token_prefix=%s "
        "strip_clean=%s hh_user_agent_configured=%s note=%s",
        user_id,
        telegram_id,
        attempt + 1,
        tl,
        prefix,
        bearer_ok,
        bool(HH_API_HH_USER_AGENT and str(HH_API_HH_USER_AGENT).strip()),
        note,
    )


def _log_hh_response_errors(status: int, body_preview: str, user_id: int | None, ctx: str) -> None:
    """Log HH JSON errors[].type / errors[].value (no secrets)."""
    try:
        parsed = json.loads(body_preview)
    except json.JSONDecodeError:
        logger.warning(
            "[HH_DIAG_ERRORS] ctx=%s status=%s user_id=%s json_parse_failed preview=%r",
            ctx,
            status,
            user_id,
            body_preview[:400],
        )
        return
    if not isinstance(parsed, dict):
        return
    req_id = parsed.get("request_id")
    errors = parsed.get("errors")
    if isinstance(errors, list):
        for i, err in enumerate(errors[:10]):
            if isinstance(err, dict):
                logger.warning(
                    "[HH_DIAG_ERRORS] ctx=%s status=%s user_id=%s idx=%s type=%s value=%s request_id=%s",
                    ctx,
                    status,
                    user_id,
                    i,
                    err.get("type"),
                    err.get("value"),
                    req_id,
                )
        if status == 403:
            _log_hh_403_hints(errors, user_id)
    elif status >= 400:
        logger.warning("[HH_DIAG_ERRORS] ctx=%s status=%s user_id=%s no_errors_array keys=%s", ctx, status, user_id, list(parsed.keys()))


def _log_hh_403_hints(errors: list, user_id: int | None) -> None:
    """Narrative hints based on HH API error docs (github.com/hhru/api)."""
    pairs: list[tuple[object, object]] = []
    values: list[str] = []
    for err in errors:
        if isinstance(err, dict):
            t, v = err.get("type"), err.get("value")
            pairs.append((t, v))
            if isinstance(v, str):
                values.append(v)
    logger.error("[HH_DIAG_403] user_id=%s errors_pairs=%s", user_id, pairs)
    if any(v == "user_auth_expected" for v in values):
        logger.error(
            "[HH_DIAG_403_HINT] user_id=%s HH API says user OAuth is required for this call "
            "(application-only token is not enough).",
            user_id,
        )
    if any(v in ("bad_authorization", "token_expired", "token_revoked") for v in values):
        logger.error(
            "[HH_DIAG_403_HINT] user_id=%s OAuth token rejected by HH — re-authorize or refresh.",
            user_id,
        )
    if any(v == "application_not_found" for v in values):
        logger.error("[HH_DIAG_403_HINT] user_id=%s OAuth application deleted or invalid client_id.", user_id)
    if pairs and all(t == "forbidden" and v in (None, "") for t, v in pairs):
        logger.error(
            "[HH_DIAG_403_HINT] user_id=%s Generic forbidden (no structured oauth value). Often: "
            "application lacks OpenAPI/API access in https://dev.hh.ru cabinet, HH rate/app policy, "
            "or wrong token class — not caused by vacancy query params like period/schedule alone.",
            user_id,
        )


def _hh_forbidden_should_prompt_reauth(body_preview: str) -> bool:
    """
    False when HH returns only a bare errors[{'type':'forbidden'}] without oauth `value`
    (typical app/OpenAPI restriction): re-authorization will not fix vacancy 403.
    True when oauth/token hints appear — user should reconnect OAuth or refresh.
    """
    try:
        parsed = json.loads(body_preview)
    except json.JSONDecodeError:
        return True
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if not isinstance(errors, list):
        return True
    oauth_values = {
        "bad_authorization",
        "token_expired",
        "token_revoked",
        "user_auth_expected",
        "application_not_found",
    }
    for err in errors:
        if isinstance(err, dict):
            if err.get("type") == "oauth":
                return True
            v = err.get("value")
            if isinstance(v, str) and v in oauth_values:
                return True
    if (
        len(errors) == 1
        and isinstance(errors[0], dict)
        and errors[0].get("type") == "forbidden"
        and errors[0].get("value") in (None, "")
    ):
        return False
    return True


def _oauth_expires_at(expires_in: int | None) -> datetime | None:
    if not expires_in:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))


def get_hh_authorize_url(user_id: int) -> str:
    """Build HH OAuth authorize URL and store state -> user mapping."""
    if not HH_CLIENT_ID or not HH_REDIRECT_URI:
        raise HHAuthorizationError("HH OAuth config is missing (HH_CLIENT_ID / HH_REDIRECT_URI)")
    state = secrets.token_urlsafe(24)
    with _HH_OAUTH_STATE_LOCK:
        _HH_OAUTH_STATE[state] = user_id
    params = {
        "response_type": "code",
        "client_id": HH_CLIENT_ID,
        "redirect_uri": HH_REDIRECT_URI,
        "state": state,
    }
    return f"{HH_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def resolve_user_id_from_state(state: str) -> int | None:
    """One-time read of stored OAuth state."""
    if not state:
        return None
    with _HH_OAUTH_STATE_LOCK:
        return _HH_OAUTH_STATE.pop(state, None)


def _request_hh_token(payload: dict[str, str]) -> dict:
    grant = payload.get("grant_type", "?")
    redirect_present = "redirect_uri" in payload
    logger.info(
        "[HH_OAUTH_HTTP] POST %s grant_type=%s redirect_uri_in_body=%s client_id_present=%s",
        HH_OAUTH_TOKEN_URL,
        grant,
        redirect_present,
        bool(payload.get("client_id")),
    )
    response = requests.post(
        HH_OAUTH_TOKEN_URL,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=20,
    )
    logger.info("[HH_OAUTH_HTTP] token_endpoint_http_status=%s grant_type=%s", response.status_code, grant)
    if response.status_code != 200:
        body = response.text[:_HH_LOG_BODY_MAX]
        try:
            ej = json.loads(body)
            if isinstance(ej, dict):
                logger.warning(
                    "[HH_OAUTH_HTTP] token_endpoint_error error=%r error_description=%r",
                    ej.get("error"),
                    ej.get("error_description"),
                )
        except json.JSONDecodeError:
            pass
        _log_hh_response_errors(response.status_code, body, None, "oauth_token_endpoint")
        raise HHAuthorizationError(f"Token request failed HTTP {response.status_code}: {body}")
    data = response.json()
    if not isinstance(data, dict) or "access_token" not in data:
        raise HHAuthorizationError("Token response does not contain access_token")
    logger.info(
        "[HH_OAUTH_HTTP] token_json_keys=%s expires_in=%s refresh_token_present=%s token_type=%s",
        sorted(data.keys()),
        data.get("expires_in"),
        bool(data.get("refresh_token")),
        data.get("token_type"),
    )
    return data


def exchange_code_and_save_tokens(user_id: int, code: str) -> None:
    """Exchange authorization code for tokens and persist for user."""
    if not HH_CLIENT_ID or not HH_CLIENT_SECRET or not HH_REDIRECT_URI:
        raise HHAuthorizationError(
            "HH OAuth config is missing (HH_CLIENT_ID / HH_CLIENT_SECRET / HH_REDIRECT_URI)"
        )
    data = _request_hh_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": HH_CLIENT_ID,
            "client_secret": HH_CLIENT_SECRET,
            "redirect_uri": HH_REDIRECT_URI,
        }
    )
    expires_at = _oauth_expires_at(data.get("expires_in"))
    access_raw = data["access_token"]
    ok = save_user_hh_tokens(
        user_id,
        access_token=access_raw,
        refresh_token=data.get("refresh_token"),
        expires_at=expires_at,
    )
    if not ok:
        raise HHAuthorizationError(f"User {user_id} not found for token storage")
    user_row = get_user_by_id(user_id)
    telegram_id = user_row.telegram_id if user_row else None
    at_saved = user_row.hh_access_token if user_row else ""
    logger.info(
        "[HH_OAUTH_EXCHANGE_OK] user_id=%s telegram_id=%s internal_mapping_ok=true "
        "access_token_len_saved=%s access_token_prefix=%s expires_at=%s expires_in_json=%s "
        "refresh_saved_present=%s token_event=fresh_from_authorization_code",
        user_id,
        telegram_id,
        len(at_saved) if at_saved else 0,
        (at_saved[:6] if at_saved and len(at_saved) >= 6 else at_saved or ""),
        expires_at,
        data.get("expires_in"),
        bool(user_row.hh_refresh_token if user_row else False),
    )
    oauth_probe_authenticated_me(user_id)


def refresh_user_hh_token(user_id: int) -> str:
    """Refresh expired HH access token and return new access token."""
    tokens = get_user_hh_tokens(user_id)
    if not tokens or not tokens.refresh_token:
        raise HHAuthorizationError("Missing refresh_token; reconnect HH account")
    data = _request_hh_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": HH_CLIENT_ID,
            "client_secret": HH_CLIENT_SECRET,
        }
    )
    new_refresh_token = data.get("refresh_token") or tokens.refresh_token
    expires_at = _oauth_expires_at(data.get("expires_in"))
    save_user_hh_tokens(
        user_id,
        access_token=data["access_token"],
        refresh_token=new_refresh_token,
        expires_at=expires_at,
    )
    user_row = get_user_by_id(user_id)
    at_saved = user_row.hh_access_token if user_row else ""
    logger.info(
        "[HH_OAUTH_REFRESH_OK] user_id=%s telegram_id=%s access_token_len_saved=%s access_token_prefix=%s "
        "expires_at=%s token_event=refresh_grant",
        user_id,
        user_row.telegram_id if user_row else None,
        len(at_saved) if at_saved else 0,
        (at_saved[:6] if at_saved and len(at_saved) >= 6 else at_saved or ""),
        expires_at,
    )
    return data["access_token"]


def _access_token_for_user(user_id: int | None) -> str | None:
    if user_id is None:
        return None
    tokens = get_user_hh_tokens(user_id)
    if not tokens:
        raise HHAuthorizationError("HH account is not connected for this user")
    if tokens.expires_at and datetime.now(timezone.utc) >= tokens.expires_at:
        logger.info(
            "[HH_API] access token expired for user_id=%s, refreshing token_event=expires_at_passed",
            user_id,
        )
        return refresh_user_hh_token(user_id)
    return tokens.access_token


def oauth_probe_authenticated_me(user_id: int) -> None:
    """
    After OAuth token save: GET /me with same Bearer + HH-User-Agent as api.hh.ru calls.
    If this returns 200 but /vacancies returns 403, token is valid — restriction is app/endpoint-level.
    """
    try:
        token = _access_token_for_user(user_id)
    except HHAuthorizationError as e:
        logger.warning("[HH_OAUTH_ME_PROBE] user_id=%s cannot_resolve_token: %s", user_id, e)
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    session = _hh_http_session()
    req = requests.Request("GET", HH_API_ME_URL, headers=headers)
    prepared = session.prepare_request(req)
    _hh_drop_duplicate_user_agent(prepared)
    _log_hh_outgoing_headers(prepared)
    logger.info(
        "[HH_OAUTH_ME_PROBE_HEADERS] user_id=%s Accept=%r HH_User_Agent=%r library_User_Agent=%r "
        "authorization_header_present=%s starts_with_Bearer_=%s",
        user_id,
        prepared.headers.get("Accept"),
        prepared.headers.get("HH-User-Agent"),
        prepared.headers.get("User-Agent"),
        bool(prepared.headers.get("Authorization")),
        str(prepared.headers.get("Authorization", "")).startswith("Bearer "),
    )
    try:
        resp = session.send(prepared, timeout=15)
    except requests.Timeout as e:
        logger.warning("[HH_OAUTH_ME_PROBE] user_id=%s GET %s timeout=%s", user_id, HH_API_ME_URL, e)
        return
    preview = resp.text[:500]
    ok = resp.status_code == 200
    logger.info(
        "[HH_OAUTH_ME_PROBE] user_id=%s url=%s http_status=%s ok=%s body_preview=%r",
        user_id,
        HH_API_ME_URL,
        resp.status_code,
        ok,
        preview,
    )
    if ok:
        logger.info(
            "[HH_OAUTH_ME_PROBE_CONCLUSION] user_id=%s authenticated_/me_200=true "
            "— if vacancy search still gets 403 with same token, cause is likely "
            "HH developer app OpenAPI/API access or endpoint policy, not invalid Bearer encoding.",
            user_id,
        )
    else:
        logger.warning(
            "[HH_OAUTH_ME_PROBE_CONCLUSION] user_id=%s authenticated_/me_http_%s "
            "— token may be rejected globally or app misconfigured before vacancy calls.",
            user_id,
            resp.status_code,
        )


def _hh_request_get(
    url: str,
    params: dict | None = None,
    *,
    timeout: float = 20,
    user_id: int | None = None,
) -> requests.Response:
    """
    GET helper for api.hh.ru with optional **applicant** user Bearer (e.g. ``/me``).

    Vacancy list ``GET /vacancies`` uses ``_hh_request_get_vacancies`` (application token only).
    """
    params = dict(params) if params else {}
    full_url = _hh_full_url(url, params)

    for attempt in range(2):
        auth_headers = None
        try:
            token = _access_token_for_user(user_id)
            if token:
                auth_headers = {"Authorization": f"Bearer {token}"}
                _log_access_token_used(user_id, token, attempt, "before_GET_api_hh_ru")
            scheme_ok = bool(auth_headers and auth_headers.get("Authorization", "").startswith("Bearer "))
            if auth_headers and not scheme_ok:
                logger.error("[HH_TOKEN_USE] user_id=%s Authorization_header_missing_Bearer_prefix", user_id)
        except HHAuthorizationError as e:
            raise HHApiError(str(e), url=full_url) from e
        try:
            response = _hh_send_get(_hh_http_session(), url, params, timeout, headers=auth_headers)
            _log_hh_round_trip_diagnostics(response, full_url, params, attempt + 1)
        except requests.Timeout as e:
            logger.error(
                "[HH_API] request timeout attempt=%s/2 url=%s error=%s "
                "(outgoing headers were logged immediately before send)",
                attempt + 1,
                full_url,
                e,
            )
            if attempt == 0:
                time.sleep(_HH_RETRY_DELAY_SEC)
                continue
            raise HHApiError(
                f"HH API request timed out: {e}",
                url=full_url,
            ) from e

        status = response.status_code
        body_preview = response.text[:_HH_LOG_BODY_MAX]

        if status == 200:
            logger.info("[HH_API] GET ok status=%s url=%s (details in HH_API_DIAG)", status, full_url)
            return response

        logger.error(
            "[HH_API] HH API error status=%s url=%s (details in HH_API_DIAG)",
            status,
            full_url,
        )
        _log_hh_response_errors(status, body_preview, user_id, "api_get_non_200")

        if attempt == 0 and user_id is not None and status == 401:
            try:
                refresh_user_hh_token(user_id)
                time.sleep(0.3)
                continue
            except HHAuthorizationError as refresh_err:
                raise HHApiError(str(refresh_err), status_code=status, url=full_url) from refresh_err

        if attempt == 0 and user_id is not None and status == 403:
            logger.info(
                "[HH_API] HTTP 403: attempting oauth refresh user_id=%s (HH may still return 403 if issue is app/OpenAPI policy)",
                user_id,
            )
            try:
                refresh_user_hh_token(user_id)
                time.sleep(0.3)
                continue
            except HHAuthorizationError as rerr:
                logger.warning("[HH_API] refresh_after_403_failed user_id=%s: %s", user_id, rerr)

        if attempt == 0 and (status == 403 or status >= 500):
            time.sleep(_HH_RETRY_DELAY_SEC)
            continue

        if status == 403:
            reprompt = False
            if user_id is not None:
                reprompt = _hh_forbidden_should_prompt_reauth(body_preview)
            logger.error(
                "[HH_API_403_FINAL] user_id=%s url=%s prompt_reauthorize=%s "
                "(non-vacancies GET with applicant Bearer; vacancy list uses application token separately)",
                user_id,
                full_url,
                reprompt,
            )
            raise HHVacanciesForbiddenError(
                f"HH API returned HTTP {status}: {body_preview}",
                user_id=user_id,
                status_code=status,
                url=full_url,
                response_body_preview=body_preview,
                prompt_reauthorize=reprompt,
            )

        raise HHApiError(
            f"HH API returned HTTP {status}: {body_preview}",
            status_code=status,
            url=full_url,
            response_body_preview=body_preview,
        )

    raise HHApiError("HH API request failed after retries", url=full_url)


def _hh_request_get_vacancies(
    url: str,
    params: dict | None = None,
    *,
    timeout: float = 20,
    log_user_id: int | None = None,
    source: str = "vacancies",
) -> requests.Response:
    """
    GET ``/vacancies`` with **application** OAuth Bearer only (never applicant user token).
    Retries once on timeout, 5xx, or 401 after invalidating the cached app token.
    """
    params = dict(params) if params else {}
    full_url = _hh_full_url(url, params)

    for attempt in range(2):
        try:
            app_token = get_hh_app_access_token()
        except HHAppTokenConfigurationError:
            raise
        except HHAuthorizationError as e:
            raise HHApiError(str(e), url=full_url) from e

        with _HH_APP_TOKEN_LOCK:
            token_present = bool(_hh_app_access_token)
            exp_at = (
                _hh_app_access_token_expires_at.isoformat()
                if _hh_app_access_token_expires_at
                else None
            )
        logger.info(
            "[HH_VACANCIES_AUTH] auth_type=application token_present=%s expires_at=%s source=%s log_user_id=%s",
            token_present,
            exp_at,
            source,
            log_user_id,
        )

        auth_headers = {
            "Authorization": f"Bearer {app_token}",
            "Accept": "application/json",
        }
        try:
            response = _hh_send_get(
                _hh_http_session(), url, params, timeout, headers=auth_headers
            )
            _log_hh_round_trip_diagnostics(response, full_url, params, attempt + 1)
        except requests.Timeout as e:
            logger.error(
                "[HH_VACANCIES] request timeout attempt=%s/2 url=%s error=%s",
                attempt + 1,
                full_url,
                e,
            )
            if attempt == 0:
                time.sleep(_HH_RETRY_DELAY_SEC)
                continue
            raise HHApiError(f"HH API request timed out: {e}", url=full_url) from e

        status = response.status_code
        body_preview = response.text[:_HH_LOG_BODY_MAX]

        if status == 200:
            logger.info(
                "[HH_VACANCIES] GET ok status=%s url=%s (details in HH_API_DIAG)",
                status,
                full_url,
            )
            return response

        logger.error(
            "[HH_VACANCIES] HH API error status=%s url=%s (details in HH_API_DIAG)",
            status,
            full_url,
        )
        _log_hh_response_errors(status, body_preview, log_user_id, "vacancies_get_non_200")

        if attempt == 0 and status == 401:
            logger.warning(
                "[HH_VACANCIES] HTTP 401 with application token — invalidating app token cache, retrying"
            )
            _invalidate_hh_app_access_token_cache()
            time.sleep(0.3)
            continue

        if attempt == 0 and (status == 403 or status >= 500):
            time.sleep(_HH_RETRY_DELAY_SEC)
            continue

        if status == 403:
            with _HH_APP_TOKEN_LOCK:
                tp = bool(_hh_app_access_token)
                tex = (
                    _hh_app_access_token_expires_at.isoformat()
                    if _hh_app_access_token_expires_at
                    else None
                )
            logger.error(
                "[HH_VACANCIES_403] application_token_used=true token_present=%s "
                "token_expires_at=%s response_status=%s log_user_id=%s hh_error_body_trunc=%r",
                tp,
                tex,
                status,
                log_user_id,
                body_preview,
            )
            raise HHVacanciesForbiddenError(
                f"HH API returned HTTP {status}: {body_preview}",
                user_id=log_user_id,
                status_code=status,
                url=full_url,
                response_body_preview=body_preview,
                prompt_reauthorize=False,
            )

        raise HHApiError(
            f"HH API returned HTTP {status}: {body_preview}",
            status_code=status,
            url=full_url,
            response_body_preview=body_preview,
        )

    raise HHApiError("HH vacancies request failed after retries", url=full_url)


def hh_api_minimal_probe(*, timeout: float = 20) -> int:
    """
    Diagnostics: minimal GET /vacancies with the same application auth as production.

    Params: text=аналитик, per_page=1, page=0 only.

    Returns:
        0 if HTTP 200, 1 otherwise.
    """
    params = {"text": "аналитик", "per_page": 1, "page": 0}
    full_url = _hh_full_url(HH_API_VACANCIES_URL, params)
    logger.warning("[HH_API_PROBE] minimal GET %s (application Bearer)", full_url)
    try:
        response = _hh_request_get_vacancies(
            HH_API_VACANCIES_URL,
            params=params,
            timeout=timeout,
            source="hh_api_minimal_probe",
        )
    except HHVacanciesForbiddenError as exc:
        logger.warning(
            "[HH_API_PROBE] forbidden status=%s body_preview=%r",
            exc.status_code,
            (exc.response_body_preview or "")[:_HH_LOG_BODY_MAX],
        )
        return 1
    except HHApiError:
        return 1
    _log_hh_probe_summary("[HH_API_PROBE]", response, full_url, params)
    return 0 if response.status_code == 200 else 1


def hh_api_no_text_probe(*, timeout: float = 20) -> int:
    """
    Diagnostics: GET /vacancies with only per_page=1, page=0 (no ``text`` param).

    Returns:
        0 if HTTP 200, 1 otherwise.
    """
    params = {"per_page": 1, "page": 0}
    full_url = _hh_full_url(HH_API_VACANCIES_URL, params)
    logger.warning("[HH_API_PROBE_NO_TEXT] GET %s (application Bearer)", full_url)
    try:
        response = _hh_request_get_vacancies(
            HH_API_VACANCIES_URL,
            params=params,
            timeout=timeout,
            source="hh_api_no_text_probe",
        )
    except HHVacanciesForbiddenError as exc:
        logger.warning(
            "[HH_API_PROBE_NO_TEXT] forbidden status=%s body_preview=%r",
            exc.status_code,
            (exc.response_body_preview or "")[:_HH_LOG_BODY_MAX],
        )
        return 1
    except HHApiError:
        return 1
    _log_hh_probe_summary("[HH_API_PROBE_NO_TEXT]", response, full_url, params)
    return 0 if response.status_code == 200 else 1


INCLUDE_TITLE_WORDS = [
    "системный аналитик",
    "бизнес аналитик",
    "бизнес-аналитик"
]

EXCLUDE_TITLE_WORDS = [
    "1с",
    "1c",
    "битрикс",
    "bitrix",
    "dwh",
    "lead",
    "senior"
]


def is_title_allowed(title: str) -> bool:
    title_lower = title.lower()

    if not any(word in title_lower for word in INCLUDE_TITLE_WORDS):
        return False

    if any(word in title_lower for word in EXCLUDE_TITLE_WORDS):
        return False

    return True


def is_remote(schedule_name: str) -> bool:
    if not schedule_name:
        return False

    schedule_lower = schedule_name.lower()

    remote_markers = [
        "удален",
        "remote"
    ]

    return any(marker in schedule_lower for marker in remote_markers)


def format_salary(salary_data):
    if not salary_data:
        return "Не указана"

    salary_from = salary_data.get("from")
    salary_to = salary_data.get("to")
    currency = salary_data.get("currency", "")

    if salary_from and salary_to:
        return f"{salary_from}-{salary_to} {currency}"

    if salary_from:
        return f"от {salary_from} {currency}"

    if salary_to:
        return f"до {salary_to} {currency}"

    return "Не указана"


_HH_TEXT_OPERATOR_TOKENS = frozenset({"and", "not", "or"})


def _sanitize_hh_api_outgoing_text(text: str | None) -> str:
    """
    Temporary HH API compatibility: outgoing text must be plain words only.
    Strips boolean operator tokens (AND/NOT/OR), collapses whitespace.
    """
    if not text or not str(text).strip():
        return ""
    out: list[str] = []
    for w in str(text).split():
        w = w.strip()
        if not w:
            continue
        if w.lower() in _HH_TEXT_OPERATOR_TOKENS:
            continue
        out.append(w)
    return " ".join(out)


def _plain_hh_search_text(
    title_keywords: str | None,
    title_exclude_keywords: str | None,
    description_keywords: str | None,
    description_exclude_keywords: str | None,
    city: str | None,
) -> str:
    """
    Join positive keyword fields + city, strip AND/NOT/OR, drop tokens listed in exclude fields.
    Exclude columns stay in DB for client-side filters; matched tokens are omitted from HH text.
    """
    exclude_lower: set[str] = set()
    for ex_src in (title_exclude_keywords, description_exclude_keywords):
        if ex_src and str(ex_src).strip():
            for w in str(ex_src).split():
                w = w.strip()
                if w:
                    exclude_lower.add(w.lower())

    chunks: list[str] = []
    for raw in (title_keywords, description_keywords):
        if raw and str(raw).strip():
            chunks.append(str(raw).strip())
    merged = " ".join(chunks)
    if city and str(city).strip():
        merged = f"{merged} {city.strip()}".strip() if merged else city.strip()

    tokens: list[str] = []
    for w in merged.split():
        w = w.strip()
        if not w:
            continue
        if w.lower() in _HH_TEXT_OPERATOR_TOKENS:
            continue
        if w.lower() in exclude_lower:
            continue
        tokens.append(w)
    return " ".join(tokens)


def _build_search_text(
    title_keywords: str | None,
    title_exclude_keywords: str | None,
    description_keywords: str | None,
    description_exclude_keywords: str | None,
    city: str | None,
) -> tuple[str, list[str]]:
    """
    Build HH API text and search_field from user inputs.

    Temporary compatibility: no AND/NOT in the outgoing HH query — only plain words from
    positive keyword fields (title + description) and city; operator tokens stripped;
    words from exclude-keyword fields removed from the HH text token list.
    search_field is fixed to name-only for this simplified query.
    """
    text = _plain_hh_search_text(
        title_keywords,
        title_exclude_keywords,
        description_keywords,
        description_exclude_keywords,
        city,
    )
    text = text or "работа"
    return text, ["name"]


def get_vacancies_page(
    page: int,
    search_params: dict | None = None,
    per_page: int = 100,
    user_id: int | None = None,
    *,
    source: str = "get_vacancies_page",
):
    """
    GET https://api.hh.ru/vacancies — vacancy search (HH OpenAPI).

    Uses **application** OAuth (``client_credentials``) via ``get_hh_app_access_token``.
    Optional ``user_id`` is **log context only** (not sent as applicant Bearer).

    Params match the public API (text, search_field, area, period, salary, etc.).
    See: https://api.hh.ru/openapi/redoc#tag/Poisk-vakansij/operation/get-vacancies
    """
    params = {
        "per_page": per_page,
        "page": page,
        "order_by": "publication_time",
    }

    if search_params:
        if search_params.get("text"):
            params["text"] = search_params["text"]
        if search_params.get("search_field"):
            params["search_field"] = search_params["search_field"]
        if search_params.get("area"):
            params["area"] = search_params["area"]
        if search_params.get("period"):
            params["period"] = search_params["period"]
        if search_params.get("experience"):
            params["experience"] = search_params["experience"]
        if search_params.get("employment"):
            params["employment"] = search_params["employment"]
        if search_params.get("schedule"):
            params["schedule"] = search_params["schedule"]
        if search_params.get("salary_from"):
            params["salary"] = search_params["salary_from"]

    if "text" not in params:
        params["text"] = "аналитик"

    cleaned_text = _sanitize_hh_api_outgoing_text(params.get("text") or "")
    params["text"] = cleaned_text if cleaned_text else "аналитик"

    logger.info("[HH_API] get_vacancies_page request URL=%s", _hh_full_url(HH_API_VACANCIES_URL, params))

    response = _hh_request_get_vacancies(
        HH_API_VACANCIES_URL,
        params=params,
        timeout=20,
        log_user_id=user_id,
        source=source,
    )

    try:
        data = response.json()
    except json.JSONDecodeError as e:
        logger.exception("[HH_API] get_vacancies_page JSON decode failed: %s", e)
        raise

    if not isinstance(data, dict):
        raise ValueError(
            f"HH API JSON must be an object (dict), got {type(data).__name__!r}"
        )

    items = data.get("items")
    logger.info(
        "[HH_API] get_vacancies_page parsed keys=%s items_len=%s found=%s",
        list(data.keys()),
        len(items) if isinstance(items, list) else type(items).__name__,
        data.get("found"),
    )

    return data


def _search_params_from_filter(f) -> dict:
    """Build HH API search params from a SavedFilter instance."""
    text, search_field = _build_search_text(
        title_keywords=getattr(f, "title_keywords", None),
        title_exclude_keywords=getattr(f, "title_exclude_keywords", None),
        description_keywords=getattr(f, "description_keywords", None),
        description_exclude_keywords=getattr(f, "description_exclude_keywords", None),
        city=getattr(f, "city", None),
    )
    params = {"text": text, "search_field": search_field}
    if getattr(f, "work_format", None):
        params["schedule"] = f.work_format
    if getattr(f, "experience", None):
        params["experience"] = f.experience
    if getattr(f, "employment", None):
        params["employment"] = f.employment
    if getattr(f, "salary_from", None):
        params["salary_from"] = f.salary_from
    return params


def _title_matches_filter(name: str, filter_obj) -> bool:
    """Check if vacancy title matches filter (exclude keywords)."""
    if not filter_obj or not getattr(filter_obj, "title_exclude_keywords", None):
        return True
    exclude = filter_obj.title_exclude_keywords.lower().split()
    name_lower = name.lower()
    return not any(word in name_lower for word in exclude if word)


def _description_matches_filter(item: dict, filter_obj) -> bool:
    """Check if vacancy description matches filter (exclude keywords)."""
    if not filter_obj or not getattr(filter_obj, "description_exclude_keywords", None):
        return True
    exclude = filter_obj.description_exclude_keywords.lower().split()
    snippet = item.get("snippet") or {}
    if isinstance(snippet, dict):
        text = " ".join(s for s in snippet.values() if isinstance(s, str)).lower()
    else:
        text = str(snippet).lower()
    text += " " + (item.get("name") or "").lower()
    return not any(word in text for word in exclude if word)


def _schedule_matches_filter(schedule_name: str, filter_obj) -> bool:
    """Check if schedule matches filter. If filter has work_format with remote, require remote."""
    wf = getattr(filter_obj, "work_format", None) if filter_obj else None
    if not wf:
        return True  # "не важно" - accept any schedule
    wf = wf.lower()
    if "remote" in wf or "удален" in wf:
        return is_remote(schedule_name)
    return True  # accept any schedule


def search_vacancies_page(
    page: int,
    search_params: dict | None = None,
    filter_obj=None,
    user_id: int | None = None,
    *,
    source: str = "search_vacancies_page",
) -> tuple[int, list]:
    """
    Fetch one page of vacancies from HH API (10 per page for pagination).

    ``user_id`` is optional **Telegram/DB context for logs only** — vacancy GETs use the
    **application** access token, not the applicant ``hh_access_token``.

    Returns:
        (found, vacancies) - found is total from HH API (best effort), vacancies is list of vacancy dicts.
    """
    per_page = 10
    if user_id is not None:
        logger.info(
            "[HH_VACANCY_SEARCH] context_internal_user_id=%s (GET /vacancies uses application Bearer only)",
            user_id,
        )
    data = get_vacancies_page(
        page,
        search_params,
        per_page=per_page,
        user_id=user_id,
        source=source,
    )

    raw_items = data.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []

    n_raw = len(raw_items)
    api_found = data.get("found") or 0
    try:
        api_found = int(api_found)
    except (TypeError, ValueError):
        api_found = 0

    # HH may omit or zero "found" while still returning items; do not treat as empty.
    if api_found == 0 and n_raw > 0:
        logger.warning(
            "[HH_API] search_vacancies_page: api_found=0 but len(raw_items)=%s — using found=%s for UI",
            n_raw,
            n_raw,
        )
        found = n_raw
    else:
        found = api_found

    vacancies = _process_vacancy_items(raw_items, filter_obj)

    logger.info(
        "[HH_API] search_vacancies_page page=%s len(raw_items)=%s api_found=%s effective_found=%s len(vacancies)=%s",
        page,
        n_raw,
        api_found,
        found,
        len(vacancies),
    )
    if n_raw > 0 and len(vacancies) == 0:
        logger.warning(
            "[HH_API] search_vacancies_page: all %s HH items dropped by _process_vacancy_items "
            "(check _FILTER_* flags and title/remote logic)",
            n_raw,
        )

    return found, vacancies


# Re-enable one at a time; when True, skipped items are logged at INFO.
_FILTER_TITLE_ENABLED = False
_FILTER_DESCRIPTION_ENABLED = False
_FILTER_SCHEDULE_REMOTE_ENABLED = False


def _process_vacancy_items(items: list, filter_obj) -> list:
    """
    Map HH API items to internal vacancy dicts (id, name, url, …).

    When all _FILTER_* flags are False, no rows are dropped (title/description/remote
    checks skipped); every item is converted. Set flags to True one-by-one to
    re-enable filters (skips are logged at INFO).
    """
    if not isinstance(items, list):
        logger.warning(
            "[HH_API] _process_vacancy_items expected list, got %s — returning empty",
            type(items).__name__,
        )
        return []

    use_custom_filter = filter_obj is not None
    n_in = len(items)
    skipped_title = 0
    skipped_desc = 0
    skipped_title_allowed = 0
    skipped_schedule = 0
    skipped_remote = 0

    logger.info(
        "[HH_API] _process_vacancy_items start n_items=%s use_custom_filter=%s "
        "filter_flags title=%s description=%s schedule_remote=%s",
        n_in,
        use_custom_filter,
        _FILTER_TITLE_ENABLED,
        _FILTER_DESCRIPTION_ENABLED,
        _FILTER_SCHEDULE_REMOTE_ENABLED,
    )

    vacancies = []

    for item in items:
        name = item.get("name", "")

        if _FILTER_TITLE_ENABLED:
            if use_custom_filter:
                if not _title_matches_filter(name, filter_obj):
                    skipped_title += 1
                    logger.info(
                        "[HH_API] _process_vacancy_items SKIP title_exclude id=%s name=%r",
                        item.get("id"),
                        name[:80] if name else "",
                    )
                    continue
            elif not is_title_allowed(name):
                skipped_title_allowed += 1
                logger.info(
                    "[HH_API] _process_vacancy_items SKIP title_allowed id=%s name=%r",
                    item.get("id"),
                    name[:80] if name else "",
                )
                continue

        if _FILTER_DESCRIPTION_ENABLED and use_custom_filter:
            if not _description_matches_filter(item, filter_obj):
                skipped_desc += 1
                logger.info(
                    "[HH_API] _process_vacancy_items SKIP description_exclude id=%s",
                    item.get("id"),
                )
                continue

        employer = item.get("employer") or {}
        schedule = item.get("schedule") or {}
        experience = item.get("experience") or {}
        employment = item.get("employment") or {}
        salary = item.get("salary")
        area = item.get("area") or {}
        schedule_name = schedule.get("name", "")
        experience_name = experience.get("name", "")
        employment_name = employment.get("name", "")

        if _FILTER_SCHEDULE_REMOTE_ENABLED:
            if use_custom_filter:
                if not _schedule_matches_filter(schedule_name, filter_obj):
                    skipped_schedule += 1
                    logger.info(
                        "[HH_API] _process_vacancy_items SKIP schedule/work_format id=%s schedule=%r",
                        item.get("id"),
                        schedule_name,
                    )
                    continue
            elif not is_remote(schedule_name):
                skipped_remote += 1
                logger.info(
                    "[HH_API] _process_vacancy_items SKIP non_remote id=%s schedule=%r",
                    item.get("id"),
                    schedule_name,
                )
                continue

        salary_from = salary.get("from") if salary else None
        salary_to = salary.get("to") if salary else None
        currency = salary.get("currency") if salary else None

        vacancy = {
            "id": str(item.get("id", "")),
            "name": name,
            "company": employer.get("name", "Не указана"),
            "salary": format_salary(salary),
            "salary_from": salary_from,
            "salary_to": salary_to,
            "currency": currency,
            "area": area.get("name", ""),
            "schedule": schedule_name or "Не указан",
            "experience": experience_name or "Не указан",
            "employment": employment_name or "Не указана",
            "url": item.get("alternate_url", ""),
            "published_at": item.get("published_at"),
            "raw_json": json.dumps(item, ensure_ascii=False),
        }
        vacancies.append(vacancy)

    n_out = len(vacancies)
    logger.info(
        "[HH_API] _process_vacancy_items done raw_items=%s out=%s "
        "skipped_title_exclude=%s skipped_desc_exclude=%s skipped_title_allowed=%s "
        "skipped_schedule=%s skipped_remote=%s",
        n_in,
        n_out,
        skipped_title,
        skipped_desc,
        skipped_title_allowed,
        skipped_schedule,
        skipped_remote,
    )
    return vacancies


def search_vacancies(search_params: dict | None = None, filter_obj=None, user_id: int | None = None):
    """
    Search vacancies from HH API (legacy: fetches 3 pages, 100 per page).
    Used by monitoring. For interactive search use search_vacancies_page.

    ``user_id`` is accepted for logging context only; GET /vacancies uses **application** OAuth only.
    """
    vacancies = []
    use_custom_filter = filter_obj is not None
    logger.info(
        "[HH_API] search_vacancies start pages=0..2 per_page=100 use_custom_filter=%s search_params=%s",
        use_custom_filter,
        search_params,
    )

    for page in range(3):
        logger.info("[HH_API] search_vacancies step=fetch_page page=%s", page)
        data = get_vacancies_page(
            page,
            search_params,
            per_page=100,
            user_id=user_id,
            source="monitor.search_vacancies",
        )

        if not isinstance(data, dict):
            logger.warning("[HH_API] search_vacancies page=%s: unexpected data type %s", page, type(data))
            continue

        raw_items = data.get("items")
        if raw_items is None:
            logger.warning("[HH_API] search_vacancies page=%s: no 'items' key, keys=%s", page, list(data.keys()))
            raw_items = []
        elif not isinstance(raw_items, list):
            logger.warning(
                "[HH_API] search_vacancies page=%s: 'items' is not a list, type=%s",
                page,
                type(raw_items).__name__,
            )
            raw_items = []

        n_before = len(raw_items)
        processed = _process_vacancy_items(raw_items, filter_obj)
        n_after = len(processed)
        logger.info(
            "[HH_API] search_vacancies page=%s len_items_before_process=%s len_after_process=%s",
            page,
            n_before,
            n_after,
        )
        vacancies.extend(processed)

    logger.info("[HH_API] search_vacancies done total_vacancies=%s", len(vacancies))
    return vacancies
