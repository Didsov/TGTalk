"""SQLite-хранилище списка новых клиентов СБИС."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Iterator


class ProcessingStatus(StrEnum):
    """Состояние поиска контактных данных клиента."""

    PROCESSED = "processed"
    QUEUED = "queued"
    SKIPPED = "skipped"
    RETRY_REQUIRED = "retry_required"


STATUS_LABELS: dict[ProcessingStatus, str] = {
    ProcessingStatus.PROCESSED: "Обработан",
    ProcessingStatus.QUEUED: "В очереди на обработку",
    ProcessingStatus.SKIPPED: "Пропущен",
    ProcessingStatus.RETRY_REQUIRED: "Требуется повторная обработка",
}


@dataclass(frozen=True)
class NewClient:
    spp_id: int
    name: str
    region: str | None
    ogrn: str | None
    inn: str
    kpp: str | None
    is_entrepreneur: bool
    registration_date: str | None
    liquidation_date: str | None
    director_last_name: str | None
    director_first_name: str | None
    director_middle_name: str | None
    sbis_phones: tuple[str, ...]
    telegram_phones: tuple[str, ...]
    sbis_emails: tuple[str, ...]
    telegram_emails: tuple[str, ...]
    status: ProcessingStatus


class NewClientStorage:
    """Сохраняет реквизиты и контакты новых клиентов без дублей."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS new_clients (
                    spp_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    region TEXT,
                    ogrn TEXT,
                    inn TEXT NOT NULL,
                    kpp TEXT,
                    is_entrepreneur INTEGER NOT NULL,
                    registration_date TEXT,
                    liquidation_date TEXT,
                    director_last_name TEXT,
                    director_first_name TEXT,
                    director_middle_name TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (is_entrepreneur IN (0, 1)),
                    CHECK (status IN (
                        'processed', 'queued', 'skipped', 'retry_required'
                    )),
                    CHECK (length(inn) IN (10, 12)),
                    CHECK (inn NOT GLOB '*[^0-9]*'),
                    CHECK (kpp IS NULL OR (
                        length(kpp) = 9 AND kpp NOT GLOB '*[^0-9]*'
                    ))
                );

                CREATE INDEX IF NOT EXISTS idx_new_clients_inn
                    ON new_clients (inn);
                CREATE INDEX IF NOT EXISTS idx_new_clients_status
                    ON new_clients (status);

                CREATE TABLE IF NOT EXISTS new_client_contacts (
                    client_spp_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (client_spp_id, kind, source, position),
                    UNIQUE (client_spp_id, kind, source, value),
                    CHECK (kind IN ('phone', 'email')),
                    CHECK (source IN ('sbis', 'telegram')),
                    FOREIGN KEY (client_spp_id) REFERENCES new_clients(spp_id)
                        ON DELETE CASCADE
                );
                """
            )

    def upsert_from_sbis(self, record: dict[str, Any]) -> NewClient:
        """Сохранить словарь преобразованной записи СБИС идемпотентно."""
        spp_id = self._positive_int(record.get("ИдентификаторСПП"))
        name = self._required(record.get("Название"), "Название")
        inn = self._digits(record.get("ИНН"), "ИНН", (10, 12))
        kpp = self._optional_digits(record.get("КПП"), "КПП", 9)
        is_entrepreneur = self._boolean(record.get("Предприниматель"))
        sbis_phones = self._contacts(record.get("Телефон"))
        sbis_emails = self._contacts(record.get("email"))

        values = (
            spp_id,
            name,
            self._optional_text(record.get("Регион")),
            self._optional_text(record.get("ОГРН")),
            inn,
            kpp,
            int(is_entrepreneur),
            self._date_text(record.get("ДатаРегистрации")),
            self._date_text(record.get("ДатаЛиквидации")),
            self._optional_text(record.get("Директор.Фамилия")),
            self._optional_text(record.get("Директор.Имя")),
            self._optional_text(record.get("Директор.Отчество")),
        )

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO new_clients (
                    spp_id, name, region, ogrn, inn, kpp, is_entrepreneur,
                    registration_date, liquidation_date, director_last_name,
                    director_first_name, director_middle_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(spp_id) DO UPDATE SET
                    name = excluded.name,
                    region = excluded.region,
                    ogrn = excluded.ogrn,
                    inn = excluded.inn,
                    kpp = excluded.kpp,
                    is_entrepreneur = excluded.is_entrepreneur,
                    registration_date = excluded.registration_date,
                    liquidation_date = excluded.liquidation_date,
                    director_last_name = excluded.director_last_name,
                    director_first_name = excluded.director_first_name,
                    director_middle_name = excluded.director_middle_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                values,
            )
            self._replace_contacts(
                connection, spp_id, "phone", "sbis", sbis_phones
            )
            self._replace_contacts(
                connection, spp_id, "email", "sbis", sbis_emails
            )

        client = self.get(spp_id)
        if client is None:  # pragma: no cover
            raise RuntimeError("Сохраненный клиент не найден")
        return client

    def save_sbis_list(self, records: Iterable[dict[str, Any]]) -> list[NewClient]:
        """Сохранить список, полученный пользовательским преобразователем СБИС."""
        return [self.upsert_from_sbis(record) for record in records]

    def replace_telegram_contacts(
        self,
        spp_id: int,
        *,
        phones: Iterable[str] | str | None = None,
        emails: Iterable[str] | str | None = None,
    ) -> NewClient:
        """Заменить найденные в Telegram контакты указанного клиента."""
        with self._connect() as connection:
            self._require_client(connection, spp_id)
            if phones is not None:
                self._replace_contacts(
                    connection, spp_id, "phone", "telegram", self._contacts(phones)
                )
            if emails is not None:
                self._replace_contacts(
                    connection, spp_id, "email", "telegram", self._contacts(emails)
                )

        client = self.get(spp_id)
        if client is None:  # pragma: no cover
            raise RuntimeError("Клиент не найден после обновления контактов")
        return client

    def set_status(
        self, spp_id: int, status: ProcessingStatus | str
    ) -> NewClient:
        """Установить статус обработки клиента."""
        try:
            clean_status = ProcessingStatus(status)
        except ValueError as error:
            raise ValueError(f"Неизвестный статус обработки: {status}") from error

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE new_clients
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE spp_id = ?
                """,
                (clean_status.value, spp_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Клиент СБИС {spp_id} не найден")

        client = self.get(spp_id)
        if client is None:  # pragma: no cover
            raise RuntimeError("Клиент не найден после обновления статуса")
        return client

    def get(self, spp_id: int) -> NewClient | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM new_clients WHERE spp_id = ?", (spp_id,)
            ).fetchone()
            if row is None:
                return None
            return self._to_client(connection, row)

    def list_for_processing(self, limit: int | None = None) -> list[NewClient]:
        """Вернуть очередь и записи, которым требуется повторная обработка."""
        if limit is not None and (isinstance(limit, bool) or limit <= 0):
            raise ValueError("limit должен быть положительным целым числом")
        query = """
            SELECT * FROM new_clients
            WHERE status IN ('queued', 'retry_required')
            ORDER BY created_at, spp_id
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._to_client(connection, row) for row in rows]

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

    @classmethod
    def _to_client(
        cls, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> NewClient:
        contacts = connection.execute(
            """
            SELECT kind, source, value FROM new_client_contacts
            WHERE client_spp_id = ? ORDER BY kind, source, position
            """,
            (row["spp_id"],),
        ).fetchall()

        def values(kind: str, source: str) -> tuple[str, ...]:
            return tuple(
                str(contact["value"])
                for contact in contacts
                if contact["kind"] == kind and contact["source"] == source
            )

        return NewClient(
            spp_id=int(row["spp_id"]),
            name=str(row["name"]),
            region=cls._row_optional(row["region"]),
            ogrn=cls._row_optional(row["ogrn"]),
            inn=str(row["inn"]),
            kpp=cls._row_optional(row["kpp"]),
            is_entrepreneur=bool(row["is_entrepreneur"]),
            registration_date=cls._row_optional(row["registration_date"]),
            liquidation_date=cls._row_optional(row["liquidation_date"]),
            director_last_name=cls._row_optional(row["director_last_name"]),
            director_first_name=cls._row_optional(row["director_first_name"]),
            director_middle_name=cls._row_optional(row["director_middle_name"]),
            sbis_phones=values("phone", "sbis"),
            telegram_phones=values("phone", "telegram"),
            sbis_emails=values("email", "sbis"),
            telegram_emails=values("email", "telegram"),
            status=ProcessingStatus(row["status"]),
        )

    @staticmethod
    def _replace_contacts(
        connection: sqlite3.Connection,
        spp_id: int,
        kind: str,
        source: str,
        contacts: tuple[str, ...],
    ) -> None:
        connection.execute(
            """
            DELETE FROM new_client_contacts
            WHERE client_spp_id = ? AND kind = ? AND source = ?
            """,
            (spp_id, kind, source),
        )
        connection.executemany(
            """
            INSERT INTO new_client_contacts
                (client_spp_id, kind, source, position, value)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                (spp_id, kind, source, position, value)
                for position, value in enumerate(contacts)
            ),
        )

    @staticmethod
    def _require_client(connection: sqlite3.Connection, spp_id: int) -> None:
        exists = connection.execute(
            "SELECT 1 FROM new_clients WHERE spp_id = ?", (spp_id,)
        ).fetchone()
        if exists is None:
            raise KeyError(f"Клиент СБИС {spp_id} не найден")

    @staticmethod
    def _positive_int(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("ИдентификаторСПП должен быть положительным числом")
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "ИдентификаторСПП должен быть положительным числом"
            ) from error
        if result <= 0:
            raise ValueError("ИдентификаторСПП должен быть положительным числом")
        return result

    @staticmethod
    def _required(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} не может быть пустым")
        return value.strip()

    @classmethod
    def _digits(
        cls, value: Any, field_name: str, lengths: tuple[int, ...]
    ) -> str:
        clean_value = cls._required(value, field_name)
        if not clean_value.isdigit() or len(clean_value) not in lengths:
            expected = " или ".join(str(length) for length in lengths)
            raise ValueError(f"{field_name} должен содержать {expected} цифр")
        return clean_value

    @classmethod
    def _optional_digits(
        cls, value: Any, field_name: str, length: int
    ) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return cls._digits(value, field_name, (length,))

    @staticmethod
    def _boolean(value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("Предприниматель должен быть логическим значением")
        return value

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        clean_value = str(value).strip()
        return clean_value or None

    @classmethod
    def _date_text(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return cls._optional_text(value)

    @classmethod
    def _contacts(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, Iterable):
            raise ValueError("Контакты должны быть строкой или списком строк")
        result: list[str] = []
        seen: set[str] = set()
        for item in items:
            clean_item = cls._required(item, "Контакт")
            if clean_item not in seen:
                seen.add(clean_item)
                result.append(clean_item)
        return tuple(result)

    @staticmethod
    def _row_optional(value: Any) -> str | None:
        return str(value) if value is not None else None
