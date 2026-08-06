"""Получение клиентов из CRM-списков СБИС."""

import json
from typing import Any

import aiohttp

from src.config import loadEnvironment, requireSetting


DEFAULT_RPC_URL = "https://online.sbis.ru/service/"

# Предположительно указывает, связана ли выборка с темой отношений списка.
# По схеме принимает true/false; сервер также принял [], но его смысл не подтвержден.
FILTER_HAS_LIST_THEME: Any = False

# Предположительно задает внутренние типы CRM-записей.
# Подтвержден только набор [1, 4, 7]; отдельные коды и [] не расшифрованы.
FILTER_KIND_OF: list[int] = [1, 4, 7]

# Предположительно фильтрует по ID ответственного сотрудника или подразделения.
# null отключает фильтр; формат непустого строкового ID нужно снять в браузере.
FILTER_RESPONSIBLE: str | None = None

# Предположительно задает коды результатов обработки клиента в списке.
# Наблюдалось ["0"]; другие коды и поведение [] пока неизвестны.
FILTER_RESULT: list[str] = ["0"]

# Предположительно выбирает вид возвращаемых сущностей.
# Подтверждено значение "Clients"; другие строковые значения неизвестны.
FILTER_SHOW_ONLY = "Clients"

# Предположительно фильтрует клиентов по внутреннему ID стадии.
# null отключает фильтр; допустимые строки зависят от настроек списка.
FILTER_STAGE: str | None = None

# Направление курсорной пагинации; подтверждено "forward", вероятно есть "backward".
NAVIGATION_DIRECTION = "forward"

# Предположительно просит сервер вернуть признак и курсор следующей страницы.
# В перехваченном браузерном запросе передается true.
NAVIGATION_HAS_MORE = True

# Размер страницы: в перехваченном браузерном запросе подтверждено значение 25.
# Верхняя граница скрытого API не подтверждена.
PAGE_SIZE = 45

# Дополнительная сортировка; null означает порядок, выбранный сервером или списком.
FILTER_SORTING: dict[str, Any] | None = None

# Имена дополнительных полей; [] не расширяет стандартную схему ответа.
FILTER_ADDITIONAL_FIELDS: list[str] = []


class SbisApiError(RuntimeError):
    """Ошибка авторизации, транспорта или контракта API СБИС."""


def _browserHeaders(method: str, browser_cookie: str) -> dict[str, str]:
    """Собрать общие заголовки скрытого браузерного API СБИС."""
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/json; charset=UTF-8",
        "Cookie": browser_cookie,
        "Origin": "https://online.sbis.ru",
        "Referer": "https://online.sbis.ru/page/crm-client-lists",
        "X-CalledMethod": method,
        "X-Requested-With": "XMLHttpRequest",
    }


def _isValidInn(inn: str) -> bool:
    """Проверить длину, состав и контрольные цифры российского ИНН."""
    if not inn.isdigit() or len(inn) not in (10, 12):
        return False

    digits = [int(character) for character in inn]
    if len(digits) == 10:
        weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
        checksum = sum(value * weight for value, weight in zip(digits, weights))
        return checksum % 11 % 10 == digits[9]

    first_weights = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    second_weights = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    first_checksum = sum(
        value * weight for value, weight in zip(digits, first_weights)
    )
    second_checksum = sum(
        value * weight for value, weight in zip(digits, second_weights)
    )
    return (
        first_checksum % 11 % 10 == digits[10]
        and second_checksum % 11 % 10 == digits[11]
    )


def _typedFields(schema: Any) -> list[str] | None:
    """Извлечь имена полей из protocol 7 schema."""
    if not isinstance(schema, list):
        return None

    fields: list[str] = []
    for field in schema:
        if not isinstance(field, dict) or not isinstance(field.get("n"), str):
            return None
        fields.append(field["n"])
    return fields


def _decodeRecord(data: list[Any], schema: Any) -> dict[str, Any]:
    fields = _typedFields(schema)
    if fields is None:
        raise SbisApiError("СБИС вернул неизвестную схему записи")

    return {
        name: _decodeTyped(value)
        for name, value in zip(fields, data, strict=False)
    }


def _decodeTyped(value: Any) -> Any:
    """Преобразовать protocol 7 record/recordset в обычные типы Python."""
    if isinstance(value, list):
        return [_decodeTyped(item) for item in value]

    if not isinstance(value, dict):
        return value

    if "d" in value and "s" in value:
        data = value["d"]
        schema = value["s"]
        type_name = str(value.get("_type", "")).lower()

        if type_name == "recordset" or (
            not type_name
            and isinstance(data, list)
            and data
            and all(isinstance(row, list) for row in data)
        ):
            return [_decodeRecord(row, schema) for row in data]

        if isinstance(data, list):
            return _decodeRecord(data, schema)

    return {key: _decodeTyped(item) for key, item in value.items()}


