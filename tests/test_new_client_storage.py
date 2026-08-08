import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
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
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
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
