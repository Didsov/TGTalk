import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from main import debugList, parseBotReportUrl


class DebugListTests(unittest.IsolatedAsyncioTestCase):
    async def test_prints_each_client_and_returns_inns(self) -> None:
        clients = [
            {"ИНН": "7707083893", "Название": "Организация один"},
            {"ИНН": "500100732259", "Name": "Организация два"},
        ]
        contacts = {
            "7707083893": [
                {"type": "phone", "value": "+7 (900) 000-00-01", "masked": False}
            ],
            "500100732259": [
                {"type": "email", "value": "example@example.test", "masked": False}
            ],
        }

        output = io.StringIO()
        with (
            patch("main.getClientsByListId", new=AsyncMock(return_value=clients)),
            patch(
                "main.getContactsByInn",
                new=AsyncMock(side_effect=lambda inn: contacts[inn]),
            ) as get_contacts,
            redirect_stdout(output),
        ):
            result = await debugList(93332)

        self.assertEqual(result, ["7707083893", "500100732259"])
        self.assertEqual(get_contacts.await_count, 2)
        self.assertIn(
            "7707083893 | Организация один | phone: +7 (900) 000-00-01",
            output.getvalue(),
        )
        self.assertIn(
            "500100732259 | Организация два | email: example@example.test",
            output.getvalue(),
        )

    async def test_skips_contact_request_when_inn_is_missing(self) -> None:
        clients = [{"Название": "Без ИНН"}]

        output = io.StringIO()
        with (
            patch("main.getClientsByListId", new=AsyncMock(return_value=clients)),
            patch("main.getContactsByInn", new=AsyncMock()) as get_contacts,
            redirect_stdout(output),
        ):
            result = await debugList(93332)

        self.assertEqual(result, [])
        get_contacts.assert_not_awaited()
        self.assertIn("<запрос контактов пропущен>", output.getvalue())


class ParseBotReportUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_downloads_and_parses_report_using_summary_inn(self) -> None:
        report_text = (
            "=== Общая сводка ===\n"
            "ИНН: 7707083893\n"
            "Телефон: 8 (900) 111-22-33\n"
            "Email: owner@example.test\n"
        )

        with patch(
            "main.download_report_text",
            new=AsyncMock(return_value=report_text),
        ) as download:
            result = await parseBotReportUrl("https://reports.example.test/r/id")

        download.assert_awaited_once_with("https://reports.example.test/r/id")
        self.assertEqual(result.phones, ("+79001112233",))
        self.assertEqual(result.emails, ("owner@example.test",))
