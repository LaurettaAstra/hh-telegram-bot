import requests


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


def get_vacancies_page(page: int):
    url = "https://api.hh.ru/vacancies"

    params = {
        "text": "аналитик",
        "per_page": 100,
        "page": page,
        "order_by": "publication_time"
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()

    return response.json()


def search_vacancies():
    vacancies = []

    # 3 страницы по 100 = 300 последних вакансий
    for page in range(3):
        data = get_vacancies_page(page)

        for item in data.get("items", []):
            name = item.get("name", "")

            if not is_title_allowed(name):
                continue

            employer = item.get("employer") or {}
            schedule = item.get("schedule") or {}
            salary = item.get("salary")
            area = item.get("area") or {}

            schedule_name = schedule.get("name", "")

            if not is_remote(schedule_name):
                continue

            vacancy = {
                "name": name,
                "company": employer.get("name", "Не указана"),
                "salary": format_salary(salary),
                "area": area.get("name", ""),
                "schedule": schedule_name or "Не указан",
                "url": item.get("alternate_url", "")
            }

            vacancies.append(vacancy)

    return vacancies