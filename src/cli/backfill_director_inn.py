"""Дозаполнение ИНН директора повторным чтением ContractorCard.Read."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from src.config import PROJECT_ROOT
from src.integrations.sbis import get_company_card
from src.integrations.sbis.companies import (
    COMPANY_CARD_RATE_LIMIT,
    COMPANY_CARD_RATE_LIMIT_PAUSE_SECONDS,
    COMPANY_CARD_REQUEST_DELAY_SECONDS,
)
from src.storage import NewClientStorage


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clients.db"
DEFAULT_LIMIT = 1000


@dataclass(frozen=True)
class DirectorInnBackfillResult:
    selected: int
    updated: int
    not_found: int
    failed: int


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
            "Перечитать карточки без director_inn или director_last_name по "
            "spp_id и contractor_uuid и дополнить данные директора."
        )
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
        help=f"максимум карточек за запуск (по умолчанию: {DEFAULT_LIMIT})",
    )
    return parser.parse_args(argv)


def _director_fields(card: dict[str, Any]) -> dict[str, Any]:
    spp_data = card.get("spp_data")
    if not isinstance(spp_data, dict):
        return {}
    director_data = spp_data
    head_data = card.get("head_data")
    if isinstance(head_data, dict) and isinstance(head_data.get("spp_data"), dict):
        director_data = head_data["spp_data"]
    return {
        "last_name": director_data.get("Директор.Фамилия"),
        "first_name": director_data.get("Директор.Имя"),
        "middle_name": director_data.get("Директор.Отчество"),
        "director_inn": director_data.get("Директор.ИНН"),
    }


async def run_backfill(
    database_path: str | Path,
    *,
    limit: int = DEFAULT_LIMIT,
) -> DirectorInnBackfillResult:
    storage = NewClientStorage(database_path)
    storage.initialize()
    clients = storage.list_without_director_inn(limit)
    updated = 0
    not_found = 0
    failed = 0

    for index, client in enumerate(clients):
        if index > 0 and index % COMPANY_CARD_RATE_LIMIT == 0:
            await asyncio.sleep(COMPANY_CARD_RATE_LIMIT_PAUSE_SECONDS)
        await asyncio.sleep(COMPANY_CARD_REQUEST_DELAY_SECONDS)
        try:
            card = await get_company_card(client.spp_id, client.contractor_uuid or "")
            director_fields = _director_fields(card)
            if not any(
                value is not None and str(value).strip()
                for value in director_fields.values()
            ):
                not_found += 1
                continue
            if storage.set_director_fields_if_missing(
                client.spp_id,
                **director_fields,
            ):
                updated += 1
        except Exception:
            # Один некорректный ответ или сетевой сбой не останавливает всю пачку.
            failed += 1

    return DirectorInnBackfillResult(
        selected=len(clients),
        updated=updated,
        not_found=not_found,
        failed=failed,
    )


def print_summary(result: DirectorInnBackfillResult) -> None:
    print(f"Выбрано карточек без ИНН или фамилии директора: {result.selected}.")
    print(f"Карточек с дополненными данными директора: {result.updated}.")
    print(f"Данные директора не найдены: {result.not_found}.")
    print(f"Ошибок чтения или валидации: {result.failed}.")


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    result = asyncio.run(
        run_backfill(arguments.database, limit=arguments.limit)
    )
    print_summary(result)


if __name__ == "__main__":
    main()
