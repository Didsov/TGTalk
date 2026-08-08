import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from src.integrations.telegram.stub_bot import (
    REPORT_EMPTY,
    REPORT_SECOND_WITHOUT_PHONE,
    REPORT_WITHOUT_PHONE,
    REPORT_WITH_PHONE,
    country_keyboard,
    create_report_app,
    inn_scenario,
    load_stub_settings,
    person_scenario,
    report_keyboard,
    report_text,
    report_url,
    search_by_inn,
    select_country,
)


class StubScenarioTests(unittest.TestCase):
    def test_selects_all_inn_scenarios_deterministically(self) -> None:
        expected = {
            "0": REPORT_WITH_PHONE,
            "1": REPORT_WITHOUT_PHONE,
            "2": "not_found",
            "3": "timeout",
            "4": "unknown",
            "5": "fallback_without_phone",
            "6": "fallback_not_found",
            "7": REPORT_EMPTY,
            "8": REPORT_WITH_PHONE,
            "9": REPORT_WITH_PHONE,
        }
        for last_digit, scenario in expected.items():
            with self.subTest(last_digit=last_digit):
                self.assertEqual(inn_scenario("123456789" + last_digit), scenario)

    def test_rejects_non_numeric_inn(self) -> None:
        self.assertEqual(inn_scenario(""), "invalid")
        self.assertEqual(inn_scenario("abc"), "invalid")

    def test_selects_person_result_by_synthetic_last_name(self) -> None:
        self.assertEqual(
            person_scenario("Тестов Тест Тестович 01.01.1990"),
            "person_with_phone",
        )
        self.assertEqual(
            person_scenario("Шкирмин Андрей Романович 02.02.1990"),
            "person_without_phone",
        )
        self.assertEqual(
            person_scenario("Ненайденов Николай Николаевич 03.03.1990"),
            "person_not_found",
        )


class StubReportTests(unittest.TestCase):
    def test_report_url_requires_caller_to_append_txt(self) -> None:
        url = report_url("http://127.0.0.1:8081/", REPORT_WITH_PHONE, "123")

        self.assertEqual(url, "http://127.0.0.1:8081/r/with_phone/123")
        self.assertFalse(url.endswith("/txt"))

    def test_phone_report_contains_allowed_fields(self) -> None:
        report = report_text(REPORT_WITH_PHONE, "1234567890")

        self.assertIn("Телефон: 79990000001", report)
        self.assertIn("Email: contact@example.test", report)
        self.assertIn("ИНН: 1234567890", report)

    def test_first_report_without_phone_contains_fallback_identity(self) -> None:
        report = report_text(REPORT_WITHOUT_PHONE, "1234567891")

        self.assertIn("Телефон: \n", report)
        self.assertIn("ФИО: Шкирмин Андрей Романович", report)
        self.assertIn("День рождения: 01.01.1990", report)

    def test_second_report_can_have_no_phone(self) -> None:
        report = report_text(REPORT_SECOND_WITHOUT_PHONE, "person-result")

        self.assertIn("Телефон: \n", report)
        self.assertNotIn("79990000001", report)

    def test_empty_report_is_really_empty(self) -> None:
        self.assertEqual(report_text(REPORT_EMPTY, "1234567897"), "")


class StubKeyboardTests(unittest.TestCase):
    def test_report_keyboard_has_url_button(self) -> None:
        keyboard = report_keyboard(
            "http://127.0.0.1:8081", REPORT_WITH_PHONE, "123"
        )

        button = keyboard.inline_keyboard[0][0]
        self.assertEqual(button.text, "📄 Открыть полный отчёт")
        self.assertEqual(button.url, "http://127.0.0.1:8081/r/with_phone/123")

    def test_country_keyboard_uses_callback_buttons(self) -> None:
        keyboard = country_keyboard("person_with_phone")
        buttons = [button for row in keyboard.inline_keyboard for button in row]

        russia = next(button for button in buttons if "Россия" in button.text)
        self.assertEqual(russia.callback_data, "country:person_with_phone:ru")
        self.assertTrue(all(button.url is None for button in buttons))


class StubHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_scenario_sends_no_answer(self) -> None:
        message = Mock(text="/inn 7700000023")
        message.reply = AsyncMock()

        await search_by_inn(message, "http://127.0.0.1:8081")

        message.reply.assert_not_awaited()

    async def test_russia_callback_sends_second_report(self) -> None:
        callback_message = Mock()
        callback_message.answer = AsyncMock()
        callback_message.edit_text = AsyncMock()
        callback = Mock(
            data="country:person_with_phone:ru",
            message=callback_message,
        )
        callback.answer = AsyncMock()

        await select_country(callback, "http://127.0.0.1:8081")

        callback.answer.assert_awaited_once_with()
        callback_message.answer.assert_awaited_once()
        callback_message.edit_text.assert_not_awaited()

    async def test_not_found_callback_edits_existing_message(self) -> None:
        callback_message = Mock()
        callback_message.answer = AsyncMock()
        callback_message.edit_text = AsyncMock()
        callback = Mock(
            data="country:person_not_found:ru",
            message=callback_message,
        )
        callback.answer = AsyncMock()

        await select_country(callback, "http://127.0.0.1:8081")

        callback.answer.assert_awaited_once_with()
        callback_message.edit_text.assert_awaited_once()
        callback_message.answer.assert_not_awaited()


class StubSettingsTests(unittest.TestCase):
    def test_uses_local_report_server_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = load_stub_settings()

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8081)
        self.assertEqual(settings.public_base_url, "http://127.0.0.1:8081")

    def test_rejects_invalid_port(self) -> None:
        with patch.dict(
            "os.environ", {"TELEGRAM_STUB_REPORT_PORT": "invalid"}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                load_stub_settings()


class StubReportServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = TestClient(TestServer(create_report_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_serves_txt_only_after_txt_suffix(self) -> None:
        landing = await self.client.get("/r/with_phone/1234567890")
        report = await self.client.get("/r/with_phone/1234567890/txt")

        self.assertIn("Добавьте /txt", await landing.text())
        self.assertIn("Телефон: 79990000001", await report.text())
        self.assertEqual(report.charset, "utf-8")

    async def test_returns_404_for_unknown_report_scenario(self) -> None:
        response = await self.client.get("/r/unknown/123/txt")

        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
