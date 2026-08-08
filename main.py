"""Диагностический запуск полного обхода CRM-списка СБИС."""

import argparse
import asyncio
from typing import Any

import aiohttp

from src.config import loadEnvironment, requireSetting
from src.domain import is_valid_inn
from src.integrations.sbis import getClientsByListId, getContactsByInn
from src.integrations.sbis.clients import SbisApiError
from src.integrations.telegram.report_downloader import download_report_text
from src.integrations.telegram.report_parser import (
    ParsedReport,
    parse_report,
    parse_report_sections,
)


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


async def parseBotReportUrl(reportUrl: str) -> ParsedReport:
    """Скачать TXT по ссылке бота и преобразовать его в ParsedReport."""
    report_text = await download_report_text(reportUrl)
    source_inn = _reportSourceInn(report_text)
    return parse_report(report_text, source_inn=source_inn)


def _reportSourceInn(reportText: str) -> str:
    """Найти корректный ИНН в общей сводке TXT-отчёта."""
    sections = parse_report_sections(reportText)
    ordered_sections = sorted(
        sections,
        key=lambda section: section.title.casefold() != "общая сводка",
    )
    for section in ordered_sections:
        for value in section.values("ИНН"):
            inn = value.strip()
            if is_valid_inn(inn):
                return inn
    raise ValueError("В отчёте бота не найден корректный ИНН")


def _printReportResult(report: ParsedReport) -> None:
    """Вывести результат парсинга без раскрытия контактов и ФИО."""
    selection = report.candidate_selection
    print("Результат парсинга отчёта Telegram:")
    print(f"  Разделов: {len(report.sections)}")
    print(f"  Телефонов: {len(report.phones)}")
    print(f"  Email: {len(report.emails)}")
    print(f"  Кандидатов ФИО/дата: {len(report.person_candidates)}")
    print(f"  Выбор кандидата: {selection.status.value}")
    print(f"  Нужна ручная проверка: {selection.requires_manual_review}")


def _parseArguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Получить контакты клиентов из CRM-списка СБИС",
    )
    parser.add_argument("listId", type=int, help="ID списка клиентов в CRM СБИС", default=90731, nargs="?")
    return parser.parse_args()


def main() -> None:
    arguments = _parseArguments()
    loadEnvironment()
    report_url = requireSetting("TELEGRAM_DEBUG_REPORT_URL")
    report = asyncio.run(parseBotReportUrl(report_url))
    _printReportResult(report)
   #asyncio.run(debugList(arguments.listId))


if __name__ == "__main__":
    main()
