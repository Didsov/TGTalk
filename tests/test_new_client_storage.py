import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.storage.new_clients import NewClientStorage, ProcessingStatus


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

    def test_rejects_invalid_sbis_record_without_creating_client(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.upsert_from_sbis(sbis_client(ИНН="123"))

        self.assertIsNone(self.storage.get(30852759))


if __name__ == "__main__":
    unittest.main()
