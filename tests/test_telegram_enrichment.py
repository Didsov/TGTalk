import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import FloodWaitError

from src.application.telegram_enrichment import (
    EnrichmentOutcome,
    TelegramBalanceCheckError,
    enrich_client,
    process_first_clients,
)
from src.integrations.telegram.report_downloader import ReportDownloadError
from src.storage.new_clients import NewClient, NewClientStorage, ProcessingStatus


SOURCE_INN = "7700000016"


def client(**changes) -> NewClient:
    values = {
        "spp_id": 101,
        "name": "ООО Тест",
        "region": "Москва",
        "ogrn": "1234567890123",
        "inn": SOURCE_INN,
        "kpp": "770001001",
        "is_entrepreneur": False,
        "registration_date": "2026-08-08",
        "liquidation_date": None,
        "director_last_name": "Тестов",
        "director_first_name": "Тест",
        "director_middle_name": "Тестович",
        "sbis_phones": (),
        "telegram_phones": (),
        "sbis_emails": (),
        "telegram_emails": (),
        "status": ProcessingStatus.QUEUED,
        "director_inn": SOURCE_INN,
        "personalised_emails": (),
    }
    values.update(changes)
    return NewClient(**values)


def message(text: str = "Готово", url: str | None = None):
    buttons = []
    if url is not None:
        buttons = [[SimpleNamespace(text="Открыть отчёт", url=url)]]
    return SimpleNamespace(raw_text=text, text=text, buttons=buttons)


PHONE_REPORT = (
    "=== Общая сводка ===\n"
    "Телефон: 79990000001\n"
    "Email: owner@example.test\n"
    f"ИНН: {SOURCE_INN}\n"
)

FALLBACK_REPORT = (
    "=== Общая сводка ===\n"
    "Телефон: \n"
    "Email: first@example.test\n"
    f"ИНН: {SOURCE_INN}\n\n"
    "=== Источник ===\n"
    f"ИНН: {SOURCE_INN}\n"
    "ФИО: Тестов Тест Тестович\n"
    "День рождения: 01.01.1990\n"
)

NO_PHONE_REPORT = (
    "=== Источник ===\n"
    "Телефон: \n"
    "Почта: second@example.test\n"
    f"ИНН: {SOURCE_INN}\n"
)


class EnrichClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_archives_downloaded_report_with_original_query(self) -> None:
        archiver = Mock()
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(return_value=message(url="http://localhost/r/phone")),
        ):
            await enrich_client(
                client(),
                Mock(),
                report_loader=AsyncMock(return_value=PHONE_REPORT),
                report_archiver=archiver,
            )

        values = archiver.record.call_args.kwargs
        self.assertEqual(values["client_spp_id"], 101)
        self.assertEqual(values["query_kind"], "inn")
        self.assertEqual(values["query_text"], f"/inn {SOURCE_INN}")
        self.assertEqual(values["outcome"], "report_saved")
        self.assertEqual(values["report_text"], PHONE_REPORT)

    async def test_archives_timeout_without_report(self) -> None:
        archiver = Mock()
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(side_effect=TimeoutError),
        ):
            outcome = await enrich_client(
                client(),
                Mock(),
                report_archiver=archiver,
            )

        self.assertEqual(outcome.error_code, "telegram_timeout")
        values = archiver.record.call_args.kwargs
        self.assertEqual(values["query_text"], f"/inn {SOURCE_INN}")
        self.assertEqual(values["outcome"], "no_response")
        self.assertIsNone(values["report_text"])

    async def test_processes_phone_found_in_inn_report(self) -> None:
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(return_value=message(url="http://localhost/r/phone")),
        ):
            outcome = await enrich_client(
                client(),
                Mock(),
                report_loader=AsyncMock(return_value=PHONE_REPORT),
            )

        self.assertEqual(outcome.status, ProcessingStatus.PROCESSED)
        self.assertEqual(outcome.phones, ("+79990000001",))
        self.assertEqual(outcome.emails, ("owner@example.test",))
        self.assertEqual(outcome.result_code, "phone_found_by_inn")
        self.assertEqual(outcome.requests_spent, 1)

    async def test_loads_lazy_report_button_before_parsing(self) -> None:
        lazy_response = SimpleNamespace(
            raw_text="Готово",
            text="Готово",
            buttons=None,
            get_buttons=AsyncMock(
                return_value=[
                    [
                        SimpleNamespace(
                            text="Открыть полный отчёт",
                            url="http://localhost/r/phone",
                        )
                    ]
                ]
            ),
        )
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(return_value=lazy_response),
        ):
            outcome = await enrich_client(
                client(),
                Mock(),
                report_loader=AsyncMock(return_value=PHONE_REPORT),
            )

        self.assertEqual(outcome.status, ProcessingStatus.PROCESSED)
        lazy_response.get_buttons.assert_awaited_once_with()

    async def test_searches_personalised_email_after_inn_report_without_phone(
        self,
    ) -> None:
        send_query = AsyncMock(
            side_effect=[
                message(url="http://localhost/r/inn-without-phone"),
                message(url="http://localhost/r/email-with-phone"),
            ]
        )
        loader = AsyncMock(side_effect=[FALLBACK_REPORT, PHONE_REPORT])
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=send_query,
        ):
            outcome = await enrich_client(
                client(
                    personalised_emails=(
                        "lookup@example.test",
                        "must-not-be-used@example.test",
                    )
                ),
                Mock(),
                report_loader=loader,
            )

        self.assertEqual(outcome.status, ProcessingStatus.PROCESSED)
        self.assertEqual(outcome.result_code, "phone_found_by_email")
        self.assertEqual(outcome.requests_spent, 2)
        self.assertEqual(send_query.await_args_list[0].args[1], f"/inn {SOURCE_INN}")
        self.assertEqual(send_query.await_args_list[1].args[1], "lookup@example.test")
        self.assertEqual(send_query.await_count, 2)

    async def test_inn_not_found_still_searches_personalised_email(self) -> None:
        send_query = AsyncMock(
            side_effect=[
                message("По запросу ничего не найдено"),
                message(url="http://localhost/r/email-with-phone"),
            ]
        )
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=send_query,
        ):
            outcome = await enrich_client(
                client(
                    personalised_emails=(
                        "lookup@example.test",
                        "must-not-be-used@example.test",
                    )
                ),
                Mock(),
                report_loader=AsyncMock(return_value=PHONE_REPORT),
            )

        self.assertEqual(outcome.status, ProcessingStatus.PROCESSED)
        self.assertEqual(outcome.result_code, "phone_found_by_email")
        self.assertEqual(send_query.await_count, 2)

    async def test_email_not_found_then_falls_back_to_person(self) -> None:
        send_query = AsyncMock(
            side_effect=[
                message(url="http://localhost/r/inn-without-phone"),
                message("По запросу ничего не найдено"),
                message("Выберите страну"),
            ]
        )
        click_russia = AsyncMock(
            return_value=message(url="http://localhost/r/person-phone")
        )
        with (
            patch(
                "src.application.telegram_enrichment.sendQueryAndWait",
                new=send_query,
            ),
            patch(
                "src.application.telegram_enrichment.clickRussiaAndWait",
                new=click_russia,
            ),
        ):
            outcome = await enrich_client(
                client(
                    personalised_emails=(
                        "lookup@example.test",
                        "must-not-be-used@example.test",
                    )
                ),
                Mock(),
                report_loader=AsyncMock(
                    side_effect=[FALLBACK_REPORT, PHONE_REPORT]
                ),
            )

        self.assertEqual(outcome.status, ProcessingStatus.PROCESSED)
        self.assertEqual(outcome.result_code, "phone_found_by_person")
        self.assertEqual(outcome.requests_spent, 3)
        self.assertEqual(send_query.await_args_list[1].args[1], "lookup@example.test")
        self.assertEqual(
            send_query.await_args_list[2].args[1],
            "Тестов Тест Тестович 01.01.1990",
        )
        self.assertEqual(send_query.await_count, 3)
        self.assertNotIn(
            "must-not-be-used@example.test",
            [call.args[1] for call in send_query.await_args_list],
        )

    async def test_does_not_send_query_after_available_balance_is_spent(self) -> None:
        send_query = AsyncMock(
            return_value=message(url="http://localhost/r/inn-without-phone")
        )
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=send_query,
        ):
            outcome = await enrich_client(
                client(personalised_emails=("lookup@example.test",)),
                Mock(),
                report_loader=AsyncMock(return_value=FALLBACK_REPORT),
                available_queries=1,
            )

        self.assertEqual(outcome.status, ProcessingStatus.RETRY_REQUIRED)
        self.assertEqual(outcome.result_code, "available_queries_exhausted")
        self.assertEqual(outcome.requests_spent, 1)
        send_query.assert_awaited_once()

    async def test_falls_back_to_person_and_clicks_russia(self) -> None:
        send_query = AsyncMock(
            side_effect=[
                message(url="http://localhost/r/without-phone"),
                message("Выберите страну"),
            ]
        )
        click_russia = AsyncMock(
            return_value=message(url="http://localhost/r/person-phone")
        )
        loader = AsyncMock(side_effect=[FALLBACK_REPORT, PHONE_REPORT])
        with (
            patch(
                "src.application.telegram_enrichment.sendQueryAndWait",
                new=send_query,
            ),
            patch(
                "src.application.telegram_enrichment.clickRussiaAndWait",
                new=click_russia,
            ),
        ):
            outcome = await enrich_client(
                client(), Mock(), report_loader=loader
            )

        self.assertEqual(outcome.status, ProcessingStatus.PROCESSED)
        self.assertEqual(outcome.result_code, "phone_found_by_person")
        self.assertEqual(
            send_query.await_args_list[1].args[1],
            "Тестов Тест Тестович 01.01.1990",
        )
        click_russia.assert_awaited_once()
        self.assertIn("first@example.test", outcome.emails)

    async def test_email_only_is_retained_when_final_report_has_no_phone(self) -> None:
        send_query = AsyncMock(
            side_effect=[
                message(url="http://localhost/r/without-phone"),
                message("Выберите страну"),
            ]
        )
        click_russia = AsyncMock(
            return_value=message(url="http://localhost/r/still-without-phone")
        )
        loader = AsyncMock(side_effect=[FALLBACK_REPORT, NO_PHONE_REPORT])
        with (
            patch(
                "src.application.telegram_enrichment.sendQueryAndWait",
                new=send_query,
            ),
            patch(
                "src.application.telegram_enrichment.clickRussiaAndWait",
                new=click_russia,
            ),
        ):
            outcome = await enrich_client(client(), Mock(), report_loader=loader)

        self.assertEqual(outcome.status, ProcessingStatus.SKIPPED)
        self.assertEqual(outcome.phones, ())
        self.assertEqual(
            outcome.emails,
            ("first@example.test", "second@example.test"),
        )
        self.assertEqual(outcome.result_code, "phone_not_found")

    async def test_combined_search_button_is_ignored_after_person_not_found(
        self,
    ) -> None:
        combined_button = SimpleNamespace(
            text="Попробовать комбинированный поиск",
            data=b"combined:ignored",
            click=AsyncMock(),
        )
        person_not_found = message("По запросу ничего не найдено")
        person_not_found.buttons = [[combined_button]]
        send_query = AsyncMock(
            side_effect=[
                message(url="http://localhost/r/without-phone"),
                message("Выберите страну"),
            ]
        )
        click_russia = AsyncMock(return_value=person_not_found)
        with (
            patch(
                "src.application.telegram_enrichment.sendQueryAndWait",
                new=send_query,
            ),
            patch(
                "src.application.telegram_enrichment.clickRussiaAndWait",
                new=click_russia,
            ),
        ):
            outcome = await enrich_client(
                client(),
                Mock(),
                report_loader=AsyncMock(return_value=FALLBACK_REPORT),
            )

        self.assertEqual(outcome.status, ProcessingStatus.SKIPPED)
        self.assertEqual(outcome.result_code, "person_not_found")
        combined_button.click.assert_not_awaited()
        self.assertEqual(send_query.await_count, 2)

    async def test_ambiguous_birth_dates_require_manual_review(self) -> None:
        ambiguous_report = FALLBACK_REPORT + (
            "\n=== Другой источник ===\n"
            f"ИНН: {SOURCE_INN}\n"
            "ФИО: Тестов Тест Тестович\n"
            "День рождения: 02.01.1990\n"
        )
        send_query = AsyncMock(
            return_value=message(url="http://localhost/r/ambiguous")
        )
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=send_query,
        ):
            outcome = await enrich_client(
                client(),
                Mock(),
                report_loader=AsyncMock(return_value=ambiguous_report),
            )

        self.assertEqual(outcome.status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(outcome.result_code, "ambiguous_person")
        send_query.assert_awaited_once()

    async def test_inn_not_found_without_email_or_candidate_is_skipped(self) -> None:
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(return_value=message("По запросу ничего не найдено")),
        ):
            outcome = await enrich_client(client(), Mock())

        self.assertEqual(outcome.status, ProcessingStatus.SKIPPED)
        self.assertEqual(outcome.result_code, "person_not_available")

    async def test_timeout_requires_retry(self) -> None:
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(side_effect=TimeoutError),
        ):
            outcome = await enrich_client(client(), Mock())

        self.assertEqual(outcome.status, ProcessingStatus.RETRY_REQUIRED)
        self.assertEqual(outcome.stage, "inn_query")

    async def test_bot_rate_limit_requires_retry(self) -> None:
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(
                return_value=message("Слишком много запросов, попробуйте позже")
            ),
        ):
            outcome = await enrich_client(client(), Mock())

        self.assertEqual(outcome.status, ProcessingStatus.RETRY_REQUIRED)
        self.assertEqual(outcome.result_code, "temporary_error")
        self.assertEqual(outcome.error_code, "bot_temporary_error")

    async def test_flood_wait_preserves_retry_delay(self) -> None:
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(side_effect=FloodWaitError(request=None, capture=17)),
        ):
            outcome = await enrich_client(client(), Mock())

        self.assertEqual(outcome.status, ProcessingStatus.RETRY_REQUIRED)
        self.assertEqual(outcome.error_code, "telegram_flood_wait")
        self.assertEqual(outcome.retry_after_seconds, 17)

    async def test_retryable_report_download_error_requires_retry(self) -> None:
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=AsyncMock(
                return_value=message(url="http://localhost/r/temporary-error")
            ),
        ):
            outcome = await enrich_client(
                client(),
                Mock(),
                report_loader=AsyncMock(
                    side_effect=ReportDownloadError("report_http_503", True)
                ),
            )

        self.assertEqual(outcome.status, ProcessingStatus.RETRY_REQUIRED)
        self.assertEqual(outcome.stage, "inn_report_download")
        self.assertEqual(outcome.error_code, "report_http_503")

    async def test_invalid_director_inn_is_skipped_without_sending(self) -> None:
        send_query = AsyncMock()
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=send_query,
        ):
            outcome = await enrich_client(
                client(director_inn="7700000017"), Mock()
            )

        self.assertEqual(outcome.status, ProcessingStatus.SKIPPED)
        self.assertEqual(outcome.result_code, "invalid_director_inn")
        send_query.assert_not_awaited()

    async def test_missing_director_inn_is_skipped_without_sending(self) -> None:
        send_query = AsyncMock()
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=send_query,
        ):
            outcome = await enrich_client(client(director_inn=None), Mock())

        self.assertEqual(outcome.status, ProcessingStatus.SKIPPED)
        self.assertEqual(outcome.result_code, "director_inn_missing")
        self.assertEqual(outcome.requests_spent, 0)
        send_query.assert_not_awaited()

    async def test_missing_director_name_is_skipped_without_sending(self) -> None:
        send_query = AsyncMock()
        with patch(
            "src.application.telegram_enrichment.sendQueryAndWait",
            new=send_query,
        ):
            outcome = await enrich_client(
                client(
                    director_last_name=None,
                    director_first_name=None,
                    director_middle_name=None,
                ),
                Mock(),
            )

        self.assertEqual(outcome.status, ProcessingStatus.SKIPPED)
        self.assertEqual(outcome.result_code, "director_name_missing")
        self.assertEqual(outcome.requests_spent, 0)
        send_query.assert_not_awaited()


class ProcessFirstClientsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.storage = NewClientStorage(
            Path(self.temp_directory.name) / "clients.db"
        )
        self.storage.initialize()
        self.get_balance = AsyncMock(return_value=100)
        self.balance_patcher = patch(
            "src.application.telegram_enrichment.getAvailableQueries",
            new=self.get_balance,
        )
        self.balance_patcher.start()
        for spp_id in (1, 2, 3):
            self.storage.upsert_from_sbis(
                {
                    "ИдентификаторСПП": spp_id,
                    "Название": f"ООО {spp_id}",
                    "Регион": "Москва",
                    "ОГРН": None,
                    "ИНН": SOURCE_INN,
                    "КПП": "770001001",
                    "Предприниматель": False,
                    "ДатаРегистрации": "2026-08-08",
                    "ДатаЛиквидации": None,
                    "Директор.Фамилия": "Тестов",
                    "Директор.Имя": "Тест",
                    "Директор.Отчество": "Тестович",
                    "Телефон": None,
                    "email": None,
                }
            )

    def tearDown(self) -> None:
        self.balance_patcher.stop()
        self.temp_directory.cleanup()

    async def test_dry_run_limits_clients_without_changing_database(self) -> None:
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation
        outcomes = [
            EnrichmentOutcome(
                client_spp_id=spp_id,
                status=ProcessingStatus.PROCESSED,
                phones=("+79990000001",),
                emails=(),
                stage="inn_report_parse",
                result_code="phone_found_by_inn",
            )
            for spp_id in (1, 2)
        ]
        with patch(
            "src.application.telegram_enrichment.enrich_client",
            new=AsyncMock(side_effect=outcomes),
        ) as enrich:
            result = await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=2,
            )

        self.assertEqual(len(result), 2)
        self.assertEqual(enrich.await_count, 2)
        self.assertEqual(self.storage.get(1).status, ProcessingStatus.QUEUED)
        self.get_balance.assert_awaited_once_with(
            telegram_client,
            "@stub",
            timeout=30,
        )

    async def test_zero_balance_stops_before_opening_conversation(self) -> None:
        self.get_balance.return_value = 0
        telegram_client = Mock()
        with patch(
            "src.application.telegram_enrichment.notify_queries_exhausted"
        ) as notify:
            outcomes = await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=3,
            )

        self.assertEqual(outcomes, [])
        telegram_client.conversation.assert_not_called()
        notify.assert_called_once_with()

    async def test_balance_check_failure_has_distinct_error(self) -> None:
        self.get_balance.side_effect = TimeoutError("synthetic")
        observer = AsyncMock()

        with self.assertRaises(TelegramBalanceCheckError):
            await process_first_clients(
                self.storage,
                Mock(),
                "@stub",
                limit=1,
                balance_observer=observer,
            )

        observer.assert_not_awaited()

    async def test_stops_batch_when_reported_balance_is_spent(self) -> None:
        self.get_balance.return_value = 1
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation
        first = EnrichmentOutcome(
            client_spp_id=1,
            status=ProcessingStatus.PROCESSED,
            phones=("+79990000001",),
            emails=(),
            stage="inn_report_parse",
            result_code="phone_found_by_inn",
            requests_spent=1,
        )
        enrich = AsyncMock(return_value=first)
        with (
            patch(
                "src.application.telegram_enrichment.enrich_client",
                new=enrich,
            ),
            patch(
                "src.application.telegram_enrichment.notify_queries_exhausted"
            ) as notify,
        ):
            outcomes = await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=3,
            )

        self.assertEqual(outcomes, [first])
        enrich.assert_awaited_once()
        notify.assert_called_once_with()

    async def test_reports_initial_and_remaining_balance_to_observer(self) -> None:
        self.get_balance.return_value = 3
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation
        observer = AsyncMock()
        outcome = EnrichmentOutcome(
            client_spp_id=1,
            status=ProcessingStatus.PROCESSED,
            phones=("+79990000001",),
            emails=(),
            stage="inn_report_parse",
            result_code="phone_found_by_inn",
            requests_spent=1,
        )
        with patch(
            "src.application.telegram_enrichment.enrich_client",
            new=AsyncMock(return_value=outcome),
        ):
            await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=1,
                balance_observer=observer,
            )

        self.assertEqual(
            [call.args[0] for call in observer.await_args_list],
            [3, 2],
        )

    async def test_balance_observer_failure_does_not_stop_enrichment(self) -> None:
        self.get_balance.return_value = 3
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation
        observer = AsyncMock(side_effect=RuntimeError("notification unavailable"))
        outcome = EnrichmentOutcome(
            client_spp_id=1,
            status=ProcessingStatus.PROCESSED,
            phones=("+79990000001",),
            emails=(),
            stage="inn_report_parse",
            result_code="phone_found_by_inn",
            requests_spent=1,
        )
        with patch(
            "src.application.telegram_enrichment.enrich_client",
            new=AsyncMock(return_value=outcome),
        ):
            outcomes = await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=1,
                balance_observer=observer,
            )

        self.assertEqual(outcomes, [outcome])
        self.assertEqual(observer.await_count, 2)

    async def test_uses_only_queued_and_retry_clients_in_first_n(self) -> None:
        self.storage.set_status(1, ProcessingStatus.PROCESSED)
        self.storage.set_status(2, ProcessingStatus.RETRY_REQUIRED)
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation

        async def outcome_for_client(current_client, *_args, **_kwargs):
            return EnrichmentOutcome(
                client_spp_id=current_client.spp_id,
                status=ProcessingStatus.SKIPPED,
                phones=(),
                emails=(),
                stage="person_candidate",
                result_code="person_not_available",
            )

        with patch(
            "src.application.telegram_enrichment.enrich_client",
            new=AsyncMock(side_effect=outcome_for_client),
        ) as enrich:
            outcomes = await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=2,
            )

        self.assertEqual([outcome.client_spp_id for outcome in outcomes], [2, 3])
        self.assertEqual(enrich.await_count, 2)

    async def test_unexpected_client_error_requires_retry_and_batch_continues(
        self,
    ) -> None:
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation
        second_outcome = EnrichmentOutcome(
            client_spp_id=2,
            status=ProcessingStatus.PROCESSED,
            phones=("+79990000001",),
            emails=(),
            stage="inn_report_parse",
            result_code="phone_found_by_inn",
        )
        with patch(
            "src.application.telegram_enrichment.enrich_client",
            new=AsyncMock(
                side_effect=[ConnectionError("temporary"), second_outcome]
            ),
        ):
            outcomes = await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=2,
            )

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0].status, ProcessingStatus.RETRY_REQUIRED)
        self.assertEqual(outcomes[0].stage, "unexpected_error")
        self.assertEqual(outcomes[1], second_outcome)

    async def test_write_mode_persists_result(self) -> None:
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation
        outcome = EnrichmentOutcome(
            client_spp_id=1,
            status=ProcessingStatus.NEEDS_REVIEW,
            phones=(),
            emails=("test@example.test",),
            stage="person_candidate",
            result_code="ambiguous_person",
        )
        with patch(
            "src.application.telegram_enrichment.enrich_client",
            new=AsyncMock(return_value=outcome),
        ):
            await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=1,
                write=True,
            )

        saved = self.storage.get(1)
        self.assertEqual(saved.status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(saved.telegram_emails, ("test@example.test",))
        self.assertEqual(
            [item.spp_id for item in self.storage.list_for_processing()],
            [2, 3],
        )

    async def test_persistence_failure_does_not_repeat_or_stop_batch(self) -> None:
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation
        first = EnrichmentOutcome(
            client_spp_id=1,
            status=ProcessingStatus.PROCESSED,
            phones=("+79990000001",),
            emails=(),
            stage="inn_report_parse",
            result_code="phone_found_by_inn",
        )
        second = EnrichmentOutcome(
            client_spp_id=2,
            status=ProcessingStatus.PROCESSED,
            phones=("+79990000002",),
            emails=(),
            stage="inn_report_parse",
            result_code="phone_found_by_inn",
        )
        original_save = self.storage.save_telegram_result

        def flaky_save(spp_id, **kwargs):
            if spp_id == 1:
                raise RuntimeError("synthetic storage failure")
            return original_save(spp_id, **kwargs)

        with (
            patch(
                "src.application.telegram_enrichment.enrich_client",
                new=AsyncMock(side_effect=[first, second]),
            ) as enrich,
            patch.object(
                self.storage,
                "save_telegram_result",
                side_effect=flaky_save,
            ),
        ):
            outcomes = await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=2,
                write=True,
            )

        self.assertEqual(enrich.await_count, 2)
        self.assertEqual(outcomes[0].result_code, "persistence_failed")
        self.assertEqual(outcomes[0].status, ProcessingStatus.RETRY_REQUIRED)
        self.assertEqual(outcomes[1], second)
        self.assertEqual(self.storage.get(1).status, ProcessingStatus.QUEUED)
        self.assertEqual(self.storage.get(2).status, ProcessingStatus.PROCESSED)
        self.assertEqual(
            [item.spp_id for item in self.storage.list_for_processing()],
            [3],
        )

    async def test_global_rate_limit_stops_before_next_client(self) -> None:
        conversation = AsyncMock()
        conversation.__aenter__.return_value = conversation
        conversation.__aexit__.return_value = None
        telegram_client = Mock()
        telegram_client.conversation.return_value = conversation
        rate_limited = EnrichmentOutcome(
            client_spp_id=1,
            status=ProcessingStatus.RETRY_REQUIRED,
            phones=(),
            emails=(),
            stage="inn_query",
            result_code="temporary_error",
            error_code="bot_temporary_error",
        )
        enrich = AsyncMock(return_value=rate_limited)

        with patch(
            "src.application.telegram_enrichment.enrich_client",
            new=enrich,
        ):
            outcomes = await process_first_clients(
                self.storage,
                telegram_client,
                "@stub",
                limit=3,
            )

        self.assertEqual(outcomes, [rate_limited])
        enrich.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
