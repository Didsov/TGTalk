"""Проверка СБИС, Telethon-сессии и отчётного Telegram-бота."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from aiogram import Bot

from src.application.health import HEALTHY, run_active_health_probes
from src.cli.report_bot import DEFAULT_DATABASE_PATH
from src.integrations.telegram.report_bot import load_report_bot_settings
from src.integrations.telegram.report_sender import notify_admins
from src.storage.reporting import IntegrationHealth, ReportingStorage


STATUS_LABELS = {
    "healthy": "работает",
    "unauthorized": "авторизация недействительна",
    "rate_limited": "ограничено по частоте",
    "unreachable": "сервис недоступен",
    "degraded": "работает с ошибкой",
    "unknown": "не проверено",
}


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить интеграции и сохранить результат в SQLite."
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Не отправлять администраторам уведомления о смене состояния.",
    )
    return parser.parse_args(argv)


def should_notify(
    previous: IntegrationHealth | None,
    current: IntegrationHealth,
) -> bool:
    """Не спамить одинаковыми ошибками, но сообщать о сбое и восстановлении."""
    if previous is not None and previous.status == current.status:
        return (
            current.status in {"unreachable", "degraded"}
            and current.consecutive_failures == 3
        )
    if current.status == HEALTHY:
        return previous is not None and previous.status != HEALTHY
    if current.status in {"unauthorized", "rate_limited"}:
        return True
    return current.consecutive_failures >= 3


def health_notification(current: IntegrationHealth) -> str:
    label = STATUS_LABELS.get(current.status, current.status)
    if current.status == HEALTHY:
        return f"Интеграция {current.integration} восстановлена и работает."
    return (
        f"Проверка интеграции {current.integration}: {label}. "
        f"Код: {current.error_code or 'unknown'}."
    )


async def run_healthcheck(arguments: argparse.Namespace) -> int:
    settings = load_report_bot_settings(arguments.database)
    storage = ReportingStorage(arguments.database)
    storage.initialize()
    async with Bot(token=settings.token) as bot:
        results, available_queries = await run_active_health_probes(bot)
        for result in results:
            previous = storage.get_integration_health(result.integration)
            current = storage.record_integration_health(
                result.integration,
                result.status,
                error_code=result.error_code,
            )
            print(
                f"{current.integration}: {STATUS_LABELS[current.status]}; "
                f"код={current.error_code or '-'}"
            )
            if (
                not arguments.no_notify
                and result.integration != "report_bot"
                and should_notify(previous, current)
            ):
                await notify_admins(
                    bot,
                    storage,
                    tuple(settings.bootstrap_admin_ids),
                    health_notification(current),
                )
        if available_queries is not None:
            storage.set_notification_state(
                "health_available_queries", str(available_queries)
            )
            print(f"available_queries: {available_queries}")
    return 0 if all(result.status == HEALTHY for result in results) else 1


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    raise SystemExit(asyncio.run(run_healthcheck(arguments)))


if __name__ == "__main__":
    main()
