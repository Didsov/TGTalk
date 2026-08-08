import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.application.telegram_enrichment import EnrichmentOutcome
from src.cli.telegram_enrichment import (
    DEFAULT_DATABASE_PATH,
    parse_arguments,
    print_summary,
    run_enrichment,
)
from src.storage import ProcessingStatus


def outcome(
    *,
    spp_id: int = 101,
    status: ProcessingStatus = ProcessingStatus.PROCESSED,
    result_code: str = "phone_found_by_inn",
    phones: tuple[str, ...] = ("+79990000001",),
    emails: tuple[str, ...] = ("owner@example.test",),
    retry_after_seconds: int | None = None,
) -> EnrichmentOutcome:
    return EnrichmentOutcome(
        client_spp_id=spp_id,
        status=status,
        phones=phones,
        emails=emails,
        stage="inn_report_parse",
        result_code=result_code,
        retry_after_seconds=retry_after_seconds,
    )


class ArgumentTests(unittest.TestCase):
    def test_parses_required_limit_with_safe_defaults(self) -> None:
        arguments = parse_arguments(["--limit", "3"])

        self.assertEqual(arguments.database, DEFAULT_DATABASE_PATH)
        self.assertEqual(arguments.limit, 3)
        self.assertEqual(arguments.timeout, 30.0)
        self.assertFalse(arguments.write)

    def test_parses_database_timeout_and_write_mode(self) -> None:
        arguments = parse_arguments(
            [
                "--database",
                "test-data/clients.db",
                "--limit",
                "2",
                "--timeout",
                "7.5",
                "--write",
            ]
        )

        self.assertEqual(arguments.database, Path("test-data/clients.db"))
        self.assertEqual(arguments.limit, 2)
        self.assertEqual(arguments.timeout, 7.5)
        self.assertTrue(arguments.write)

    def test_rejects_non_positive_limit(self) -> None:
        for value in ("0", "-1", "1.5"):
            with self.subTest(value=value):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parse_arguments(["--limit", value])

    def test_rejects_non_positive_or_non_finite_timeout(self) -> None:
        for value in ("0", "-0.1", "nan", "inf", "text"):
            with self.subTest(value=value):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parse_arguments(["--limit", "1", "--timeout", value])


class RunEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_requested_dry_run_and_closes_telegram(self) -> None:
        database_path = Path("test-data/clients.db")
        storage = Mock()
        telegram_client = Mock()
        expected = [outcome()]

        with (
            patch(
                "src.cli.telegram_enrichment.NewClientStorage",
                return_value=storage,
            ) as storage_class,
            patch("src.cli.telegram_enrichment.loadEnvironment") as load_environment,
            patch(
                "src.cli.telegram_enrichment.allowed_report_hosts",
                return_value=("localhost:8081",),
            ) as validate_report_hosts,
            patch(
                "src.cli.telegram_enrichment.openTg",
                new=AsyncMock(return_value=telegram_client),
            ) as open_tg,
            patch(
                "src.cli.telegram_enrichment.closeTg",
                new=AsyncMock(),
            ) as close_tg,
            patch(
                "src.cli.telegram_enrichment.requireSetting",
                return_value="@stub_search_bot",
            ) as require_setting,
            patch(
                "src.cli.telegram_enrichment.process_first_clients",
                new=AsyncMock(return_value=expected),
            ) as process,
        ):
            result = await run_enrichment(
                database_path,
                limit=2,
                timeout=7.5,
            )

        storage_class.assert_called_once_with(database_path)
        storage.initialize.assert_called_once_with()
        load_environment.assert_called_once_with()
        validate_report_hosts.assert_called_once_with()
        open_tg.assert_awaited_once_with()
        require_setting.assert_called_once_with("TELEGRAM_TARGET_BOT")
        process.assert_awaited_once_with(
            storage,
            telegram_client,
            "@stub_search_bot",
            limit=2,
            write=False,
            timeout=7.5,
        )
        close_tg.assert_awaited_once_with(telegram_client)
        self.assertEqual(result, expected)

    async def test_passes_write_mode(self) -> None:
        storage = Mock()
        telegram_client = Mock()

        with (
            patch(
                "src.cli.telegram_enrichment.NewClientStorage",
                return_value=storage,
            ),
            patch("src.cli.telegram_enrichment.loadEnvironment"),
            patch(
                "src.cli.telegram_enrichment.allowed_report_hosts",
                return_value=("localhost:8081",),
            ),
            patch(
                "src.cli.telegram_enrichment.openTg",
                new=AsyncMock(return_value=telegram_client),
            ),
            patch(
                "src.cli.telegram_enrichment.closeTg",
                new=AsyncMock(),
            ),
            patch(
                "src.cli.telegram_enrichment.requireSetting",
                return_value="@stub_search_bot",
            ),
            patch(
                "src.cli.telegram_enrichment.process_first_clients",
                new=AsyncMock(return_value=[]),
            ) as process,
        ):
            await run_enrichment("clients.db", limit=1, write=True)

        self.assertTrue(process.await_args.kwargs["write"])

    async def test_closes_telegram_when_processing_fails(self) -> None:
        storage = Mock()
        telegram_client = Mock()

        with (
            patch(
                "src.cli.telegram_enrichment.NewClientStorage",
                return_value=storage,
            ),
            patch("src.cli.telegram_enrichment.loadEnvironment"),
            patch(
                "src.cli.telegram_enrichment.allowed_report_hosts",
                return_value=("localhost:8081",),
            ),
            patch(
                "src.cli.telegram_enrichment.openTg",
                new=AsyncMock(return_value=telegram_client),
            ),
            patch(
                "src.cli.telegram_enrichment.closeTg",
                new=AsyncMock(),
            ) as close_tg,
            patch(
                "src.cli.telegram_enrichment.requireSetting",
                return_value="@stub_search_bot",
            ),
            patch(
                "src.cli.telegram_enrichment.process_first_clients",
                new=AsyncMock(side_effect=RuntimeError("test failure")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                await run_enrichment("clients.db", limit=1)

        close_tg.assert_awaited_once_with(telegram_client)

    async def test_missing_target_does_not_open_telegram(self) -> None:
        open_tg = AsyncMock()

        with (
            patch("src.cli.telegram_enrichment.loadEnvironment") as load_environment,
            patch(
                "src.cli.telegram_enrichment.requireSetting",
                side_effect=RuntimeError("missing target"),
            ),
            patch("src.cli.telegram_enrichment.openTg", new=open_tg),
        ):
            with self.assertRaisesRegex(RuntimeError, "missing target"):
                await run_enrichment("clients.db", limit=1)

        load_environment.assert_called_once_with()
        open_tg.assert_not_awaited()

    async def test_missing_report_allowlist_does_not_open_telegram(self) -> None:
        open_tg = AsyncMock()

        with (
            patch("src.cli.telegram_enrichment.loadEnvironment"),
            patch(
                "src.cli.telegram_enrichment.requireSetting",
                return_value="@stub_search_bot",
            ),
            patch(
                "src.cli.telegram_enrichment.allowed_report_hosts",
                side_effect=RuntimeError("missing allowlist"),
            ),
            patch("src.cli.telegram_enrichment.openTg", new=open_tg),
        ):
            with self.assertRaisesRegex(RuntimeError, "missing allowlist"):
                await run_enrichment("clients.db", limit=1)

        open_tg.assert_not_awaited()


class SummaryTests(unittest.TestCase):
    def test_prints_only_aggregate_non_sensitive_results(self) -> None:
        outcomes = [
            outcome(),
            outcome(
                spp_id=102,
                status=ProcessingStatus.RETRY_REQUIRED,
                result_code="temporary_error",
                phones=("+79990000002",),
                emails=("hidden@example.test",),
                retry_after_seconds=17,
            ),
        ]
        output = io.StringIO()

        print_summary(outcomes, write=False, output=output)

        text = output.getvalue()
        self.assertIn("dry-run", text)
        self.assertIn("Обработано клиентов: 2", text)
        self.assertIn("Обработан: 1", text)
        self.assertIn("Требуется повторная обработка: 1", text)
        self.assertIn("phone_found_by_inn: 1", text)
        self.assertIn("не раньше чем через 17 сек", text)
        self.assertNotIn("+79990000001", text)
        self.assertNotIn("+79990000002", text)
        self.assertNotIn("owner@example.test", text)
        self.assertNotIn("hidden@example.test", text)
        self.assertNotIn("101", text)
        self.assertNotIn("102", text)


if __name__ == "__main__":
    unittest.main()