def _buildPayload(
    listId: int,
    position: dict[str, Any] | None,
) -> dict[str, Any]:
    """Собрать запрос CrmClients.ListClients по зафиксированному контракту."""
    position_type = "Строка" if position is None else "Запись"
    return {
        "jsonrpc": "2.0",
        "protocol": 7,
        "method": "CrmClients.ListClients",
        "params": {
            "Фильтр": {
                "d": [
                    FILTER_HAS_LIST_THEME,
                    FILTER_KIND_OF,
                    listId,
                    FILTER_RESPONSIBLE,
                    FILTER_RESULT,
                    FILTER_SHOW_ONLY,
                    FILTER_STAGE,
                ],
                "s": [
                    {"t": "Логическое", "n": "HasListTheme"},
                    {"t": {"n": "Массив", "t": "Число целое"}, "n": "KindOf"},
                    {"t": "Число целое", "n": "ListId"},
                    {"t": "Строка", "n": "Responsible"},
                    {"t": {"n": "Массив", "t": "Строка"}, "n": "Result"},
                    {"t": "Строка", "n": "ShowOnly"},
                    {"t": "Строка", "n": "Stage"},
                ],
                "_type": "record",
                "f": 0,
            },
            "Сортировка": FILTER_SORTING,
            "Навигация": {
                "d": [
                    NAVIGATION_DIRECTION,
                    NAVIGATION_HAS_MORE,
                    PAGE_SIZE,
                    position,
                ],
                "s": [
                    {"t": "Строка", "n": "Direction"},
                    {"t": "Логическое", "n": "HasMore"},
                    {"t": "Число целое", "n": "Limit"},
                    {"t": position_type, "n": "Position"},
                ],
                "_type": "record",
                "f": 0,
            },
            "ДопПоля": FILTER_ADDITIONAL_FIELDS,
        },
        "id": 1,
    }


def _buildReadCardPayload(inn: str) -> dict[str, Any]:
    """Собрать запрос BillingContractor.ReadCard для поиска карточки по ИНН."""
    return {
        "jsonrpc": "2.0",
        "protocol": 7,
        "method": "BillingContractor.ReadCard",
        "params": {
            "Requisites": {
                "d": [inn, None, None, None],
                "s": [
                    {"t": "Строка", "n": "INN"},
                    {"t": "Строка", "n": "KPP"},
                    {"t": "Строка", "n": "CountryCode"},
                    {"t": "Число целое", "n": "BillingExtId"},
                ],
                "_type": "record",
                "f": 0,
            }
        },
        "id": 1,
    }


def _navigation(
    result: dict[str, Any],
) -> tuple[bool | None, dict[str, Any] | None]:
    """Прочитать HasMore и Position из распространенных мест ответа protocol 7."""
    direct_has_more = result.get("n")
    has_more = direct_has_more if isinstance(direct_has_more, bool) else None

    # Браузер оборачивает m.nextPosition[0] в запись Position.CompositeKey.
    metadata = _decodeTyped(result.get("m"))
    if isinstance(metadata, dict):
        next_position = metadata.get("nextPosition")
        if isinstance(next_position, list) and next_position:
            next_position = next_position[0]
        if isinstance(next_position, str) and next_position:
            position = {
                "d": [next_position],
                "s": [{"t": "Строка", "n": "CompositeKey"}],
                "_type": "record",
                "f": 1,
            }
            return has_more, position

    return has_more, None


async def _readJson(response: aiohttp.ClientResponse) -> dict[str, Any]:
    if response.status >= 400:
        raise SbisApiError(f"СБИС вернул HTTP {response.status}")

    try:
        payload = await response.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError) as error:
        raise SbisApiError("СБИС вернул ответ не в формате JSON") from error

    if not isinstance(payload, dict):
        raise SbisApiError("СБИС вернул JSON неизвестного формата")
    return payload


async def _getPage(
    session: aiohttp.ClientSession,
    rpc_url: str,
    browser_cookie: str,
    listId: int,
    position: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], bool | None, dict[str, Any] | None]:
    response = await session.post(
        rpc_url,
        headers=_browserHeaders("CrmClients.ListClients", browser_cookie),
        json=_buildPayload(listId, position),
    )
    try:
        payload = await _readJson(response)
    finally:
        response.release()

    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        raise SbisApiError(f"CrmClients.ListClients вернул ошибку, код: {code}")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise SbisApiError("Ответ CrmClients.ListClients не содержит result")

    decoded = _decodeTyped(result)
    if not isinstance(decoded, list) or not all(
        isinstance(client, dict) for client in decoded
    ):
        raise SbisApiError("result CrmClients.ListClients не является recordset")

    has_more, next_position = _navigation(result)
    return decoded, has_more, next_position


