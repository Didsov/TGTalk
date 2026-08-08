import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.cli.telegram_last_message import (
    LastMessageInspection,
    inspect_last_message,
    print_inspection,
)
from src.integrations.telegram.bot_client import BotResponseKind
from src.storage import NewClient, ProcessingStatus, TelegramSearchAttempt


def client() -> NewClient:
    return NewClient(
        spp_id=101,
        name="ООО Тест",
        region=None,
        ogrn=None,
        inn="7707083893",
        kpp="770001001",
        is_entrepreneur=False,
        registration_date=None,
        liquidation_date=None,
        director_last_name="Тестов",
        director_first_name="Тест",
        director_middle_name="Тестович",
        sbis_phones=(),
        telegram_phones=(),
        sbis_emails=(),
        telegram_emails=(),
        status=ProcessingStatus.NEEDS_REVIEW,
    )


class LastMessageInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_reads_last_message_without_sending_or_writing(self) -> None:
        storage = Mock()
        storage.latest_telegram_attempt.return_value = TelegramSearchAttempt(
            client_spp_id=101,
            attempt_number=1,
            stage="inn_query",
            result_code="report_url_missing",
            error_code=None,
            created_at="2026-08-08 11:08:22",
        )
        storage.get.return_value = client()
        telegram_client = Mock()
        message = SimpleNamespace(id=77, raw_text="Готово", text="Готово")
        buttons = [SimpleNamespace(text="Отчёт", url="https://report/r/1", data=None)]
        report_text = (
            "=== Общая сводка ===\n"
            "ИНН: 7707083893\n"
            "Телефон: 8 (900) 111-22-33\n"
        )
        with (
            patch("src.cli.telegram_last_message.loadEnvironment"),
            patch(
                "src.cli.telegram_last_message.requireSetting",
                return_value="@paid_bot",
            ),
            patch(
                "src.cli.telegram_last_message.allowed_report_hosts",
                return_value=("*.report.test",),
            ),
            patch(
                "src.cli.telegram_last_message.NewClientStorage",
                return_value=storage,
            ),
            patch(
                "src.cli.telegram_last_message.openTg",
                new=AsyncMock(return_value=telegram_client),
            ),
            patch(
                "src.cli.telegram_last_message.closeTg",
                new=AsyncMock(),
            ),
            patch(
                "src.cli.telegram_last_message.getLatestIncomingMessage",
                new=AsyncMock(return_value=message),
            ) as get_latest,
            patch(
                "src.cli.telegram_last_message.getMessageButtons",
                new=AsyncMock(return_value=buttons),
            ),
            patch(
                "src.cli.telegram_last_message.extractReportUrlAsync",
                new=AsyncMock(return_value="https://report/r/1"),
            ),
            patch(
                "src.cli.telegram_last_message.download_report_text",
                new=AsyncMock(return_value=report_text),
            ),
        ):
            result = await inspect_last_message("clients.db")

        get_latest.assert_awaited_once_with(
            telegram_client, "@paid_bot", limit=20
        )
        storage.save_telegram_result.assert_not_called()
        self.assertTrue(result.report_url_found)
        self.assertEqual(result.phones_found, 1)
        self.assertIsNone(result.saved_status)

    def test_prints_diagnostics_without_contacts(self) -> None:
        result = LastMessageInspection(
            client_spp_id=101,
            message_id=77,
            response_kind=BotResponseKind.OTHER,
            button_count=1,
            url_button_count=1,
            callback_button_count=0,
            report_url_found=True,
            phones_found=1,
            emails_found=0,
            candidate_status="ambiguous",
        )
        output = io.StringIO()

        print_inspection(result, output=output)

        text = output.getvalue()
        self.assertIn("URL-кнопок: 1", text)
        self.assertIn("БД не изменена", text)
        self.assertNotIn("+7900", text)


if __name__ == "__main__":
    unittest.main()
