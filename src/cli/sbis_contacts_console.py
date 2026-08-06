"""Консольная проверка получения контактов клиента СБИС по ИНН."""

import argparse
import asyncio
from typing import Any

from src.integrations.sbis import getContactsByInn


def _oneLine(value: Any) -> str:
    """Убрать переводы строк из значения для компактного вывода."""
    if value is None:
        return ""
    return " ".join(str(value).splitlines()).strip()


async def printContactsByInn(inn: str) -> list[dict[str, Any]]:
    """Получить контакты по ИНН, вывести их и вернуть исходный список."""
    contacts = await getContactsByInn(inn)
    if not contacts:
        print(f"ИНН {inn}: контакты не найдены.")
        return contacts

    for contact in contacts:
        kind = _oneLine(contact.get("type")) or "other"
        value = _oneLine(contact.get("value")) or "<пустое значение>"
        masked = " [скрыт]" if contact.get("masked") is True else ""
        print(f"{inn} | {kind} | {value}{masked}")

    print(f"Получено контактов: {len(contacts)}.")
    return contacts


def _parseArguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Получить контакты клиента СБИС по ИНН",
    )
    parser.add_argument("inn", help="ИНН организации или ИП из 10 или 12 цифр")
    return parser.parse_args()


def main() -> None:
    arguments = _parseArguments()
    asyncio.run(printContactsByInn(arguments.inn))


if __name__ == "__main__":
    main()

