import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from src.cli.sbis_clients_console import printClientsTable


class PrintClientsTableTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_full_list_but_prints_only_ten_rows(self) -> None:
        clients = [
            {"ИНН": f"25000000{index:02d}", "Название": f"Организация {index}"}
            for index in range(11)
        ]

        output = io.StringIO()
        with (
            patch(
                "src.cli.sbis_clients_console.getClientsByListId",
                new=AsyncMock(return_value=clients),
            ) as get_clients,
            redirect_stdout(output),
        ):
            result = await printClientsTable(100451)

        get_clients.assert_awaited_once_with(100451)
        self.assertIs(result, clients)
        self.assertIn("Организация 9", output.getvalue())
        self.assertNotIn("Организация 10", output.getvalue())
        self.assertIn("Получено клиентов: 11. Показано: 10.", output.getvalue())
