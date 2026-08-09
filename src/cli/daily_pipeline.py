"""Единый ежедневный запуск сбора СБИС и Telegram-обогащения."""

from __future__ import annotations

import argparse
import asyncio
import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot

from src.application.telegram_enrichment import (
    BalanceObserver,
    EnrichmentOutcome,
    TelegramBalanceCheckError,
)
from src.cli.telegram_enrichment import print_summary, run_enrichment
from src.config import PROJECT_ROOT
from src.integrations.sbis import get_open_companies_by_date
from src.integrations.telegram.report_bot import load_report_bot_settings
from src.integrations.telegram.report_sender import (
    ReportDispatchResult,
    notify_admins,
    notify_balance_check_failed,
    observe_query_balance,
    retry_failed_report_deliveries,
    send_daily_report,
    send_late_update_reports,
)
from src.storage import NewClientStorage, ReportingStorage


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clients.db"
DEFAULT_LIMIT = 100
DEFAULT_TIMEOUT = 45.0
REPORT_DELTA = 7
PipelineProgressObserver = Callable[[str, int], None]


@dataclass(frozen=True)
class DailyPipelineResult:
    target_date: date
    collected_cards: int
    enrichment_outcomes: tuple[EnrichmentOutcome, ...]
    report_dispatch: ReportDispatchResult | None = None
    late_update_dispatches: tuple[ReportDispatchResult, ...] = ()
    retry_dispatches: tuple[ReportDispatchResult, ...] = ()


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
            "последовательно обработать очередь через Telegram, затем "
            "отправить подписчикам отчет D-7."
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
    balance_observer: BalanceObserver | None = None,
    progress_observer: PipelineProgressObserver | None = None,
) -> DailyPipelineResult:
    """Последовательно выполнить сбор СБИС и обогащение с записью в БД."""
    effective_date = target_date or (date.today() - timedelta(days=1))
    if progress_observer is not None:
        progress_observer("sbis_collection", 0)
    cards = await get_open_companies_by_date(
        effective_date,
        database_path=database_path,
    )
    if progress_observer is not None:
        progress_observer("telegram_enrichment", len(cards))
    enrichment_options: dict[str, Any] = {
        "limit": limit,
        "timeout": timeout,
        "write": True,
    }
    if balance_observer is not None:
        enrichment_options["balance_observer"] = balance_observer
    outcomes = await run_enrichment(database_path, **enrichment_options)
    if progress_observer is not None:
        progress_observer("completed", len(cards))
    return DailyPipelineResult(
        target_date=effective_date,
        collected_cards=len(cards),
        enrichment_outcomes=tuple(outcomes),
    )


def print_daily_summary(result: DailyPipelineResult) -> None:
    print(f"Дата сбора: {result.target_date.isoformat()}.")
    print(f"Новых карточек СБИС собрано: {result.collected_cards}.")
    print_summary(result.enrichment_outcomes, write=True)
    if result.report_dispatch is not None:
        dispatch = result.report_dispatch
        print(
            "Отчет D-7: "
            f"организаций {dispatch.clients_count}, "
            f"доставлено {dispatch.sent}, ошибок доставки {dispatch.failed}."
        )
    if result.late_update_dispatches:
        print(
            "Дополнительных отчетов по поздним результатам: "
            f"{len(result.late_update_dispatches)}."
        )
    if result.retry_dispatches:
        print(
            "Повторно обработано недоставленных отчетов: "
            f"{len(result.retry_dispatches)}."
        )


