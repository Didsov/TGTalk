import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.storage.organizations import OrganizationStorage


class OrganizationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "data" / "clients.db"
        self.storage = OrganizationStorage(self.database_path)
        self.storage.initialize()

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def test_add_and_get_organization_with_contact_lists(self) -> None:
        saved = self.storage.add(
            inn="7707083893",
            kpp="773601001",
            name='ООО "Пример"',
            head_name="Иванов Иван Иванович",
            phones=["+79990000001", "+79990000002", "+79990000001"],
            emails=["info@example.test", "director@example.test"],
        )

        loaded = self.storage.get(saved.id)

        self.assertEqual(loaded, saved)
        self.assertEqual(saved.phones, ("+79990000001", "+79990000002"))
        self.assertEqual(
            saved.emails, ("info@example.test", "director@example.test")
        )

    def test_find_by_inn_returns_multiple_cards(self) -> None:
        for kpp in ("773601001", "773602001"):
            self.storage.add(
                inn="7707083893",
                kpp=kpp,
                name="ООО Филиал",
                head_name="Петров Петр Петрович",
            )

        found = self.storage.find_by_inn("7707083893")

        self.assertEqual([item.kpp for item in found], ["773601001", "773602001"])

    def test_individual_entrepreneur_can_have_no_kpp(self) -> None:
        saved = self.storage.add(
            inn="500100732259",
            kpp=None,
            name="ИП Сидоров С.С.",
            head_name="Сидоров Сергей Сергеевич",
        )

        self.assertIsNone(saved.kpp)

    def test_rejects_invalid_requisites_without_partial_insert(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.add(
                inn="123",
                kpp="123",
                name="ООО Ошибка",
                head_name="Иванов Иван Иванович",
            )

        with closing(sqlite3.connect(self.database_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
