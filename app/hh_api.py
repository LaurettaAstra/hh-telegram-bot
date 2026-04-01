import json
import logging
import requests

logger = logging.getLogger(__name__)

# Preview length for logged response.text in get_vacancies_page
_HH_LOG_TEXT_PREVIEW = 1000


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


def _build_search_text(
    title_keywords: str | None,
    title_exclude_keywords: str | None,
    description_keywords: str | None,
    description_exclude_keywords: str | None,
    city: str | None,
) -> tuple[str, list[str]]:
    """
    Build HH API text and search_field from user inputs.
    - Keywords: AND between words
    - Exclude: NOT operator for each excluded word
    - search_field: name when title, description when description, both when both

    Returns:
        (text, search_field_list)
    """
    text_parts = []
    search_fields = []

    def _build_part(keywords: str | None, exclude: str | None) -> str:
        if not keywords or not keywords.strip():
            return ""
        words = [w.strip() for w in keywords.split() if w.strip()]
        part = " AND ".join(words)
        if exclude and exclude.strip():
            for ex in exclude.strip().split():
                ex = ex.strip()
                if ex:
                    part += f" NOT {ex}"
        return part

    title_part = _build_part(title_keywords, title_exclude_keywords)
    desc_part = _build_part(description_keywords, description_exclude_keywords)

    if title_part:
        text_parts.append(title_part)
        search_fields.append("name")
    if desc_part:
        text_parts.append(desc_part)
        search_fields.append("description")

    text = " AND ".join(text_parts) if text_parts else ""
    if city and city.strip():
        text = f"{text} {city.strip()}".strip() if text else city.strip()

    return text or "работа", search_fields if search_fields else ["name"]


def get_vacancies_page(page: int, search_params: dict | None = None, per_page: int = 100):
    """
    GET https://api.hh.ru/vacancies — official HH API (public vacancy search).

    Params match the public API (text, search_field, area, period, salary, etc.).
    See: https://api.hh.ru/openapi/redoc#tag/Poisk-vakansij/operation/get-vacancies
    """
    url = "https://api.hh.ru/vacancies"

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

    prepared = requests.Request("GET", url, params=params).prepare()
    logger.info("[HH_API] get_vacancies_page request URL=%s", prepared.url)

    response = requests.get(url, params=params, timeout=20)

    preview = response.text[:_HH_LOG_TEXT_PREVIEW]
    logger.info(
        "[HH_API] get_vacancies_page response status_code=%s text_preview_first_%s_chars=%r",
        response.status_code,
        _HH_LOG_TEXT_PREVIEW,
        preview,
    )

    response.raise_for_status()

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
    """Build HH API search params from a SavedFilter instance.
    Uses strict search: search_field for title/description, AND/NOT operators."""
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