async def _run(arguments: argparse.Namespace) -> DailyPipelineResult:
    settings = load_report_bot_settings(arguments.database)
    reporting_storage = ReportingStorage(arguments.database)
    reporting_storage.initialize()
    client_storage = NewClientStorage(arguments.database)
    client_storage.initialize()

    effective_date = arguments.date or (date.today() - timedelta(days=1))
    pipeline_run = reporting_storage.start_pipeline_run(effective_date)
    bootstrap_admin_ids = tuple(settings.bootstrap_admin_ids)
    result: DailyPipelineResult | None = None
    pipeline_error: Exception | None = None
    pipeline_error_stage: str | None = None
    report_error: Exception | None = None
    cleanup_error: Exception | None = None
    outer_error: Exception | None = None
    available_queries: int | None = None
    collected_cards = 0
    current_stage = "startup"

    def progress_observer(stage: str, cards_count: int) -> None:
        nonlocal current_stage, collected_cards
        current_stage = stage
        collected_cards = cards_count

    try:
        async with Bot(token=settings.token) as report_bot:

            async def safe_notify(text: str) -> None:
                try:
                    await notify_admins(
                        report_bot,
                        reporting_storage,
                        bootstrap_admin_ids,
                        text,
                    )
                except Exception:
                    # Ошибка служебного уведомления не должна скрыть исходный сбой.
                    return

            async def balance_observer(balance: int) -> None:
                nonlocal available_queries
                available_queries = balance
                try:
                    await observe_query_balance(
                        report_bot,
                        reporting_storage,
                        bootstrap_admin_ids,
                        balance,
                        low_threshold=settings.low_query_threshold,
                    )
                except Exception:
                    # Сбой служебного уведомления не останавливает обогащение.
                    return

            try:
                result = await run_daily_pipeline(
                    arguments.database,
                    target_date=arguments.date,
                    limit=arguments.limit,
                    timeout=arguments.timeout,
                    balance_observer=balance_observer,
                    progress_observer=progress_observer,
                )
                collected_cards = result.collected_cards
            except TelegramBalanceCheckError as error:
                pipeline_error = error
                pipeline_error_stage = "telegram_balance"
                try:
                    await notify_balance_check_failed(
                        report_bot,
                        reporting_storage,
                        bootstrap_admin_ids,
                    )
                except Exception:
                    pass
            except Exception as error:
                pipeline_error = error
                pipeline_error_stage = current_stage
                await safe_notify(
                    "Ежедневный конвейер остановился. "
                    f"Этап: {current_stage}. "
                    f"Код ошибки: {type(error).__name__}."
                )

            report_date = date.today() - timedelta(days=REPORT_DELTA) # Потом поменять на Срок
            retry_dispatches: tuple[ReportDispatchResult, ...] = ()
            late_updates: tuple[ReportDispatchResult, ...] = ()
            dispatch: ReportDispatchResult | None = None
            try:
                current_stage = "reports"
                retry_dispatches = await retry_failed_report_deliveries(
                    report_bot,
                    reporting_storage,
                    bootstrap_admin_ids=bootstrap_admin_ids,
                    exclude_daily_date=report_date,
                )
                late_updates = await send_late_update_reports(
                    report_bot,
                    client_storage,
                    reporting_storage,
                    bootstrap_admin_ids=bootstrap_admin_ids,
                    eligible_through=report_date,
                )
                dispatch = await send_daily_report(
                    report_bot,
                    client_storage,
                    reporting_storage,
                    report_date,
                    bootstrap_admin_ids=bootstrap_admin_ids,
                )
                dispatches = (
                    *retry_dispatches,
                    *late_updates,
                    dispatch,
                )
                failed_deliveries = sum(item.failed for item in dispatches)
                if failed_deliveries > 0:
                    await safe_notify(
                        "Отчеты сформированы, но часть доставок завершилась "
                        f"ошибкой. Не доставлено: {failed_deliveries}."
                    )
            except Exception as error:
                report_error = error
                await safe_notify(
                    "Не удалось сформировать или отправить ежедневный отчет. "
                    f"Код ошибки: {type(error).__name__}."
                )

            retention_days = getattr(settings, "report_retention_days", None)
            if retention_days is not None:
                try:
                    current_stage = "report_cleanup"
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        days=retention_days
                    )
                    reporting_storage.delete_report_runs_created_before(cutoff)
                except Exception as error:
                    cleanup_error = error
                    await safe_notify(
                        "Не удалось очистить устаревшие снимки отчетов. "
                        f"Код ошибки: {type(error).__name__}."
                    )

            if result is not None:
                result = DailyPipelineResult(
                    target_date=result.target_date,
                    collected_cards=result.collected_cards,
                    enrichment_outcomes=result.enrichment_outcomes,
                    report_dispatch=dispatch,
                    late_update_dispatches=late_updates,
                    retry_dispatches=retry_dispatches,
                )
    except Exception as error:
        outer_error = error

    final_error = pipeline_error or report_error or cleanup_error or outer_error
    error_stage = (
        pipeline_error_stage
        if pipeline_error is not None
        else "reports"
        if report_error is not None
        else "report_cleanup"
        if cleanup_error is not None
        else "report_bot"
        if outer_error is not None
        else None
    )
    processing_counts = Counter(
        outcome.status.value
        for outcome in (() if result is None else result.enrichment_outcomes)
    )
    try:
        reporting_storage.finish_pipeline_run(
            pipeline_run.id,
            status="failed" if final_error is not None else "completed",
            collected_cards=collected_cards,
            processing_status_counts=dict(processing_counts),
            available_queries=available_queries,
            error_stage=error_stage,
            error_code=(
                None if final_error is None else type(final_error).__name__[:100]
            ),
        )
    except Exception as state_error:
        # Сбой журнала не должен скрывать исходную ошибку СБИС/Telegram/отчета.
        if final_error is None:
            final_error = state_error

    if final_error is not None:
        raise final_error
    if result is None:  # pragma: no cover - внутренний инвариант
        raise RuntimeError("Ежедневный конвейер не вернул результат")
    print_daily_summary(result)
    return result


def main(argv: Sequence[str] | None = None) -> None:
    asyncio.run(_run(parse_arguments(argv)))


if __name__ == "__main__":
    main()
