"""Запуск отдельного Telegram-бота подписок и отчетов."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from aiogram import Bot, Dispatcher

from src.config import PROJECT_ROOT
from src.integrations.telegram.report_bot import (
    ReportBotService,
    ReportBotSettings,
    create_report_router,
    load_report_bot_settings,
)
from src.storage.new_clients import NewClientStorage
from src.storage.reporting import ReportingStorage


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clients.db"


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Разобрать параметры запуска без чтения секретов окружения."""
    parser = argparse.ArgumentParser(
        description=(
            "Запустить закрытого Telegram-бота подписок и отчетов. "
            "Ручные выборки читаются только из указанной SQLite-БД."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help=f"SQLite-файл (по умолчанию: {DEFAULT_DATABASE_PATH})",
    )
    return parser.parse_args(argv)


async def run_report_bot(settings: ReportBotSettings) -> None:
    """Инициализировать хранилища и запустить long polling отчетного бота."""
    access_storage = ReportingStorage(settings.database_path)
    access_storage.initialize()
    client_storage = NewClientStorage(settings.database_path)
    client_storage.initialize()

    service = ReportBotService(
        access_storage,
        client_storage,
        bootstrap_admin_ids=settings.bootstrap_admin_ids,
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(create_report_router(service))

    logging.info("Отчетный бот запущен; SQLite: %s", settings.database_path)
    async with Bot(token=settings.token) as bot:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )


def main(argv: Sequence[str] | None = None) -> None:
    """Точка входа ``python -m src.cli.report_bot``."""
    arguments = parse_arguments(argv)
    settings = load_report_bot_settings(arguments.database)
    asyncio.run(run_report_bot(settings))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
