"""Запуск первых N клиентов очереди через Telegram-поиск."""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from src.application.telegram_enrichment import (
    EnrichmentOutcome,
    process_first_clients,
)
from src.config import PROJECT_ROOT, loadEnvironment, requireSetting
from src.integrations.telegram.bot_client import closeTg, openTg
from src.integrations.telegram.report_downloader import allowed_report_hosts
from src.storage import NewClientStorage, ProcessingStatus, STATUS_LABELS


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clients.db"
DEFAULT_TIMEOUT = 30.0


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "значение должно быть целым числом"
        ) from error
    if number <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "значение должно быть числом"
        ) from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(
            "значение должно быть конечным числом больше нуля"
        )
    return number


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Обработать первые N клиентов очереди через Telegram-бота. "
            "Без --write результаты поиска не сохраняются."
        ),
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help=f"SQLite-файл (по умолчанию: {DEFAULT_DATABASE_PATH})",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        required=True,
        help="максимальное число клиентов для последовательной обработки",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=DEFAULT_TIMEOUT,
        help=(
            "таймаут одного действия Telegram в секундах "
            f"(по умолчанию: {DEFAULT_TIMEOUT:g})"
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="сохранить контакты, статусы и попытки в SQLite",
    )
    return parser.parse_args(argv)


async def run_enrichment(
    database_path: str | Path,
    *,
    limit: int,
    timeout: float = DEFAULT_TIMEOUT,
    write: bool = False,
) -> list[EnrichmentOutcome]:
    """Открыть Telegram, обработать очередь и гарантированно закрыть клиент."""
    loadEnvironment()
    bot_username = requireSetting("TELEGRAM_TARGET_BOT")
    allowed_report_hosts()

    storage = NewClientStorage(database_path)
    storage.initialize()

    telegram_client = await openTg()
    try:
        return await process_first_clients(
            storage,
            telegram_client,
            bot_username,
            limit=limit,
            write=write,
            timeout=timeout,
        )
    finally:
        await closeTg(telegram_client)


def print_summary(
    outcomes: Sequence[EnrichmentOutcome],
    *,
    write: bool,
    output: TextIO | None = None,
) -> None:
    """Напечатать агрегаты без ИНН, названий и найденных контактов."""
    stream = output if output is not None else sys.stdout
    mode = "запись в БД" if write else "dry-run, результаты не сохраняются"
    print(f"Режим: {mode}.", file=stream)
    print(f"Обработано клиентов: {len(outcomes)}.", file=stream)

    status_counts = Counter(outcome.status for outcome in outcomes)
    print("Статусы:", file=stream)
    for status in ProcessingStatus:
        print(
            f"  {STATUS_LABELS[status]}: {status_counts[status]}",
            file=stream,
        )

    result_counts = Counter(outcome.result_code for outcome in outcomes)
    if result_counts:
        print("Коды результата:", file=stream)
        for result_code, count in sorted(result_counts.items()):
            print(f"  {result_code}: {count}", file=stream)

    retry_delays = [
        outcome.retry_after_seconds
        for outcome in outcomes
        if outcome.retry_after_seconds is not None
    ]
    if retry_delays:
        print(
            "Повторный запуск не раньше чем через "
            f"{max(retry_delays)} сек.",
            file=stream,
        )


async def _run(arguments: argparse.Namespace) -> list[EnrichmentOutcome]:
    outcomes = await run_enrichment(
        arguments.database,
        limit=arguments.limit,
        timeout=arguments.timeout,
        write=arguments.write,
    )
    print_summary(outcomes, write=arguments.write)
    return outcomes


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    asyncio.run(_run(arguments))


if __name__ == "__main__":
    main()
