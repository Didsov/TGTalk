"""Офлайн-тесты мониторинга интеграций."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.application.health import (
    DEGRADED,
    HEALTHY,
    RATE_LIMITED,
    UNAUTHORIZED,
    HealthProbeResult,
    _classify_failure,
    probe_report_bot,
    run_active_health_probes,
    run_health_probes,
)
from src.cli.healthcheck import should_notify


class HealthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_bot_get_me_is_read_only_health_probe(self) -> None:
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=1)))

        result = await probe_report_bot(bot)

        self.assertEqual(result, HealthProbeResult("report_bot", HEALTHY))
        bot.get_me.assert_awaited_once_with()

    async def test_probes_run_without_messages_to_paid_bot(self) -> None:
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=1)))
        sbis = AsyncMock(return_value=HealthProbeResult("sbis", HEALTHY))
        telethon = AsyncMock(
            return_value=HealthProbeResult("telethon", UNAUTHORIZED, "expired")
        )

        results = await run_health_probes(
            bot, sbis_probe=sbis, telethon_probe=telethon
        )

        self.assertEqual(
            tuple(item.integration for item in results),
            ("sbis", "telethon", "report_bot"),
        )

    async def test_active_probe_returns_free_profile_balance(self) -> None:
        bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(id=1)))
        sbis = AsyncMock(return_value=HealthProbeResult("sbis", HEALTHY))
        telethon = AsyncMock(
            return_value=(HealthProbeResult("telethon", HEALTHY), 34)
        )

        results, balance = await run_active_health_probes(
            bot,
            sbis_probe=sbis,
            telethon_balance_probe=telethon,
        )

        self.assertEqual(balance, 34)
        self.assertTrue(all(item.status == HEALTHY for item in results))

    def test_classifies_authorization_and_rate_limit_separately(self) -> None:
        self.assertEqual(
            _classify_failure("sbis", RuntimeError("HTTP 403")).status,
            UNAUTHORIZED,
        )
        self.assertEqual(
            _classify_failure("sbis", RuntimeError("HTTP 429")).status,
            RATE_LIMITED,
        )
        self.assertEqual(
            _classify_failure("sbis", RuntimeError("invalid payload")).status,
            DEGRADED,
        )


class NotificationPolicyTests(unittest.TestCase):
    @staticmethod
    def state(status: str, failures: int = 0) -> SimpleNamespace:
        return SimpleNamespace(status=status, consecutive_failures=failures)

    def test_immediate_auth_alert_and_transition_only(self) -> None:
        current = self.state(UNAUTHORIZED, 1)
        self.assertTrue(should_notify(self.state(HEALTHY), current))
        self.assertFalse(should_notify(self.state(UNAUTHORIZED, 1), current))

    def test_network_alert_waits_for_three_failures(self) -> None:
        self.assertFalse(
            should_notify(self.state(HEALTHY), self.state("unreachable", 2))
        )
        self.assertTrue(
            should_notify(
                self.state("unreachable", 2), self.state("unreachable", 3)
            )
        )

    def test_recovery_is_reported(self) -> None:
        self.assertTrue(
            should_notify(self.state("unreachable", 3), self.state(HEALTHY))
        )
