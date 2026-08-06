"""SQLite-хранилище реквизитов организаций и их контактов."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class Organization:
    """Запись организации вместе со списками контактных данных."""

    id: int
    inn: str
    kpp: str | None
    name: str
    head_name: str
    phones: tuple[str, ...]
    emails: tuple[str, ...]


class OrganizationStorage:
    """Создает и обслуживает локальную SQLite-базу организаций."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """Создать каталог и таблицы, если они еще не существуют."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS organizations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inn TEXT NOT NULL,
                    kpp TEXT,
                    name TEXT NOT NULL,
                    head_name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (length(inn) IN (10, 12)),
                    CHECK (inn NOT GLOB '*[^0-9]*'),
                    CHECK (kpp IS NULL OR (
                        length(kpp) = 9 AND kpp NOT GLOB '*[^0-9]*'
                    ))
                );

                CREATE INDEX IF NOT EXISTS idx_organizations_inn
                    ON organizations (inn);
                CREATE INDEX IF NOT EXISTS idx_organizations_inn_kpp
                    ON organizations (inn, kpp);

                CREATE TABLE IF NOT EXISTS organization_phones (
                    organization_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    PRIMARY KEY (organization_id, position),
                    UNIQUE (organization_id, phone),
                    FOREIGN KEY (organization_id) REFERENCES organizations(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS organization_emails (
                    organization_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    PRIMARY KEY (organization_id, position),
                    UNIQUE (organization_id, email),
                    FOREIGN KEY (organization_id) REFERENCES organizations(id)
                        ON DELETE CASCADE
                );
                """
            )

    def add(
        self,
        *,
        inn: str,
        kpp: str | None,
        name: str,
        head_name: str,
        phones: Iterable[str] = (),
        emails: Iterable[str] = (),
    ) -> Organization:
        """Атомарно добавить организацию и ее контактные списки."""
        clean_inn = self._digits(inn, "ИНН", (10, 12))
        clean_kpp = self._optional_digits(kpp, "КПП", 9)
        clean_name = self._required(name, "Название организации")
        clean_head_name = self._required(head_name, "Имя руководителя")
        clean_phones = self._contacts(phones, "Телефон")
        clean_emails = self._contacts(emails, "Электронная почта")

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO organizations (inn, kpp, name, head_name)
                VALUES (?, ?, ?, ?)
                """,
                (clean_inn, clean_kpp, clean_name, clean_head_name),
            )
            organization_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO organization_phones
                    (organization_id, position, phone)
                VALUES (?, ?, ?)
                """,
                (
                    (organization_id, position, phone)
                    for position, phone in enumerate(clean_phones)
                ),
            )
            connection.executemany(
                """
                INSERT INTO organization_emails
                    (organization_id, position, email)
                VALUES (?, ?, ?)
                """,
                (
                    (organization_id, position, email)
                    for position, email in enumerate(clean_emails)
                ),
            )

        organization = self.get(organization_id)
        if organization is None:  # pragma: no cover - защита от поврежденной БД
            raise RuntimeError("Добавленная организация не найдена")
        return organization

    def get(self, organization_id: int) -> Organization | None:
        """Получить организацию по внутреннему идентификатору."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, inn, kpp, name, head_name
                FROM organizations
                WHERE id = ?
                """,
                (organization_id,),
            ).fetchone()
            if row is None:
                return None
            return self._to_organization(connection, row)

    def find_by_inn(self, inn: str) -> list[Organization]:
        """Найти все карточки с ИНН, не считая ИНН уникальным ключом."""
        clean_inn = self._digits(inn, "ИНН", (10, 12))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, inn, kpp, name, head_name
                FROM organizations
                WHERE inn = ?
                ORDER BY id
                """,
                (clean_inn,),
            ).fetchall()
            return [self._to_organization(connection, row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _to_organization(
        connection: sqlite3.Connection, row: sqlite3.Row
    ) -> Organization:
        organization_id = int(row["id"])
        phones = connection.execute(
            """
            SELECT phone FROM organization_phones
            WHERE organization_id = ? ORDER BY position
            """,
            (organization_id,),
        ).fetchall()
        emails = connection.execute(
            """
            SELECT email FROM organization_emails
            WHERE organization_id = ? ORDER BY position
            """,
            (organization_id,),
        ).fetchall()
        return Organization(
            id=organization_id,
            inn=str(row["inn"]),
            kpp=str(row["kpp"]) if row["kpp"] is not None else None,
            name=str(row["name"]),
            head_name=str(row["head_name"]),
            phones=tuple(str(item["phone"]) for item in phones),
            emails=tuple(str(item["email"]) for item in emails),
        )

    @staticmethod
    def _required(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} не может быть пустым")
        return value.strip()

    @classmethod
    def _digits(
        cls, value: str, field_name: str, lengths: tuple[int, ...]
    ) -> str:
        clean_value = cls._required(value, field_name)
        if not clean_value.isdigit() or len(clean_value) not in lengths:
            expected = " или ".join(str(length) for length in lengths)
            raise ValueError(f"{field_name} должен содержать {expected} цифр")
        return clean_value

    @classmethod
    def _optional_digits(
        cls, value: str | None, field_name: str, length: int
    ) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return cls._digits(value, field_name, (length,))

    @classmethod
    def _contacts(cls, values: Iterable[str], field_name: str) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            clean_value = cls._required(value, field_name)
            if clean_value not in seen:
                seen.add(clean_value)
                result.append(clean_value)
        return tuple(result)
