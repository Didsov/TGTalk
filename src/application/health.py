"""Безопасные проверки доступности внешних интеграций."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp
from aiogram import Bot
from telethon import TelegramClient

from src.config import loadEnvironment, requireSetting
from src.integrations.sbis.companies import _get_company_page
from src.integrations.telegram.bot_client import SESSION_PATH
from src.integrations.telegram.bot_client import getAvailableQueries


HEALTHY = "healthy"
UNAUTHORIZED = "unauthorized"
RATE_LIMITED = "rate_limited"
UNREACHABLE = "unreachable"
DEGRADED = "degraded"


@dataclass(frozen=True)
class HealthProbeResult:
    """Обезличенный результат одной проверки."""

    integration: str
    status: str
    error_code: str | None = None


async def probe_sbis(timeout: float = 30) -> HealthProbeResult:
    """Проверить cookie тем же read-only запросом списка, что использует конвейер."""
    loadEnvironment()
    cookie = requireSetting("SBIS_BROWSER_COOKIE")
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            await _get_company_page(session, cookie, 0)
    except Exception as error:
        return _classify_failure("sbis", error)
    return HealthProbeResult("sbis", HEALTHY)


async def probe_telethon(timeout: float = 30) -> HealthProbeResult:
    """Проверить авторизацию пользовательской сессии без сообщений платному боту."""
    loadEnvironment()
    api_id_text = requireSetting("TELEGRAM_API_ID")
    api_hash = requireSetting("TELEGRAM_API_HASH")
    try:
        api_id = int(api_id_text)
    except ValueError:
        return HealthProbeResult("telethon", DEGRADED, "invalid_api_id")

    client = TelegramClient(SESSION_PATH, api_id, api_hash)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=timeout):
            return HealthProbeResult("telethon", UNAUTHORIZED, "session_unauthorized")
        me = await asyncio.wait_for(client.get_me(), timeout=timeout)
        if me is None:
            return HealthProbeResult("telethon", UNAUTHORIZED, "profile_missing")
    except Exception as error:
        return _classify_failure("telethon", error)
    finally:
        await client.disconnect()
    return HealthProbeResult("telethon", HEALTHY)


async def probe_telethon_with_balance(
    timeout: float = 30,
) -> tuple[HealthProbeResult, int | None]:
    """Проверить сессию и бесплатно прочитать баланс через меню поискового бота."""
    loadEnvironment()
    api_id_text = requireSetting("TELEGRAM_API_ID")
    api_hash = requireSetting("TELEGRAM_API_HASH")
    bot_username = requireSetting("TELEGRAM_TARGET_BOT")
    try:
        api_id = int(api_id_text)
    except ValueError:
        return HealthProbeResult("telethon", DEGRADED, "invalid_api_id"), None

    client = TelegramClient(SESSION_PATH, api_id, api_hash)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=timeout):
            return (
                HealthProbeResult(
                    "telethon", UNAUTHORIZED, "session_unauthorized"
                ),
                None,
            )
        balance = await getAvailableQueries(
            client,
            bot_username,
            timeout=timeout,
        )
    except Exception as error:
        return _classify_failure("telethon", error), None
    finally:
        await client.disconnect()
    return HealthProbeResult("telethon", HEALTHY), balance


async def probe_report_bot(bot: Bot) -> HealthProbeResult:
    """Проверить токен отчётного бота через безопасный Bot API getMe."""
    try:
        await bot.get_me()
    except Exception as error:
        return _classify_failure("report_bot", error)
    return HealthProbeResult("report_bot", HEALTHY)


async def run_health_probes(
    bot: Bot,
    *,
    sbis_probe: Callable[[], Awaitable[HealthProbeResult]] = probe_sbis,
    telethon_probe: Callable[[], Awaitable[HealthProbeResult]] = probe_telethon,
) -> tuple[HealthProbeResult, ...]:
    """Последовательно проверить интеграции, не создавая всплеска запросов."""
    return (
        await sbis_probe(),
        await telethon_probe(),
        await probe_report_bot(bot),
    )


async def run_active_health_probes(
    bot: Bot,
    *,
    sbis_probe: Callable[[], Awaitable[HealthProbeResult]] = probe_sbis,
    telethon_balance_probe: Callable[
        [], Awaitable[tuple[HealthProbeResult, int | None]]
    ] = probe_telethon_with_balance,
) -> tuple[tuple[HealthProbeResult, ...], int | None]:
    """Проверить интеграции и получить текущий бесплатный остаток запросов."""
    sbis_result = await sbis_probe()
    telethon_result, balance = await telethon_balance_probe()
    report_bot_result = await probe_report_bot(bot)
    return (sbis_result, telethon_result, report_bot_result), balance


def _classify_failure(integration: str, error: Exception) -> HealthProbeResult:
    """Классифицировать сбой по безопасному коду без текста ответа и секретов."""
    name = type(error).__name__
    text = str(error).casefold()
    if "429" in text or name == "FloodWaitError":
        return HealthProbeResult(integration, RATE_LIMITED, "rate_limited")
    if (
        "401" in text
        or "403" in text
        or "unauthorized" in text
        or name
        in {
            "AuthKeyUnregisteredError",
            "SessionRevokedError",
            "UserDeactivatedError",
            "TelegramUnauthorizedError",
        }
    ):
        return HealthProbeResult(integration, UNAUTHORIZED, "authorization_failed")
    if isinstance(error, (TimeoutError, aiohttp.ClientError)) or name in {
        "ConnectionError",
        "OSError",
    }:
        return HealthProbeResult(integration, UNREACHABLE, "service_unreachable")
    return HealthProbeResult(integration, DEGRADED, name[:100])
