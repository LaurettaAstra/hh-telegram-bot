import json
import logging
import time
from urllib.parse import urlencode

import requests

from app.config import HH_API_HH_USER_AGENT

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

HH_API_VACANCIES_URL = "https://api.hh.ru/vacancies"

# Lazily populated on first call to _hh_request_get — single shared client for all api.hh.ru GETs.
_HH_HTTP_CLIENT: requests.Session | None = None


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


def _hh_send_get(session: requests.Session, url: str, params: dict, timeout: float) -> requests.Response:
    req = requests.Request("GET", url, params=params)
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


def _hh_request_get(
    url: str,
    params: dict | None = None,
    *,
    timeout: float = 20,
) -> requests.Response:
    """
    Sole entry point for GET requests to api.hh.ru.
    Uses one shared requests.Session created on first call; headers are set on the Session only.
    One retry on 403, 5xx, or timeout. Raises HHApiError on failure after retries.
    Returns response only for HTTP 200.
    """
    params = dict(params) if params else {}
    full_url = _hh_full_url(url, params)

    for attempt in range(2):
        try:
            response = _hh_send_get(_hh_http_session(), url, params, timeout)
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

        if attempt == 0 and (status == 403 or status >= 500):
            time.sleep(_HH_RETRY_DELAY_SEC)
            continue

        raise HHApiError(
            f"HH API returned HTTP {status}: {body_preview}",
            status_code=status,
            url=full_url,
            response_body_preview=body_preview,
        )

    raise HHApiError("HH API request failed after retries", url=full_url)


def hh_api_minimal_probe(*, timeout: float = 20) -> int:
    """
    Temporary diagnostics: one minimal GET /vacancies using the same Session + HH-User-Agent
    as production. Uses ``_hh_send_get`` + ``_hh_http_session()`` — the same HTTP primitive
    inside ``_hh_request_get`` (single attempt so 403/200 both produce a Response to log).

    Params: text=аналитик, per_page=1, page=0 only.

    After the response, logs ``[HH_API_PROBE]`` with status, body, outgoing headers, effective URL.

    Returns:
        0 if HTTP 200, 1 otherwise.
    """
    params = {"text": "аналитик", "per_page": 1, "page": 0}
    full_url = _hh_full_url(HH_API_VACANCIES_URL, params)
    logger.warning(
        "[HH_API_PROBE] minimal GET %s (single _hh_send_get; same session as _hh_request_get)",
        full_url,
    )
    response = _hh_send_get(_hh_http_session(), HH_API_VACANCIES_URL, params, timeout)
    _log_hh_probe_summary("[HH_API_PROBE]", response, full_url, params)
    return 0 if response.status_code == 200 else 1


def hh_api_no_text_probe(*, timeout: float = 20) -> int:
    """
    Temporary diagnostics: GET /vacancies with only per_page=1, page=0 (no ``text`` param).

    Same Session + HH-User-Agent as production via ``_hh_send_get`` / ``_hh_http_session``.

    Logs with tag ``[HH_API_PROBE_NO_TEXT]``.

    Returns:
        0 if HTTP 200, 1 otherwise.
    """
    params = {"per_page": 1, "page": 0}
    full_url = _hh_full_url(HH_API_VACANCIES_URL, params)
    logger.warning(
        "[HH_API_PROBE_NO_TEXT] GET %s (single _hh_send_get; same session as _hh_request_get)",
        full_url,
    )
    response = _hh_send_get(_hh_http_session(), HH_API_VACANCIES_URL, params, timeout)
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


def get_vacancies_page(page: int, search_params: dict | None = None, per_page: int = 100):
    """
    GET https://api.hh.ru/vacancies — official HH API (public vacancy search).

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

    response = _hh_request_get(HH_API_VACANCIES_URL, params=params, timeout=20)

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


def search_vacancies_page(page: int, search_params: dict | None = None, filter_obj=None) -> tuple[int, list]:
    """
    Fetch one page of vacancies from HH API (10 per page for pagination).

    Returns:
        (found, vacancies) - found is total from HH API (best effort), vacancies is list of vacancy dicts.
    """
    per_page = 10
    data = get_vacancies_page(page, search_params, per_page=per_page)

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


def search_vacancies(search_params: dict | None = None, filter_obj=None):
    """
    Search vacancies from HH API (legacy: fetches 3 pages, 100 per page).
    Used by monitoring. For interactive search use search_vacancies_page.
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
        data = get_vacancies_page(page, search_params, per_page=100)

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
