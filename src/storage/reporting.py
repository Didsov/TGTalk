"""SQLite-хранилище доступа, отчетов и доставок отчетного бота."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PERMANENT_DELIVERY_ERROR_CODES = frozenset(
    {"access_revoked", "retry_exhausted", "telegram_forbidden"}
)
TELEGRAM_REPORT_PART_MAX_LENGTH = 4096
MAX_RENDERED_REPORT_PARTS = 1000
MAX_ATTEMPTS = 5
DELIVERY_RETRY_BASE_SECONDS = 30
DELIVERY_RETRY_MAX_SECONDS = 3600
DELIVERY_RETRY_JITTER_SECONDS = 10


class BootstrapAdminRemovalError(ValueError):
    """Попытка удалить администратора, заданного конфигурацией приложения."""


class ReportDeliveryClaimError(RuntimeError):
    """Доставка уже принадлежит другому процессу или больше не ожидается."""


@dataclass(frozen=True)
class ReportRun:
    """Неизменяемый запуск формирования отчета."""

    id: int
    kind: str
    cohort_date: str
    revision: int
    created_at: str


@dataclass(frozen=True)
class ReportItemDraft:
    """Снимок одной организации перед сохранением в отчет."""

    client_spp_id: int
    company_name: str
    director_name: str | None
    status: str
    registration_date: str | None = None
    sbis_phones: tuple[str, ...] = ()
    sbis_emails: tuple[str, ...] = ()
    personalised_phones: tuple[str, ...] = ()
    personalised_emails: tuple[str, ...] = ()
    telegram_phones: tuple[str, ...] = ()
    telegram_emails: tuple[str, ...] = ()
    result_code: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ReportItem(ReportItemDraft):
    """Сохраненный снимок организации в составе отчета."""

    id: int = 0
    report_id: int = 0
    position: int = 0


@dataclass(frozen=True)
class ReportDelivery:
    """Состояние доставки конкретного отчета конкретному получателю."""

    report_id: int
    user_id: int
    status: str
    attempts: int
    claim_token: str | None
    claimed_at: str | None
    sent_at: str | None
    failed_at: str | None
    telegram_message_id: int | None
    error_code: str | None
    next_retry_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AdminAuditEntry:
    """Обезличенная запись административного действия."""

    id: int
    actor_fingerprint: str
    action: str
    target_fingerprint: str | None
    result: str
    reason_code: str | None
    created_at: str


@dataclass(frozen=True)
class NotificationState:
    """Сохраненное состояние дедупликации служебного уведомления."""

    key: str
    value: str
    updated_at: str


@dataclass(frozen=True)
class PipelineRun:
    """Обезличенный итог одного запуска ежедневного конвейера."""

    id: int
    target_date: str
    status: str
    started_at: str
    finished_at: str | None
    collected_cards: int
    processing_status_counts: dict[str, int]
    available_queries: int | None
    error_stage: str | None
    error_code: str | None


@dataclass(frozen=True)
class IntegrationHealth:
    """Последнее безопасное состояние одной внешней интеграции."""

    integration: str
    status: str
    checked_at: str
    last_ok_at: str | None
    error_code: str | None
    consecutive_failures: int


class ReportingStorage:
    """Управляет доступом к боту, снимками отчетов и их доставкой."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        """Создать каталог, таблицы и индексы отчетной подсистемы."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS report_whitelist_users (
                    user_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS report_whitelist_admins (
                    user_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS report_subscriptions (
                    user_id INTEGER PRIMARY KEY,
                    subscribed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS report_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    cohort_date TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (kind, cohort_date, revision),
                    CHECK (revision > 0)
                );

                CREATE TABLE IF NOT EXISTS report_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    client_spp_id INTEGER NOT NULL,
                    company_name TEXT NOT NULL,
                    director_name TEXT,
                    status TEXT NOT NULL,
                    registration_date TEXT,
                    sbis_phones_json TEXT NOT NULL,
                    sbis_emails_json TEXT NOT NULL,
                    personalised_phones_json TEXT NOT NULL,
                    personalised_emails_json TEXT NOT NULL,
                    telegram_phones_json TEXT NOT NULL,
                    telegram_emails_json TEXT NOT NULL,
                    result_code TEXT,
                    error_code TEXT,
                    UNIQUE (report_id, client_spp_id),
                    UNIQUE (report_id, position),
                    FOREIGN KEY (report_id) REFERENCES report_runs(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS report_rendered_parts (
                    report_id INTEGER NOT NULL,
                    part_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (report_id, part_index),
                    CHECK (part_index >= 0),
                    CHECK (length(content) BETWEEN 1 AND 4096),
                    FOREIGN KEY (report_id) REFERENCES report_runs(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS report_deliveries (
                    report_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    claimed_at TEXT,
                    sent_at TEXT,
                    failed_at TEXT,
                    telegram_message_id INTEGER,
                    error_code TEXT,
                    next_retry_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (report_id, user_id),
                    UNIQUE (report_id, user_id),
                    CHECK (status IN ('pending', 'sent', 'failed')),
                    CHECK (attempts >= 0),
                    FOREIGN KEY (report_id) REFERENCES report_runs(id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_report_deliveries_pending
                    ON report_deliveries (report_id, status, claimed_at);

                CREATE TABLE IF NOT EXISTS report_delivery_parts (
                    report_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    part_index INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    message_id INTEGER,
                    sent_at TEXT,
                    PRIMARY KEY (report_id, user_id, part_index),
                    CHECK (part_index >= 0),
                    CHECK (status IN ('pending', 'sent')),
                    FOREIGN KEY (report_id, user_id)
                        REFERENCES report_deliveries(report_id, user_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS report_admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_fingerprint TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_fingerprint TEXT,
                    result TEXT NOT NULL,
                    reason_code TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_report_admin_audit_created
                    ON report_admin_audit (created_at, id);

                CREATE TABLE IF NOT EXISTS report_notification_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    collected_cards INTEGER NOT NULL DEFAULT 0,
                    processing_status_counts_json TEXT NOT NULL DEFAULT '{}',
                    available_queries INTEGER,
                    error_stage TEXT,
                    error_code TEXT,
                    CHECK (status IN ('running', 'completed', 'failed')),
                    CHECK (collected_cards >= 0),
                    CHECK (
                        available_queries IS NULL OR available_queries >= 0
                    )
                );

                CREATE TABLE IF NOT EXISTS integration_health (
                    integration TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    last_ok_at TEXT,
                    error_code TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    CHECK (status IN (
                        'healthy', 'unauthorized', 'rate_limited',
                        'unreachable', 'degraded', 'unknown'
                    )),
                    CHECK (consecutive_failures >= 0)
                );
                """
            )
            self._ensure_report_item_columns(connection)
            self._ensure_report_delivery_columns(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_deliveries_retry
                ON report_deliveries (
                    status, next_retry_at, attempts, report_id
                )
                """
            )

    def record_integration_health(
        self,
        integration: str,
        status: str,
        *,
        error_code: str | None = None,
    ) -> IntegrationHealth:
        """Сохранить результат проверки, не записывая секреты и ответы сервисов."""
        clean_integration = integration.strip().casefold()
        if not clean_integration or len(clean_integration) > 50:
            raise ValueError("Некорректное имя интеграции")
        allowed_statuses = {
            "healthy",
            "unauthorized",
            "rate_limited",
            "unreachable",
            "degraded",
            "unknown",
        }
        if status not in allowed_statuses:
            raise ValueError("Некорректный статус интеграции")
        clean_error = None if error_code is None else error_code.strip()[:100]
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            previous = connection.execute(
                "SELECT * FROM integration_health WHERE integration = ?",
                (clean_integration,),
            ).fetchone()
            failures = (
                0
                if status == "healthy"
                else (0 if previous is None else int(previous["consecutive_failures"]))
                + 1
            )
            last_ok_at = (
                now
                if status == "healthy"
                else None if previous is None else previous["last_ok_at"]
            )
            connection.execute(
                """
                INSERT INTO integration_health (
                    integration, status, checked_at, last_ok_at,
                    error_code, consecutive_failures
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(integration) DO UPDATE SET
                    status = excluded.status,
                    checked_at = excluded.checked_at,
                    last_ok_at = excluded.last_ok_at,
                    error_code = excluded.error_code,
                    consecutive_failures = excluded.consecutive_failures
                """,
                (
                    clean_integration,
                    status,
                    now,
                    last_ok_at,
                    clean_error,
                    failures,
                ),
            )
            row = connection.execute(
                "SELECT * FROM integration_health WHERE integration = ?",
                (clean_integration,),
            ).fetchone()
            assert row is not None
            return self._to_integration_health(row)

    def get_integration_health(self, integration: str) -> IntegrationHealth | None:
        """Получить последнее состояние одной интеграции."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM integration_health WHERE integration = ?",
                (integration.strip().casefold(),),
            ).fetchone()
            return None if row is None else self._to_integration_health(row)

    def list_integration_health(self) -> tuple[IntegrationHealth, ...]:
        """Получить состояния всех проверенных интеграций."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM integration_health ORDER BY integration"
            ).fetchall()
            return tuple(self._to_integration_health(row) for row in rows)

    def is_user_allowed(
        self,
        user_id: int,
        *,
        bootstrap_admin_ids: Iterable[int] = (),
    ) -> bool:
        """Проверить наличие пользователя хотя бы в одном белом списке."""
        clean_user_id = self._user_id(user_id)
        if clean_user_id in self._user_ids(bootstrap_admin_ids):
            return True
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM report_whitelist_users WHERE user_id = ?
                UNION ALL
                SELECT 1 FROM report_whitelist_admins WHERE user_id = ?
                LIMIT 1
                """,
                (clean_user_id, clean_user_id),
            ).fetchone()
            return row is not None

    def is_admin(
        self,
        user_id: int,
        *,
        bootstrap_admin_ids: Iterable[int] = (),
    ) -> bool:
        """Проверить сохраненные и конфигурационные права администратора."""
        clean_user_id = self._user_id(user_id)
        if clean_user_id in self._user_ids(bootstrap_admin_ids):
            return True
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM report_whitelist_admins WHERE user_id = ?",
                (clean_user_id,),
            ).fetchone()
            return row is not None

    def add_user(
        self,
        user_id: int,
        *,
        actor_user_id: int | None = None,
    ) -> bool:
        """Идемпотентно добавить обычного пользователя; вернуть факт изменения."""
        return self._add_access_entry(
            "report_whitelist_users",
            self._user_id(user_id),
            actor_user_id=actor_user_id,
            action="user.add",
        )

    def remove_user(
        self,
        user_id: int,
        *,
        actor_user_id: int | None = None,
        bootstrap_admin_ids: Iterable[int] = (),
    ) -> bool:
        """Удалить обычного пользователя, сохранив доступ при роли администратора."""
        return self._remove_access_entry(
            "report_whitelist_users",
            self._user_id(user_id),
            actor_user_id=actor_user_id,
            action="user.remove",
            bootstrap_admin_ids=bootstrap_admin_ids,
        )

    def add_admin(
        self,
        user_id: int,
        *,
        actor_user_id: int | None = None,
        bootstrap_admin_ids: Iterable[int] = (),
    ) -> bool:
        """Идемпотентно добавить администратора в изменяемый белый список."""
        clean_user_id = self._user_id(user_id)
        if clean_user_id in self._user_ids(bootstrap_admin_ids):
            return False
        return self._add_access_entry(
            "report_whitelist_admins",
            clean_user_id,
            actor_user_id=actor_user_id,
            action="admin.add",
        )

    def remove_admin(
        self,
        user_id: int,
        *,
        actor_user_id: int | None = None,
        bootstrap_admin_ids: Iterable[int] = (),
    ) -> bool:
        """Удалить изменяемого администратора, но не bootstrap-администратора."""
        clean_user_id = self._user_id(user_id)
        bootstrap_ids = self._user_ids(bootstrap_admin_ids)
        if clean_user_id in bootstrap_ids:
            if actor_user_id is not None:
                self.record_admin_audit(
                    actor_user_id=actor_user_id,
                    action="admin.remove",
                    target_user_id=clean_user_id,
                    result="denied",
                    reason_code="bootstrap_admin",
                )
            raise BootstrapAdminRemovalError(
                "Bootstrap-администратор задается конфигурацией и не удаляется"
            )
        return self._remove_access_entry(
            "report_whitelist_admins",
            clean_user_id,
            actor_user_id=actor_user_id,
            action="admin.remove",
            bootstrap_admin_ids=bootstrap_ids,
        )

    def list_admins(
        self, *, bootstrap_admin_ids: Iterable[int] = ()
    ) -> tuple[int, ...]:
        """Вернуть объединенный отсортированный список администраторов."""
        result = set(self._user_ids(bootstrap_admin_ids))
        with self._connect() as connection:
            result.update(
                int(row["user_id"])
                for row in connection.execute(
                    "SELECT user_id FROM report_whitelist_admins"
                ).fetchall()
            )
        return tuple(sorted(result))

    def list_users(self) -> tuple[int, ...]:
        """Вернуть обычный белый список без списка администраторов."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT user_id FROM report_whitelist_users ORDER BY user_id"
            ).fetchall()
            return tuple(int(row["user_id"]) for row in rows)

    def subscribe(
        self,
        user_id: int,
        *,
        bootstrap_admin_ids: Iterable[int] = (),
    ) -> bool:
        """Идемпотентно подписать разрешенного пользователя на отчеты."""
        clean_user_id = self._user_id(user_id)
        bootstrap_ids = self._user_ids(bootstrap_admin_ids)
        now = self._utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._is_allowed_in_connection(
                connection, clean_user_id, bootstrap_ids
            ):
                raise PermissionError("Пользователь отсутствует в белом списке")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO report_subscriptions
                    (user_id, subscribed_at)
                VALUES (?, ?)
                """,
                (clean_user_id, now),
            )
            return cursor.rowcount == 1

    def unsubscribe(self, user_id: int) -> bool:
        """Идемпотентно отменить подписку пользователя."""
        clean_user_id = self._user_id(user_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM report_subscriptions WHERE user_id = ?",
                (clean_user_id,),
            )
            return cursor.rowcount == 1

    def is_subscribed(self, user_id: int) -> bool:
        """Проверить наличие активной записи подписки пользователя."""
        clean_user_id = self._user_id(user_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM report_subscriptions WHERE user_id = ?",
                (clean_user_id,),
            ).fetchone()
            return row is not None

    def list_subscribers(
        self, *, bootstrap_admin_ids: Iterable[int] = ()
    ) -> tuple[int, ...]:
        """Вернуть активных подписчиков, которые все еще имеют доступ."""
        bootstrap_ids = self._user_ids(bootstrap_admin_ids)
        with self._connect() as connection:
            allowed = set(bootstrap_ids)
            allowed.update(
                int(row["user_id"])
                for row in connection.execute(
                    """
                    SELECT user_id FROM report_whitelist_users
                    UNION
                    SELECT user_id FROM report_whitelist_admins
                    """
                ).fetchall()
            )
            subscribers = {
                int(row["user_id"])
                for row in connection.execute(
                    "SELECT user_id FROM report_subscriptions"
                ).fetchall()
            }
        return tuple(sorted(allowed & subscribers))

    def get_or_create_report_run(
        self,
        *,
        kind: str,
        cohort_date: date | str,
        revision: int = 1,
        items: Iterable[ReportItemDraft | Mapping[str, Any]] = (),
        delivery_user_ids: Iterable[int] | None = None,
    ) -> tuple[ReportRun, bool]:
        """Атомарно получить запуск и сообщить, был ли он создан caller-ом."""
        clean_kind = self._required_text(kind, "Вид отчета", maximum=80)
        clean_date = self._date_text(cohort_date)
        clean_revision = self._positive_int(revision, "Ревизия")
        clean_items = tuple(self._report_item(item) for item in items)
        if len({item.client_spp_id for item in clean_items}) != len(clean_items):
            raise ValueError("Один клиент указан в отчете несколько раз")
        clean_delivery_user_ids = (
            None
            if delivery_user_ids is None
            else tuple(sorted(self._user_ids(delivery_user_ids)))
        )
        now = self._utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            insert_cursor = connection.execute(
                """
                INSERT OR IGNORE INTO report_runs
                    (kind, cohort_date, revision, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (clean_kind, clean_date, clean_revision, now),
            )
            row = connection.execute(
                """
                SELECT * FROM report_runs
                WHERE kind = ? AND cohort_date = ? AND revision = ?
                """,
                (clean_kind, clean_date, clean_revision),
            ).fetchone()
            if row is None:  # pragma: no cover - защита от поврежденной БД
                raise RuntimeError("Созданный запуск отчета не найден")
            report_id = int(row["id"])
            if insert_cursor.rowcount == 0:
                return self._to_report_run(row), False
            self._insert_report_items(connection, report_id, clean_items)
            if clean_delivery_user_ids is not None:
                self._insert_report_deliveries(
                    connection,
                    report_id,
                    clean_delivery_user_ids,
                    created_at=now,
                )
            return self._to_report_run(row), True

    def create_report_run(
        self,
        *,
        kind: str,
        cohort_date: date | str,
        revision: int = 1,
        items: Iterable[ReportItemDraft | Mapping[str, Any]] = (),
        delivery_user_ids: Iterable[int] | None = None,
    ) -> ReportRun:
        """Совместимо вернуть идемпотентно созданный или существующий запуск."""
        report_run, _ = self.get_or_create_report_run(
            kind=kind,
            cohort_date=cohort_date,
            revision=revision,
            items=items,
            delivery_user_ids=delivery_user_ids,
        )
        return report_run

    def create_next_report_run(
        self,
        *,
        kind: str,
        cohort_date: date | str,
        items: Iterable[ReportItemDraft | Mapping[str, Any]] = (),
        delivery_user_ids: Iterable[int] | None = None,
    ) -> ReportRun:
        """Атомарно выделить следующую ревизию и сохранить ее снимок."""
        clean_kind = self._required_text(kind, "Вид отчета", maximum=80)
        clean_date = self._date_text(cohort_date)
        clean_items = tuple(self._report_item(item) for item in items)
        if len({item.client_spp_id for item in clean_items}) != len(clean_items):
            raise ValueError("Один клиент указан в отчете несколько раз")
        clean_delivery_user_ids = (
            None
            if delivery_user_ids is None
            else tuple(sorted(self._user_ids(delivery_user_ids)))
        )
        now = self._utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision_row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision
                FROM report_runs WHERE kind = ? AND cohort_date = ?
                """,
                (clean_kind, clean_date),
            ).fetchone()
            revision = int(revision_row["next_revision"])
            cursor = connection.execute(
                """
                INSERT INTO report_runs (kind, cohort_date, revision, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (clean_kind, clean_date, revision, now),
            )
            report_id = int(cursor.lastrowid)
            self._insert_report_items(connection, report_id, clean_items)
            if clean_delivery_user_ids is not None:
                self._insert_report_deliveries(
                    connection,
                    report_id,
                    clean_delivery_user_ids,
                    created_at=now,
                )
            row = connection.execute(
                "SELECT * FROM report_runs WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("Созданная ревизия отчета не найдена")
            return self._to_report_run(row)

    def create_next_report_run_for_client_revisions(
        self,
        *,
        kind: str,
        cohort_date: date | str,
        items: Iterable[ReportItemDraft | Mapping[str, Any]],
        client_revisions: Mapping[int, tuple[int, int | None]],
        delivery_user_ids: Iterable[int] | None = None,
    ) -> ReportRun | None:
        """Атомарно создать следующую ревизию только для выигравших CAS строк."""
        clean_kind = self._required_text(kind, "Вид отчета", maximum=80)
        clean_date = self._date_text(cohort_date)
        clean_items = tuple(self._report_item(item) for item in items)
        item_ids = tuple(item.client_spp_id for item in clean_items)
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("Один клиент указан в отчете несколько раз")
        clean_revisions = self._client_revisions(client_revisions)
        if set(item_ids) != set(clean_revisions):
            raise ValueError(
                "Ключи client_revisions должны точно совпадать со строками отчета"
            )
        clean_delivery_user_ids = (
            None
            if delivery_user_ids is None
            else tuple(sorted(self._user_ids(delivery_user_ids)))
        )
        if not clean_items:
            return None
        now = self._utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ", ".join("?" for _ in item_ids)
            rows = connection.execute(
                f"""
                SELECT spp_id, data_revision, reported_revision
                FROM new_clients
                WHERE spp_id IN ({placeholders})
                """,
                item_ids,
            ).fetchall()
            current_revisions = {
                int(row["spp_id"]): (
                    int(row["data_revision"]),
                    None
                    if row["reported_revision"] is None
                    else int(row["reported_revision"]),
                )
                for row in rows
            }
            winning_items = tuple(
                item
                for item in clean_items
                if current_revisions.get(item.client_spp_id)
                == clean_revisions[item.client_spp_id]
            )
            if not winning_items:
                return None

            revision_row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision
                FROM report_runs WHERE kind = ? AND cohort_date = ?
                """,
                (clean_kind, clean_date),
            ).fetchone()
            revision = int(revision_row["next_revision"])
            cursor = connection.execute(
                """
                INSERT INTO report_runs (kind, cohort_date, revision, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (clean_kind, clean_date, revision, now),
            )
            report_id = int(cursor.lastrowid)
            self._insert_report_items(
                connection, report_id, winning_items
            )
            if clean_delivery_user_ids is not None:
                self._insert_report_deliveries(
                    connection,
                    report_id,
                    clean_delivery_user_ids,
                    created_at=now,
                )

            for item in winning_items:
                expected_data, expected_reported = clean_revisions[
                    item.client_spp_id
                ]
                update = connection.execute(
                    """
                    UPDATE new_clients
                    SET report_id = ?, reported_at = ?,
                        reported_revision = data_revision
                    WHERE spp_id = ? AND data_revision = ?
                        AND reported_revision IS ?
                    """,
                    (
                        str(report_id),
                        now,
                        item.client_spp_id,
                        expected_data,
                        expected_reported,
                    ),
                )
                if update.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE
                    raise RuntimeError("CAS ревизии клиента неожиданно проигран")

            row = connection.execute(
                "SELECT * FROM report_runs WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("Созданный CAS-отчет не найден")
            return self._to_report_run(row)

    def get_report_run(self, report_id: int) -> ReportRun | None:
        """Получить запуск по внутреннему идентификатору."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM report_runs WHERE id = ?", (clean_report_id,)
            ).fetchone()
            return None if row is None else self._to_report_run(row)

    def find_report_run(
        self,
        *,
        kind: str,
        cohort_date: date | str,
        revision: int = 1,
    ) -> ReportRun | None:
        """Найти запуск по его идемпотентному бизнес-ключу."""
        clean_kind = self._required_text(kind, "Вид отчета", maximum=80)
        clean_date = self._date_text(cohort_date)
        clean_revision = self._positive_int(revision, "Ревизия")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM report_runs
                WHERE kind = ? AND cohort_date = ? AND revision = ?
                """,
                (clean_kind, clean_date, clean_revision),
            ).fetchone()
            return None if row is None else self._to_report_run(row)

    def latest_report_run(self, kind: str | None = None) -> ReportRun | None:
        """Получить последний созданный запуск, при необходимости одного вида."""
        parameters: tuple[Any, ...] = ()
        where = ""
        if kind is not None:
            clean_kind = self._required_text(kind, "Вид отчета", maximum=80)
            where = "WHERE kind = ?"
            parameters = (clean_kind,)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM report_runs
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            return None if row is None else self._to_report_run(row)

    def latest_deliverable_report_run(self) -> ReportRun | None:
        """Получить последний запуск, для которого создавались доставки."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT runs.* FROM report_runs AS runs
                WHERE EXISTS (
                    SELECT 1 FROM report_deliveries AS deliveries
                    WHERE deliveries.report_id = runs.id
                )
                ORDER BY runs.created_at DESC, runs.id DESC
                LIMIT 1
                """
            ).fetchone()
            return None if row is None else self._to_report_run(row)

    def list_report_runs_with_failed_deliveries(
        self, *, limit: int = 100
    ) -> list[ReportRun]:
        """Вернуть запуски с неудачными доставками без дублирования строк."""
        clean_limit = self._positive_int(limit, "Лимит")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT runs.*
                FROM report_runs AS runs
                INNER JOIN report_deliveries AS deliveries
                    ON deliveries.report_id = runs.id
                WHERE deliveries.status = 'failed'
                ORDER BY runs.id
                LIMIT ?
                """,
                (clean_limit,),
            ).fetchall()
            return [self._to_report_run(row) for row in rows]

    def list_report_runs_with_open_deliveries(
        self, *, limit: int = 100
    ) -> list[ReportRun]:
        """Вернуть запуски с ожидающими или повторяемыми доставками.

        Уже закрепленные ``pending``-доставки тоже считаются открытыми:
        срок устаревания их claim проверяется непосредственно при новом claim.
        """
        clean_limit = self._positive_int(limit, "Лимит")
        now = self._utc_now()
        permanent_codes = tuple(sorted(PERMANENT_DELIVERY_ERROR_CODES))
        placeholders = ", ".join("?" for _ in permanent_codes)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT runs.*
                FROM report_runs AS runs
                INNER JOIN report_deliveries AS deliveries
                    ON deliveries.report_id = runs.id
                WHERE deliveries.status = 'pending'
                    OR (
                        deliveries.status = 'failed'
                        AND deliveries.attempts < ?
                        AND (
                            deliveries.next_retry_at IS NULL
                            OR deliveries.next_retry_at <= ?
                        )
                        AND (
                            deliveries.error_code IS NULL
                            OR deliveries.error_code NOT IN ({placeholders})
                        )
                    )
                ORDER BY runs.id
                LIMIT ?
                """,
                (
                    MAX_ATTEMPTS,
                    now,
                    *permanent_codes,
                    clean_limit,
                ),
            ).fetchall()
            return [self._to_report_run(row) for row in rows]

    def list_report_items(self, report_id: int) -> list[ReportItem]:
        """Получить строки отчета в зафиксированном порядке."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM report_items
                WHERE report_id = ? ORDER BY position, id
                """,
                (clean_report_id,),
            ).fetchall()
            return [self._to_report_item(row) for row in rows]

    def latest_report_item_for_client(
        self,
        client_spp_id: int,
        *,
        kinds: Iterable[str],
    ) -> ReportItem | None:
        """Найти последний снимок клиента только в разрешенных видах отчетов."""
        clean_client_spp_id = self._positive_int(
            client_spp_id, "Идентификатор СПП"
        )
        if isinstance(kinds, (str, bytes)):
            raise TypeError("Виды отчетов должны быть iterable строк")
        try:
            source_kinds = tuple(kinds)
        except TypeError as error:
            raise TypeError("Виды отчетов должны быть iterable строк") from error
        clean_kinds: list[str] = []
        seen: set[str] = set()
        for kind in source_kinds:
            if not isinstance(kind, str):
                raise TypeError("Каждый вид отчета должен быть строкой")
            clean_kind = self._required_text(
                kind, "Вид отчета", maximum=80
            )
            if clean_kind not in seen:
                clean_kinds.append(clean_kind)
                seen.add(clean_kind)
        if not clean_kinds:
            raise ValueError("Нужно указать хотя бы один вид отчета")
        placeholders = ", ".join("?" for _ in clean_kinds)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT items.*
                FROM report_items AS items
                INNER JOIN report_runs AS runs ON runs.id = items.report_id
                WHERE items.client_spp_id = ?
                    AND runs.kind IN ({placeholders})
                ORDER BY runs.id DESC
                LIMIT 1
                """,
                (clean_client_spp_id, *clean_kinds),
            ).fetchone()
            return None if row is None else self._to_report_item(row)

    def ensure_report_rendered_parts(
        self, report_id: int, parts: Iterable[str]
    ) -> tuple[str, ...]:
        """Один раз атомарно сохранить и далее возвращать точный рендер отчета."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_parts = self._rendered_parts(parts)
        now = self._utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_report(connection, clean_report_id)
            rows = connection.execute(
                """
                SELECT part_index, content FROM report_rendered_parts
                WHERE report_id = ? ORDER BY part_index
                """,
                (clean_report_id,),
            ).fetchall()
            if rows:
                indexes = tuple(int(row["part_index"]) for row in rows)
                if indexes != tuple(range(len(rows))):
                    raise ValueError("Сохраненные части отчета имеют разрыв индексов")
                return tuple(str(row["content"]) for row in rows)
            connection.executemany(
                """
                INSERT INTO report_rendered_parts (
                    report_id, part_index, content, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (clean_report_id, part_index, content, now)
                    for part_index, content in enumerate(clean_parts)
                ),
            )
            return clean_parts

    def ensure_report_deliveries(
        self, report_id: int, user_ids: Iterable[int]
    ) -> list[ReportDelivery]:
        """Идемпотентно создать ожидающие доставки для снимка получателей."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_user_ids = self._user_ids(user_ids)
        now = self._utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_report(connection, clean_report_id)
            self._insert_report_deliveries(
                connection,
                clean_report_id,
                clean_user_ids,
                created_at=now,
                ignore_existing=True,
            )
            return self._deliveries_for_users(
                connection, clean_report_id, clean_user_ids
            )

    def list_pending_deliveries(
        self, report_id: int, *, limit: int = 100
    ) -> list[ReportDelivery]:
        """Прочитать ожидающие доставки без присвоения worker-у."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_limit = self._positive_int(limit, "Лимит")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM report_deliveries
                WHERE report_id = ? AND status = 'pending'
                ORDER BY created_at, user_id LIMIT ?
                """,
                (clean_report_id, clean_limit),
            ).fetchall()
            return [self._to_delivery(row) for row in rows]

    def ensure_delivery_parts(
        self,
        report_id: int,
        user_id: int,
        part_count: int,
    ) -> None:
        """Зафиксировать количество частей доставки без права его изменения."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_user_id = self._user_id(user_id)
        clean_part_count = self._positive_int(part_count, "Количество частей")
        expected_indexes = tuple(range(clean_part_count))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_delivery(
                connection, clean_report_id, clean_user_id
            )
            rows = connection.execute(
                """
                SELECT part_index FROM report_delivery_parts
                WHERE report_id = ? AND user_id = ?
                ORDER BY part_index
                """,
                (clean_report_id, clean_user_id),
            ).fetchall()
            existing_indexes = tuple(int(row["part_index"]) for row in rows)
            if existing_indexes:
                if existing_indexes != expected_indexes:
                    raise ValueError(
                        "Количество частей доставки уже зафиксировано"
                    )
                return
            connection.executemany(
                """
                INSERT INTO report_delivery_parts (
                    report_id, user_id, part_index, status
                ) VALUES (?, ?, ?, 'pending')
                """,
                (
                    (clean_report_id, clean_user_id, part_index)
                    for part_index in expected_indexes
                ),
            )

    def sent_delivery_part_indexes(
        self, report_id: int, user_id: int
    ) -> set[int]:
        """Вернуть индексы уже отправленных частей для безопасного resume."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_user_id = self._user_id(user_id)
        with self._connect() as connection:
            self._require_delivery(
                connection, clean_report_id, clean_user_id
            )
            rows = connection.execute(
                """
                SELECT part_index FROM report_delivery_parts
                WHERE report_id = ? AND user_id = ? AND status = 'sent'
                """,
                (clean_report_id, clean_user_id),
            ).fetchall()
            return {int(row["part_index"]) for row in rows}

    def mark_delivery_part_sent(
        self,
        report_id: int,
        user_id: int,
        part_index: int,
        message_id: int | None = None,
    ) -> bool:
        """Идемпотентно отметить одну существующую часть отправленной."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_user_id = self._user_id(user_id)
        clean_part_index = self._nonnegative_int(part_index, "Индекс части")
        clean_message_id = (
            None
            if message_id is None
            else self._positive_int(message_id, "ID сообщения")
        )
        now = self._utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status FROM report_delivery_parts
                WHERE report_id = ? AND user_id = ? AND part_index = ?
                """,
                (clean_report_id, clean_user_id, clean_part_index),
            ).fetchone()
            if row is None:
                raise KeyError(
                    "Часть доставки не создана через ensure_delivery_parts"
                )
            if row["status"] == "sent":
                return False
            connection.execute(
                """
                UPDATE report_delivery_parts
                SET status = 'sent', message_id = ?, sent_at = ?
                WHERE report_id = ? AND user_id = ? AND part_index = ?
                """,
                (
                    clean_message_id,
                    now,
                    clean_report_id,
                    clean_user_id,
                    clean_part_index,
                ),
            )
            return True

    def delivery_status_counts(self, report_id: int) -> dict[str, int]:
        """Посчитать доставки отчета по всем стабильным статусам."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        result = {"pending": 0, "sent": 0, "failed": 0}
        with self._connect() as connection:
            self._require_report(connection, clean_report_id)
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM report_deliveries
                WHERE report_id = ?
                GROUP BY status
                """,
                (clean_report_id,),
            ).fetchall()
            for row in rows:
                result[str(row["status"])] = int(row["count"])
        return result

    def claim_pending_deliveries(
        self,
        report_id: int,
        *,
        claim_token: str,
        limit: int = 100,
        stale_after_seconds: int = 900,
    ) -> list[ReportDelivery]:
        """Атомарно закрепить доступные доставки за одним worker-ом."""
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_token = self._required_text(
            claim_token, "Токен обработки", maximum=200
        )
        clean_limit = self._positive_int(limit, "Лимит")
        if isinstance(stale_after_seconds, bool) or stale_after_seconds < 0:
            raise ValueError("Срок устаревания должен быть неотрицательным")
        now = self._utc_now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        ).isoformat(timespec="seconds")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_report(connection, clean_report_id)
            connection.execute(
                """
                UPDATE report_deliveries
                SET status = 'failed', claim_token = NULL, claimed_at = NULL,
                    failed_at = ?, error_code = 'retry_exhausted',
                    next_retry_at = NULL, updated_at = ?
                WHERE report_id = ? AND status = 'pending'
                    AND attempts >= ?
                    AND (
                        claim_token IS NULL OR claimed_at IS NULL
                        OR claimed_at <= ?
                    )
                """,
                (
                    now,
                    now,
                    clean_report_id,
                    MAX_ATTEMPTS,
                    stale_before,
                ),
            )
            rows = connection.execute(
                """
                SELECT user_id FROM report_deliveries
                WHERE report_id = ? AND status = 'pending'
                    AND attempts < ?
                    AND (
                        claim_token IS NULL OR claimed_at IS NULL
                        OR claimed_at <= ?
                    )
                ORDER BY created_at, user_id LIMIT ?
                """,
                (
                    clean_report_id,
                    MAX_ATTEMPTS,
                    stale_before,
                    clean_limit,
                ),
            ).fetchall()
            user_ids = tuple(int(row["user_id"]) for row in rows)
            if not user_ids:
                return []
            placeholders = ", ".join("?" for _ in user_ids)
            connection.execute(
                f"""
                UPDATE report_deliveries
                SET claim_token = ?, claimed_at = ?, attempts = attempts + 1,
                    updated_at = ?
                WHERE report_id = ? AND user_id IN ({placeholders})
                """,
                (clean_token, now, now, clean_report_id, *user_ids),
            )
            return self._deliveries_for_users(
                connection, clean_report_id, user_ids
            )

    def mark_delivery_sent(
        self,
        report_id: int,
        user_id: int,
        *,
        claim_token: str,
        telegram_message_id: int | None = None,
    ) -> ReportDelivery:
        """Атомарно отметить закрепленную доставку успешно отправленной."""
        return self._finish_delivery(
            report_id,
            user_id,
            claim_token=claim_token,
            status="sent",
            telegram_message_id=telegram_message_id,
            error_code=None,
        )

    def mark_delivery_failed(
        self,
        report_id: int,
        user_id: int,
        *,
        claim_token: str,
        error_code: str,
    ) -> ReportDelivery:
        """Атомарно отметить закрепленную доставку неуспешной."""
        return self._finish_delivery(
            report_id,
            user_id,
            claim_token=claim_token,
            status="failed",
            telegram_message_id=None,
            error_code=self._required_text(
                error_code, "Код ошибки", maximum=100
            ),
        )

    def retry_failed_deliveries(
        self,
        report_id: int,
        *,
        user_ids: Iterable[int] | None = None,
        retryable_only: bool = False,
    ) -> int:
        """Вернуть выбранные неудачные доставки в ожидающее состояние.

        При ``retryable_only=True`` постоянные ошибки доступа остаются
        завершенными и не создают бесконечный цикл повторной отправки.
        """
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_user_ids = None if user_ids is None else self._user_ids(user_ids)
        if not isinstance(retryable_only, bool):
            raise TypeError("retryable_only должен быть логическим значением")
        now = self._utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_report(connection, clean_report_id)
            clauses: list[str] = []
            user_parameters: tuple[Any, ...] = ()
            if clean_user_ids is not None and not clean_user_ids:
                return 0
            if clean_user_ids is not None:
                placeholders = ", ".join("?" for _ in clean_user_ids)
                clauses.append(f"user_id IN ({placeholders})")
                user_parameters = tuple(clean_user_ids)
            retry_parameters: tuple[Any, ...] = ()
            if retryable_only:
                permanent_codes = tuple(sorted(PERMANENT_DELIVERY_ERROR_CODES))
                placeholders = ", ".join("?" for _ in permanent_codes)
                clauses.append(
                    f"(error_code IS NULL OR error_code NOT IN ({placeholders}))"
                )
                clauses.append("attempts < ?")
                clauses.append("(next_retry_at IS NULL OR next_retry_at <= ?)")
                retry_parameters = (*permanent_codes, MAX_ATTEMPTS, now)
            suffix = "" if not clauses else " AND " + " AND ".join(clauses)
            attempts_assignment = "attempts" if retryable_only else "0"
            cursor = connection.execute(
                f"""
                UPDATE report_deliveries
                SET status = 'pending', claim_token = NULL, claimed_at = NULL,
                    failed_at = NULL, error_code = NULL, next_retry_at = NULL,
                    attempts = {attempts_assignment}, updated_at = ?
                WHERE report_id = ? AND status = 'failed'{suffix}
                """,
                (
                    now,
                    clean_report_id,
                    *user_parameters,
                    *retry_parameters,
                ),
            )
            return cursor.rowcount

    def record_admin_audit(
        self,
        *,
        actor_user_id: int,
        action: str,
        target_user_id: int | None = None,
        result: str = "success",
        reason_code: str | None = None,
    ) -> AdminAuditEntry:
        """Записать действие без Telegram ID, контактов и содержимого отчета."""
        actor = self._user_id(actor_user_id)
        target = (
            None if target_user_id is None else self._user_id(target_user_id)
        )
        clean_action = self._required_text(action, "Действие", maximum=100)
        clean_result = self._required_text(result, "Результат", maximum=40)
        clean_reason = self._optional_text(
            reason_code, "Код причины", maximum=100
        )
        now = self._utc_now()
        with self._connect() as connection:
            row_id = self._insert_audit(
                connection,
                actor_user_id=actor,
                action=clean_action,
                target_user_id=target,
                result=clean_result,
                reason_code=clean_reason,
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM report_admin_audit WHERE id = ?", (row_id,)
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("Запись аудита не найдена")
            return self._to_audit_entry(row)

    def list_admin_audit(self, *, limit: int = 100) -> list[AdminAuditEntry]:
        """Получить последние обезличенные административные действия."""
        clean_limit = self._positive_int(limit, "Лимит")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM report_admin_audit
                ORDER BY id DESC LIMIT ?
                """,
                (clean_limit,),
            ).fetchall()
            return [self._to_audit_entry(row) for row in rows]

    def get_notification_state(self, key: str) -> NotificationState | None:
        """Получить состояние дедупликации уведомления по ключу."""
        clean_key = self._required_text(key, "Ключ уведомления", maximum=100)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM report_notification_state WHERE key = ?",
                (clean_key,),
            ).fetchone()
            return None if row is None else self._to_notification_state(row)

    def set_notification_state(self, key: str, value: str) -> NotificationState:
        """Идемпотентно сохранить текущее состояние уведомления."""
        clean_key = self._required_text(key, "Ключ уведомления", maximum=100)
        clean_value = self._required_text(
            value, "Состояние уведомления", maximum=4000
        )
        now = self._utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO report_notification_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (clean_key, clean_value, now),
            )
            row = connection.execute(
                "SELECT * FROM report_notification_state WHERE key = ?",
                (clean_key,),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("Состояние уведомления не найдено")
            return self._to_notification_state(row)

    def delete_report_runs_created_before(self, cutoff: datetime) -> int:
        """Удалить старые запуски отчетов вместе с зависимыми снимками."""
        if not isinstance(cutoff, datetime):
            raise TypeError("cutoff должен быть datetime")
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("cutoff должен содержать часовой пояс")
        clean_cutoff = cutoff.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM report_runs WHERE created_at < ?",
                (clean_cutoff,),
            )
            return cursor.rowcount

    def start_pipeline_run(self, target_date: date | str) -> PipelineRun:
        """Начать обезличенную запись выполнения ежедневного конвейера."""
        clean_target_date = self._date_text(target_date)
        now = self._utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pipeline_runs (
                    target_date, status, started_at,
                    collected_cards, processing_status_counts_json
                ) VALUES (?, 'running', ?, 0, '{}')
                """,
                (clean_target_date, now),
            )
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("Запуск конвейера не найден после создания")
            return self._to_pipeline_run(row)

    def finish_pipeline_run(
        self,
        pipeline_run_id: int,
        *,
        status: str,
        collected_cards: int,
        processing_status_counts: Mapping[str, int],
        available_queries: int | None = None,
        error_stage: str | None = None,
        error_code: str | None = None,
    ) -> PipelineRun:
        """Идемпотентно завершить запуск только агрегатами без ПД."""
        clean_run_id = self._positive_int(pipeline_run_id, "ID конвейера")
        if status not in {"completed", "failed"}:
            raise ValueError("Статус должен быть completed или failed")
        clean_collected_cards = self._nonnegative_int(
            collected_cards, "Количество карточек"
        )
        clean_counts = self._processing_status_counts(
            processing_status_counts
        )
        clean_available_queries = (
            None
            if available_queries is None
            else self._nonnegative_int(
                available_queries, "Количество доступных запросов"
            )
        )
        clean_error_stage = self._optional_operational_code(
            error_stage, "Этап ошибки"
        )
        clean_error_code = self._optional_operational_code(
            error_code, "Код ошибки"
        )
        now = self._utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM pipeline_runs WHERE id = ?", (clean_run_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"Запуск конвейера {clean_run_id} не найден")
            if current["status"] != "running":
                return self._to_pipeline_run(current)
            connection.execute(
                """
                UPDATE pipeline_runs
                SET status = ?, finished_at = ?, collected_cards = ?,
                    processing_status_counts_json = ?, available_queries = ?,
                    error_stage = ?, error_code = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    now,
                    clean_collected_cards,
                    json.dumps(clean_counts, ensure_ascii=True, sort_keys=True),
                    clean_available_queries,
                    clean_error_stage,
                    clean_error_code,
                    clean_run_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE id = ?", (clean_run_id,)
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("Завершенный запуск конвейера не найден")
            return self._to_pipeline_run(row)

    def latest_pipeline_run(self) -> PipelineRun | None:
        """Получить последнюю запись ежедневного конвейера."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._to_pipeline_run(row)

    def _add_access_entry(
        self,
        table: str,
        user_id: int,
        *,
        actor_user_id: int | None,
        action: str,
    ) -> bool:
        now = self._utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"INSERT OR IGNORE INTO {table} (user_id, created_at) VALUES (?, ?)",
                (user_id, now),
            )
            changed = cursor.rowcount == 1
            if actor_user_id is not None:
                self._insert_audit(
                    connection,
                    actor_user_id=self._user_id(actor_user_id),
                    action=action,
                    target_user_id=user_id,
                    result="changed" if changed else "unchanged",
                    reason_code=None,
                    created_at=now,
                )
            return changed

    def _insert_report_items(
        self,
        connection: sqlite3.Connection,
        report_id: int,
        items: Iterable[ReportItemDraft],
    ) -> None:
        connection.executemany(
            """
            INSERT INTO report_items (
                report_id, position, client_spp_id, company_name,
                director_name, status, registration_date,
                sbis_phones_json, sbis_emails_json,
                personalised_phones_json, personalised_emails_json,
                telegram_phones_json, telegram_emails_json,
                result_code, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    report_id,
                    position,
                    item.client_spp_id,
                    item.company_name,
                    item.director_name,
                    item.status,
                    item.registration_date,
                    self._contacts_json(item.sbis_phones),
                    self._contacts_json(item.sbis_emails),
                    self._contacts_json(item.personalised_phones),
                    self._contacts_json(item.personalised_emails),
                    self._contacts_json(item.telegram_phones),
                    self._contacts_json(item.telegram_emails),
                    item.result_code,
                    item.error_code,
                )
                for position, item in enumerate(items)
            ),
        )

    @staticmethod
    def _insert_report_deliveries(
        connection: sqlite3.Connection,
        report_id: int,
        user_ids: Iterable[int],
        *,
        created_at: str,
        ignore_existing: bool = False,
    ) -> None:
        conflict = "OR IGNORE " if ignore_existing else ""
        connection.executemany(
            f"""
            INSERT {conflict}INTO report_deliveries (
                report_id, user_id, status, attempts, created_at, updated_at
            ) VALUES (?, ?, 'pending', 0, ?, ?)
            """,
            (
                (report_id, user_id, created_at, created_at)
                for user_id in user_ids
            ),
        )

    @staticmethod
    def _ensure_report_item_columns(connection: sqlite3.Connection) -> None:
        """Добавить поля полного снимка в БД ранней версии без потери данных."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(report_items)")
        }
        migrations = {
            "registration_date": "TEXT",
            "personalised_phones_json": "TEXT NOT NULL DEFAULT '[]'",
            "personalised_emails_json": "TEXT NOT NULL DEFAULT '[]'",
            "result_code": "TEXT",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE report_items ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _ensure_report_delivery_columns(connection: sqlite3.Connection) -> None:
        """Добавить план повторной доставки в БД ранней версии."""
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(report_deliveries)")
        }
        if "next_retry_at" not in columns:
            connection.execute(
                "ALTER TABLE report_deliveries ADD COLUMN next_retry_at TEXT"
            )

    def _remove_access_entry(
        self,
        table: str,
        user_id: int,
        *,
        actor_user_id: int | None,
        action: str,
        bootstrap_admin_ids: Iterable[int],
    ) -> bool:
        now = self._utc_now()
        bootstrap_ids = self._user_ids(bootstrap_admin_ids)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE user_id = ?", (user_id,)
            )
            changed = cursor.rowcount == 1
            if not self._is_allowed_in_connection(
                connection, user_id, bootstrap_ids
            ):
                connection.execute(
                    "DELETE FROM report_subscriptions WHERE user_id = ?",
                    (user_id,),
                )
            if actor_user_id is not None:
                self._insert_audit(
                    connection,
                    actor_user_id=self._user_id(actor_user_id),
                    action=action,
                    target_user_id=user_id,
                    result="changed" if changed else "unchanged",
                    reason_code=None,
                    created_at=now,
                )
            return changed

    def _finish_delivery(
        self,
        report_id: int,
        user_id: int,
        *,
        claim_token: str,
        status: str,
        telegram_message_id: int | None,
        error_code: str | None,
    ) -> ReportDelivery:
        clean_report_id = self._positive_int(report_id, "ID отчета")
        clean_user_id = self._user_id(user_id)
        clean_token = self._required_text(
            claim_token, "Токен обработки", maximum=200
        )
        if telegram_message_id is not None:
            telegram_message_id = self._positive_int(
                telegram_message_id, "ID сообщения"
            )
        now_datetime = datetime.now(timezone.utc)
        now = now_datetime.isoformat(timespec="seconds")
        sent_at = now if status == "sent" else None
        failed_at = now if status == "failed" else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT attempts FROM report_deliveries
                WHERE report_id = ? AND user_id = ? AND status = 'pending'
                    AND claim_token = ?
                """,
                (clean_report_id, clean_user_id, clean_token),
            ).fetchone()
            if current is None:
                raise ReportDeliveryClaimError(
                    "Доставка не закреплена за указанным worker-ом"
                )
            attempts = int(current["attempts"])
            next_retry_at = None
            if (
                status == "failed"
                and error_code not in PERMANENT_DELIVERY_ERROR_CODES
                and attempts < MAX_ATTEMPTS
            ):
                next_retry_at = self._delivery_next_retry_at(
                    now_datetime,
                    report_id=clean_report_id,
                    user_id=clean_user_id,
                    attempts=attempts,
                )
            cursor = connection.execute(
                """
                UPDATE report_deliveries
                SET status = ?, claim_token = NULL, claimed_at = NULL,
                    sent_at = ?, failed_at = ?, telegram_message_id = ?,
                    error_code = ?, next_retry_at = ?, updated_at = ?
                WHERE report_id = ? AND user_id = ? AND status = 'pending'
                    AND claim_token = ?
                """,
                (
                    status,
                    sent_at,
                    failed_at,
                    telegram_message_id,
                    error_code,
                    next_retry_at,
                    now,
                    clean_report_id,
                    clean_user_id,
                    clean_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ReportDeliveryClaimError(
                    "Доставка не закреплена за указанным worker-ом"
                )
            row = connection.execute(
                """
                SELECT * FROM report_deliveries
                WHERE report_id = ? AND user_id = ?
                """,
                (clean_report_id, clean_user_id),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("Доставка не найдена")
            return self._to_delivery(row)

    @staticmethod
    def _is_allowed_in_connection(
        connection: sqlite3.Connection,
        user_id: int,
        bootstrap_admin_ids: set[int],
    ) -> bool:
        if user_id in bootstrap_admin_ids:
            return True
        row = connection.execute(
            """
            SELECT 1 FROM report_whitelist_users WHERE user_id = ?
            UNION ALL
            SELECT 1 FROM report_whitelist_admins WHERE user_id = ?
            LIMIT 1
            """,
            (user_id, user_id),
        ).fetchone()
        return row is not None

    @staticmethod
    def _deliveries_for_users(
        connection: sqlite3.Connection,
        report_id: int,
        user_ids: Iterable[int],
    ) -> list[ReportDelivery]:
        ordered_ids = tuple(user_ids)
        if not ordered_ids:
            return []
        placeholders = ", ".join("?" for _ in ordered_ids)
        rows = connection.execute(
            f"""
            SELECT * FROM report_deliveries
            WHERE report_id = ? AND user_id IN ({placeholders})
            ORDER BY created_at, user_id
            """,
            (report_id, *ordered_ids),
        ).fetchall()
        return [ReportingStorage._to_delivery(row) for row in rows]

    @staticmethod
    def _require_report(connection: sqlite3.Connection, report_id: int) -> None:
        row = connection.execute(
            "SELECT 1 FROM report_runs WHERE id = ?", (report_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Отчет {report_id} не найден")

    @staticmethod
    def _require_delivery(
        connection: sqlite3.Connection, report_id: int, user_id: int
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM report_deliveries
            WHERE report_id = ? AND user_id = ?
            """,
            (report_id, user_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"Доставка отчета {report_id} пользователю не найдена")

    @staticmethod
    def _insert_audit(
        connection: sqlite3.Connection,
        *,
        actor_user_id: int,
        action: str,
        target_user_id: int | None,
        result: str,
        reason_code: str | None,
        created_at: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO report_admin_audit (
                actor_fingerprint, action, target_fingerprint,
                result, reason_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ReportingStorage._fingerprint(actor_user_id),
                action,
                None
                if target_user_id is None
                else ReportingStorage._fingerprint(target_user_id),
                result,
                reason_code,
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _fingerprint(user_id: int) -> str:
        return hashlib.sha256(
            f"inntophone-report-audit:{user_id}".encode("ascii")
        ).hexdigest()

    @classmethod
    def _client_revisions(
        cls,
        values: Mapping[int, tuple[int, int | None]],
    ) -> dict[int, tuple[int, int | None]]:
        if not isinstance(values, Mapping):
            raise TypeError("client_revisions должен быть mapping")
        result: dict[int, tuple[int, int | None]] = {}
        for raw_spp_id, revisions in values.items():
            spp_id = cls._positive_int(raw_spp_id, "Идентификатор СПП")
            if spp_id in result:
                raise ValueError("Идентификатор СПП указан несколько раз")
            if not isinstance(revisions, tuple) or len(revisions) != 2:
                raise TypeError(
                    "Ревизии клиента должны быть tuple(data, reported)"
                )
            expected_data = cls._nonnegative_int(
                revisions[0], "Ожидаемая ревизия данных"
            )
            expected_reported = (
                None
                if revisions[1] is None
                else cls._nonnegative_int(
                    revisions[1], "Ожидаемая ревизия отчета"
                )
            )
            result[spp_id] = (expected_data, expected_reported)
        return result

    @classmethod
    def _report_item(
        cls, item: ReportItemDraft | Mapping[str, Any]
    ) -> ReportItemDraft:
        if isinstance(item, ReportItemDraft):
            values: Mapping[str, Any] = item.__dict__
        elif isinstance(item, Mapping):
            values = item
        else:
            raise TypeError("Строка отчета должна быть ReportItemDraft или mapping")
        return ReportItemDraft(
            client_spp_id=cls._positive_int(
                values.get("client_spp_id"), "Идентификатор СПП"
            ),
            company_name=cls._required_text(
                values.get("company_name"), "Название организации", maximum=500
            ),
            director_name=cls._optional_text(
                values.get("director_name"), "ФИО директора", maximum=500
            ),
            status=cls._required_text(
                values.get("status"), "Статус", maximum=80
            ),
            registration_date=cls._optional_date_text(
                values.get("registration_date")
            ),
            sbis_phones=cls._contacts(values.get("sbis_phones", ())),
            sbis_emails=cls._contacts(values.get("sbis_emails", ())),
            personalised_phones=cls._contacts(
                values.get("personalised_phones", ())
            ),
            personalised_emails=cls._contacts(
                values.get("personalised_emails", ())
            ),
            telegram_phones=cls._contacts(values.get("telegram_phones", ())),
            telegram_emails=cls._contacts(values.get("telegram_emails", ())),
            result_code=cls._optional_text(
                values.get("result_code"), "Код результата", maximum=100
            ),
            error_code=cls._optional_text(
                values.get("error_code"), "Код ошибки", maximum=100
            ),
        )

    @staticmethod
    def _to_report_run(row: sqlite3.Row) -> ReportRun:
        return ReportRun(
            id=int(row["id"]),
            kind=str(row["kind"]),
            cohort_date=str(row["cohort_date"]),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _to_report_item(row: sqlite3.Row) -> ReportItem:
        return ReportItem(
            client_spp_id=int(row["client_spp_id"]),
            company_name=str(row["company_name"]),
            director_name=row["director_name"],
            status=str(row["status"]),
            registration_date=row["registration_date"],
            sbis_phones=tuple(json.loads(row["sbis_phones_json"])),
            sbis_emails=tuple(json.loads(row["sbis_emails_json"])),
            personalised_phones=tuple(
                json.loads(row["personalised_phones_json"])
            ),
            personalised_emails=tuple(
                json.loads(row["personalised_emails_json"])
            ),
            telegram_phones=tuple(json.loads(row["telegram_phones_json"])),
            telegram_emails=tuple(json.loads(row["telegram_emails_json"])),
            result_code=row["result_code"],
            error_code=row["error_code"],
            id=int(row["id"]),
            report_id=int(row["report_id"]),
            position=int(row["position"]),
        )

    @staticmethod
    def _to_delivery(row: sqlite3.Row) -> ReportDelivery:
        return ReportDelivery(
            report_id=int(row["report_id"]),
            user_id=int(row["user_id"]),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            claim_token=row["claim_token"],
            claimed_at=row["claimed_at"],
            sent_at=row["sent_at"],
            failed_at=row["failed_at"],
            telegram_message_id=(
                None
                if row["telegram_message_id"] is None
                else int(row["telegram_message_id"])
            ),
            error_code=row["error_code"],
            next_retry_at=row["next_retry_at"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _to_audit_entry(row: sqlite3.Row) -> AdminAuditEntry:
        return AdminAuditEntry(
            id=int(row["id"]),
            actor_fingerprint=str(row["actor_fingerprint"]),
            action=str(row["action"]),
            target_fingerprint=row["target_fingerprint"],
            result=str(row["result"]),
            reason_code=row["reason_code"],
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _to_notification_state(row: sqlite3.Row) -> NotificationState:
        return NotificationState(
            key=str(row["key"]),
            value=str(row["value"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _to_pipeline_run(row: sqlite3.Row) -> PipelineRun:
        counts = json.loads(row["processing_status_counts_json"])
        if not isinstance(counts, dict):  # pragma: no cover - поврежденная БД
            raise ValueError("Агрегаты конвейера повреждены")
        return PipelineRun(
            id=int(row["id"]),
            target_date=str(row["target_date"]),
            status=str(row["status"]),
            started_at=str(row["started_at"]),
            finished_at=row["finished_at"],
            collected_cards=int(row["collected_cards"]),
            processing_status_counts={
                str(key): int(value) for key, value in counts.items()
            },
            available_queries=(
                None
                if row["available_queries"] is None
                else int(row["available_queries"])
            ),
            error_stage=row["error_stage"],
            error_code=row["error_code"],
        )

    @staticmethod
    def _to_integration_health(row: sqlite3.Row) -> IntegrationHealth:
        return IntegrationHealth(
            integration=str(row["integration"]),
            status=str(row["status"]),
            checked_at=str(row["checked_at"]),
            last_ok_at=row["last_ok_at"],
            error_code=row["error_code"],
            consecutive_failures=int(row["consecutive_failures"]),
        )

    @classmethod
    def _contacts_json(cls, values: Iterable[str]) -> str:
        return json.dumps(cls._contacts(values), ensure_ascii=False)

    @staticmethod
    def _rendered_parts(parts: Iterable[str]) -> tuple[str, ...]:
        if isinstance(parts, (str, bytes)):
            raise TypeError("Части отчета должны быть iterable строк, а не строкой")
        try:
            result = tuple(parts)
        except TypeError as error:
            raise TypeError("Части отчета должны быть iterable строк") from error
        if not result:
            raise ValueError("Отчет должен содержать хотя бы одну часть")
        if len(result) > MAX_RENDERED_REPORT_PARTS:
            raise ValueError(
                f"Отчет содержит больше {MAX_RENDERED_REPORT_PARTS} частей"
            )
        for part in result:
            if not isinstance(part, str):
                raise TypeError("Каждая часть отчета должна быть строкой")
            if not part.strip():
                raise ValueError("Часть отчета не должна быть пустой")
            if len(part) > TELEGRAM_REPORT_PART_MAX_LENGTH:
                raise ValueError(
                    "Часть отчета превышает лимит Telegram в "
                    f"{TELEGRAM_REPORT_PART_MAX_LENGTH} символов"
                )
        return result

    @staticmethod
    def _contacts(values: Any) -> tuple[str, ...]:
        if values is None:
            return ()
        source = (values,) if isinstance(values, str) else values
        result: list[str] = []
        seen: set[str] = set()
        try:
            for value in source:
                clean = str(value).strip()
                if clean and clean not in seen:
                    result.append(clean)
                    seen.add(clean)
        except TypeError as error:
            raise TypeError("Контакты должны быть строкой или iterable") from error
        return tuple(result)

    @classmethod
    def _processing_status_counts(
        cls, values: Mapping[str, int]
    ) -> dict[str, int]:
        if not isinstance(values, Mapping):
            raise TypeError("Агрегаты статусов должны быть mapping")
        result: dict[str, int] = {}
        for key, value in values.items():
            if not isinstance(key, str):
                raise TypeError("Ключ агрегата статуса должен быть строкой")
            clean_key = cls._operational_code(key, "Ключ агрегата статуса")
            if clean_key in result:
                raise ValueError("Ключ агрегата статуса указан несколько раз")
            result[clean_key] = cls._nonnegative_int(
                value, f"Количество для статуса {clean_key}"
            )
        return result

    @classmethod
    def _optional_operational_code(
        cls, value: Any, field: str
    ) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return cls._operational_code(value, field)

    @classmethod
    def _operational_code(cls, value: Any, field: str) -> str:
        result = cls._required_text(value, field, maximum=100)
        allowed_punctuation = "_.:-/"
        if any(
            not character.isascii()
            or not (character.isalnum() or character in allowed_punctuation)
            for character in result
        ):
            raise ValueError(
                f"{field} должен быть обезличенным машинным кодом"
            )
        return result

    @staticmethod
    def _delivery_next_retry_at(
        now: datetime,
        *,
        report_id: int,
        user_id: int,
        attempts: int,
    ) -> str:
        exponent = max(0, attempts - 1)
        base_delay = min(
            DELIVERY_RETRY_MAX_SECONDS,
            DELIVERY_RETRY_BASE_SECONDS * (2**exponent),
        )
        digest = hashlib.sha256(
            f"{report_id}:{user_id}:{attempts}".encode("ascii")
        ).digest()
        jitter = int.from_bytes(digest[:2], "big") % (
            DELIVERY_RETRY_JITTER_SECONDS + 1
        )
        delay = min(DELIVERY_RETRY_MAX_SECONDS, base_delay + jitter)
        return (now + timedelta(seconds=delay)).isoformat(timespec="seconds")

    @staticmethod
    def _date_text(value: date | str) -> str:
        if isinstance(value, datetime):
            result = value.date()
        elif isinstance(value, date):
            result = value
        elif isinstance(value, str):
            try:
                result = date.fromisoformat(value.strip())
            except ValueError as error:
                raise ValueError("Дата отчета должна иметь формат YYYY-MM-DD") from error
        else:
            raise TypeError("Дата отчета должна быть date или строкой YYYY-MM-DD")
        return result.isoformat()

    @classmethod
    def _optional_date_text(cls, value: Any) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return cls._date_text(value)

    @staticmethod
    def _required_text(value: Any, field: str, *, maximum: int) -> str:
        if value is None:
            raise ValueError(f"{field} не заполнен")
        result = str(value).strip()
        if not result:
            raise ValueError(f"{field} не заполнен")
        if len(result) > maximum:
            raise ValueError(f"{field} длиннее {maximum} символов")
        return result

    @classmethod
    def _optional_text(
        cls, value: Any, field: str, *, maximum: int
    ) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        return cls._required_text(value, field, maximum=maximum)

    @staticmethod
    def _positive_int(value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field} должен быть положительным целым числом")
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{field} должен быть положительным целым числом"
            ) from error
        if result <= 0:
            raise ValueError(f"{field} должен быть положительным целым числом")
        return result

    @staticmethod
    def _nonnegative_int(value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field} должен быть неотрицательным целым числом")
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{field} должен быть неотрицательным целым числом"
            ) from error
        if result < 0:
            raise ValueError(f"{field} должен быть неотрицательным целым числом")
        return result

    @classmethod
    def _user_id(cls, value: Any) -> int:
        return cls._positive_int(value, "Telegram ID")

    @classmethod
    def _user_ids(cls, values: Iterable[int]) -> set[int]:
        return {cls._user_id(value) for value in values}

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()
