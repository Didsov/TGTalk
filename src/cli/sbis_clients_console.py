"""Консольная проверка получения клиентов из CRM-списка СБИС."""

import argparse
import asyncio
from typing import Any

from src.integrations.sbis import getClientsByListId


OUTPUT_LIMIT = 10
ORGANIZATION_MAX_WIDTH = 60


def _oneLine(value: Any) -> str:
    """Преобразовать значение в одну строку для печати таблицы."""
    if value is None:
        return ""
    return " ".join(str(value).splitlines()).strip()


def _organizationName(client: dict[str, Any]) -> str:
    """Выбрать первое заполненное название организации из известных полей."""
    for field in ("Название", "Name", "Контрагент"):
        value = _oneLine(client.get(field))
        if value:
            return value
    return "<название отсутствует>"


def _truncate(value: str, width: int) -> str:
    """Ограничить ширину значения без изменения исходных данных клиента."""
    if len(value) <= width:
        return value
    return value[: width - 3] + "..."


def _printTable(clients: list[dict[str, Any]]) -> None:
    """Напечатать первые десять клиентов в виде таблицы."""
    rows = [
        (
            _oneLine(client.get("ИНН")) or "<нет ИНН>",
            _truncate(_organizationName(client), ORGANIZATION_MAX_WIDTH),
        )
        for client in clients[:OUTPUT_LIMIT]
    ]

    inn_width = max([len("ИНН"), *(len(inn) for inn, _ in rows)])
    organization_width = max(
        [len("Организация"), *(len(organization) for _, organization in rows)]
    )
    separator = f"+-{'-' * inn_width}-+-{'-' * organization_width}-+"

    print(separator)
    print(f"| {'ИНН':<{inn_width}} | {'Организация':<{organization_width}} |")
    print(separator)
    for inn, organization in rows:
        print(f"| {inn:<{inn_width}} | {organization:<{organization_width}} |")
    print(separator)
    print(f"Получено клиентов: {len(clients)}. Показано: {len(rows)}.")


async def printClientsTable(listId: int) -> list[dict[str, Any]]:
    """Полностью загрузить CRM-список, вывести до 10 строк и вернуть всех клиентов."""
    clients = await getClientsByListId(listId)
    _printTable(clients)
    return clients


def _parseArguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Показать первые 10 клиентов CRM-списка СБИС",
    )
    parser.add_argument("listId", type=int, help="ID списка клиентов в CRM СБИС")
    return parser.parse_args()


def main() -> None:
    arguments = _parseArguments()
    asyncio.run(printClientsTable(arguments.listId))


if __name__ == "__main__":
    main()

