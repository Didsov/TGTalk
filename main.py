"""Диагностический запуск полного обхода CRM-списка СБИС."""

import argparse
import asyncio
from typing import Any

import aiohttp

from src.integrations.sbis import getClientsByListId, getContactsByInn
from src.integrations.sbis.clients import SbisApiError


def _oneLine(value: Any) -> str:
    """Убрать переводы строк, чтобы одна организация занимала одну строку."""
    if value is None:
        return ""
    return " ".join(str(value).splitlines()).strip()


def _organizationName(client: dict[str, Any]) -> str:
    """Получить название организации из первого известного заполненного поля."""
    for field in ("Название", "Name", "Контрагент"):
        value = _oneLine(client.get(field))
        if value:
            return value
    return "<название отсутствует>"


def _contactsText(contacts: list[dict[str, Any]]) -> str:
    """Собрать контакты в компактную строку для диагностического вывода."""
    if not contacts:
        return "<контакты не найдены>"

    parts: list[str] = []
    for contact in contacts:
        kind = _oneLine(contact.get("type")) or "other"
        value = _oneLine(contact.get("value"))
        if value:
            masked = " [скрыт]" if contact.get("masked") is True else ""
            parts.append(f"{kind}: {value}{masked}")
    return "; ".join(parts) or "<контакты не найдены>"


async def debugList(listId: int) -> list[str]:
    """Обойти CRM-список, вывести контакты и вернуть найденные ИНН."""
    clients = await getClientsByListId(listId)
    inns: list[str] = []

    print("ИНН | Организация | Контакты")
    print("-" * 100)

    for client in clients:
        inn = _oneLine(client.get("ИНН"))
        organization = _organizationName(client)

        if not inn:
            print(f"<нет ИНН> | {organization} | <запрос контактов пропущен>")
            continue

        inns.append(inn)
        try:
            contacts = await getContactsByInn(inn)
            contacts_text = _contactsText(contacts)
        except (SbisApiError, aiohttp.ClientError, TimeoutError, ValueError) as error:
            # Не выводим тело ответа или cookie, только безопасный класс ошибки.
            contacts_text = f"<ошибка {type(error).__name__}>"

        print(f"{inn} | {organization} | {contacts_text}")

    print("-" * 100)
    print(f"Клиентов: {len(clients)}. ИНН для обработки: {len(inns)}.")
    return inns


def _parseArguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Получить контакты клиентов из CRM-списка СБИС",
    )
    parser.add_argument("listId", type=int, help="ID списка клиентов в CRM СБИС", default=90731, nargs="?")
    return parser.parse_args()


def main() -> None:
    arguments = _parseArguments()
    asyncio.run(debugList(arguments.listId))


if __name__ == "__main__":
    main()

