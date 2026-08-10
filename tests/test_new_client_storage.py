import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path
from threading import Barrier

from src.storage.new_clients import (
    NewClientStorage,
    ProcessingStatus,
    TelegramClaimError,
)


def sbis_client(**changes):
    record = {
        "ИдентификаторСПП": 30852759,
        "Название": 'ООО "Новый клиент"',
        "Регион": "Москва",
        "ОГРН": "1127746271355",
        "ИНН": "7736641983",
        "КПП": "773601001",
        "Предприниматель": False,
        "ДатаРегистрации": date(2012, 4, 10),
        "ДатаЛиквидации": None,
        "Директор.Фамилия": "Иванов",
        "Директор.Имя": "Иван",
        "Директор.Отчество": "Иванович",
        "Телефон": "+79990000001",
        "email": "office@example.test",
    }
    record.update(changes)
    return record


def company_card(**changes):
    card = {
        "ИНН": "7736641983",
        "КПП": "773601001",
        "ОГРН": "1127746271355",
        "ShortName": 'ООО "Карточка клиента"',
        "ДатаРегистрации": "2012-04-10",
        "ДатаЛиквидации": None,
        "АдресЮридический": "г. Москва, тестовый адрес",
        "spp_data": {
            "ИдентификаторСПП": 30852759,
            "Регион": "Москва",
            "Предприниматель": False,
            "Директор.Фамилия": "Иванов",
            "Директор.Имя": "Иван",
            "Директор.Отчество": "Иванович",
            "Директор.ИНН": "500100732259",
        },
        "extra_data": {
            "Контрагент.GetPersonalisedContacts": [
                {
                    "Phones": ["+79990000001", "+79990000002"],
                    "Emails": ["card@example.test"],
                }
            ]
        },
    }
    card.update(changes)
    return card


class NewClientStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_directory.name) / "data" / "clients.db"
        self.storage = NewClientStorage(database_path)
        self.storage.initialize()

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_saves_fields_and_sbis_contacts_from_converted_record(self) -> None:
        saved = self.storage.upsert_from_sbis(sbis_client())

        self.assertEqual(saved.spp_id, 30852759)
        self.assertEqual(saved.inn, "7736641983")
        self.assertEqual(saved.registration_date, "2012-04-10")
        self.assertEqual(saved.director_last_name, "Иванов")
        self.assertEqual(saved.sbis_phones, ("+79990000001",))
        self.assertEqual(saved.sbis_emails, ("office@example.test",))
        self.assertEqual(saved.status, ProcessingStatus.QUEUED)

    def test_saves_company_card_and_marks_personalised_contacts_separately(self) -> None:
        saved = self.storage.upsert_from_company_card(company_card())

        self.assertEqual(saved.name, 'ООО "Карточка клиента"')
        self.assertEqual(saved.legal_address, "г. Москва, тестовый адрес")
        self.assertEqual(saved.director_last_name, "Иванов")
        self.assertEqual(saved.director_inn, "500100732259")
        self.assertEqual(
            saved.personalised_phones,
            ("+79990000001", "+79990000002"),
        )
        self.assertEqual(saved.personalised_emails, ("card@example.test",))
        self.assertEqual(saved.sbis_phones, ())
        self.assertEqual(saved.telegram_phones, ())

    def test_company_card_prefers_director_from_head_data(self) -> None:
        card = company_card(
            head_data={
                "spp_data": {
                    "Директор.Фамилия": "Петров",
                    "Директор.Имя": "Пётр",
                    "Директор.Отчество": "Петрович",
                    "Директор.ИНН": "7715964180",
                }
            }
        )

        saved = self.storage.upsert_from_company_card(card)

        self.assertEqual(saved.director_last_name, "Петров")
        self.assertEqual(saved.director_first_name, "Пётр")
        self.assertEqual(saved.director_middle_name, "Петрович")
        self.assertEqual(saved.director_inn, "7715964180")

    def test_company_card_uses_root_spp_data_without_head_data(self) -> None:
        saved = self.storage.upsert_from_company_card(company_card())

        self.assertEqual(saved.director_last_name, "Иванов")
        self.assertEqual(saved.director_first_name, "Иван")
        self.assertEqual(saved.director_middle_name, "Иванович")
        self.assertEqual(saved.director_inn, "500100732259")

    def test_company_card_saves_contractor_uuid(self) -> None:
        contractor_uuid = "40bc4f3e-92a4-11f1-81b4-057c77c03283"

        saved = self.storage.upsert_from_company_card(
            company_card(),
            contractor_uuid=contractor_uuid,
        )

        self.assertEqual(saved.contractor_uuid, contractor_uuid)

    def test_uuid_backfill_does_not_replace_existing_value(self) -> None:
        original_uuid = "40bc4f3e-92a4-11f1-81b4-057c77c03283"
        another_uuid = "4ef36ad8-b640-4d88-b038-ef456183521b"
        self.storage.upsert_from_company_card(
            company_card(), contractor_uuid=original_uuid
        )

        updated = self.storage.set_contractor_uuid_if_missing(
            30852759, another_uuid
        )

        self.assertFalse(updated)
        self.assertEqual(self.storage.get(30852759).contractor_uuid, original_uuid)

    def test_director_inn_backfill_updates_only_empty_value(self) -> None:
        card = company_card()
        card["spp_data"] = dict(card["spp_data"])
        card["spp_data"].pop("Директор.ИНН")
        self.storage.upsert_from_company_card(
            card,
            contractor_uuid="40bc4f3e-92a4-11f1-81b4-057c77c03283",
        )

        first_update = self.storage.set_director_inn_if_missing(
            30852759, "500100732259"
        )
        second_update = self.storage.set_director_inn_if_missing(
            30852759, "7715964180"
        )

        self.assertTrue(first_update)
        self.assertFalse(second_update)
        self.assertEqual(self.storage.get(30852759).director_inn, "500100732259")

    def test_director_backfill_fills_missing_names_without_overwrite(self) -> None:
        card = company_card()
        card["spp_data"] = dict(card["spp_data"])
        card["spp_data"].pop("Директор.Имя")
        card["spp_data"].pop("Директор.Отчество")
        card["spp_data"].pop("Директор.ИНН")
        self.storage.upsert_from_company_card(card)

        updated = self.storage.set_director_fields_if_missing(
            30852759,
            last_name="Петров",
            first_name="Пётр",
            middle_name="Петрович",
            director_inn="500100732259",
        )

        saved = self.storage.get(30852759)
        self.assertTrue(updated)
        self.assertEqual(saved.director_last_name, "Иванов")
        self.assertEqual(saved.director_first_name, "Пётр")
        self.assertEqual(saved.director_middle_name, "Петрович")
        self.assertEqual(saved.director_inn, "500100732259")

    def test_director_backfill_selects_missing_last_name_with_existing_inn(self) -> None:
        card = company_card()
        card["spp_data"] = dict(card["spp_data"])
        card["spp_data"].pop("Директор.Фамилия")
        self.storage.upsert_from_company_card(
            card,
            contractor_uuid="40bc4f3e-92a4-11f1-81b4-057c77c03283",
        )

        selected = self.storage.list_without_director_inn()

        self.assertEqual([client.spp_id for client in selected], [30852759])
        self.assertEqual(selected[0].director_inn, "500100732259")
        self.assertIsNone(selected[0].director_last_name)

    def test_repeat_company_card_import_preserves_telegram_state(self) -> None:
        self.storage.upsert_from_company_card(company_card())
        self.storage.replace_telegram_contacts(30852759, phones=["+79991112233"])
        self.storage.set_status(30852759, ProcessingStatus.PROCESSED)

        updated = company_card()
        updated["extra_data"] = {
            "Контрагент.GetPersonalisedContacts": [
                {"Phones": ["+79990000003"], "Emails": []}
            ]
        }
        saved = self.storage.upsert_from_company_card(updated)

        self.assertEqual(saved.personalised_phones, ("+79990000003",))
        self.assertEqual(saved.telegram_phones, ("+79991112233",))
        self.assertEqual(saved.status, ProcessingStatus.PROCESSED)

    def test_company_card_import_preserves_report_fields(self) -> None:
        self.storage.upsert_from_company_card(company_card())
        with closing(sqlite3.connect(self.storage.database_path)) as connection:
            connection.execute(
                """
                UPDATE new_clients
                SET report_id = ?, reported_at = ?
                WHERE spp_id = ?
                """,
                ("weekly-2026-08-09", "2026-08-09 09:00:00", 30852759),
            )
            connection.commit()

        saved = self.storage.upsert_from_company_card(company_card())

        self.assertEqual(saved.report_id, "weekly-2026-08-09")
        self.assertEqual(saved.reported_at, "2026-08-09 09:00:00")

    def test_repeat_import_updates_sbis_but_preserves_telegram_and_status(self) -> None:
        self.storage.upsert_from_sbis(sbis_client())
        self.storage.replace_telegram_contacts(
            30852759,
            phones=["+79991112233"],
            emails=["owner@example.test"],
        )
        self.storage.set_status(30852759, ProcessingStatus.PROCESSED)

        saved = self.storage.upsert_from_sbis(
            sbis_client(Название='ООО "Новое название"', Телефон="+79990000002")
        )

        self.assertEqual(saved.name, 'ООО "Новое название"')
        self.assertEqual(saved.sbis_phones, ("+79990000002",))
        self.assertEqual(saved.telegram_phones, ("+79991112233",))
        self.assertEqual(saved.telegram_emails, ("owner@example.test",))
        self.assertEqual(saved.status, ProcessingStatus.PROCESSED)

    def test_processing_list_excludes_processed_and_skipped(self) -> None:
        records = [
            sbis_client(ИдентификаторСПП=1),
            sbis_client(ИдентификаторСПП=2),
            sbis_client(ИдентификаторСПП=3),
            sbis_client(ИдентификаторСПП=4),
        ]
        self.storage.save_sbis_list(records)
        self.storage.set_status(1, ProcessingStatus.PROCESSED)
        self.storage.set_status(2, ProcessingStatus.SKIPPED)
        self.storage.set_status(4, ProcessingStatus.RETRY_REQUIRED)

        pending = self.storage.list_for_processing()

        self.assertEqual([client.spp_id for client in pending], [3, 4])

    def test_report_selection_reads_only_requested_registration_date(self) -> None:
        self.storage.save_sbis_list(
            [
                sbis_client(
                    ИдентификаторСПП=1,
                    Название="Бета",
                    ДатаРегистрации="2026-08-01",
                ),
                sbis_client(
                    ИдентификаторСПП=2,
                    Название="Альфа",
                    ДатаРегистрации="2026-08-01T15:20:00",
                ),
                sbis_client(
                    ИдентификаторСПП=3,
                    ДатаРегистрации="2026-08-02",
                ),
            ]
        )

        selected = self.storage.list_by_registration_date("2026-08-01")

        self.assertEqual([client.spp_id for client in selected], [2, 1])

    def test_registration_date_stats_count_processed_and_total(self) -> None:
        self.storage.save_sbis_list(
            [
                sbis_client(
                    ИдентификаторСПП=1, ДатаРегистрации="2026-08-07"
                ),
                sbis_client(
                    ИдентификаторСПП=2, ДатаРегистрации="2026-08-07"
                ),
                sbis_client(
                    ИдентификаторСПП=3, ДатаРегистрации="2026-08-08"
                ),
            ]
        )
        self.storage.set_status(1, ProcessingStatus.PROCESSED)
        self.storage.set_status(2, ProcessingStatus.SKIPPED)

        stats = self.storage.registration_date_stats(
            "2026-08-01", "2026-08-31"
        )

        self.assertEqual(
            [
                (item.registration_date, item.processed, item.total)
                for item in stats
            ],
            [("2026-08-08", 0, 1), ("2026-08-07", 1, 2)],
        )

    def test_returns_latest_attempts_and_marks_clients_reported(self) -> None:
        self.storage.save_sbis_list(
            [sbis_client(ИдентификаторСПП=1), sbis_client(ИдентификаторСПП=2)]
        )
        for result_code in ("first", "second"):
            self.storage.save_telegram_result(
                1,
                status=ProcessingStatus.RETRY_REQUIRED,
                stage="test",
                result_code=result_code,
            )

        attempts = self.storage.latest_attempts_for_clients([1, 2])
        self.storage.mark_reported([1, 2], "daily-2026-08-01")

        self.assertEqual(attempts[1].result_code, "second")
        self.assertNotIn(2, attempts)
        self.assertEqual(self.storage.get(1).report_id, "daily-2026-08-01")
        self.assertIsNotNone(self.storage.get(2).reported_at)

    def test_lists_only_clients_changed_after_previous_report(self) -> None:
        self.storage.save_sbis_list(
            [sbis_client(ИдентификаторСПП=1), sbis_client(ИдентификаторСПП=2)]
        )
        self.storage.mark_reported([1, 2], "1")
        self.storage.set_status(1, ProcessingStatus.PROCESSED)

        updates = self.storage.list_report_updates()

        self.assertEqual([client.spp_id for client in updates], [1])

    def test_report_updates_detect_revision_even_in_same_second(self) -> None:
        self.storage.save_sbis_list([sbis_client(ИдентификаторСПП=1)])
        self.storage.mark_reported([1], "1")
        self.storage.replace_telegram_contacts(1, phones=["+79991112233"])
        with closing(sqlite3.connect(self.storage.database_path)) as connection:
            connection.execute(
                """
                UPDATE new_clients
                SET updated_at = reported_at
                WHERE spp_id = 1
                """
            )
            connection.commit()

        updates = self.storage.list_report_updates()

        self.assertEqual([client.spp_id for client in updates], [1])

    def test_repeated_mark_reported_closes_current_revision(self) -> None:
        self.storage.save_sbis_list([sbis_client(ИдентификаторСПП=1)])
        self.storage.mark_reported([1], "main")
        self.storage.set_status(1, ProcessingStatus.PROCESSED)
        self.assertEqual(
            [client.spp_id for client in self.storage.list_report_updates()],
            [1],
        )

        self.storage.mark_reported([1], "supplement")

        self.assertEqual(self.storage.list_report_updates(), [])
        client = self.storage.get(1)
        self.assertEqual(client.report_id, "supplement")
        self.assertEqual(client.reported_revision, client.data_revision)

    def test_meaningful_public_updates_increment_data_revision(self) -> None:
        created = self.storage.upsert_from_sbis(sbis_client())
        self.assertEqual(created.data_revision, 0)

        from_sbis = self.storage.upsert_from_sbis(sbis_client())
        from_card = self.storage.upsert_from_company_card(company_card())
        telegram_contacts = self.storage.replace_telegram_contacts(
            30852759, phones=["+79991112233"]
        )
        telegram_result = self.storage.save_telegram_result(
            30852759,
            status=ProcessingStatus.PROCESSED,
            stage="test",
            result_code="found",
        )
        status_update = self.storage.set_status(
            30852759, ProcessingStatus.SKIPPED
        )

        self.assertEqual(from_sbis.data_revision, 1)
        self.assertEqual(from_card.data_revision, 2)
        self.assertEqual(telegram_contacts.data_revision, 3)
        self.assertEqual(telegram_result.data_revision, 4)
        self.assertEqual(status_update.data_revision, 5)

    def test_mark_reported_does_not_change_data_or_updated_at(self) -> None:
        self.storage.upsert_from_sbis(sbis_client(ИдентификаторСПП=1))
        with closing(sqlite3.connect(self.storage.database_path)) as connection:
            connection.execute(
                "UPDATE new_clients SET updated_at = ? WHERE spp_id = 1",
                ("2000-01-01 00:00:00",),
            )
            connection.commit()

        self.storage.mark_reported([1], "main")

        with closing(sqlite3.connect(self.storage.database_path)) as connection:
            row = connection.execute(
                """
                SELECT updated_at, data_revision, reported_revision
                FROM new_clients WHERE spp_id = 1
                """
            ).fetchone()
        self.assertEqual(row[0], "2000-01-01 00:00:00")
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 0)

    def test_mark_reported_does_not_hide_a_concurrent_data_change(self) -> None:
        snapshot = self.storage.upsert_from_sbis(
            sbis_client(ИдентификаторСПП=1)
        )
        changed = self.storage.set_status(1, ProcessingStatus.PROCESSED)

        self.storage.mark_reported(
            [1],
            "stale-report",
            expected_revisions={1: snapshot.data_revision},
            expected_reported_revisions={1: snapshot.reported_revision},
        )

        self.assertIsNone(self.storage.get(1).reported_at)
        self.storage.mark_reported(
            [1],
            "current-report",
            expected_revisions={1: changed.data_revision},
            expected_reported_revisions={1: changed.reported_revision},
        )
        self.assertEqual(self.storage.get(1).report_id, "current-report")

    def test_mark_reported_uses_reported_revision_as_compare_and_swap(self) -> None:
        snapshot = self.storage.upsert_from_sbis(
            sbis_client(ИдентификаторСПП=1)
        )
        self.storage.mark_reported(
            [1],
            "winner",
            expected_revisions={1: snapshot.data_revision},
            expected_reported_revisions={1: snapshot.reported_revision},
        )

        self.storage.mark_reported(
            [1],
            "stale-worker",
            expected_revisions={1: snapshot.data_revision},
            expected_reported_revisions={1: snapshot.reported_revision},
        )

        self.assertEqual(self.storage.get(1).report_id, "winner")

    def test_claim_and_release_do_not_change_data_revision(self) -> None:
        created = self.storage.upsert_from_sbis(
            sbis_client(ИдентификаторСПП=1)
        )

        claimed = self.storage.claim_for_processing(1, "worker")[0]
        released = self.storage.release_claim(1, "worker")

        self.assertEqual(created.data_revision, 0)
        self.assertEqual(claimed.data_revision, 0)
        self.assertEqual(released.data_revision, 0)

    def test_report_update_hydration_does_not_use_n_plus_one_queries(self) -> None:
        self.storage.save_sbis_list(
            [
                sbis_client(ИдентификаторСПП=1),
                sbis_client(ИдентификаторСПП=2),
                sbis_client(ИдентификаторСПП=3),
            ]
        )
        self.storage.mark_reported([1, 2, 3], "main")
        for spp_id in (1, 2, 3):
            self.storage.replace_telegram_contacts(
                spp_id, phones=[f"+7999000000{spp_id}"]
            )

        class TracedStorage(NewClientStorage):
            def __init__(self, database_path):
                super().__init__(database_path)
                self.statements: list[str] = []

            @contextmanager
            def _connect(self):
                with super()._connect() as connection:
                    connection.set_trace_callback(self.statements.append)
                    yield connection

        traced = TracedStorage(self.storage.database_path)
        clients = traced.list_report_updates()
        select_count = sum(
            statement.lstrip().upper().startswith("SELECT")
            for statement in traced.statements
        )

        self.assertEqual([client.spp_id for client in clients], [1, 2, 3])
        self.assertEqual(select_count, 3)

    def test_lists_unreported_clients_only_through_cutoff_date(self) -> None:
        self.storage.save_sbis_list(
            [
                sbis_client(ИдентификаторСПП=1, ДатаРегистрации="2026-08-01"),
                sbis_client(ИдентификаторСПП=2, ДатаРегистрации="2026-08-02"),
                sbis_client(ИдентификаторСПП=3, ДатаРегистрации="2026-08-03"),
            ]
        )
        self.storage.mark_reported([1], "already-sent")

        clients = self.storage.list_unreported_through("2026-08-02")

        self.assertEqual([client.spp_id for client in clients], [2])

    def test_manual_review_is_not_returned_for_automatic_processing(self) -> None:
        self.storage.upsert_from_sbis(sbis_client())

        saved = self.storage.set_status(
            30852759, ProcessingStatus.NEEDS_REVIEW
        )

        self.assertEqual(saved.status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(self.storage.list_for_processing(), [])

    def test_saves_telegram_contacts_status_and_attempt_atomically(self) -> None:
        self.storage.upsert_from_sbis(sbis_client())

        saved = self.storage.save_telegram_result(
            30852759,
            phones=["+79991112233"],
            emails=["owner@example.test"],
            status=ProcessingStatus.PROCESSED,
            stage="inn_report",
            result_code="phone_found",
        )

        self.assertEqual(saved.telegram_phones, ("+79991112233",))
        self.assertEqual(saved.telegram_emails, ("owner@example.test",))
        self.assertEqual(saved.status, ProcessingStatus.PROCESSED)

        attempt = self.storage.latest_telegram_attempt(
            result_code="phone_found"
        )
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.client_spp_id, 30852759)
        self.assertEqual(attempt.attempt_number, 1)
        self.assertEqual(attempt.stage, "inn_report")

    def test_initialize_migrates_existing_database_without_review_column(self) -> None:
        self.temp_directory.cleanup()
        self.temp_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_directory.name) / "legacy.db"
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                """
                CREATE TABLE new_clients (
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
                    report_id TEXT,
                    reported_at TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                INSERT INTO new_clients (
                    spp_id, name, inn, is_entrepreneur,
                    report_id, reported_at
                ) VALUES (1, 'Историческая организация', '7736641983', 0,
                          'old-report', '2026-08-01 10:00:00')
                """
            )
            connection.commit()
        storage = NewClientStorage(database_path)

        storage.initialize()

        with closing(sqlite3.connect(database_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(new_clients)")
            }
        self.assertIn("needs_review", columns)
        self.assertIn("telegram_claim_token", columns)
        self.assertIn("telegram_claimed_at", columns)
        self.assertIn("legal_address", columns)
        self.assertIn("director_inn", columns)
        self.assertIn("report_id", columns)
        self.assertIn("reported_at", columns)
        self.assertIn("data_revision", columns)
        self.assertIn("reported_revision", columns)
        with closing(sqlite3.connect(database_path)) as connection:
            revisions = connection.execute(
                """
                SELECT data_revision, reported_revision
                FROM new_clients WHERE spp_id = 1
                """
            ).fetchone()
        self.assertEqual(revisions, (0, 0))
        self.assertEqual(storage.list_report_updates(), [])

    def test_concurrent_tokens_cannot_claim_the_same_client(self) -> None:
        self.storage.upsert_from_sbis(sbis_client())
        barrier = Barrier(2)

        def claim(token: str) -> list[int]:
            competing_storage = NewClientStorage(self.storage.database_path)
            barrier.wait()
            return [
                client.spp_id
                for client in competing_storage.claim_for_processing(1, token)
            ]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ("worker-a", "worker-b")))

        self.assertEqual(sorted(len(result) for result in results), [0, 1])
        self.assertEqual([item for result in results for item in result], [30852759])
        self.assertEqual(self.storage.list_for_processing(), [])

    def test_stale_claim_can_be_recovered_by_another_worker(self) -> None:
        self.storage.upsert_from_sbis(sbis_client())
        self.storage.claim_for_processing(1, "old-worker")
        with closing(sqlite3.connect(self.storage.database_path)) as connection:
            connection.execute(
                """
                UPDATE new_clients
                SET telegram_claimed_at = '2000-01-01 00:00:00'
                WHERE spp_id = ?
                """,
                (30852759,),
            )
            connection.commit()

        claimed = self.storage.claim_for_processing(
            1,
            "new-worker",
            stale_after_seconds=900,
        )

        self.assertEqual([client.spp_id for client in claimed], [30852759])
        with closing(sqlite3.connect(self.storage.database_path)) as connection:
            token = connection.execute(
                "SELECT telegram_claim_token FROM new_clients WHERE spp_id = ?",
                (30852759,),
            ).fetchone()[0]
        self.assertEqual(token, "new-worker")

    def test_save_with_matching_claim_clears_claim_atomically(self) -> None:
        self.storage.upsert_from_sbis(sbis_client())
        self.storage.claim_for_processing(1, "worker-a")

        saved = self.storage.save_telegram_result(
            30852759,
            phones=["+79991112233"],
            status=ProcessingStatus.PROCESSED,
            stage="inn_report",
            result_code="phone_found",
            claim_token="worker-a",
        )

        self.assertEqual(saved.status, ProcessingStatus.PROCESSED)
        with closing(sqlite3.connect(self.storage.database_path)) as connection:
            claim = connection.execute(
                """
                SELECT telegram_claim_token, telegram_claimed_at
                FROM new_clients WHERE spp_id = ?
                """,
                (30852759,),
            ).fetchone()
        self.assertEqual(claim, (None, None))

    def test_save_cannot_bypass_an_active_claim(self) -> None:
        self.storage.upsert_from_sbis(sbis_client())
        self.storage.claim_for_processing(1, "worker-a")

        with self.assertRaisesRegex(TelegramClaimError, "закреплён"):
            self.storage.save_telegram_result(
                30852759,
                status=ProcessingStatus.PROCESSED,
                stage="inn_report",
                result_code="phone_found",
            )
        with self.assertRaisesRegex(TelegramClaimError, "не совпадает"):
            self.storage.save_telegram_result(
                30852759,
                status=ProcessingStatus.PROCESSED,
                stage="inn_report",
                result_code="phone_found",
                claim_token="worker-b",
            )

        self.assertEqual(
            self.storage.get(30852759).status,
            ProcessingStatus.QUEUED,
        )

    def test_release_claim_requires_owner_and_returns_client_to_queue(self) -> None:
        self.storage.upsert_from_sbis(sbis_client())
        self.storage.claim_for_processing(1, "worker-a")

        with self.assertRaisesRegex(TelegramClaimError, "не принадлежит"):
            self.storage.release_claim(30852759, "worker-b")

        released = self.storage.release_claim(30852759, "worker-a")

        self.assertEqual(released.spp_id, 30852759)
        self.assertEqual(
            [client.spp_id for client in self.storage.list_for_processing()],
            [30852759],
        )

    def test_rejects_invalid_sbis_record_without_creating_client(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.upsert_from_sbis(sbis_client(ИНН="123"))

        self.assertIsNone(self.storage.get(30852759))


if __name__ == "__main__":
    unittest.main()
