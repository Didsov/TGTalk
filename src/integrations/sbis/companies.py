"""Получение открытых организаций за календарный день из СБИС."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import aiohttp

from src.config import PROJECT_ROOT, loadEnvironment, requireSetting
from src.integrations.sbis.clients import SbisApiError
from src.integrations.sbis.sbis_recordset_to_list import sbis_recordset_to_list
from src.storage import NewClientStorage


COMPANY_RPC_URL = "https://online.sbis.ru/service/?x_version=26.3248-150.6"
COMPANY_PAGE_SIZE = 40
COMPANY_CARD_REQUEST_DELAY_SECONDS = 2.0
COMPANY_CARD_RATE_LIMIT = 20
COMPANY_CARD_RATE_LIMIT_PAUSE_SECONDS = 60.0
COMPANY_CARD_MAX_ATTEMPTS = 5
DEFAULT_COMPANY_DATABASE_PATH = PROJECT_ROOT / "data" / "clients.db"


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


def _company_card_headers(browser_cookie: str) -> dict[str, str]:
    """Заголовки браузерного вызова ContractorCard.Read."""
    headers = _company_headers(browser_cookie)
    headers["X-CalledMethod"] = "ContractorCard.Read"
    headers["X-OriginalMethodName"] = "Q29udHJhY3RvckNhcmQuUmVhZA=="
    return headers


def _build_company_card_payload(
    spp_id: int,
    contractor_uuid: str,
) -> dict[str, Any]:
    """Собрать payload карточки из полей записи Contractor.ListCompany."""
    if isinstance(spp_id, bool) or not isinstance(spp_id, int) or spp_id == 0:
        raise ValueError("ИдентификаторСПП должен быть ненулевым целым числом")
    if not isinstance(contractor_uuid, str) or not contractor_uuid.strip():
        raise ValueError("UUID организации обязателен")
    try:
        normalized_uuid = str(UUID(contractor_uuid.strip()))
    except ValueError as error:
        raise ValueError("UUID организации имеет неверный формат") from error

    return {
        "jsonrpc": "2.0",
        "protocol": 7,
        "method": "ContractorCard.Read",
        "params": {
            "ИдО": -abs(spp_id),
            "ИмяМетода": None,
            "ДопПоля": {
                "browser": True,
                "firstLoad": True,
                "page": "crm",
                "ContractorUUID": normalized_uuid,
                "isRead": True,
                "anchor": "about",
                "tab": {"all": True},
                "CountryCode": "643",
            },
        },
        "id": 1,
    }


def _sbis_record_to_dict(result: dict[str, Any]) -> dict[str, Any]:
    """Преобразовать одиночную запись СБИС d/s в обычный словарь."""
    schema = result.get("s")
    values = result.get("d")
    if not isinstance(schema, list) or not isinstance(values, list):
        raise SbisApiError("Ответ ContractorCard.Read не содержит запись d/s")

    record: dict[str, Any] = {}
    for index, field in enumerate(schema):
        if not isinstance(field, dict):
            raise SbisApiError("Схема ContractorCard.Read имеет неверный формат")
        field_name = field.get("n")
        if not isinstance(field_name, str) or not field_name:
            field_name = f"field_{index}"
        value = values[index] if index < len(values) else None
        record[field_name] = _convert_sbis_value(value)
    return record


def _convert_sbis_value(value: Any) -> Any:
    """Рекурсивно преобразовать вложенные record/recordset СБИС."""
    if isinstance(value, dict):
        value_type = value.get("_type")
        if (
            value_type == "record"
            and isinstance(value.get("s"), list)
            and isinstance(value.get("d"), list)
        ):
            return _sbis_record_to_dict(value)
        if value_type == "recordset":
            schema = value.get("s")
            rows = value.get("d")
            if isinstance(schema, list) and isinstance(rows, list):
                return [
                    _sbis_record_to_dict({"s": schema, "d": row})
                    for row in rows
                    if isinstance(row, list)
                ]
        return {key: _convert_sbis_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_convert_sbis_value(item) for item in value]
    return value


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


async def get_company_card(
    spp_id: int,
    contractor_uuid: str,
) -> dict[str, Any]:
    """Получить подробную карточку по ИдентификаторСПП и UUID из списка."""
    payload = _build_company_card_payload(spp_id, contractor_uuid)
    loadEnvironment()
    browser_cookie = requireSetting("SBIS_BROWSER_COOKIE")
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(COMPANY_CARD_MAX_ATTEMPTS):
            response = await session.post(
                COMPANY_RPC_URL,
                headers=_company_card_headers(browser_cookie),
                json=payload,
            )
            if (
                response.status == 429
                and attempt + 1 < COMPANY_CARD_MAX_ATTEMPTS
            ):
                retry_after = _retry_after_seconds(response, attempt)
                response.release()
                print(
                    "ContractorCard.Read вернул HTTP 429; "
                    f"повтор {attempt + 2}/{COMPANY_CARD_MAX_ATTEMPTS} "
                    f"через {retry_after:g} сек."
                )
                await asyncio.sleep(retry_after)
                continue
            try:
                if response.status >= 400:
                    raise SbisApiError(f"СБИС вернул HTTP {response.status}")
                try:
                    response_payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError) as error:
                    raise SbisApiError(
                        "СБИС вернул ответ не в формате JSON"
                    ) from error
            finally:
                response.release()
            break

    if not isinstance(response_payload, dict):
        raise SbisApiError("СБИС вернул JSON неизвестного формата")
    if "error" in response_payload:
        error = response_payload["error"]
        details = error.get("details") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        raise SbisApiError(details or message or "ContractorCard.Read вернул ошибку")

    result = response_payload.get("result")
    if not isinstance(result, dict):
        raise SbisApiError("Ответ ContractorCard.Read не содержит result")
    return _sbis_record_to_dict(result)


def _retry_after_seconds(response: Any, attempt: int) -> float:
    """После HTTP 429 ждать не меньше полного минутного окна лимита."""
    headers = getattr(response, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return COMPANY_CARD_RATE_LIMIT_PAUSE_SECONDS
    return max(delay, COMPANY_CARD_RATE_LIMIT_PAUSE_SECONDS)


def _save_companies(
    records: list[dict[str, Any]],
    database_path: str | Path = DEFAULT_COMPANY_DATABASE_PATH,
) -> None:
    """Идемпотентно сохранить подробные карточки организаций в SQLite."""
    storage = NewClientStorage(database_path)
    storage.initialize()
    storage.save_company_cards(records)


async def get_open_companies_by_date(
    target_date: date | str | None = None,
    *,
    database_path: str | Path = DEFAULT_COMPANY_DATABASE_PATH,
) -> list[dict[str, Any]]:
    """Получить и сохранить организации за день; по умолчанию — за сегодня.

    Метод полагается на заданную в payload сортировку ДатаРегистрации по убыванию
    и прекращает запросы после первой записи старше целевой даты.
    """
    if target_date is None:
        target = date.today()
    elif isinstance(target_date, datetime):
        target = target_date.date()
    elif isinstance(target_date, date):
        target = target_date
    elif isinstance(target_date, str):
        try:
            target = date.fromisoformat(target_date.strip())
        except ValueError as error:
            raise ValueError("target_date должен иметь формат YYYY-MM-DD") from error
    else:
        raise TypeError(
            "target_date должен быть date, строкой YYYY-MM-DD или None"
        )

    loadEnvironment()
    browser_cookie = requireSetting("SBIS_BROWSER_COOKIE")
    timeout = aiohttp.ClientTimeout(total=60)
    selected: list[dict[str, Any]] = []
    page_number = 0
    previous_date: date | None = None
    reached_older_date = False

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while not reached_older_date:
            records, has_more = await _get_company_page(
                session, browser_cookie, page_number
            )
            print(
                f"Страница {page_number + 1} получена: "
                f"{len(records)} организаций"
            )
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
                reached_older_date = True
                break

            if reached_older_date or not has_more:
                break
            page_number += 1

    storage = NewClientStorage(database_path)
    storage.initialize()
    cards: list[dict[str, Any]] = []
    requested_cards = 0
    for index, record in enumerate(selected, start=1):
        spp_id = record.get("ИдентификаторСПП")
        contractor_uuid = record.get("UUID")
        if not isinstance(spp_id, int) or isinstance(spp_id, bool):
            raise SbisApiError("Запись списка не содержит ИдентификаторСПП")
        if storage.get(spp_id) is not None:
            print(
                f"Карточка {index}/{len(selected)} уже есть в БД; пропущена"
            )
            continue
        if not isinstance(contractor_uuid, str) or not contractor_uuid.strip():
            raise SbisApiError("Запись списка не содержит UUID")
        if requested_cards > 0 and requested_cards % COMPANY_CARD_RATE_LIMIT == 0:
            print(
                f"Получено {requested_cards} новых карточек; пауза "
                f"{COMPANY_CARD_RATE_LIMIT_PAUSE_SECONDS:g} сек. "
                "из-за лимита ContractorCard.Read"
            )
            await asyncio.sleep(COMPANY_CARD_RATE_LIMIT_PAUSE_SECONDS)
        await asyncio.sleep(COMPANY_CARD_REQUEST_DELAY_SECONDS)
        card = await get_company_card(spp_id, contractor_uuid)
        requested_cards += 1
        cards.append(card)
        storage.upsert_from_company_card(card)
        spp_data = card.get("spp_data")
        director_collected = isinstance(spp_data, dict) and any(
            spp_data.get(field)
            for field in (
                "Директор.Фамилия",
                "Директор.Имя",
                "Директор.Отчество",
            )
        )
        print(
            f"Карточка {index}/{len(selected)} собрана; "
            f"ФИО директора: {'получено' if director_collected else 'не найдено'}; "
            "записана в БД"
        )

    return cards
