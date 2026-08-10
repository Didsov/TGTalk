"""Дозаполнение UUID клиентов из списка Contractor.ListCompany."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from src.config import PROJECT_ROOT
from src.integrations.sbis import get_company_uuids
from src.storage import NewClientStorage


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "clients.db"


@dataclass(frozen=True)
class UuidBackfillResult:
    requested: int
    found: int
    updated: int


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Обойти Contractor.ListCompany и заполнить отсутствующие "
            "contractor_uuid в SQLite."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help=f"SQLite-файл (по умолчанию: {DEFAULT_DATABASE_PATH})",
    )
    return parser.parse_args(argv)


async def run_backfill(database_path: str | Path) -> UuidBackfillResult:
    storage = NewClientStorage(database_path)
    storage.initialize()
    clients = storage.list_without_contractor_uuid()
    if not clients:
        return UuidBackfillResult(requested=0, found=0, updated=0)

    dated_clients = sorted(
        clients,
        key=lambda client: _registration_date(client.registration_date),
        reverse=True,
    )
    oldest_registration_date = _registration_date(
        dated_clients[-1].registration_date
    )
    found = await get_company_uuids(
        [client.spp_id for client in dated_clients],
        oldest_registration_date=oldest_registration_date,
    )
    updated = sum(
        storage.set_contractor_uuid_if_missing(spp_id, contractor_uuid)
        for spp_id, contractor_uuid in found.items()
    )
    return UuidBackfillResult(
        requested=len(clients),
        found=len(found),
        updated=updated,
    )


def _registration_date(value: str | None) -> date:
    """Прочитать обязательную дату регистрации из сохраненной карточки."""
    if value is None or not value.strip():
        raise ValueError(
            "Для backfill UUID у всех выбранных клиентов нужна ДатаРегистрации"
        )
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError as error:
        raise ValueError(
            "ДатаРегистрации в БД должна иметь формат ISO"
        ) from error


def print_summary(result: UuidBackfillResult) -> None:
    print(f"Без UUID до запуска: {result.requested}.")
    print(f"Найдено UUID в списке: {result.found}.")
    print(f"Записано UUID: {result.updated}.")
    print(f"Не найдено в списке: {result.requested - result.found}.")


def main(argv: Sequence[str] | None = None) -> None:
    arguments = parse_arguments(argv)
    print_summary(asyncio.run(run_backfill(arguments.database)))


if __name__ == "__main__":
    main()
