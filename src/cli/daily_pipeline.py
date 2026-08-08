"""Единый ежедневный запуск сбора СБИС и Telegram-обогащения."""

from __future__ import annotations

import argparse
import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from src.application.telegram_enrichment import EnrichmentOutcome
from src.cli.telegram_enrichment import print_summary, run_enrichment
from src.config import PROJECT_ROOT
from src.integrations.sbis import get_open_companies_by_date


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clients.db"
DEFAULT_LIMIT = 100
DEFAULT_TIMEOUT = 45.0


@dataclass(frozen=True)
class DailyPipelineResult:
    target_date: date
    collected_cards: int
    enrichment_outcomes: tuple[EnrichmentOutcome, ...]


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("значение должно быть целым числом") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("значение должно быть числом") from error
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(
            "значение должно быть конечным числом больше нуля"
        )
    return number


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "дата должна иметь формат YYYY-MM-DD"
        ) from error


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Получить новые карточки СБИС за день, сохранить их в SQLite и "
            "последовательно обработать очередь через Telegram."
        )
    )
    parser.add_argument(
        "--date",
        type=_iso_date,
        help="дата регистрации YYYY-MM-DD; по умолчанию используется вчера",
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
        default=DEFAULT_LIMIT,
        help=f"максимум клиентов очереди (по умолчанию: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=DEFAULT_TIMEOUT,
        help=f"таймаут Telegram в секундах (по умолчанию: {DEFAULT_TIMEOUT:g})",
    )
    return parser.parse_args(argv)


async def run_daily_pipeline(
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    *,
    target_date: date | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
) -> DailyPipelineResult:
    """Последовательно выполнить сбор СБИС и обогащение с записью в БД."""
    effective_date = target_date or (date.today() - timedelta(days=1))
    cards = await get_open_companies_by_date(
        effective_date,
        database_path=database_path,
    )
    outcomes = await run_enrichment(
        database_path,
        limit=limit,
        timeout=timeout,
        write=True,
    )
    return DailyPipelineResult(
        target_date=effective_date,
        collected_cards=len(cards),
        enrichment_outcomes=tuple(outcomes),
    )


def print_daily_summary(result: DailyPipelineResult) -> None:
    print(f"Дата сбора: {result.target_date.isoformat()}.")
    print(f"Новых карточек СБИС собрано: {result.collected_cards}.")
    print_summary(result.enrichment_outcomes, write=True)


async def _run(arguments: argparse.Namespace) -> DailyPipelineResult:
    result = await run_daily_pipeline(
        arguments.database,
        target_date=arguments.date,
        limit=arguments.limit,
        timeout=arguments.timeout,
    )
    print_daily_summary(result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_run(parse_arguments(argv)))


if __name__ == "__main__":
    main()
