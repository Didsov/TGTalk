import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from src.cli.sbis_contacts_console import printContactsByInn


class PrintContactsByInnTests(unittest.IsolatedAsyncioTestCase):
    async def test_prints_contacts_and_returns_them(self) -> None:
        contacts = [
            {
                "type": "phone",
                "value": "+7 (900) 000-00-01",
                "masked": False,
            },
            {
                "type": "email",
                "value": "example@example.test",
                "masked": False,
            },
        ]

        output = io.StringIO()
        with (
            patch(
                "src.cli.sbis_contacts_console.getContactsByInn",
                new=AsyncMock(return_value=contacts),
            ) as get_contacts,
            redirect_stdout(output),
        ):
            result = await printContactsByInn("500100732259")

        get_contacts.assert_awaited_once_with("500100732259")
        self.assertIs(result, contacts)
        self.assertIn("phone | +7 (900) 000-00-01", output.getvalue())
        self.assertIn("Получено контактов: 2.", output.getvalue())

    async def test_reports_when_contacts_are_missing(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "src.cli.sbis_contacts_console.getContactsByInn",
                new=AsyncMock(return_value=[]),
            ),
            redirect_stdout(output),
        ):
            result = await printContactsByInn("500100732259")

        self.assertEqual(result, [])
        self.assertIn("контакты не найдены", output.getvalue())

