import unittest
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.application.telegram_enrichment import EnrichmentOutcome
from src.cli.daily_pipeline import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_LIMIT,
    DailyPipelineResult,
    parse_arguments,
    print_daily_summary,
    run_daily_pipeline,
)
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
