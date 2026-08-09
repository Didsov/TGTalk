import tempfile
import unittest
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock, patch

from src.application.reporting import load_client_report
from src.integrations.telegram.report_sender import (
    DAILY_REPORT_KIND,
    LATE_UPDATE_REPORT_KIND,
    notify_balance_check_failed,
    observe_query_balance,
    report_download_keyboard,
    report_item_from_entry,
    retry_failed_report_deliveries,
    send_daily_report,
    send_late_update_reports,
)
from src.storage import NewClientStorage, ProcessingStatus
from src.storage.reporting import ReportingStorage


def stored_client(spp_id: int = 1) -> dict:
    return {
        "ИдентификаторСПП": spp_id,
        "Название": f'ООО "Организация {spp_id}"',
        "Регион": "Москва",
        "ОГРН": "1127746271355",
        "ИНН": "7736641983",
        "КПП": "773601001",
        "Предприниматель": False,
        "ДатаРегистрации": "2026-08-02",
        "ДатаЛиквидации": None,
        "Директор.Фамилия": "Иванов",
        "Директор.Имя": "Иван",
        "Директор.Отчество": "Иванович",
        "Телефон": "+79990000001",
        "email": "sbis@example.test",
    }


class ReportSenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "clients.db"
        self.clients = NewClientStorage(database_path)
        self.reporting = ReportingStorage(database_path)
        self.clients.initialize()
        self.reporting.initialize()
        self.clients.upsert_from_sbis(stored_client())
        self.clients.replace_telegram_contacts(
            1,
            phones=["+79991112233"],
            emails=["found@example.test"],
        )
        self.clients.set_status(1, ProcessingStatus.PROCESSED)
        self.reporting.add_user(101)
        self.reporting.subscribe(101)

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_failed_deliveries_due(self, report_id: int) -> None:
        """Сдвинуть backoff только в офлайн-тесте автоматического повтора."""
        with closing(sqlite3.connect(self.clients.database_path)) as connection:
            connection.execute(
                """
                UPDATE report_deliveries
                SET next_retry_at = '2000-01-01T00:00:00+00:00'
                WHERE report_id = ? AND status = 'failed'
                """,
                (report_id,),
            )
            connection.commit()

    async def test_sends_daily_report_and_does_not_duplicate_delivery(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=77))

        first = await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        second = await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )

        self.assertEqual(first.clients_count, 1)
        self.assertEqual(first.sent, 1)
        self.assertEqual(second.sent, 0)
        self.assertEqual(bot.send_message.await_count, 1)
        saved = self.clients.get(1)
        self.assertEqual(saved.report_id, str(first.report_id))
        self.assertIsNotNone(saved.reported_at)

    async def test_one_recipient_failure_does_not_stop_other_delivery(self) -> None:
        self.reporting.add_user(102)
        self.reporting.subscribe(102)
        bot = Mock()

        async def send(chat_id, *args, **kwargs):
            if chat_id == 101:
                raise RuntimeError("offline")
            return Mock(message_id=88)

        bot.send_message = AsyncMock(side_effect=send)

        result = await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )

        self.assertEqual(result.sent, 1)
        self.assertEqual(result.failed, 1)
        deliveries = self.reporting.ensure_report_deliveries(
            result.report_id, [101, 102]
        )
        self.assertEqual(
            {delivery.status for delivery in deliveries},
            {"failed", "sent"},
        )

    async def test_sends_one_late_update_after_client_changes(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=77))
        await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        self.clients.replace_telegram_contacts(
            1, phones=["+79992223344"]
        )
        bot.send_message.reset_mock()

        first = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
        )
        second = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].clients_count, 1)
        self.assertEqual(second, ())
        self.assertIn("Дополнение", bot.send_message.await_args.args[1])

    async def test_does_not_send_supplement_when_only_timestamp_changed(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=77))
        await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        with closing(sqlite3.connect(self.clients.database_path)) as connection:
            connection.execute(
                """
                UPDATE new_clients
                SET reported_at = '2000-01-01 10:00:00',
                    updated_at = '2000-01-02 11:00:00'
                WHERE spp_id = 1
                """
            )
            connection.commit()
        bot.send_message.reset_mock()

        results = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
        )

        self.assertEqual(results, ())
        bot.send_message.assert_not_awaited()
        # Служебная временная метка не является изменением данных карточки.
        self.assertEqual(self.clients.list_report_updates(), [])

    async def test_does_not_send_supplement_for_only_attempt_code_change(
        self,
    ) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=77))
        await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        self.clients.save_telegram_result(
            1,
            phones=["+79991112233"],
            emails=["found@example.test"],
            status=ProcessingStatus.PROCESSED,
            stage="repeat",
            result_code="same_contacts_new_attempt_code",
        )
        bot.send_message.reset_mock()

        results = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
        )

        self.assertEqual(results, ())
        bot.send_message.assert_not_awaited()

    async def test_repeated_main_report_does_not_erase_late_contact_change(
        self,
    ) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=77))
        await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        self.clients.replace_telegram_contacts(
            1, phones=["+79993334455"]
        )

        await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        updates = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
        )

        self.assertEqual(len(updates), 1)

    async def test_new_client_after_main_report_goes_to_supplement(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=77))
        main = await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        self.clients.upsert_from_sbis(stored_client(2))
        self.clients.replace_telegram_contacts(2, phones=["+79994445566"])
        self.clients.set_status(2, ProcessingStatus.PROCESSED)

        supplements = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
            eligible_through=date(2026, 8, 2),
        )

        self.assertEqual(len(supplements), 1)
        self.assertEqual(supplements[0].clients_count, 1)
        main_items = self.reporting.list_report_items(main.report_id)
        self.assertEqual(
            [item.client_spp_id for item in main_items],
            [1],
        )

    async def test_crash_after_main_snapshot_does_not_duplicate_supplement(
        self,
    ) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=77))
        main = await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        with closing(sqlite3.connect(self.clients.database_path)) as connection:
            connection.execute(
                """
                UPDATE new_clients
                SET report_id = NULL, reported_at = NULL
                WHERE spp_id = 1
                """
            )
            connection.commit()
        bot.send_message.reset_mock()

        supplements = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
            eligible_through=date(2026, 8, 2),
        )

        self.assertEqual(supplements, ())
        bot.send_message.assert_not_awaited()
        recovered = self.clients.get(1)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.report_id, str(main.report_id))
        self.assertIsNotNone(recovered.reported_at)

    async def test_crash_after_supplement_snapshot_does_not_duplicate_it(
        self,
    ) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=77))
        await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        self.clients.replace_telegram_contacts(
            1, phones=["+79995556677"]
        )
        first = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
        )
        self.assertEqual(len(first), 1)
        supplement = self.reporting.find_report_run(
            kind=LATE_UPDATE_REPORT_KIND,
            cohort_date=date(2026, 8, 2),
        )
        self.assertIsNotNone(supplement)
        with closing(sqlite3.connect(self.clients.database_path)) as connection:
            connection.execute(
                """
                UPDATE new_clients
                SET report_id = NULL, reported_at = NULL
                WHERE spp_id = 1
                """
            )
            connection.commit()
        bot.send_message.reset_mock()

        second = await send_late_update_reports(
            bot,
            self.clients,
            self.reporting,
            eligible_through=date(2026, 8, 2),
        )

        self.assertEqual(second, ())
        bot.send_message.assert_not_awaited()
        recovered = self.clients.get(1)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.report_id, str(supplement.id))

    async def test_retries_failed_delivery_from_saved_report_snapshot(self) -> None:
        bot = Mock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("offline"))
        initial = await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        self.assertEqual(initial.failed, 1)
        self.make_failed_deliveries_due(initial.report_id)
        bot.send_message = AsyncMock(return_value=Mock(message_id=99))

        with patch(
            "src.integrations.telegram.report_sender.render_report_html",
            return_value=("ИЗМЕНЕННЫЙ ШАБЛОН",),
        ):
            retries = await retry_failed_report_deliveries(
                bot,
                self.reporting,
            )

        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].report_id, initial.report_id)
        self.assertEqual(retries[0].sent, 1)
        self.assertIn("ровно 7 дней назад", bot.send_message.await_args.args[1])
        self.assertNotEqual(
            bot.send_message.await_args.args[1],
            "ИЗМЕНЕННЫЙ ШАБЛОН",
        )

    async def test_resumes_already_pending_delivery_without_failed_rows(
        self,
    ) -> None:
        report = load_client_report(self.clients, date(2026, 8, 2))
        report_run = self.reporting.create_report_run(
            kind=DAILY_REPORT_KIND,
            cohort_date=date(2026, 8, 2),
            items=(report_item_from_entry(entry) for entry in report.entries),
        )
        self.reporting.ensure_report_deliveries(report_run.id, [101])
        bot = Mock()
        bot.send_message = AsyncMock(return_value=Mock(message_id=99))

        retries = await retry_failed_report_deliveries(
            bot,
            self.reporting,
        )

        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].report_id, report_run.id)
        self.assertEqual(retries[0].sent, 1)
        bot.send_message.assert_awaited()

    async def test_multipart_retry_skips_already_sent_parts(self) -> None:
        for spp_id in range(2, 13):
            record = stored_client(spp_id)
            record["Название"] = f"Организация {spp_id} " + "А" * 350
            self.clients.upsert_from_sbis(record)
            self.clients.replace_telegram_contacts(
                spp_id,
                phones=[f"+7999000{spp_id:04d}"],
            )
            self.clients.set_status(spp_id, ProcessingStatus.PROCESSED)
        bot = Mock()
        bot.send_message = AsyncMock(
            side_effect=[Mock(message_id=1), RuntimeError("offline")]
        )

        initial = await send_daily_report(
            bot,
            self.clients,
            self.reporting,
            date(2026, 8, 2),
        )
        first_part = bot.send_message.await_args_list[0].args[1]
        self.make_failed_deliveries_due(initial.report_id)
        bot.send_message = AsyncMock(return_value=Mock(message_id=2))

        retries = await retry_failed_report_deliveries(
            bot,
            self.reporting,
        )

        self.assertEqual(initial.failed, 1)
        self.assertEqual(retries[0].sent, 1)
        retried_parts = [call.args[1] for call in bot.send_message.await_args_list]
        self.assertNotIn(first_part, retried_parts)

    def test_download_callback_fits_telegram_limit(self) -> None:
        keyboard = report_download_keyboard(42)
        callback_data = keyboard.inline_keyboard[0][0].callback_data

        self.assertEqual(callback_data, "report:xlsx:id:42")
        self.assertLessEqual(len(callback_data.encode("utf-8")), 64)


class BalanceNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "clients.db"
        self.reporting = ReportingStorage(database_path)
        self.reporting.initialize()
        self.bot = Mock()
        self.bot.send_message = AsyncMock(return_value=Mock(message_id=1))

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_notifies_only_on_balance_level_transitions(self) -> None:
        arguments = (self.bot, self.reporting, (9001,))

        await observe_query_balance(*arguments, 15, low_threshold=10)
        await observe_query_balance(*arguments, 9, low_threshold=10)
        await observe_query_balance(*arguments, 8, low_threshold=10)
        await observe_query_balance(*arguments, 0, low_threshold=10)
        await observe_query_balance(*arguments, 0, low_threshold=10)
        await observe_query_balance(*arguments, 20, low_threshold=10)
        await observe_query_balance(*arguments, 7, low_threshold=10)

        self.assertEqual(self.bot.send_message.await_count, 3)
        sent_texts = [call.args[1] for call in self.bot.send_message.await_args_list]
        self.assertIn("9", sent_texts[0])
        self.assertIn("закончились", sent_texts[1])
        self.assertIn("7", sent_texts[2])

    async def test_rejects_invalid_threshold(self) -> None:
        with self.assertRaises(ValueError):
            await observe_query_balance(
                self.bot,
                self.reporting,
                (9001,),
                5,
                low_threshold=0,
            )

    async def test_retries_alert_for_only_admin_with_failed_delivery(self) -> None:
        self.reporting.add_admin(9002)

        async def first_send(chat_id, _text):
            if chat_id == 9002:
                raise RuntimeError("offline")
            return Mock(message_id=1)

        self.bot.send_message = AsyncMock(side_effect=first_send)
        await observe_query_balance(
            self.bot,
            self.reporting,
            (9001,),
            3,
            low_threshold=10,
        )
        self.bot.send_message = AsyncMock(return_value=Mock(message_id=2))

        await observe_query_balance(
            self.bot,
            self.reporting,
            (9001,),
            3,
            low_threshold=10,
        )

        self.bot.send_message.assert_awaited_once_with(
            9002,
            ANY,
        )

    async def test_balance_check_failure_is_throttled_until_success(self) -> None:
        arguments = (self.bot, self.reporting, (9001,))

        await notify_balance_check_failed(*arguments)
        await notify_balance_check_failed(*arguments)
        await observe_query_balance(*arguments, 15, low_threshold=10)
        await notify_balance_check_failed(*arguments)

        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertTrue(
            all(
                "Не удалось получить остаток" in call.args[1]
                for call in self.bot.send_message.await_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