async def getClientByInn(inn: str) -> dict[str, Any] | None:
    """Получить карточку клиента СБИС по российскому ИНН."""
    if not isinstance(inn, str) or not _isValidInn(inn):
        raise ValueError("inn должен быть корректным российским ИНН из 10 или 12 цифр")

    loadEnvironment()
    rpc_url = requireSetting("SBIS_RPC_URL") or DEFAULT_RPC_URL
    browser_cookie = requireSetting("SBIS_BROWSER_COOKIE")

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        response = await session.post(
            rpc_url,
            headers=_browserHeaders("BillingContractor.ReadCard", browser_cookie),
            json=_buildReadCardPayload(inn),
        )
        try:
            payload = await _readJson(response)
        finally:
            response.release()

    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        raise SbisApiError(f"BillingContractor.ReadCard вернул ошибку, код: {code}")

    result = payload.get("result")
    if result is None:
        return None

    decoded = _decodeTyped(result)
    if not isinstance(decoded, dict):
        raise SbisApiError("result BillingContractor.ReadCard не является записью")
    return decoded


def _walkValues(value: Any):
    """Последовательно обойти вложенные словари и списки ответа СБИС."""
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from _walkValues(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walkValues(nested)


def _contactValue(row: dict[str, Any]) -> str:
    """Извлечь отображаемое значение контакта с резервом на внутреннюю запись."""
    row_title = row.get("RowTitle")
    if isinstance(row_title, str) and row_title.strip():
        return row_title.strip()

    contact = row.get("Contact")
    if isinstance(contact, dict):
        data = contact.get("d")
        if isinstance(data, list) and len(data) > 3 and isinstance(data[3], str):
            return data[3].strip()
    return ""


def _contactKind(row: dict[str, Any], value: str) -> str:
    """Определить вид контакта по действиям интерфейса и внутреннему коду."""
    actions = row.get("Actions")
    if isinstance(actions, list):
        if "copy_phone" in actions or "tel_link" in actions:
            return "phone"
        if "copy_email" in actions or "mail_client" in actions:
            return "email"

    contact = row.get("Contact")
    if isinstance(contact, dict):
        data = contact.get("d")
        if isinstance(data, list) and len(data) > 1:
            if data[1] == 10:
                return "phone"
            if data[1] == 20:
                return "email"

    if "@" in value:
        return "email"
    return "other"


def extractContacts(card: dict[str, Any]) -> list[dict[str, Any]]:
    """Собрать уникальные контакты из декодированной карточки клиента."""
    contacts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for value in _walkValues(card):
        if not isinstance(value, dict) or value.get("ContactType") != "contact":
            continue

        contact_value = _contactValue(value)
        if not contact_value:
            continue

        kind = _contactKind(value, contact_value)
        unique_key = (kind, contact_value.casefold())
        if unique_key in seen:
            continue
        seen.add(unique_key)

        contacts.append(
            {
                "id": value.get("ID"),
                "type": kind,
                "value": contact_value,
                "masked": value.get("Masked") is True,
            }
        )

    return contacts


async def getContactsByInn(inn: str) -> list[dict[str, Any]]:
    """Получить карточку по ИНН и собрать из нее телефонные и email-контакты."""
    card = await getClientByInn(inn)
    if card is None:
        return []
    return extractContacts(card)


async def getClientsByListId(listId: int) -> list[dict[str, Any]]:
    """Получить всех клиентов CRM-списка по его идентификатору."""
    if isinstance(listId, bool) or not isinstance(listId, int) or listId <= 0:
        raise ValueError("listId должен быть положительным целым числом")

    loadEnvironment()
    rpc_url = requireSetting("SBIS_RPC_URL") or DEFAULT_RPC_URL
    browser_cookie = requireSetting("SBIS_BROWSER_COOKIE")

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        clients: list[dict[str, Any]] = []
        position: dict[str, Any] | None = None
        seen_positions: set[str] = set()

        while True:
            page, has_more, next_position = await _getPage(
                session,
                rpc_url,
                browser_cookie,
                listId,
                position,
            )
            clients.extend(page)

            # В Wasaby размер result.d не обязан совпадать с Limit. Продолжение
            # определяется только флагом result.n и наличием m.nextPosition.
            if has_more is False or not next_position:
                return clients
            position_key = json.dumps(
                next_position,
                ensure_ascii=False,
                sort_keys=True,
            )
            if position_key in seen_positions:
                raise SbisApiError("СБИС повторил Position при пагинации")

            seen_positions.add(position_key)
            position = next_position
