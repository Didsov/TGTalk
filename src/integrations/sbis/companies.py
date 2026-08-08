"""Получение открытых организаций за календарный день из СБИС."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import aiohttp

from src.config import loadEnvironment, requireSetting
from src.integrations.sbis.clients import SbisApiError
from src.integrations.sbis.sbis_recordset_to_list import sbis_recordset_to_list


COMPANY_RPC_URL = "https://online.sbis.ru/service/?x_version=26.3248-150.6"
COMPANY_PAGE_SIZE = 40


def _company_headers(browser_cookie: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Cookie": browser_cookie,
        "Origin": "https://online.sbis.ru",
        "Referer": "https://online.sbis.ru/page/company-list",
        "X-CalledMethod": "Contractor.ListCompany",
        "X-OriginalMethodName": "Q29udHJhY3Rvci5MaXN0Q29tcGFueQ==",
        "X-Requested-With": "XMLHttpRequest",
    }


def _build_company_payload(page_number: int) -> dict[str, Any]:
    """Собрать неизменяемые фильтр/сортировку и навигацию нужной страницы."""
    return {
        "jsonrpc": "2.0",
        "protocol": 7,
        "method": "Contractor.ListCompany",
        "params": {
            "Фильтр": {
                "d": [
                    "643",
                    "Надежность",
                    ["643-BA568D-17133D"],
                    True,
                    True,
                    True,
                ],
                "s": [
                    {"t": "Строка", "n": "CountryCode"},
                    {"t": "Строка", "n": "NameColumn"},
                    {"t": {"n": "Массив", "t": "Строка"}, "n": "RegionCodes"},
                    {"t": "Логическое", "n": "externalContractors"},
                    {"t": "Логическое", "n": "selectionFromPanel"},
                    {"t": "Логическое", "n": "useRegionCode"},
                ],
                "_type": "record",
                "f": 0,
            },
            "Сортировка": {
                "d": [[False, "ДатаРегистрации", True]],
                "s": [
                    {"t": "Логическое", "n": "l"},
                    {"t": "Строка", "n": "n"},
                    {"t": "Логическое", "n": "o"},
                ],
                "_type": "recordset",
                "f": 0,
            },
            "Навигация": {
                "d": [True, COMPANY_PAGE_SIZE, page_number],
                "s": [
                    {"t": "Логическое", "n": "ЕстьЕще"},
                    {"t": "Число целое", "n": "РазмерСтраницы"},
                    {"t": "Число целое", "n": "Страница"},
                ],
                "_type": "record",
                "f": 0,
            },
            "ДопПоля": [],
        },
        "id": 1,
    }


def _calendar_date(value: Any) -> date:
    """Привести значение ДатаРегистрации СБИС к календарной дате."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise SbisApiError("У организации отсутствует ДатаРегистрации")

    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise SbisApiError(f"Неизвестный формат ДатаРегистрации: {text}")


async def _get_company_page(
    session: aiohttp.ClientSession,
    browser_cookie: str,
    page_number: int,
) -> tuple[list[dict[str, Any]], bool]:
    response = await session.post(
        COMPANY_RPC_URL,
        headers=_company_headers(browser_cookie),
        json=_build_company_payload(page_number),
    )
    try:
        if response.status >= 400:
            raise SbisApiError(f"СБИС вернул HTTP {response.status}")
        try:
            payload = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as error:
            raise SbisApiError("СБИС вернул ответ не в формате JSON") from error
    finally:
        response.release()

    if not isinstance(payload, dict):
        raise SbisApiError("СБИС вернул JSON неизвестного формата")
    if "error" in payload:
        error = payload["error"]
        details = error.get("details") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise SbisApiError(details or message or "Contractor.ListCompany вернул ошибку")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise SbisApiError("Ответ Contractor.ListCompany не содержит result")
    records = sbis_recordset_to_list(result)
    has_more = result.get("n")
    if not isinstance(has_more, bool):
        raise SbisApiError("Ответ Contractor.ListCompany не содержит result.n")
    return records, has_more


async def get_open_companies_by_date(target_date: date | str) -> list[dict[str, Any]]:
    """Получить организации, зарегистрированные строго в указанный день.

    Метод полагается на заданную в payload сортировку ДатаРегистрации по убыванию
    и прекращает запросы после первой записи старше целевой даты.
    """
    if isinstance(target_date, datetime):
        target = target_date.date()
    elif isinstance(target_date, date):
        target = target_date
    elif isinstance(target_date, str):
        try:
            target = date.fromisoformat(target_date.strip())
        except ValueError as error:
            raise ValueError("target_date должен иметь формат YYYY-MM-DD") from error
    else:
        raise TypeError("target_date должен быть date или строкой YYYY-MM-DD")

    loadEnvironment()
    browser_cookie = requireSetting("SBIS_BROWSER_COOKIE")
    timeout = aiohttp.ClientTimeout(total=60)
    selected: list[dict[str, Any]] = []
    page_number = 0
    previous_date: date | None = None

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:

            records, has_more = await _get_company_page(
                session, browser_cookie, page_number
            )
            print(previous_date)
            for record in records:
                registration_date = _calendar_date(record.get("ДатаРегистрации"))
                if previous_date is not None and registration_date > previous_date:
                    raise SbisApiError(
                        "Contractor.ListCompany нарушил сортировку "
                        "ДатаРегистрации от новых к старым"
                    )
                previous_date = registration_date

                if registration_date > target:
                    continue
                if registration_date == target:
                    selected.append(record)
                    continue
                return selected

            if not has_more:
                return selected
            page_number += 1
