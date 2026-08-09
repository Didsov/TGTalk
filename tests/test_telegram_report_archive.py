import json
import tempfile
import unittest
from pathlib import Path

from src.storage.telegram_reports import TelegramReportArchive


class TelegramReportArchiveTests(unittest.TestCase):
    def test_saves_report_and_query_metadata_without_full_query_in_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = TelegramReportArchive(directory)
            metadata_path = archive.record(
                client_spp_id=101,
                client_name="ООО Тест",
                query_kind="inn",
                query_text="/inn 7700000016",
                outcome="report_saved",
                report_text="Тестовый отчёт",
            )

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            report_path = metadata_path.parent / metadata["report_file"]

            self.assertEqual(metadata["client_spp_id"], 101)
            self.assertEqual(metadata["client_name"], "ООО Тест")
            self.assertEqual(metadata["query"], "/inn 7700000016")
            self.assertEqual(report_path.read_text(encoding="utf-8"), "Тестовый отчёт")
            self.assertNotIn("7700000016", metadata_path.name)

    def test_no_response_creates_metadata_without_fake_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata_path = TelegramReportArchive(directory).record(
                client_spp_id=102,
                client_name="ИП Тестов",
                query_kind="email",
                query_text="owner@example.test",
                outcome="no_response",
            )

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["outcome"], "no_response")
            self.assertIsNone(metadata["report_file"])
            self.assertIsNone(metadata["response_file"])
            self.assertEqual(
                sorted(path.suffix for path in metadata_path.parent.iterdir()),
                [".json"],
            )

    def test_response_without_report_is_saved_for_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata_path = TelegramReportArchive(directory).record(
                client_spp_id=103,
                client_name="ООО Тест",
                query_kind="person",
                query_text="Тестов Тест Тестович 01.01.1990",
                outcome="not_found",
                response_text="Ничего не найдено",
            )

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            response_path = metadata_path.parent / metadata["response_file"]
            self.assertEqual(
                response_path.read_text(encoding="utf-8"), "Ничего не найдено"
            )
