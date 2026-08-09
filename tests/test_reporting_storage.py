import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

from src.storage.new_clients import NewClientStorage
from src.storage.reporting import (
    BootstrapAdminRemovalError,
    ReportDeliveryClaimError,
    ReportItemDraft,
    ReportingStorage,
)


def report_item(client_spp_id: int = 101) -> ReportItemDraft:
    return ReportItemDraft(
        client_spp_id=client_spp_id,
        company_name='ООО "Тест"',
        director_name="Иванов Иван Иванович",
        status="processed",
        registration_date="2026-08-02",
        sbis_phones=("+79990000001",),
        sbis_emails=("office@example.test",),
        personalised_phones=("+79990000003",),
        personalised_emails=("director@example.test",),
        telegram_phones=("+79990000002",),
        telegram_emails=("owner@example.test",),
        result_code="phone_found_by_inn",
    )


def sbis_client(client_spp_id: int) -> dict[str, object]:
    return {
        "ИдентификаторСПП": client_spp_id,
        "Название": f'ООО "Клиент {client_spp_id}"',
        "Регион": "Москва",
        "ОГРН": "1127746271355",
        "ИНН": "7736641983",
        "КПП": "773601001",
        "Предприниматель": False,
        "ДатаРегистрации": "2026-08-02",
        "ДатаЛиквидации": None,
        "Директор.Фамилия": "Иванов",
        "Директор.Имя": "Иван",
        "Директор.Отчество": "Иванович",
        "Телефон": "+79990000001",
        "email": "office@example.test",
    }


class ReportingStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "data" / "clients.db"
        self.storage = ReportingStorage(self.database_path)
        self.storage.initialize()

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_initialization_is_idempotent_and_enables_foreign_keys(self) -> None:
        self.storage.initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0],
                "wal",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO report_items (
                        report_id, position, client_spp_id, company_name,
                        status, sbis_phones_json, sbis_emails_json,
                        personalised_phones_json, personalised_emails_json,
                        telegram_phones_json, telegram_emails_json
                    ) VALUES (
                        999, 0, 1, 'Тест', 'queued',
                        '[]', '[]', '[]', '[]', '[]', '[]'
                    )
                    """
                )

    def test_separates_user_and_admin_lists_and_combines_access(self) -> None:
        self.assertTrue(self.storage.add_user(100, actor_user_id=900))
        self.assertFalse(self.storage.add_user(100, actor_user_id=900))
        self.assertTrue(self.storage.add_admin(200, actor_user_id=900))

        self.assertTrue(self.storage.is_user_allowed(100))
        self.assertFalse(self.storage.is_admin(100))
        self.assertTrue(self.storage.is_user_allowed(200))
        self.assertTrue(self.storage.is_admin(200))
        self.assertEqual(
            self.storage.list_admins(bootstrap_admin_ids={900}),
            (200, 900),
        )
        self.assertEqual(self.storage.list_users(), (100,))

    def test_bootstrap_admin_is_allowed_and_cannot_be_removed(self) -> None:
        bootstrap = {900}

        self.assertTrue(
            self.storage.is_user_allowed(900, bootstrap_admin_ids=bootstrap)
        )
        self.assertTrue(self.storage.is_admin(900, bootstrap_admin_ids=bootstrap))
        self.assertFalse(
            self.storage.add_admin(900, bootstrap_admin_ids=bootstrap)
        )
        with self.assertRaises(BootstrapAdminRemovalError):
            self.storage.remove_admin(
                900,
                actor_user_id=900,
                bootstrap_admin_ids=bootstrap,
            )

    def test_subscription_requires_access_and_is_idempotent(self) -> None:
        with self.assertRaises(PermissionError):
            self.storage.subscribe(100)

        self.storage.add_user(100)
        self.assertTrue(self.storage.subscribe(100))
        self.assertFalse(self.storage.subscribe(100))
        self.assertTrue(self.storage.is_subscribed(100))
        self.assertEqual(self.storage.list_subscribers(), (100,))
        self.assertTrue(self.storage.unsubscribe(100))
        self.assertFalse(self.storage.unsubscribe(100))
        self.assertFalse(self.storage.is_subscribed(100))

    def test_removing_last_role_also_removes_subscription(self) -> None:
        self.storage.add_user(100)
        self.storage.add_admin(100)
        self.storage.subscribe(100)

        self.storage.remove_user(100)
        self.assertEqual(self.storage.list_subscribers(), (100,))
        self.storage.remove_admin(100)

        self.assertEqual(self.storage.list_subscribers(), ())

    def test_creates_report_and_items_idempotently_by_business_key(self) -> None:
        first = self.storage.create_report_run(
            kind="weekly",
            cohort_date=date(2026, 8, 2),
            items=(report_item(),),
        )
        second = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            items=(report_item(), report_item(102)),
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(
            self.storage.find_report_run(
                kind="weekly", cohort_date="2026-08-02"
            ),
            first,
        )
        items = self.storage.list_report_items(first.id)
        self.assertEqual([item.client_spp_id for item in items], [101])
        self.assertEqual(items[0].sbis_phones, ("+79990000001",))
        self.assertEqual(items[0].personalised_phones, ("+79990000003",))
        self.assertEqual(items[0].telegram_emails, ("owner@example.test",))
        self.assertEqual(items[0].result_code, "phone_found_by_inn")

    def test_get_or_create_reports_exact_insert_winner(self) -> None:
        first, first_created = self.storage.get_or_create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            items=(report_item(101),),
            delivery_user_ids=(100,),
        )
        repeated, repeated_created = self.storage.get_or_create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            items=(report_item(202),),
            delivery_user_ids=(200,),
        )

        self.assertTrue(first_created)
        self.assertFalse(repeated_created)
        self.assertEqual(repeated, first)
        self.assertEqual(
            [item.client_spp_id for item in self.storage.list_report_items(first.id)],
            [101],
        )
        self.assertEqual(
            [
                delivery.user_id
                for delivery in self.storage.list_pending_deliveries(first.id)
            ],
            [100],
        )

    def test_concurrent_get_or_create_has_one_created_winner(self) -> None:
        barrier = Barrier(2)

        def get_or_create(_worker: int):
            competing = ReportingStorage(self.database_path)
            barrier.wait()
            return competing.get_or_create_report_run(
                kind="weekly-race",
                cohort_date="2026-08-02",
                items=(report_item(101),),
                delivery_user_ids=(100,),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(get_or_create, (1, 2)))

        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(len({report.id for report, _ in results}), 1)

    def test_report_revision_creates_separate_run(self) -> None:
        first = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02", revision=1
        )
        revised = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02", revision=2
        )

        self.assertNotEqual(first.id, revised.id)

    def test_atomically_allocates_next_revision_with_own_snapshot(self) -> None:
        first = self.storage.create_next_report_run(
            kind="manual",
            cohort_date="2026-08-02",
            items=(report_item(101),),
            delivery_user_ids=(900,),
        )
        second = self.storage.create_next_report_run(
            kind="manual",
            cohort_date="2026-08-02",
            items=(report_item(202),),
        )

        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 2)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(
            [item.client_spp_id for item in self.storage.list_report_items(first.id)],
            [101],
        )
        self.assertEqual(
            [
                delivery.user_id
                for delivery in self.storage.list_pending_deliveries(first.id)
            ],
            [900],
        )
        self.assertEqual(
            [item.client_spp_id for item in self.storage.list_report_items(second.id)],
            [202],
        )

    def test_client_revision_cas_creates_only_one_sequential_supplement(self) -> None:
        clients = NewClientStorage(self.database_path)
        clients.initialize()
        clients.upsert_from_sbis(sbis_client(101))
        clients.mark_reported((101,), "main")
        changed = clients.replace_telegram_contacts(
            101, phones=("+79991112233",)
        )
        revisions = {101: (changed.data_revision, changed.reported_revision)}

        first = self.storage.create_next_report_run_for_client_revisions(
            kind="supplement",
            cohort_date="2026-08-02",
            items=(report_item(101),),
            client_revisions=revisions,
            delivery_user_ids=(700,),
        )
        second = self.storage.create_next_report_run_for_client_revisions(
            kind="supplement",
            cohort_date="2026-08-02",
            items=(report_item(101),),
            client_revisions=revisions,
            delivery_user_ids=(700,),
        )

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        saved = clients.get(101)
        self.assertEqual(saved.report_id, str(first.id))
        self.assertEqual(saved.reported_revision, saved.data_revision)
        self.assertEqual(
            [item.client_spp_id for item in self.storage.list_report_items(first.id)],
            [101],
        )
        self.assertEqual(
            [
                delivery.user_id
                for delivery in self.storage.list_pending_deliveries(first.id)
            ],
            [700],
        )

    def test_client_revision_cas_selects_only_matching_items(self) -> None:
        clients = NewClientStorage(self.database_path)
        clients.initialize()
        clients.upsert_from_sbis(sbis_client(101))
        clients.upsert_from_sbis(sbis_client(202))
        clients.mark_reported((101, 202), "main")
        first = clients.replace_telegram_contacts(101, phones=("+79991112233",))
        second = clients.replace_telegram_contacts(202, phones=("+79992223344",))

        report = self.storage.create_next_report_run_for_client_revisions(
            kind="supplement",
            cohort_date="2026-08-02",
            items=(report_item(101), report_item(202)),
            client_revisions={
                101: (first.data_revision, first.reported_revision),
                202: (second.data_revision + 1, second.reported_revision),
            },
            delivery_user_ids=(),
        )

        self.assertIsNotNone(report)
        self.assertEqual(
            [item.client_spp_id for item in self.storage.list_report_items(report.id)],
            [101],
        )
        self.assertEqual(clients.get(101).reported_revision, first.data_revision)
        self.assertEqual(
            clients.get(202).reported_revision, second.reported_revision
        )

    def test_concurrent_client_revision_cas_has_one_winner(self) -> None:
        clients = NewClientStorage(self.database_path)
        clients.initialize()
        clients.upsert_from_sbis(sbis_client(101))
        clients.mark_reported((101,), "main")
        changed = clients.replace_telegram_contacts(
            101, phones=("+79991112233",)
        )
        revisions = {101: (changed.data_revision, changed.reported_revision)}
        barrier = Barrier(2)

        def create_supplement(_worker: int):
            competing = ReportingStorage(self.database_path)
            barrier.wait()
            return competing.create_next_report_run_for_client_revisions(
                kind="supplement",
                cohort_date="2026-08-02",
                items=(report_item(101),),
                client_revisions=revisions,
                delivery_user_ids=(700,),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create_supplement, (1, 2)))

        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM report_runs WHERE kind = 'supplement'"
            ).fetchone()[0]
        self.assertEqual(run_count, 1)
        saved = clients.get(101)
        self.assertEqual(saved.report_id, str(winners[0].id))
        self.assertEqual(saved.reported_revision, saved.data_revision)

    def test_client_revision_cas_requires_exact_valid_revision_keys(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.create_next_report_run_for_client_revisions(
                kind="supplement",
                cohort_date="2026-08-02",
                items=(report_item(101),),
                client_revisions={},
                delivery_user_ids=(),
            )
        with self.assertRaises(ValueError):
            self.storage.create_next_report_run_for_client_revisions(
                kind="supplement",
                cohort_date="2026-08-02",
                items=(report_item(101),),
                client_revisions={101: (-1, None)},
                delivery_user_ids=(),
            )

    def test_report_creation_freezes_delivery_recipients_on_first_call(self) -> None:
        first = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            items=(report_item(),),
            delivery_user_ids=(200, 100, 200),
        )
        repeated = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            items=(report_item(202),),
            delivery_user_ids=(300,),
        )

        self.assertEqual(first, repeated)
        self.assertEqual(
            [
                delivery.user_id
                for delivery in self.storage.list_pending_deliveries(first.id)
            ],
            [100, 200],
        )
        self.assertEqual(
            [item.client_spp_id for item in self.storage.list_report_items(first.id)],
            [101],
        )

    def test_empty_delivery_snapshot_stays_empty_on_repeat(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            delivery_user_ids=(),
        )
        self.storage.create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            delivery_user_ids=(100,),
        )

        self.assertEqual(self.storage.list_pending_deliveries(report.id), [])

    def test_finds_latest_client_item_only_in_selected_report_kinds(self) -> None:
        weekly = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2026-08-01",
            items=(report_item(101),),
        )
        supplement_item = report_item(101)
        supplement_item = ReportItemDraft(
            **{**supplement_item.__dict__, "result_code": "supplement_result"}
        )
        supplement = self.storage.create_report_run(
            kind="supplement",
            cohort_date="2026-08-02",
            items=(supplement_item,),
        )
        self.storage.create_report_run(
            kind="manual",
            cohort_date="2026-08-03",
            items=(report_item(101),),
        )

        selected = self.storage.latest_report_item_for_client(
            101, kinds=("weekly", "supplement")
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.report_id, supplement.id)
        self.assertEqual(selected.result_code, "supplement_result")
        self.assertEqual(
            self.storage.latest_report_item_for_client(
                101, kinds=("weekly",)
            ).report_id,
            weekly.id,
        )
        self.assertIsNone(
            self.storage.latest_report_item_for_client(
                999, kinds=("weekly", "supplement")
            )
        )
        with self.assertRaises(ValueError):
            self.storage.latest_report_item_for_client(101, kinds=())

    def test_returns_latest_report_with_optional_kind_filter(self) -> None:
        self.assertIsNone(self.storage.latest_report_run())
        weekly = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-01"
        )
        manual = self.storage.create_report_run(
            kind="manual", cohort_date="2026-08-02"
        )
        newest_weekly = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-03"
        )

        self.assertEqual(self.storage.latest_report_run(), newest_weekly)
        self.assertEqual(self.storage.latest_report_run("weekly"), newest_weekly)
        self.assertEqual(self.storage.latest_report_run("manual"), manual)
        self.assertNotEqual(self.storage.latest_report_run("weekly"), weekly)

    def test_latest_deliverable_report_ignores_manual_snapshot(self) -> None:
        self.assertIsNone(self.storage.latest_deliverable_report_run())
        delivered = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-01"
        )
        self.storage.ensure_report_deliveries(delivered.id, (100,))
        self.storage.create_report_run(
            kind="manual", cohort_date="2026-08-02"
        )

        self.assertEqual(
            self.storage.latest_deliverable_report_run(), delivered
        )

    def test_lists_distinct_report_runs_with_failed_deliveries(self) -> None:
        first = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-01"
        )
        second = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02"
        )
        third = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-03"
        )
        self.storage.ensure_report_deliveries(first.id, (100, 200))
        self.storage.ensure_report_deliveries(second.id, (300,))
        self.storage.ensure_report_deliveries(third.id, (400,))
        first_claims = self.storage.claim_pending_deliveries(
            first.id, claim_token="worker"
        )
        for delivery in first_claims:
            self.storage.mark_delivery_failed(
                first.id,
                delivery.user_id,
                claim_token="worker",
                error_code="network_error",
            )
        self.storage.claim_pending_deliveries(
            second.id, claim_token="worker"
        )
        self.storage.mark_delivery_failed(
            second.id,
            300,
            claim_token="worker",
            error_code="telegram_forbidden",
        )

        self.assertEqual(
            self.storage.list_report_runs_with_failed_deliveries(),
            [first, second],
        )
        self.assertEqual(
            self.storage.list_report_runs_with_failed_deliveries(limit=1),
            [first],
        )
        with self.assertRaises(ValueError):
            self.storage.list_report_runs_with_failed_deliveries(limit=0)

    def test_lists_runs_with_pending_or_retryable_failed_deliveries(self) -> None:
        pending = self.storage.create_report_run(
            kind="manual", cohort_date="2026-08-01"
        )
        claimed_pending = self.storage.create_report_run(
            kind="manual", cohort_date="2026-08-02"
        )
        retryable_failed = self.storage.create_report_run(
            kind="manual", cohort_date="2026-08-03"
        )
        permanent_failed = self.storage.create_report_run(
            kind="manual", cohort_date="2026-08-04"
        )
        sent = self.storage.create_report_run(
            kind="manual", cohort_date="2026-08-05"
        )
        for report, user_id in (
            (pending, 100),
            (claimed_pending, 200),
            (retryable_failed, 300),
            (permanent_failed, 400),
            (sent, 500),
        ):
            self.storage.ensure_report_deliveries(report.id, (user_id,))
        self.storage.claim_pending_deliveries(
            claimed_pending.id, claim_token="stopped-worker"
        )
        self.storage.claim_pending_deliveries(
            retryable_failed.id, claim_token="worker"
        )
        self.storage.mark_delivery_failed(
            retryable_failed.id,
            300,
            claim_token="worker",
            error_code="network_error",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE report_deliveries SET next_retry_at = ?
                WHERE report_id = ? AND user_id = ?
                """,
                ("2000-01-01T00:00:00+00:00", retryable_failed.id, 300),
            )
            connection.commit()
        self.storage.claim_pending_deliveries(
            permanent_failed.id, claim_token="worker"
        )
        self.storage.mark_delivery_failed(
            permanent_failed.id,
            400,
            claim_token="worker",
            error_code="access_revoked",
        )
        self.storage.claim_pending_deliveries(sent.id, claim_token="worker")
        self.storage.mark_delivery_sent(
            sent.id, 500, claim_token="worker"
        )

        self.assertEqual(
            self.storage.list_report_runs_with_open_deliveries(),
            [pending, claimed_pending, retryable_failed],
        )
        self.assertEqual(
            self.storage.list_report_runs_with_open_deliveries(limit=2),
            [pending, claimed_pending],
        )
        with self.assertRaises(ValueError):
            self.storage.list_report_runs_with_open_deliveries(limit=0)

    def test_initialize_migrates_early_report_item_snapshot(self) -> None:
        self.temp_directory.cleanup()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "legacy.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE report_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    cohort_date TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (kind, cohort_date, revision)
                );
                CREATE TABLE report_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    client_spp_id INTEGER NOT NULL,
                    company_name TEXT NOT NULL,
                    director_name TEXT,
                    status TEXT NOT NULL,
                    sbis_phones_json TEXT NOT NULL,
                    sbis_emails_json TEXT NOT NULL,
                    telegram_phones_json TEXT NOT NULL,
                    telegram_emails_json TEXT NOT NULL,
                    error_code TEXT,
                    UNIQUE (report_id, client_spp_id),
                    UNIQUE (report_id, position),
                    FOREIGN KEY (report_id) REFERENCES report_runs(id)
                        ON DELETE CASCADE
                );
                """
            )
        self.storage = ReportingStorage(self.database_path)

        self.storage.initialize()
        report = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            items=(report_item(),),
        )

        saved = self.storage.list_report_items(report.id)[0]
        self.assertEqual(saved.registration_date, "2026-08-02")
        self.assertEqual(saved.personalised_emails, ("director@example.test",))
        self.assertEqual(saved.result_code, "phone_found_by_inn")

    def test_initialize_migrates_delivery_retry_column(self) -> None:
        self.temp_directory.cleanup()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "legacy-delivery.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE report_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    cohort_date TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (kind, cohort_date, revision)
                );
                CREATE TABLE report_deliveries (
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (report_id, user_id),
                    UNIQUE (report_id, user_id),
                    FOREIGN KEY (report_id) REFERENCES report_runs(id)
                        ON DELETE CASCADE
                );
                """
            )
        self.storage = ReportingStorage(self.database_path)

        self.storage.initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(report_deliveries)"
                )
            }
        self.assertIn("next_retry_at", columns)

    def test_delivery_claim_and_completion_are_atomic(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02"
        )
        deliveries = self.storage.ensure_report_deliveries(
            report.id, (200, 100, 200)
        )

        self.assertEqual([item.user_id for item in deliveries], [100, 200])
        self.assertEqual(len(self.storage.list_pending_deliveries(report.id)), 2)
        claimed = self.storage.claim_pending_deliveries(
            report.id, claim_token="worker-one", limit=1
        )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].attempts, 1)
        self.assertEqual(claimed[0].claim_token, "worker-one")

        sent = self.storage.mark_delivery_sent(
            report.id,
            claimed[0].user_id,
            claim_token="worker-one",
            telegram_message_id=123,
        )

        self.assertEqual(sent.status, "sent")
        self.assertIsNotNone(sent.sent_at)
        self.assertEqual(sent.telegram_message_id, 123)
        with self.assertRaises(ReportDeliveryClaimError):
            self.storage.mark_delivery_sent(
                report.id,
                claimed[0].user_id,
                claim_token="worker-one",
            )

    def test_delivery_parts_resume_skips_already_sent_part(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02"
        )
        self.storage.ensure_report_deliveries(report.id, (100,))
        self.storage.ensure_delivery_parts(report.id, 100, 3)

        changed = self.storage.mark_delivery_part_sent(
            report.id, 100, 0, message_id=700
        )

        self.assertTrue(changed)
        self.assertEqual(
            self.storage.sent_delivery_part_indexes(report.id, 100), {0}
        )
        self.assertFalse(
            self.storage.mark_delivery_part_sent(
                report.id, 100, 0, message_id=701
            )
        )
        self.storage.ensure_delivery_parts(report.id, 100, 3)
        with self.assertRaises(ValueError):
            self.storage.ensure_delivery_parts(report.id, 100, 4)
        with self.assertRaises(KeyError):
            self.storage.mark_delivery_part_sent(report.id, 100, 3)

        with closing(sqlite3.connect(self.database_path)) as connection:
            saved_message_id = connection.execute(
                """
                SELECT message_id FROM report_delivery_parts
                WHERE report_id = ? AND user_id = ? AND part_index = 0
                """,
                (report.id, 100),
            ).fetchone()[0]
        self.assertEqual(saved_message_id, 700)

    def test_failed_delivery_can_be_requeued(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02"
        )
        self.storage.ensure_report_deliveries(report.id, (100,))
        self.storage.claim_pending_deliveries(
            report.id, claim_token="worker-one"
        )

        failed = self.storage.mark_delivery_failed(
            report.id,
            100,
            claim_token="worker-one",
            error_code="bot_blocked",
        )
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_code, "bot_blocked")
        self.assertEqual(self.storage.retry_failed_deliveries(report.id), 1)
        self.assertEqual(
            self.storage.list_pending_deliveries(report.id)[0].status,
            "pending",
        )

    def test_failed_delivery_waits_for_backoff_before_automatic_retry(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02", delivery_user_ids=(100,)
        )
        claimed = self.storage.claim_pending_deliveries(
            report.id, claim_token="worker"
        )[0]
        self.assertEqual(claimed.attempts, 1)

        failed = self.storage.mark_delivery_failed(
            report.id,
            100,
            claim_token="worker",
            error_code="network_error",
        )

        self.assertIsNotNone(failed.next_retry_at)
        delay = datetime.fromisoformat(failed.next_retry_at) - datetime.fromisoformat(
            failed.failed_at
        )
        self.assertGreaterEqual(delay.total_seconds(), 30)
        self.assertLessEqual(delay.total_seconds(), 40)
        self.assertEqual(
            self.storage.list_report_runs_with_open_deliveries(), []
        )
        self.assertEqual(
            self.storage.retry_failed_deliveries(
                report.id, retryable_only=True
            ),
            0,
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE report_deliveries SET next_retry_at = ?",
                ("2000-01-01T00:00:00+00:00",),
            )
            connection.commit()
        self.assertEqual(
            self.storage.list_report_runs_with_open_deliveries(), [report]
        )
        self.assertEqual(
            self.storage.retry_failed_deliveries(
                report.id, retryable_only=True
            ),
            1,
        )

    def test_attempt_cap_stops_auto_retry_but_manual_override_resets_it(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02", delivery_user_ids=(100,)
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE report_deliveries SET attempts = 4 WHERE report_id = ?",
                (report.id,),
            )
            connection.commit()
        claimed = self.storage.claim_pending_deliveries(
            report.id, claim_token="last-attempt"
        )[0]
        self.assertEqual(claimed.attempts, 5)
        failed = self.storage.mark_delivery_failed(
            report.id,
            100,
            claim_token="last-attempt",
            error_code="network_error",
        )

        self.assertIsNone(failed.next_retry_at)
        self.assertEqual(
            self.storage.retry_failed_deliveries(
                report.id, retryable_only=True
            ),
            0,
        )
        self.assertEqual(
            self.storage.list_report_runs_with_open_deliveries(), []
        )
        self.assertEqual(
            self.storage.retry_failed_deliveries(
                report.id, retryable_only=False
            ),
            1,
        )
        reclaimed = self.storage.claim_pending_deliveries(
            report.id, claim_token="manual-override"
        )[0]
        self.assertEqual(reclaimed.attempts, 1)

    def test_stale_fifth_claim_becomes_terminal_retry_exhausted(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02", delivery_user_ids=(100,)
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE report_deliveries SET attempts = 4 WHERE report_id = ?",
                (report.id,),
            )
            connection.commit()
        active = self.storage.claim_pending_deliveries(
            report.id, claim_token="fifth-attempt"
        )[0]
        self.assertEqual(active.attempts, 5)
        self.assertEqual(
            self.storage.list_report_runs_with_open_deliveries(), [report]
        )

        self.assertEqual(
            self.storage.claim_pending_deliveries(
                report.id,
                claim_token="other-worker",
                stale_after_seconds=900,
            ),
            [],
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            status = connection.execute(
                "SELECT status FROM report_deliveries WHERE report_id = ?",
                (report.id,),
            ).fetchone()[0]
            self.assertEqual(status, "pending")
            connection.execute(
                "UPDATE report_deliveries SET claimed_at = ? WHERE report_id = ?",
                ("2000-01-01T00:00:00+00:00", report.id),
            )
            connection.commit()
        self.assertEqual(
            self.storage.list_report_runs_with_open_deliveries(), [report]
        )

        self.assertEqual(
            self.storage.claim_pending_deliveries(
                report.id,
                claim_token="recovery-worker",
                stale_after_seconds=900,
            ),
            [],
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT status, error_code, claim_token, next_retry_at
                FROM report_deliveries WHERE report_id = ?
                """,
                (report.id,),
            ).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertEqual(row[1], "retry_exhausted")
        self.assertIsNone(row[2])
        self.assertIsNone(row[3])
        self.assertEqual(
            self.storage.list_report_runs_with_open_deliveries(), []
        )
        self.assertEqual(
            self.storage.retry_failed_deliveries(
                report.id, retryable_only=True
            ),
            0,
        )

    def test_retryable_only_preserves_permanent_delivery_errors(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02"
        )
        self.storage.ensure_report_deliveries(report.id, (100, 200, 300))
        self.storage.claim_pending_deliveries(
            report.id, claim_token="worker"
        )
        for user_id, error_code in (
            (100, "network_error"),
            (200, "access_revoked"),
            (300, "telegram_forbidden"),
        ):
            self.storage.mark_delivery_failed(
                report.id,
                user_id,
                claim_token="worker",
                error_code=error_code,
            )

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                UPDATE report_deliveries SET next_retry_at = ?
                WHERE report_id = ? AND user_id = 100
                """,
                ("2000-01-01T00:00:00+00:00", report.id),
            )
            connection.commit()

        changed = self.storage.retry_failed_deliveries(
            report.id, retryable_only=True
        )

        self.assertEqual(changed, 1)
        self.assertEqual(
            self.storage.delivery_status_counts(report.id),
            {"pending": 1, "sent": 0, "failed": 2},
        )

    def test_counts_every_delivery_status_including_zero(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02"
        )
        self.assertEqual(
            self.storage.delivery_status_counts(report.id),
            {"pending": 0, "sent": 0, "failed": 0},
        )
        self.storage.ensure_report_deliveries(report.id, (100, 200, 300))
        self.storage.claim_pending_deliveries(
            report.id, claim_token="worker", limit=2
        )
        self.storage.mark_delivery_sent(
            report.id, 100, claim_token="worker"
        )
        self.storage.mark_delivery_failed(
            report.id,
            200,
            claim_token="worker",
            error_code="bot_blocked",
        )

        self.assertEqual(
            self.storage.delivery_status_counts(report.id),
            {"pending": 1, "sent": 1, "failed": 1},
        )
        with self.assertRaises(KeyError):
            self.storage.delivery_status_counts(999)

    def test_report_deletion_cascades_to_items_and_deliveries(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2026-08-02",
            items=(report_item(),),
        )
        self.storage.ensure_report_deliveries(report.id, (100,))

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM report_runs WHERE id = ?", (report.id,))
            connection.commit()
            item_count = connection.execute(
                "SELECT COUNT(*) FROM report_items"
            ).fetchone()[0]
            delivery_count = connection.execute(
                "SELECT COUNT(*) FROM report_deliveries"
            ).fetchone()[0]

        self.assertEqual(item_count, 0)
        self.assertEqual(delivery_count, 0)

    def test_rendered_report_parts_are_frozen_on_first_call(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02"
        )

        first = self.storage.ensure_report_rendered_parts(
            report.id, ("Первая часть", "Вторая часть")
        )
        repeated = self.storage.ensure_report_rendered_parts(
            report.id, ("Новый рендер",)
        )

        self.assertEqual(first, ("Первая часть", "Вторая часть"))
        self.assertEqual(repeated, first)

    def test_rendered_report_parts_validate_input_and_cascade(self) -> None:
        report = self.storage.create_report_run(
            kind="weekly", cohort_date="2026-08-02"
        )
        for invalid_parts, error_type in (
            ((), ValueError),
            (("",), ValueError),
            (("А" * 4097,), ValueError),
            ("Одна строка", TypeError),
            ((123,), TypeError),
        ):
            with self.subTest(invalid_parts=invalid_parts):
                with self.assertRaises(error_type):
                    self.storage.ensure_report_rendered_parts(
                        report.id, invalid_parts
                    )
        saved = self.storage.ensure_report_rendered_parts(
            report.id, ("А" * 4096,)
        )
        self.assertEqual(len(saved[0]), 4096)

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM report_runs WHERE id = ?", (report.id,))
            connection.commit()
            part_count = connection.execute(
                "SELECT COUNT(*) FROM report_rendered_parts"
            ).fetchone()[0]
        self.assertEqual(part_count, 0)

    def test_audit_does_not_store_raw_telegram_ids_or_details(self) -> None:
        entry = self.storage.record_admin_audit(
            actor_user_id=987654321,
            action="user.add",
            target_user_id=123456789,
            result="changed",
        )

        self.assertEqual(entry.action, "user.add")
        self.assertNotEqual(entry.actor_fingerprint, "987654321")
        with closing(sqlite3.connect(self.database_path)) as connection:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'report_admin_audit'"
            ).fetchone()[0]
            values = " ".join(
                str(value)
                for value in connection.execute(
                    "SELECT * FROM report_admin_audit"
                ).fetchone()
            )
        self.assertNotIn("987654321", values)
        self.assertNotIn("123456789", values)
        self.assertNotIn("phone", sql.casefold())
        self.assertNotIn("email", sql.casefold())

    def test_notification_state_is_upserted_with_utc_timestamp(self) -> None:
        self.assertIsNone(self.storage.get_notification_state("query_balance"))

        first = self.storage.set_notification_state("query_balance", "low")
        second = self.storage.set_notification_state("query_balance", "empty")

        self.assertEqual(first.key, "query_balance")
        self.assertEqual(second.value, "empty")
        self.assertEqual(
            self.storage.get_notification_state("query_balance"), second
        )
        self.assertTrue(second.updated_at.endswith("+00:00"))

    def test_retention_deletes_old_reports_with_all_dependencies(self) -> None:
        old = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2020-01-01",
            items=(report_item(101),),
            delivery_user_ids=(100,),
        )
        self.storage.ensure_report_rendered_parts(old.id, ("Старый отчет",))
        self.storage.ensure_delivery_parts(old.id, 100, 1)
        current = self.storage.create_report_run(
            kind="weekly",
            cohort_date="2030-01-01",
            items=(report_item(202),),
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE report_runs SET created_at = ? WHERE id = ?",
                ("2020-01-01T00:00:00+00:00", old.id),
            )
            connection.execute(
                "UPDATE report_runs SET created_at = ? WHERE id = ?",
                ("2030-01-01T00:00:00+00:00", current.id),
            )
            connection.commit()

        deleted = self.storage.delete_report_runs_created_before(
            datetime(2025, 1, 1, tzinfo=timezone.utc)
        )

        self.assertEqual(deleted, 1)
        self.assertIsNone(self.storage.get_report_run(old.id))
        self.assertEqual(self.storage.get_report_run(current.id).id, current.id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            for table in (
                "report_items",
                "report_rendered_parts",
                "report_deliveries",
                "report_delivery_parts",
            ):
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE report_id = ?",
                    (old.id,),
                ).fetchone()[0]
                self.assertEqual(count, 0, table)
        with self.assertRaises(ValueError):
            self.storage.delete_report_runs_created_before(datetime(2025, 1, 1))

    def test_pipeline_run_records_only_aggregate_operational_state(self) -> None:
        self.assertIsNone(self.storage.latest_pipeline_run())
        started = self.storage.start_pipeline_run(date(2026, 8, 8))

        self.assertEqual(started.status, "running")
        self.assertEqual(started.target_date, "2026-08-08")
        self.assertEqual(started.processing_status_counts, {})
        self.assertIsNone(started.finished_at)

        finished = self.storage.finish_pipeline_run(
            started.id,
            status="completed",
            collected_cards=53,
            processing_status_counts={
                "processed": 30,
                "skipped": 20,
                "needs_review": 3,
            },
            available_queries=34,
        )

        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.collected_cards, 53)
        self.assertEqual(finished.processing_status_counts["processed"], 30)
        self.assertEqual(finished.available_queries, 34)
        self.assertIsNotNone(finished.finished_at)
        repeated = self.storage.finish_pipeline_run(
            started.id,
            status="failed",
            collected_cards=0,
            processing_status_counts={},
            error_stage="should_not_replace",
            error_code="should_not_replace",
        )
        self.assertEqual(repeated, finished)
        self.assertEqual(self.storage.latest_pipeline_run(), finished)

    def test_pipeline_failed_run_saves_generic_error_codes(self) -> None:
        started = self.storage.start_pipeline_run("2026-08-08")

        failed = self.storage.finish_pipeline_run(
            started.id,
            status="failed",
            collected_cards=12,
            processing_status_counts={"processed": 4, "retry_required": 8},
            available_queries=0,
            error_stage="telegram_enrichment",
            error_code="queries_exhausted",
        )

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error_stage, "telegram_enrichment")
        self.assertEqual(failed.error_code, "queries_exhausted")
        self.assertEqual(self.storage.latest_pipeline_run(), failed)


if __name__ == "__main__":
    unittest.main()
