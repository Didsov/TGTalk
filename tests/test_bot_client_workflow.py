import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.integrations.telegram.bot_client import (
    BotResponseKind,
    classifyBotResponse,
    clickRussiaAndWait,
    clickRussiaCallback,
    extractReportUrl,
    extractReportUrlAsync,
    getLatestIncomingMessage,
    sendMessageAndGetResponse,
    sendQueryAndWait,
    waitForResponseOrEdit,
)


class ResponseHelpersTests(unittest.TestCase):
    def test_classifies_explicit_not_found_response(self) -> None:
        message = SimpleNamespace(
            raw_text="К сожалению, по данному запросу ничего не найдено."
        )

        self.assertEqual(
            classifyBotResponse(message),
            BotResponseKind.NOT_FOUND,
        )

    def test_classifies_explicit_unknown_response(self) -> None:
        self.assertEqual(
            classifyBotResponse("Поиск завершён, но формат результата неизвестен."),
            BotResponseKind.UNKNOWN,
        )

    def test_classifies_rate_limit_as_retryable(self) -> None:
        self.assertEqual(
            classifyBotResponse("Слишком много запросов, попробуйте позже."),
            BotResponseKind.RETRYABLE_ERROR,
        )

    def test_does_not_treat_country_prompt_as_terminal(self) -> None:
        message = SimpleNamespace(raw_text="Выберите страну для поиска.")

        self.assertEqual(classifyBotResponse(message), BotResponseKind.OTHER)

    def test_extracts_report_url_without_clicking_button(self) -> None:
        callback = SimpleNamespace(text="Россия", url=None)
        report = SimpleNamespace(
            text="Открыть полный отчёт",
            url="https://reports.example.test/r/123",
            click=Mock(),
        )
        message = SimpleNamespace(buttons=[[callback], [report]])

        result = extractReportUrl(message)

        self.assertEqual(result, "https://reports.example.test/r/123")
        report.click.assert_not_called()

    def test_returns_none_without_url_button(self) -> None:
        message = SimpleNamespace(
            buttons=[[SimpleNamespace(text="Россия", url=None)]]
        )

        self.assertIsNone(extractReportUrl(message))


class AsyncMessageReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_url_from_lazily_loaded_buttons(self) -> None:
        message = SimpleNamespace(
            buttons=None,
            get_buttons=AsyncMock(
                return_value=[
                    [
                        SimpleNamespace(
                            text="Открыть полный отчёт",
                            url="https://reports.example.test/r/1",
                        )
                    ]
                ]
            ),
        )

        result = await extractReportUrlAsync(message)

        self.assertEqual(result, "https://reports.example.test/r/1")
        message.get_buttons.assert_awaited_once_with()

    async def test_reads_latest_incoming_without_sending(self) -> None:
        incoming = SimpleNamespace(id=2, out=False)
        client = SimpleNamespace(
            get_messages=AsyncMock(
                return_value=[SimpleNamespace(id=3, out=True), incoming]
            )
        )

        result = await getLatestIncomingMessage(client, "@target", limit=5)

        self.assertIs(result, incoming)
        client.get_messages.assert_awaited_once_with("@target", limit=5)


class ConversationHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_uses_existing_conversation_and_returns_message(self) -> None:
        sent = SimpleNamespace(id=10)
        response = SimpleNamespace(
            id=11,
            raw_text="Ответ",
            buttons=[[SimpleNamespace(text="Отчёт", url="https://reports.example/r/1")]],
        )
        never_edit = asyncio.get_running_loop().create_future()
        conversation = Mock()
        conversation.send_message = AsyncMock(return_value=sent)
        conversation.get_response = AsyncMock(return_value=response)
        conversation.get_edit.return_value = never_edit

        result = await sendQueryAndWait(conversation, "/inn 123", timeout=7)

        conversation.send_message.assert_awaited_once_with("/inn 123")
        conversation.get_response.assert_awaited_once_with(sent, timeout=7)
        self.assertTrue(never_edit.cancelled())
        self.assertIs(result, response)

    async def test_query_waits_for_edit_that_adds_report_button(self) -> None:
        sent = SimpleNamespace(id=12)
        intermediate = SimpleNamespace(id=13, raw_text="Поиск выполняется", buttons=[])
        edited = SimpleNamespace(
            id=13,
            raw_text="Отчёт готов",
            buttons=None,
            get_buttons=AsyncMock(
                return_value=[[SimpleNamespace(
                    text="Открыть отчёт",
                    url="https://reports.example/r/2",
                )]]
            ),
        )
        first_edit = asyncio.get_running_loop().create_future()
        second_response = asyncio.get_running_loop().create_future()
        conversation = Mock()
        conversation.send_message = AsyncMock(return_value=sent)
        conversation.get_response.side_effect = [
            asyncio.sleep(0, result=intermediate),
            second_response,
        ]
        conversation.get_edit.side_effect = [
            first_edit,
            asyncio.sleep(0, result=edited),
        ]

        result = await sendQueryAndWait(conversation, "/inn 123", timeout=1)

        self.assertIs(result, edited)
        self.assertEqual(conversation.get_response.call_count, 2)
        self.assertEqual(conversation.get_edit.call_count, 2)
        edited.get_buttons.assert_awaited_once_with()

    async def test_full_message_wrapper_opens_conversation(self) -> None:
        sent = SimpleNamespace(id=20)
        response = SimpleNamespace(
            id=21,
            raw_text="Ничего не найдено",
            buttons=[],
        )
        never_edit = asyncio.get_running_loop().create_future()
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.send_message.return_value = sent
        conversation.get_response.return_value = response
        conversation.get_edit.return_value = never_edit
        client = Mock()
        client.conversation.return_value = conversation

        result = await sendMessageAndGetResponse(
            client,
            "@target_bot",
            "/inn 123",
            timeout=9,
        )

        client.conversation.assert_called_once_with("@target_bot", timeout=9)
        conversation.get_response.assert_awaited_once_with(sent, timeout=9)
        self.assertIs(result, response)

    async def test_rejects_empty_query(self) -> None:
        conversation = Mock()

        with self.assertRaisesRegex(ValueError, "пустое сообщение"):
            await sendQueryAndWait(conversation, "")

    async def test_wait_returns_new_response_and_cancels_edit_waiter(self) -> None:
        response = SimpleNamespace(id=31, raw_text="Новый ответ")
        never_edit = asyncio.get_running_loop().create_future()
        conversation = Mock()
        conversation.get_response.return_value = asyncio.sleep(0, result=response)
        conversation.get_edit.return_value = never_edit
        prompt = SimpleNamespace(id=30)

        result = await waitForResponseOrEdit(conversation, prompt, timeout=1)

        self.assertIs(result, response)
        self.assertTrue(never_edit.cancelled())

    async def test_wait_returns_edited_message_and_cancels_response_waiter(self) -> None:
        edited = SimpleNamespace(id=41, raw_text="Ничего не найдено")
        never_response = asyncio.get_running_loop().create_future()
        conversation = Mock()
        conversation.get_response.return_value = never_response
        conversation.get_edit.return_value = asyncio.sleep(0, result=edited)
        prompt = SimpleNamespace(id=40)

        result = await waitForResponseOrEdit(conversation, prompt, timeout=1)

        self.assertIs(result, edited)
        conversation.get_edit.assert_called_once_with(timeout=1)
        self.assertTrue(never_response.cancelled())

    async def test_wait_times_out_and_cancels_both_waiters(self) -> None:
        response = asyncio.get_running_loop().create_future()
        edit = asyncio.get_running_loop().create_future()
        conversation = Mock()
        conversation.get_response.return_value = response
        conversation.get_edit.return_value = edit

        with self.assertRaisesRegex(asyncio.TimeoutError, "не прислал ответ"):
            await waitForResponseOrEdit(
                conversation,
                SimpleNamespace(id=50),
                timeout=0.01,
            )

        self.assertTrue(response.cancelled())
        self.assertTrue(edit.cancelled())

    async def test_wait_removes_cancelled_telethon_pending_future(self) -> None:
        response = SimpleNamespace(id=56, raw_text="Новый ответ")

        class ConversationLike:
            def __init__(self) -> None:
                self._pending_responses = {}
                self._pending_edits = {}

            def get_response(self, message, *, timeout):
                future = asyncio.get_running_loop().create_future()
                self._pending_responses[message.id] = future

                def resolve() -> None:
                    self._pending_responses.pop(message.id, None)
                    future.set_result(response)

                asyncio.get_running_loop().call_soon(resolve)
                return asyncio.wait_for(future, timeout)

            def get_edit(self, *, timeout):
                future = asyncio.get_running_loop().create_future()
                self._pending_edits[55] = future
                return asyncio.wait_for(future, timeout)

        conversation = ConversationLike()

        result = await waitForResponseOrEdit(
            conversation,
            SimpleNamespace(id=55),
            timeout=1,
        )

        self.assertIs(result, response)
        self.assertEqual(conversation._pending_responses, {})
        self.assertEqual(conversation._pending_edits, {})


class CallbackHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicks_russia_callback_by_text_not_position(self) -> None:
        kazakhstan = SimpleNamespace(
            text="Казахстан",
            data=b"country:kz",
            click=AsyncMock(),
        )
        russia = SimpleNamespace(
            text="🇷🇺 Россия",
            data=b"country:ru",
            click=AsyncMock(return_value="clicked"),
        )
        message = SimpleNamespace(buttons=[[kazakhstan, russia]])

        result = await clickRussiaCallback(message)

        self.assertEqual(result, "clicked")
        russia.click.assert_awaited_once_with()
        kazakhstan.click.assert_not_awaited()

    async def test_rejects_russia_url_button(self) -> None:
        message = SimpleNamespace(
            buttons=[
                [
                    SimpleNamespace(
                        text="Россия",
                        data=None,
                        url="https://example.test",
                        click=AsyncMock(),
                    )
                ]
            ]
        )

        with self.assertRaisesRegex(ValueError, "не является callback"):
            await clickRussiaCallback(message)

    async def test_click_and_wait_registers_waiters_before_callback(self) -> None:
        order: list[str] = []
        response = SimpleNamespace(
            id=61,
            raw_text="Готово",
            buttons=[
                [
                    SimpleNamespace(
                        text="Открыть отчёт",
                        url="https://reports.example.test/r/1",
                    )
                ]
            ],
        )

        async def get_response(*args, **kwargs):
            order.append("response_wait_registered")
            await asyncio.sleep(0)
            return response

        async def get_edit(*args, **kwargs):
            order.append("edit_wait_registered")
            await asyncio.Future()

        async def click():
            order.append("clicked")

        conversation = Mock()
        conversation.get_response = get_response
        conversation.get_edit = get_edit
        message = SimpleNamespace(
            id=60,
            buttons=[
                [
                    SimpleNamespace(
                        text="Россия",
                        data=b"country:ru",
                        click=click,
                    )
                ]
            ],
        )

        result = await clickRussiaAndWait(conversation, message, timeout=1)

        self.assertIs(result, response)
        self.assertLess(order.index("response_wait_registered"), order.index("clicked"))
        self.assertLess(order.index("edit_wait_registered"), order.index("clicked"))

    async def test_click_waits_past_intermediate_edit_for_report(self) -> None:
        intermediate = SimpleNamespace(
            id=70,
            raw_text="Поиск выполняется",
            buttons=[],
        )
        final = SimpleNamespace(
            id=71,
            raw_text="Готово",
            buttons=[
                [
                    SimpleNamespace(
                        text="Открыть отчёт",
                        url="https://reports.example.test/r/2",
                    )
                ]
            ],
        )
        pending_response = asyncio.get_running_loop().create_future()
        pending_edit = asyncio.get_running_loop().create_future()
        conversation = Mock()
        conversation.get_response.side_effect = [
            pending_response,
            asyncio.sleep(0, result=final),
        ]
        conversation.get_edit.side_effect = [
            asyncio.sleep(0, result=intermediate),
            pending_edit,
        ]
        message = SimpleNamespace(
            id=69,
            raw_text="Выберите страну",
            buttons=[
                [
                    SimpleNamespace(
                        text="Россия",
                        data=b"country:ru",
                        click=AsyncMock(),
                    )
                ]
            ],
        )

        result = await clickRussiaAndWait(conversation, message, timeout=1)

        self.assertIs(result, final)
        self.assertEqual(conversation.get_response.call_count, 2)
        self.assertEqual(conversation.get_edit.call_count, 2)


if __name__ == "__main__":
    unittest.main()
