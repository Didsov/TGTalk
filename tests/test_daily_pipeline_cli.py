import unittest
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.application.telegram_enrichment import EnrichmentOutcome
from src.application.telegram_enrichment import TelegramBalanceCheckError
from src.cli.daily_pipeline import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_LIMIT,
    DailyPipelineResult,
    parse_arguments,
    print_daily_summary,
    run_daily_pipeline,
    _run,
)
from src.integrations.telegram.report_sender import ReportDispatchResult
from src.storage import ProcessingStatus


class ArgumentTests(unittest.TestCase):
    def test_uses_safe_daily_defaults(self) -> None:
        arguments = parse_arguments([])

        self.assertIsNone(arguments.date)
        self.assertEqual(arguments.database, DEFAULT_DATABASE_PATH)
        self.assertEqual(arguments.limit, DEFAULT_LIMIT)
        self.assertEqual(arguments.timeout, 45.0)

    def test_parses_date_database_limit_and_timeout(self) -> None:
        arguments = parse_arguments(
            [
                "--date",
                "2026-08-09",
                "--database",
                "other.db",
                "--limit",
                "25",
                "--timeout",
                "60",
            ]
        )

        self.assertEqual(arguments.date, date(2026, 8, 9))
        self.assertEqual(arguments.database, Path("other.db"))
        self.assertEqual(arguments.limit, 25)
        self.assertEqual(arguments.timeout, 60.0)

    def test_rejects_invalid_date_and_limit(self) -> None:
        with self.assertRaises(SystemExit):
            parse_arguments(["--date", "09.08.2026"])
        with self.assertRaises(SystemExit):
            parse_arguments(["--limit", "0"])


class DailyPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_previous_day_by_default(self) -> None:
        expected_date = date.today() - timedelta(days=1)
        with (
            patch(
                "src.cli.daily_pipeline.get_open_companies_by_date",
                new=AsyncMock(return_value=[]),
            ) as collection,
            patch(
                "src.cli.daily_pipeline.run_enrichment",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await run_daily_pipeline("daily.db")

        collection.assert_awaited_once_with(
            expected_date,
            database_path="daily.db",
        )
        self.assertEqual(result.target_date, expected_date)

    async def test_collects_before_enrichment_and_writes_results(self) -> None:
        order: list[str] = []
        outcome = EnrichmentOutcome(
            client_spp_id=1,
            status=ProcessingStatus.PROCESSED,
            phones=("+79990000001",),
            emails=(),
            stage="inn_report_parse",
            result_code="phone_found_by_inn",
            requests_spent=1,
        )

        async def collect(*args, **kwargs):
            order.append("collect")
            return [{"ИдентификаторСПП": 1}]

        async def enrich(*args, **kwargs):
            order.append("enrich")
            return [outcome]

        with (
            patch(
                "src.cli.daily_pipeline.get_open_companies_by_date",
                new=AsyncMock(side_effect=collect),
            ) as collection,
            patch(
                "src.cli.daily_pipeline.run_enrichment",
                new=AsyncMock(side_effect=enrich),
            ) as enrichment,
        ):
            result = await run_daily_pipeline(
                "daily.db",
                target_date=date(2026, 8, 9),
                limit=50,
                timeout=60,
            )

        self.assertEqual(order, ["collect", "enrich"])
        collection.assert_awaited_once_with(
            date(2026, 8, 9),
            database_path="daily.db",
        )
        enrichment.assert_awaited_once_with(
            "daily.db",
            limit=50,
            timeout=60,
            write=True,
        )
        self.assertEqual(result.collected_cards, 1)
        self.assertEqual(result.enrichment_outcomes, (outcome,))

    async def test_does_not_start_telegram_when_collection_fails(self) -> None:
        enrichment = AsyncMock()
        with (
            patch(
                "src.cli.daily_pipeline.get_open_companies_by_date",
                new=AsyncMock(side_effect=RuntimeError("collection failed")),
            ),
            patch(
                "src.cli.daily_pipeline.run_enrichment",
                new=enrichment,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "collection failed"):
                await run_daily_pipeline("daily.db")

        enrichment.assert_not_awaited()

    async def test_cli_continues_when_balance_notification_fails(self) -> None:
        pipeline_result = DailyPipelineResult(
            target_date=date(2026, 8, 8),
            collected_cards=1,
            enrichment_outcomes=(),
        )
        dispatch = ReportDispatchResult(
            report_id=1,
            cohort_date=date.today() - timedelta(days=7),
            clients_count=2,
            sent=1,
            failed=0,
        )
        report_bot = Mock()
        report_bot.__aenter__ = AsyncMock(return_value=report_bot)
        report_bot.__aexit__ = AsyncMock(return_value=None)

        async def pipeline(*args, **kwargs):
            await kwargs["balance_observer"](9)
            return pipeline_result

        arguments = SimpleNamespace(
            database=Path("daily.db"),
            date=None,
            limit=10,
            timeout=45,
        )
        settings = SimpleNamespace(
            token="test-token",
            bootstrap_admin_ids=frozenset({100}),
            low_query_threshold=10,
            report_retention_days=None,
        )
        reporting_storage = Mock()
        reporting_storage.start_pipeline_run.return_value = SimpleNamespace(id=31)
        with (
            patch("src.cli.daily_pipeline.Bot", return_value=report_bot),
            patch(
                "src.cli.daily_pipeline.load_report_bot_settings",
                return_value=settings,
            ),
            patch(
                "src.cli.daily_pipeline.ReportingStorage",
                return_value=reporting_storage,
            ),
            patch("src.cli.daily_pipeline.NewClientStorage"),
            patch(
                "src.cli.daily_pipeline.run_daily_pipeline",
                new=AsyncMock(side_effect=pipeline),
            ),
            patch(
                "src.cli.daily_pipeline.observe_query_balance",
                new=AsyncMock(side_effect=RuntimeError("notification unavailable")),
            ) as observe,
            patch(
                "src.cli.daily_pipeline.send_daily_report",
                new=AsyncMock(return_value=dispatch),
            ) as send_report,
            patch(
                "src.cli.daily_pipeline.retry_failed_report_deliveries",
                new=AsyncMock(return_value=()),
            ),
            patch(
                "src.cli.daily_pipeline.send_late_update_reports",
                new=AsyncMock(return_value=()),
            ),
            patch("src.cli.daily_pipeline.print_daily_summary"),
        ):
            result = await _run(arguments)

        observe.assert_awaited_once()
        send_report.assert_awaited_once()
        self.assertEqual(
            send_report.await_args.args[3],
            date.today() - timedelta(days=7),
        )
        self.assertEqual(result.report_dispatch, dispatch)
        reporting_storage.start_pipeline_run.assert_called_once_with(
            date.today() - timedelta(days=1)
        )
        finish_options = reporting_storage.finish_pipeline_run.call_args.kwargs
        self.assertEqual(finish_options["status"], "completed")
        self.assertEqual(finish_options["collected_cards"], 1)
        self.assertEqual(finish_options["available_queries"], 9)
        self.assertEqual(finish_options["processing_status_counts"], {})

    async def test_cli_still_sends_sql_report_when_collection_fails(self) -> None:
        dispatch = ReportDispatchResult(
            report_id=2,
            cohort_date=date.today() - timedelta(days=7),
            clients_count=1,
            sent=1,
            failed=0,
        )
        report_bot = Mock()
        report_bot.__aenter__ = AsyncMock(return_value=report_bot)
        report_bot.__aexit__ = AsyncMock(return_value=None)
        arguments = SimpleNamespace(
            database=Path("daily.db"), date=None, limit=10, timeout=45
        )
        settings = SimpleNamespace(
            token="test-token",
            bootstrap_admin_ids=frozenset({100}),
            low_query_threshold=10,
            report_retention_days=None,
        )
        reporting_storage = Mock()
        reporting_storage.start_pipeline_run.return_value = SimpleNamespace(id=32)
        with (
            patch("src.cli.daily_pipeline.Bot", return_value=report_bot),
            patch(
                "src.cli.daily_pipeline.load_report_bot_settings",
                return_value=settings,
            ),
            patch(
                "src.cli.daily_pipeline.ReportingStorage",
                return_value=reporting_storage,
            ),
            patch("src.cli.daily_pipeline.NewClientStorage"),
            patch(
                "src.cli.daily_pipeline.run_daily_pipeline",
                new=AsyncMock(side_effect=RuntimeError("SBIS unavailable")),
            ),
            patch(
                "src.cli.daily_pipeline.notify_admins", new=AsyncMock()
            ) as notify,
            patch(
                "src.cli.daily_pipeline.retry_failed_report_deliveries",
                new=AsyncMock(return_value=()),
            ),
            patch(
                "src.cli.daily_pipeline.send_late_update_reports",
                new=AsyncMock(return_value=()),
            ),
            patch(
                "src.cli.daily_pipeline.send_daily_report",
                new=AsyncMock(return_value=dispatch),
            ) as send_report,
        ):
            with self.assertRaisesRegex(RuntimeError, "SBIS unavailable"):
                await _run(arguments)

        notify.assert_awaited_once()
        send_report.assert_awaited_once()
        finish_options = reporting_storage.finish_pipeline_run.call_args.kwargs
        self.assertEqual(finish_options["status"], "failed")
        self.assertEqual(finish_options["error_code"], "RuntimeError")

    async def test_balance_failure_has_separate_alert_and_pipeline_stage(self) -> None:
        dispatch = ReportDispatchResult(
            report_id=3,
            cohort_date=date.today() - timedelta(days=7),
            clients_count=0,
            sent=0,
            failed=0,
        )
        report_bot = Mock()
        report_bot.__aenter__ = AsyncMock(return_value=report_bot)
        report_bot.__aexit__ = AsyncMock(return_value=None)
        arguments = SimpleNamespace(
            database=Path("daily.db"), date=None, limit=10, timeout=45
        )
        settings = SimpleNamespace(
            token="test-token",
            bootstrap_admin_ids=frozenset({100}),
            low_query_threshold=10,
            report_retention_days=None,
        )
        reporting_storage = Mock()
        reporting_storage.start_pipeline_run.return_value = SimpleNamespace(id=33)
        with (
            patch("src.cli.daily_pipeline.Bot", return_value=report_bot),
            patch(
                "src.cli.daily_pipeline.load_report_bot_settings",
                return_value=settings,
            ),
            patch(
                "src.cli.daily_pipeline.ReportingStorage",
                return_value=reporting_storage,
            ),
            patch("src.cli.daily_pipeline.NewClientStorage"),
            patch(
                "src.cli.daily_pipeline.run_daily_pipeline",
                new=AsyncMock(
                    side_effect=TelegramBalanceCheckError("balance unavailable")
                ),
            ),
            patch(
                "src.cli.daily_pipeline.notify_admins", new=AsyncMock()
            ),
            patch(
                "src.cli.daily_pipeline.notify_balance_check_failed",
                new=AsyncMock(),
            ) as notify_balance_failed,
            patch(
                "src.cli.daily_pipeline.retry_failed_report_deliveries",
                new=AsyncMock(return_value=()),
            ),
            patch(
                "src.cli.daily_pipeline.send_late_update_reports",
                new=AsyncMock(return_value=()),
            ),
            patch(
                "src.cli.daily_pipeline.send_daily_report",
                new=AsyncMock(return_value=dispatch),
            ),
        ):
            with self.assertRaises(TelegramBalanceCheckError):
                await _run(arguments)

        notify_balance_failed.assert_awaited_once_with(
            report_bot,
            reporting_storage,
            (100,),
        )
        finish_options = reporting_storage.finish_pipeline_run.call_args.kwargs
        self.assertEqual(finish_options["status"], "failed")
        self.assertEqual(finish_options["error_stage"], "telegram_balance")

    async def test_alerts_failed_deliveries_and_cleans_expired_snapshots(
        self,
    ) -> None:
        outcomes = (
            EnrichmentOutcome(
                client_spp_id=1,
                status=ProcessingStatus.PROCESSED,
                phones=(),
                emails=(),
                stage="done",
                result_code="found",
            ),
            EnrichmentOutcome(
                client_spp_id=2,
                status=ProcessingStatus.SKIPPED,
                phones=(),
                emails=(),
                stage="done",
                result_code="not_found",
            ),
        )
        pipeline_result = DailyPipelineResult(
            target_date=date(2026, 8, 8),
            collected_cards=4,
            enrichment_outcomes=outcomes,
        )
        report_date = date.today() - timedelta(days=7)

        def dispatch(report_id: int, failed: int) -> ReportDispatchResult:
            return ReportDispatchResult(
                report_id=report_id,
                cohort_date=report_date,
                clients_count=1,
                sent=0,
                failed=failed,
            )

        report_bot = Mock()
        report_bot.__aenter__ = AsyncMock(return_value=report_bot)
        report_bot.__aexit__ = AsyncMock(return_value=None)
        arguments = SimpleNamespace(
            database=Path("daily.db"), date=None, limit=10, timeout=45
        )
        settings = SimpleNamespace(
            token="test-token",
            bootstrap_admin_ids=frozenset({100}),
            low_query_threshold=10,
            report_retention_days=90,
        )
        reporting_storage = Mock()
        reporting_storage.start_pipeline_run.return_value = SimpleNamespace(id=34)
        before = datetime.now(timezone.utc) - timedelta(days=90, seconds=1)
        with (
            patch("src.cli.daily_pipeline.Bot", return_value=report_bot),
            patch(
                "src.cli.daily_pipeline.load_report_bot_settings",
                return_value=settings,
            ),
            patch(
                "src.cli.daily_pipeline.ReportingStorage",
                return_value=reporting_storage,
            ),
            patch("src.cli.daily_pipeline.NewClientStorage"),
            patch(
                "src.cli.daily_pipeline.run_daily_pipeline",
                new=AsyncMock(return_value=pipeline_result),
            ),
            patch(
                "src.cli.daily_pipeline.notify_admins", new=AsyncMock()
            ) as notify,
            patch(
                "src.cli.daily_pipeline.retry_failed_report_deliveries",
                new=AsyncMock(return_value=(dispatch(1, 1),)),
            ),
            patch(
                "src.cli.daily_pipeline.send_late_update_reports",
                new=AsyncMock(return_value=(dispatch(2, 2),)),
            ),
            patch(
                "src.cli.daily_pipeline.send_daily_report",
                new=AsyncMock(return_value=dispatch(3, 3)),
            ),
            patch("src.cli.daily_pipeline.print_daily_summary"),
        ):
            await _run(arguments)
        after = datetime.now(timezone.utc) - timedelta(days=90) + timedelta(
            seconds=1
        )

        alert = notify.await_args.args[3]
        self.assertIn("Не доставлено: 6", alert)
        cutoff = reporting_storage.delete_report_runs_created_before.call_args.args[0]
        self.assertIsNotNone(cutoff.tzinfo)
        self.assertGreaterEqual(cutoff, before)
        self.assertLessEqual(cutoff, after)
        finish_options = reporting_storage.finish_pipeline_run.call_args.kwargs
        self.assertEqual(finish_options["status"], "completed")
        self.assertEqual(
            finish_options["processing_status_counts"],
            {"processed": 1, "skipped": 1},
        )


class SummaryTests(unittest.TestCase):
    def test_prints_only_aggregate_pipeline_summary(self) -> None:
        result = DailyPipelineResult(
            target_date=date(2026, 8, 9),
            collected_cards=2,
            enrichment_outcomes=(
                EnrichmentOutcome(
                    client_spp_id=123,
                    status=ProcessingStatus.PROCESSED,
                    phones=("+79990000001",),
                    emails=("hidden@example.test",),
                    stage="inn_report_parse",
                    result_code="phone_found_by_inn",
                ),
            ),
        )
        output = StringIO()

        with patch("sys.stdout", output):
            print_daily_summary(result)

        text = output.getvalue()
        self.assertIn("Дата сбора: 2026-08-09", text)
        self.assertIn("Новых карточек СБИС собрано: 2", text)
        self.assertNotIn("+79990000001", text)
        self.assertNotIn("hidden@example.test", text)
        self.assertNotIn("123", text)


if __name__ == "__main__":
    unittest.main()
