import unittest
from os import environ
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.integrations.telegram.bot_client import (
    closeTg,
    openTg,
    sendMessageAndWait,
)


class SendMessageAndWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_text_and_returns_response(self) -> None:
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.get_response.return_value = SimpleNamespace(raw_text="246")

        client = Mock()
        client.conversation.return_value = conversation

        result = await sendMessageAndWait(client, "@test_bot", "123")

        client.conversation.assert_called_once_with("@test_bot", timeout=30)
        conversation.send_message.assert_awaited_once_with("123")
        conversation.get_response.assert_awaited_once_with()
        self.assertEqual(result, "246")

    async def test_rejects_empty_message(self) -> None:
        client = Mock()

        with self.assertRaisesRegex(ValueError, "пустое сообщение"):
            await sendMessageAndWait(client, "@test_bot", "")

        client.conversation.assert_not_called()


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_tg_creates_and_starts_client(self) -> None:
        settings = {
            "TELEGRAM_API_ID": "12345",
            "TELEGRAM_API_HASH": "test_hash",
            "TELEGRAM_PHONE": "+79990000000",
        }
        client = Mock()
        client.start = AsyncMock()

        with (
            patch.dict(environ, settings, clear=True),
            patch("src.config.load_dotenv"),
            patch(
                "src.integrations.telegram.bot_client.TelegramClient",
                return_value=client,
            ) as client_class,
        ):
            result = await openTg()

        client_class.assert_called_once()
        client.start.assert_awaited_once_with(phone="+79990000000")
        self.assertIs(result, client)

    async def test_close_tg_disconnects_client(self) -> None:
        client = Mock()
        client.disconnect = AsyncMock()

        await closeTg(client)

        client.disconnect.assert_awaited_once_with()
