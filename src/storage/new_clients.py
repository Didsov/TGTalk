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
    NEEDS_REVIEW = "needs_review"


class TelegramClaimError(RuntimeError):
    """Операция отклонена, потому что запись принадлежит другому worker-у."""


STATUS_LABELS: dict[ProcessingStatus, str] = {
    ProcessingStatus.PROCESSED: "Обработан",
    ProcessingStatus.QUEUED: "В очереди на обработку",
    ProcessingStatus.SKIPPED: "Пропущен",
    ProcessingStatus.RETRY_REQUIRED: "Требуется повторная обработка",
    ProcessingStatus.NEEDS_REVIEW: "Требуется ручная проверка",
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
    legal_address: str | None = None
    director_inn: str | None = None
    personalised_phones: tuple[str, ...] = ()
    personalised_emails: tuple[str, ...] = ()
    report_id: str | None = None
    reported_at: str | None = None
    data_revision: int = 0
    reported_revision: int | None = None


@dataclass(frozen=True)
class TelegramSearchAttempt:
    client_spp_id: int
    attempt_number: int
    stage: str
    result_code: str
    error_code: str | None
    created_at: str


@dataclass(frozen=True)
class RegistrationDayStats:
    """Количество всех и обработанных организаций за календарный день."""

    registration_date: str
    processed: int
    total: int


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
                    legal_address TEXT,
                    director_inn TEXT,
                    report_id TEXT,
                    reported_at TEXT,
                    data_revision INTEGER NOT NULL DEFAULT 0,
                    reported_revision INTEGER,
                    status TEXT NOT NULL DEFAULT 'queued',
                    needs_review INTEGER NOT NULL DEFAULT 0,
                    telegram_claim_token TEXT,
                    telegram_claimed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK (is_entrepreneur IN (0, 1)),
                    CHECK (status IN (
                        'processed', 'queued', 'skipped', 'retry_required'
                    )),
                    CHECK (needs_review IN (0, 1)),
                    CHECK (data_revision >= 0),
                    CHECK (
                        reported_revision IS NULL OR reported_revision >= 0
                    ),
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

                CREATE TABLE IF NOT EXISTS telegram_search_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_spp_id INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    result_code TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (client_spp_id, attempt_number),
                    FOREIGN KEY (client_spp_id) REFERENCES new_clients(spp_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS new_client_personalised_contacts (
                    client_spp_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (client_spp_id, kind, position),
                    UNIQUE (client_spp_id, kind, value),
                    CHECK (kind IN ('phone', 'email')),
                    FOREIGN KEY (client_spp_id) REFERENCES new_clients(spp_id)
                        ON DELETE CASCADE
                );
                """
            )
            self._ensure_needs_review_column(connection)
            self._ensure_telegram_claim_columns(connection)
            self._ensure_company_card_columns(connection)
            self._ensure_report_columns(connection)
            self._ensure_report_revision_columns(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_new_clients_telegram_claim
                ON new_clients (telegram_claim_token, telegram_claimed_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_new_clients_report_revision
                ON new_clients (reported_revision, data_revision)
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
                    data_revision = new_clients.data_revision + 1,
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

    def upsert_from_company_card(self, card: dict[str, Any]) -> NewClient:
        """Сохранить данные ContractorCard.Read и отдельные personalised-контакты."""
        spp_data = card.get("spp_data")
        extra_data = card.get("extra_data")
        if not isinstance(spp_data, dict):
            raise ValueError("Карточка не содержит spp_data")
        if not isinstance(extra_data, dict):
            raise ValueError("Карточка не содержит extra_data")

        spp_id = self._positive_int(
            spp_data.get("ИдентификаторСПП", card.get("ИдентификаторСПП"))
        )
        inn = self._digits(card.get("ИНН"), "ИНН", (10, 12))
        kpp = self._optional_digits(card.get("КПП"), "КПП", 9)
        contacts = extra_data.get("Контрагент.GetPersonalisedContacts")
        contact_rows = contacts if isinstance(contacts, list) else []
        phones = self._nested_contacts(contact_rows, "Phones")
        emails = self._nested_contacts(contact_rows, "Emails")

        values = (
            spp_id,
            self._required(card.get("ShortName"), "ShortName"),
            self._optional_text(spp_data.get("Регион")),
            self._optional_text(card.get("ОГРН", spp_data.get("ОГРН"))),
            inn,
            kpp,
            int(bool(spp_data.get("Предприниматель"))),
            self._date_text(card.get("ДатаРегистрации")),
            self._date_text(card.get("ДатаЛиквидации")),
            self._optional_text(spp_data.get("Директор.Фамилия")),
            self._optional_text(spp_data.get("Директор.Имя")),
            self._optional_text(spp_data.get("Директор.Отчество")),
            self._optional_text(card.get("АдресЮридический")),
            self._digits(
                spp_data.get("Директор.ИНН"),
                "Директор.ИНН",
                (10, 12),
            )
            if spp_data.get("Директор.ИНН") else None,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO new_clients (
                    spp_id, name, region, ogrn, inn, kpp, is_entrepreneur,
                    registration_date, liquidation_date, director_last_name,
                    director_first_name, director_middle_name, legal_address,
                    director_inn
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(spp_id) DO UPDATE SET
                    name = excluded.name, region = excluded.region,
                    ogrn = excluded.ogrn, inn = excluded.inn, kpp = excluded.kpp,
                    is_entrepreneur = excluded.is_entrepreneur,
                    registration_date = excluded.registration_date,
                    liquidation_date = excluded.liquidation_date,
                    director_last_name = excluded.director_last_name,
                    director_first_name = excluded.director_first_name,
                    director_middle_name = excluded.director_middle_name,
                    legal_address = excluded.legal_address,
                    director_inn = excluded.director_inn,
                    data_revision = new_clients.data_revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                values,
            )
            self._replace_personalised_contacts(connection, spp_id, "phone", phones)
            self._replace_personalised_contacts(connection, spp_id, "email", emails)

        client = self.get(spp_id)
        if client is None:  # pragma: no cover
            raise RuntimeError("Сохраненная карточка клиента не найдена")
        return client

    def save_company_cards(self, cards: Iterable[dict[str, Any]]) -> list[NewClient]:
        """Идемпотентно сохранить набор подробных карточек СБИС."""
        return [self.upsert_from_company_card(card) for card in cards]

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
            if phones is not None or emails is not None:
                connection.execute(
                    """
                    UPDATE new_clients
                    SET data_revision = data_revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE spp_id = ?
                    """,
                    (spp_id,),
                )

        client = self.get(spp_id)
        if client is None:  # pragma: no cover
            raise RuntimeError("Клиент не найден после обновления контактов")
        return client

    def save_telegram_result(
        self,
        spp_id: int,
        *,
        phones: Iterable[str] | str = (),
        emails: Iterable[str] | str = (),
        status: ProcessingStatus | str,
        stage: str,
        result_code: str,
        error_code: str | None = None,
        claim_token: str | None = None,
    ) -> NewClient:
        """Атомарно сохранить итог и освободить принадлежащий caller-у claim.

        Без ``claim_token`` сохранять разрешено только незахваченную запись.
        Это сохраняет совместимость старых вызовов, но не позволяет им снять
        claim другого worker-а.
        """
        clean_status, stored_status, needs_review = self._stored_status(status)
        clean_stage = self._required(stage, "Этап Telegram")
        clean_result_code = self._required(result_code, "Код результата Telegram")
        clean_error_code = self._optional_text(error_code)
        clean_phones = self._contacts(phones)
        clean_emails = self._contacts(emails)
        clean_claim_token = (
            self._required(claim_token, "Telegram claim token")
            if claim_token is not None
            else None
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_client(connection, spp_id)
            self._require_telegram_claim(
                connection,
                spp_id,
                clean_claim_token,
            )
            self._replace_contacts(
                connection, spp_id, "phone", "telegram", clean_phones
            )
            self._replace_contacts(
                connection, spp_id, "email", "telegram", clean_emails
            )
            connection.execute(
                """
                UPDATE new_clients
                SET status = ?, needs_review = ?,
                    telegram_claim_token = NULL,
                    telegram_claimed_at = NULL,
                    data_revision = data_revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE spp_id = ?
                """,
                (stored_status, needs_review, spp_id),
            )
            attempt_number = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt_number), 0) + 1
                    FROM telegram_search_attempts
                    WHERE client_spp_id = ?
                    """,
                    (spp_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO telegram_search_attempts (
                    client_spp_id, attempt_number, stage, result_code, error_code
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    spp_id,
                    attempt_number,
                    clean_stage,
                    clean_result_code,
                    clean_error_code,
                ),
            )

        client = self.get(spp_id)
        if client is None:  # pragma: no cover
            raise RuntimeError("Клиент не найден после сохранения результата")
        if client.status is not clean_status:  # pragma: no cover
            raise RuntimeError("Статус клиента сохранён некорректно")
        return client

    def claim_for_processing(
        self,
        limit: int,
        claim_token: str,
        stale_after_seconds: int = 900,
    ) -> list[NewClient]:
        """Атомарно закрепить первые доступные записи за одним worker-ом."""
        self._validate_processing_limit(limit)
        clean_claim_token = self._required(
            claim_token,
            "Telegram claim token",
        )
        if (
            isinstance(stale_after_seconds, bool)
            or not isinstance(stale_after_seconds, int)
            or stale_after_seconds < 0
        ):
            raise ValueError(
                "stale_after_seconds должен быть неотрицательным целым числом"
            )

        stale_modifier = f"-{stale_after_seconds} seconds"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM new_clients
                WHERE status IN ('queued', 'retry_required')
                    AND needs_review = 0
                    AND (
                        telegram_claim_token IS NULL
                        OR telegram_claimed_at IS NULL
                        OR datetime(telegram_claimed_at) <= datetime('now', ?)
                    )
                ORDER BY created_at, spp_id
                LIMIT ?
                """,
                (stale_modifier, limit),
            ).fetchall()
            spp_ids = tuple(int(row["spp_id"]) for row in rows)
            connection.executemany(
                """
                UPDATE new_clients
                SET telegram_claim_token = ?,
                    telegram_claimed_at = CURRENT_TIMESTAMP
                WHERE spp_id = ?
                """,
                (
                    (clean_claim_token, spp_id)
                    for spp_id in spp_ids
                ),
            )
            return [self._to_client(connection, row) for row in rows]

    def release_claim(self, spp_id: int, claim_token: str) -> NewClient:
        """Освободить claim только при совпадении его непрозрачного токена."""
        clean_claim_token = self._required(
            claim_token,
            "Telegram claim token",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_client(connection, spp_id)
            cursor = connection.execute(
                """
                UPDATE new_clients
                SET telegram_claim_token = NULL,
                    telegram_claimed_at = NULL
                WHERE spp_id = ? AND telegram_claim_token = ?
                """,
                (spp_id, clean_claim_token),
            )
            if cursor.rowcount == 0:
                raise TelegramClaimError(
                    f"Telegram claim клиента СБИС {spp_id} не принадлежит caller-у"
                )

        client = self.get(spp_id)
        if client is None:  # pragma: no cover
            raise RuntimeError("Клиент не найден после освобождения claim")
        return client

    def set_status(
        self, spp_id: int, status: ProcessingStatus | str
    ) -> NewClient:
        """Установить статус обработки клиента."""
        clean_status, stored_status, needs_review = self._stored_status(status)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE new_clients
                SET status = ?, needs_review = ?,
                    data_revision = data_revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE spp_id = ?
                """,
                (stored_status, needs_review, spp_id),
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

    def latest_telegram_attempt(
        self,
        *,
        result_code: str | None = None,
    ) -> TelegramSearchAttempt | None:
        """Вернуть последнюю попытку Telegram, при необходимости по коду."""
        query = """
            SELECT client_spp_id, attempt_number, stage, result_code,
                   error_code, created_at
            FROM telegram_search_attempts
        """
        parameters: tuple[str, ...] = ()
        if result_code is not None:
            clean_result_code = self._required(
                result_code, "Код результата Telegram"
            )
            query += " WHERE result_code = ?"
            parameters = (clean_result_code,)
        query += " ORDER BY id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        return TelegramSearchAttempt(
            client_spp_id=int(row["client_spp_id"]),
            attempt_number=int(row["attempt_number"]),
            stage=str(row["stage"]),
            result_code=str(row["result_code"]),
            error_code=self._row_optional(row["error_code"]),
            created_at=str(row["created_at"]),
        )

    def list_for_processing(self, limit: int | None = None) -> list[NewClient]:
        """Вернуть очередь и записи, которым требуется повторная обработка."""
        if limit is not None:
            self._validate_processing_limit(limit)
        query = """
            SELECT * FROM new_clients
            WHERE status IN ('queued', 'retry_required')
                AND needs_review = 0
                AND telegram_claim_token IS NULL
            ORDER BY created_at, spp_id
        """
        parameters: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._to_client(connection, row) for row in rows]

    def list_by_registration_date(
        self,
        target_date: date | str,
    ) -> list[NewClient]:
        """Вернуть уже сохраненные карточки за календарную дату без внешних запросов."""
        clean_date = self._date_text(target_date)
        if clean_date is None:  # pragma: no cover - обязательный аргумент
            raise ValueError("Дата отчета не может быть пустой")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM new_clients
                WHERE date(registration_date) = date(?)
                ORDER BY name COLLATE NOCASE, spp_id
                """,
                (clean_date,),
            ).fetchall()
            return [self._to_client(connection, row) for row in rows]

    def registration_date_stats(
        self,
        date_from: date | str,
        date_to: date | str,
    ) -> tuple[RegistrationDayStats, ...]:
        """Вернуть агрегаты по дням из SQLite без внешних запросов."""
        clean_from = self._date_text(date_from)
        clean_to = self._date_text(date_to)
        if clean_from is None or clean_to is None:  # pragma: no cover
            raise ValueError("Границы периода обязательны")
        if clean_from > clean_to:
            raise ValueError("Начало периода не может быть позже окончания")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT date(registration_date) AS registration_day,
                       SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END)
                           AS processed_count,
                       COUNT(*) AS total_count
                FROM new_clients
                WHERE registration_date IS NOT NULL
                  AND date(registration_date) BETWEEN date(?) AND date(?)
                GROUP BY date(registration_date)
                ORDER BY registration_day DESC
                """,
                (clean_from, clean_to),
            ).fetchall()
            return tuple(
                RegistrationDayStats(
                    registration_date=str(row["registration_day"]),
                    processed=int(row["processed_count"]),
                    total=int(row["total_count"]),
                )
                for row in rows
            )

    def latest_attempts_for_clients(
        self,
        spp_ids: Iterable[int],
    ) -> dict[int, TelegramSearchAttempt]:
        """Вернуть последнюю Telegram-попытку для каждого указанного клиента."""
        clean_ids = tuple(dict.fromkeys(int(spp_id) for spp_id in spp_ids))
        if not clean_ids:
            return {}
        placeholders = ", ".join("?" for _ in clean_ids)
        query = f"""
            SELECT attempts.client_spp_id, attempts.attempt_number,
                   attempts.stage, attempts.result_code, attempts.error_code,
                   attempts.created_at
            FROM telegram_search_attempts AS attempts
            JOIN (
                SELECT client_spp_id, MAX(attempt_number) AS attempt_number
                FROM telegram_search_attempts
                WHERE client_spp_id IN ({placeholders})
                GROUP BY client_spp_id
            ) AS latest
              ON latest.client_spp_id = attempts.client_spp_id
             AND latest.attempt_number = attempts.attempt_number
        """
        with self._connect() as connection:
            rows = connection.execute(query, clean_ids).fetchall()
        return {
            int(row["client_spp_id"]): TelegramSearchAttempt(
                client_spp_id=int(row["client_spp_id"]),
                attempt_number=int(row["attempt_number"]),
                stage=str(row["stage"]),
                result_code=str(row["result_code"]),
                error_code=self._row_optional(row["error_code"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        }

    def mark_reported(
        self,
        spp_ids: Iterable[int],
        report_id: str,
        *,
        expected_revisions: dict[int, int] | None = None,
        expected_reported_revisions: dict[int, int | None] | None = None,
    ) -> None:
        """Пометить карточки включенными в отчет без потери параллельных правок."""
        clean_report_id = self._required(report_id, "Идентификатор отчета")
        clean_ids = tuple(dict.fromkeys(int(spp_id) for spp_id in spp_ids))
        if not clean_ids:
            return
        if expected_reported_revisions is not None and expected_revisions is None:
            raise ValueError(
                "Ожидаемая reported-ревизия требует ожидаемую ревизию данных"
            )

        def clean_revision(value: int, field: str) -> int:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} должна быть неотрицательным целым числом")
            return value

        clean_revisions = None
        if expected_revisions is not None:
            clean_revisions = {
                int(spp_id): clean_revision(revision, "Ревизия данных")
                for spp_id, revision in expected_revisions.items()
            }
            missing = set(clean_ids) - clean_revisions.keys()
            if missing:
                raise ValueError("Не для всех клиентов указана ожидаемая ревизия")
        clean_reported_revisions: dict[int, int | None] | None = None
        if expected_reported_revisions is not None:
            clean_reported_revisions = {
                int(spp_id): (
                    None
                    if revision is None
                    else clean_revision(revision, "Reported-ревизия")
                )
                for spp_id, revision in expected_reported_revisions.items()
            }
            missing = set(clean_ids) - clean_reported_revisions.keys()
            if missing:
                raise ValueError(
                    "Не для всех клиентов указана ожидаемая reported-ревизия"
                )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if clean_revisions is None:
                connection.executemany(
                    """
                    UPDATE new_clients
                    SET report_id = ?, reported_at = CURRENT_TIMESTAMP,
                        reported_revision = data_revision
                    WHERE spp_id = ?
                    """,
                    ((clean_report_id, spp_id) for spp_id in clean_ids),
                )
            elif clean_reported_revisions is None:
                connection.executemany(
                    """
                    UPDATE new_clients
                    SET report_id = ?, reported_at = CURRENT_TIMESTAMP,
                        reported_revision = data_revision
                    WHERE spp_id = ? AND data_revision = ?
                    """,
                    (
                        (clean_report_id, spp_id, clean_revisions[spp_id])
                        for spp_id in clean_ids
                        if spp_id in clean_revisions
                    ),
                )
            else:
                connection.executemany(
                    """
                    UPDATE new_clients
                    SET report_id = ?, reported_at = CURRENT_TIMESTAMP,
                        reported_revision = data_revision
                    WHERE spp_id = ? AND data_revision = ?
                      AND reported_revision IS ?
                    """,
                    (
                        (
                            clean_report_id,
                            spp_id,
                            clean_revisions[spp_id],
                            clean_reported_revisions[spp_id],
                        )
                        for spp_id in clean_ids
                    ),
                )

    def list_report_updates(self) -> list[NewClient]:
        """Вернуть карточки, изменившиеся после их последнего включения в отчет."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM new_clients
                WHERE reported_at IS NOT NULL
                  AND data_revision > COALESCE(reported_revision, -1)
                ORDER BY registration_date, name COLLATE NOCASE, spp_id
                """
            ).fetchall()
            return self._to_clients(connection, rows)

    def list_unreported_through(
        self,
        target_date: date | str,
    ) -> list[NewClient]:
        """Вернуть еще не отраженные в отчетах карточки не новее указанной даты."""
        clean_date = self._date_text(target_date)
        if clean_date is None:  # pragma: no cover - обязательный аргумент
            raise ValueError("Граница отчета не может быть пустой")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM new_clients
                WHERE reported_at IS NULL
                  AND registration_date IS NOT NULL
                  AND date(registration_date) <= date(?)
                ORDER BY registration_date, name COLLATE NOCASE, spp_id
                """,
                (clean_date,),
            ).fetchall()
            return [self._to_client(connection, row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
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
        personalised_contacts = connection.execute(
            """
            SELECT kind, value FROM new_client_personalised_contacts
            WHERE client_spp_id = ? ORDER BY kind, position
            """,
            (row["spp_id"],),
        ).fetchall()
        return cls._client_from_row(row, contacts, personalised_contacts)

    @classmethod
    def _to_clients(
        cls,
        connection: sqlite3.Connection,
        rows: Iterable[sqlite3.Row],
    ) -> list[NewClient]:
        """Гидратировать набор клиентов тремя запросами вместо N+1."""
        selected_rows = tuple(rows)
        if not selected_rows:
            return []
        spp_ids = tuple(int(row["spp_id"]) for row in selected_rows)
        placeholders = ", ".join("?" for _ in spp_ids)
        contacts_by_client: dict[int, list[sqlite3.Row]] = {
            spp_id: [] for spp_id in spp_ids
        }
        personalised_by_client: dict[int, list[sqlite3.Row]] = {
            spp_id: [] for spp_id in spp_ids
        }
        contact_rows = connection.execute(
            f"""
            SELECT client_spp_id, kind, source, value
            FROM new_client_contacts
            WHERE client_spp_id IN ({placeholders})
            ORDER BY client_spp_id, kind, source, position
            """,
            spp_ids,
        ).fetchall()
        for contact in contact_rows:
            contacts_by_client[int(contact["client_spp_id"])].append(contact)
        personalised_rows = connection.execute(
            f"""
            SELECT client_spp_id, kind, value
            FROM new_client_personalised_contacts
            WHERE client_spp_id IN ({placeholders})
            ORDER BY client_spp_id, kind, position
            """,
            spp_ids,
        ).fetchall()
        for contact in personalised_rows:
            personalised_by_client[int(contact["client_spp_id"])].append(
                contact
            )
        return [
            cls._client_from_row(
                row,
                contacts_by_client[int(row["spp_id"])],
                personalised_by_client[int(row["spp_id"])],
            )
            for row in selected_rows
        ]

    @classmethod
    def _client_from_row(
        cls,
        row: sqlite3.Row,
        contacts: Iterable[sqlite3.Row],
        personalised_contacts: Iterable[sqlite3.Row],
    ) -> NewClient:
        contacts = tuple(contacts)
        personalised_contacts = tuple(personalised_contacts)

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
            status=(
                ProcessingStatus.NEEDS_REVIEW
                if bool(row["needs_review"])
                else ProcessingStatus(row["status"])
            ),
            legal_address=cls._row_optional(row["legal_address"]),
            director_inn=cls._row_optional(row["director_inn"]),
            personalised_phones=tuple(
                str(contact["value"])
                for contact in personalised_contacts
                if contact["kind"] == "phone"
            ),
            personalised_emails=tuple(
                str(contact["value"])
                for contact in personalised_contacts
                if contact["kind"] == "email"
            ),
            report_id=cls._row_optional(row["report_id"]),
            reported_at=cls._row_optional(row["reported_at"]),
            data_revision=int(row["data_revision"]),
            reported_revision=(
                None
                if row["reported_revision"] is None
                else int(row["reported_revision"])
            ),
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
    def _replace_personalised_contacts(
        connection: sqlite3.Connection,
        spp_id: int,
        kind: str,
        contacts: tuple[str, ...],
    ) -> None:
        connection.execute(
            """
            DELETE FROM new_client_personalised_contacts
            WHERE client_spp_id = ? AND kind = ?
            """,
            (spp_id, kind),
        )
        connection.executemany(
            """
            INSERT INTO new_client_personalised_contacts
                (client_spp_id, kind, position, value)
            VALUES (?, ?, ?, ?)
            """,
            (
                (spp_id, kind, position, value)
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
    def _ensure_needs_review_column(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(new_clients)")
        }
        if "needs_review" not in columns:
            connection.execute(
                """
                ALTER TABLE new_clients
                ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0
                CHECK (needs_review IN (0, 1))
                """
            )

    @staticmethod
    def _ensure_telegram_claim_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(new_clients)")
        }
        if "telegram_claim_token" not in columns:
            connection.execute(
                "ALTER TABLE new_clients ADD COLUMN telegram_claim_token TEXT"
            )
        if "telegram_claimed_at" not in columns:
            connection.execute(
                "ALTER TABLE new_clients ADD COLUMN telegram_claimed_at TEXT"
            )

    @staticmethod
    def _ensure_company_card_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(new_clients)")
        }
        if "legal_address" not in columns:
            connection.execute(
                "ALTER TABLE new_clients ADD COLUMN legal_address TEXT"
            )
        if "director_inn" not in columns:
            connection.execute(
                "ALTER TABLE new_clients ADD COLUMN director_inn TEXT"
            )

    @staticmethod
    def _ensure_report_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(new_clients)")
        }
        if "report_id" not in columns:
            connection.execute(
                "ALTER TABLE new_clients ADD COLUMN report_id TEXT"
            )
        if "reported_at" not in columns:
            connection.execute(
                "ALTER TABLE new_clients ADD COLUMN reported_at TEXT"
            )

    @staticmethod
    def _ensure_report_revision_columns(connection: sqlite3.Connection) -> None:
        """Добавить ревизии и закрыть исторические уже отправленные строки."""
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(new_clients)")
        }
        if "data_revision" not in columns:
            connection.execute(
                """
                ALTER TABLE new_clients
                ADD COLUMN data_revision INTEGER NOT NULL DEFAULT 0
                CHECK (data_revision >= 0)
                """
            )
        if "reported_revision" not in columns:
            connection.execute(
                """
                ALTER TABLE new_clients
                ADD COLUMN reported_revision INTEGER
                CHECK (
                    reported_revision IS NULL OR reported_revision >= 0
                )
                """
            )
        connection.execute(
            """
            UPDATE new_clients
            SET reported_revision = data_revision
            WHERE reported_at IS NOT NULL AND reported_revision IS NULL
            """
        )

    @staticmethod
    def _require_telegram_claim(
        connection: sqlite3.Connection,
        spp_id: int,
        claim_token: str | None,
    ) -> None:
        stored_claim = connection.execute(
            "SELECT telegram_claim_token FROM new_clients WHERE spp_id = ?",
            (spp_id,),
        ).fetchone()[0]
        if claim_token is None:
            if stored_claim is not None:
                raise TelegramClaimError(
                    f"Клиент СБИС {spp_id} уже закреплён за другим worker-ом"
                )
            return
        if stored_claim != claim_token:
            raise TelegramClaimError(
                f"Telegram claim клиента СБИС {spp_id} не совпадает"
            )

    @staticmethod
    def _validate_processing_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit должен быть положительным целым числом")

    @staticmethod
    def _stored_status(
        status: ProcessingStatus | str,
    ) -> tuple[ProcessingStatus, str, int]:
        try:
            clean_status = ProcessingStatus(status)
        except ValueError as error:
            raise ValueError(f"Неизвестный статус обработки: {status}") from error
        if clean_status is ProcessingStatus.NEEDS_REVIEW:
            return clean_status, ProcessingStatus.RETRY_REQUIRED.value, 1
        return clean_status, clean_status.value, 0

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

    @classmethod
    def _nested_contacts(
        cls,
        rows: list[Any],
        field_name: str,
    ) -> tuple[str, ...]:
        result: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get(field_name)
            if value is None:
                continue
            result.extend(cls._contacts(value))
        return tuple(dict.fromkeys(result))

    @staticmethod
    def _row_optional(value: Any) -> str | None:
        return str(value) if value is not None else None
