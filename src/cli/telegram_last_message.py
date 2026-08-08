"""Повторная обработка последнего ответа Telegram без нового запроса."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from src.config import PROJECT_ROOT, loadEnvironment, requireSetting
from src.integrations.telegram.bot_client import (
    BotResponseKind,
    classifyBotResponse,
    closeTg,
    extractReportUrlAsync,
    getLatestIncomingMessage,
    getMessageButtons,
    openTg,
)
from src.integrations.telegram.report_downloader import (
    allowed_report_hosts,
    download_report_text,
)
from src.integrations.telegram.report_parser import parse_report
from src.storage import NewClientStorage, ProcessingStatus


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clients.db"


@dataclass(frozen=True)
class LastMessageInspection:
    client_spp_id: int
    message_id: int | None
    response_kind: BotResponseKind
    button_count: int
    url_button_count: int
    callback_button_count: int
    report_url_found: bool
    phones_found: int = 0
    emails_found: int = 0
    candidate_status: str | None = None
    saved_status: ProcessingStatus | None = None


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("значение должно быть целым числом") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Повторно разобрать последнее входящее сообщение бота, "
            "не отправляя новый поисковый запрос."
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--client-spp-id",
        type=_positive_int,
        help=(
            "ID карточки; без него используется последняя попытка "
            "report_url_missing"
        ),
    )
    parser.add_argument("--history-limit", type=_positive_int, default=20)
    parser.add_argument(
        "--write",
        action="store_true",
        help="сохранить контакты и новый статус; без флага БД не меняется",
    )
    return parser.parse_args(argv)


async def inspect_last_message(
    database_path: str | Path,
    *,
    client_spp_id: int | None = None,
    history_limit: int = 20,
    write: bool = False,
) -> LastMessageInspection:
    """Прочитать и разобрать последний ответ без вызова send_message."""
    loadEnvironment()
    bot_username = requireSetting("TELEGRAM_TARGET_BOT")
    allowed_report_hosts()

    storage = NewClientStorage(database_path)
    storage.initialize()
    if client_spp_id is None:
        attempt = storage.latest_telegram_attempt(
            result_code="report_url_missing"
        )
        if attempt is None:
            raise RuntimeError("В БД нет попытки report_url_missing")
        client_spp_id = attempt.client_spp_id
    client = storage.get(client_spp_id)
    if client is None:
        raise RuntimeError(f"Клиент СБИС {client_spp_id} не найден")

    telegram_client = await openTg()
    try:
        message = await getLatestIncomingMessage(
            telegram_client,
            bot_username,
            limit=history_limit,
        )
        buttons = await getMessageButtons(message)
        report_url = await extractReportUrlAsync(message)
    finally:
        await closeTg(telegram_client)

    base = dict(
        client_spp_id=client_spp_id,
        message_id=getattr(message, "id", None),
        response_kind=classifyBotResponse(message),
        button_count=len(buttons),
        url_button_count=sum(bool(getattr(button, "url", None)) for button in buttons),
        callback_button_count=sum(
            getattr(button, "data", None) is not None for button in buttons
        ),
        report_url_found=report_url is not None,
    )
    if report_url is None:
        return LastMessageInspection(**base)

    report_text = await download_report_text(report_url)
    director_name = " ".join(
        part.strip()
        for part in (
            client.director_last_name,
            client.director_first_name,
            client.director_middle_name,
        )
        if part and part.strip()
    ) or None
    report = parse_report(
        report_text,
        source_inn=client.inn,
        expected_director_name=director_name,
    )
    phones = tuple(dict.fromkeys(client.telegram_phones + report.phones))
    emails = tuple(dict.fromkeys(client.telegram_emails + report.emails))
    saved_status: ProcessingStatus | None = None
    if write:
        saved_status = (
            ProcessingStatus.PROCESSED
            if phones
            else ProcessingStatus.NEEDS_REVIEW
        )
        storage.save_telegram_result(
            client_spp_id,
            phones=phones,
            emails=emails,
            status=saved_status,
            stage="last_message_recheck",
            result_code=(
                "phone_found_in_last_message"
                if phones
                else "last_message_report_without_phone"
            ),
        )
    return LastMessageInspection(
        **base,
        phones_found=len(report.phones),
        emails_found=len(report.emails),
        candidate_status=report.candidate_selection.status.value,
        saved_status=saved_status,
    )


def print_inspection(
    result: LastMessageInspection,
    *,
    output: TextIO | None = None,
) -> None:
    stream = output if output is not None else sys.stdout
    print(f"Клиент СПП: {result.client_spp_id}", file=stream)
    print(f"Последнее входящее сообщение: {result.message_id}", file=stream)
    print(f"Тип ответа: {result.response_kind.value}", file=stream)
    print(f"Кнопок: {result.button_count}", file=stream)
    print(f"URL-кнопок: {result.url_button_count}", file=stream)
    print(f"Callback-кнопок: {result.callback_button_count}", file=stream)
    print(f"Ссылка на отчёт найдена: {result.report_url_found}", file=stream)
    if result.report_url_found:
        print(f"Телефонов в отчёте: {result.phones_found}", file=stream)
        print(f"Email в отчёте: {result.emails_found}", file=stream)
        print(f"Кандидат ФИО/дата: {result.candidate_status}", file=stream)
    if result.saved_status is None:
        print("БД не изменена (dry-run).", file=stream)
    else:
        print(f"Сохранён статус: {result.saved_status.value}", file=stream)


async def _run(arguments: argparse.Namespace) -> LastMessageInspection:
    result = await inspect_last_message(
        arguments.database,
        client_spp_id=arguments.client_spp_id,
        history_limit=arguments.history_limit,
        write=arguments.write,
    )
    print_inspection(result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_run(parse_arguments(argv)))


if __name__ == "__main__":
    main()
