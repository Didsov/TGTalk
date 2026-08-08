"""Детерминированная имитация Telegram-бота поиска контактов."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import web
from dotenv import load_dotenv


router = Router()

REPORT_WITH_PHONE = "with_phone"
REPORT_WITHOUT_PHONE = "without_phone"
REPORT_SECOND_WITHOUT_PHONE = "second_without_phone"
REPORT_EMPTY = "empty"

COUNTRIES = (
    ("🇷🇺 Россия", "ru"),
    ("🇰🇿 Казахстан", "kz"),
    ("🇧🇾 Беларусь", "by"),
    ("🇺🇦 Украина", "ua"),
    ("🇺🇿 Узбекистан", "uz"),
    ("🇹🇲 Туркменистан", "tm"),
    ("🇰🇬 Кыргызстан", "kg"),
)


@dataclass(frozen=True)
class StubSettings:
    host: str
    port: int
    public_base_url: str


def load_stub_settings() -> StubSettings:
    """Прочитать настройки локальной HTTP-раздачи тестовых отчётов."""
    host = os.getenv("TELEGRAM_STUB_REPORT_HOST", "127.0.0.1")
    port_text = os.getenv("TELEGRAM_STUB_REPORT_PORT", "8081")
    try:
        port = int(port_text)
    except ValueError as error:
        raise RuntimeError("TELEGRAM_STUB_REPORT_PORT должен быть целым числом") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("TELEGRAM_STUB_REPORT_PORT должен быть от 1 до 65535")

    public_base_url = os.getenv(
        "TELEGRAM_STUB_REPORT_BASE_URL", f"http://{host}:{port}"
    ).rstrip("/")
    return StubSettings(host=host, port=port, public_base_url=public_base_url)


def inn_scenario(inn: str) -> str:
    """Выбрать воспроизводимый сценарий по последней цифре ИНН."""
    if not inn or not inn.isdigit():
        return "invalid"
    return {
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
    }[inn[-1]]


def person_scenario(text: str) -> str:
    """Определить результат второго поиска по тестовой фамилии."""
    normalized = " ".join(text.casefold().split())
    if normalized.startswith("тестов "):
        return "person_with_phone"
    if normalized.startswith("шкирмин "):
        if normalized.endswith("01.01.1990"):
            return "person_with_phone"
        if normalized.endswith("02.02.1990"):
            return "person_without_phone"
        return "person_not_found"
    if normalized.startswith("безномеров "):
        return "person_without_phone"
    if normalized.startswith("ненайденов "):
        return "person_not_found"
    if normalized.startswith("молчунов "):
        return "person_timeout"
    return "person_not_found"


def report_url(base_url: str, scenario: str, identifier: str) -> str:
    """Сформировать ссылку кнопки без суффикса /txt, как у реального бота."""
    safe_identifier = quote(identifier, safe="")
    return f"{base_url.rstrip('/')}/r/{scenario}/{safe_identifier}"


def report_keyboard(base_url: str, scenario: str, identifier: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📄 Открыть полный отчёт",
                    url=report_url(base_url, scenario, identifier),
                )
            ]
        ]
    )


def country_keyboard(scenario: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(COUNTRIES), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"country:{scenario}:{code}",
                )
                for label, code in COUNTRIES[index : index + 2]
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_text(scenario: str, identifier: str) -> str:
    """Вернуть обезличенный TXT-отчёт выбранного сценария."""
    if scenario == REPORT_EMPTY:
        return ""

    if scenario == REPORT_WITH_PHONE:
        return (
            "=== Общая сводка ===\n"
            "Телефон: 79990000001\n"
            "Email: contact@example.test\n"
            f"ИНН: {identifier}\n\n"
            "=== Тестовый источник 2026 ===\n"
            "ФИО: Примеров Пётр Петрович\n"
            "День рождения: 01.01.1990\n"
            f"ИНН: {identifier}\n"
            "Телефон: 79990000001\n"
        )

    if scenario == REPORT_WITHOUT_PHONE:
        person = "Шкирмин Андрей Романович"
        birthday = "01.01.1990"
    elif scenario == "fallback_without_phone":
        person = "Шкирмин Андрей Романович"
        birthday = "02.02.1990"
    elif scenario == "fallback_not_found":
        person = "Шкирмин Андрей Романович"
        birthday = "03.03.1990"
    elif scenario == REPORT_SECOND_WITHOUT_PHONE:
        return (
            "=== Общая сводка ===\n"
            "Телефон: \n"
            "Email: second@example.test\n\n"
            "=== Тестовый источник 2026 ===\n"
            "ФИО: Шкирмин Андрей Романович\n"
            "День рождения: 02.02.1990\n"
        )
    else:
        raise KeyError(scenario)

    return (
        "=== Общая сводка ===\n"
        "Телефон: \n"
        "Email: info@example.test\n"
        f"ИНН: {identifier}\n\n"
        "=== Тестовый источник 2026 ===\n"
        f"ФИО: {person}\n"
        f"День рождения: {birthday}\n"
        f"ИНН: {identifier}\n"
    )


def _inn_from_message(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


@router.message(CommandStart())
@router.message(Command("help"))
async def show_help(message: Message) -> None:
    await message.answer(
        "Тестовый бот поиска. Отправьте /inn <ИНН>. "
        "Сценарий определяется последней цифрой ИНН."
    )


@router.message(Command("inn"))
async def search_by_inn(message: Message, report_base_url: str) -> None:
    inn = _inn_from_message(message.text or "")
    scenario = inn_scenario(inn)

    if scenario == "invalid":
        await message.reply("ИНН должен состоять только из цифр.")
        return
    if scenario == "timeout":
        # Намеренное молчание позволяет проверить timeout основного клиента.
        return
    if scenario == "not_found":
        await message.reply(
            "К сожалению, по данному запросу ничего не найдено.\n"
            "Количество доступных запросов не изменилось."
        )
        return
    if scenario == "unknown":
        await message.reply("Поиск завершён, но формат результата неизвестен.")
        return

    report_scenario = scenario
    await message.reply(
        f"🔎 Запрос: {inn}\nОбнаружены тестовые совпадения.",
        reply_markup=report_keyboard(report_base_url, report_scenario, inn),
    )


@router.message(F.text)
async def search_by_person(message: Message) -> None:
    text = (message.text or "").strip()
    scenario = person_scenario(text)
    if scenario == "person_timeout":
        return

    await message.reply(
        f"Параметры запроса распознаны.\nЗапрос: {text}\n\n"
        "Подтвердите параметры запроса и выберите страну для поиска.",
        reply_markup=country_keyboard(scenario),
    )


@router.callback_query(F.data.startswith("country:"))
async def select_country(callback: CallbackQuery, report_base_url: str) -> None:
    data = callback.data or ""
    _, scenario, country = data.split(":", maxsplit=2)
    await callback.answer()
    if callback.message is None:
        return

    if country != "ru" or scenario == "person_not_found":
        await callback.message.edit_text(
            "К сожалению, по данному запросу ничего не найдено.\n"
            "Количество доступных запросов не изменилось.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Попробовать комбинированный поиск",
                            callback_data="combined:ignored",
                        )
                    ]
                ]
            ),
        )
        return

    if scenario == "person_without_phone":
        report_scenario = REPORT_SECOND_WITHOUT_PHONE
    else:
        report_scenario = REPORT_WITH_PHONE
    await callback.message.answer(
        "Поиск завершён. Открыть полный тестовый отчёт можно по ссылке.",
        reply_markup=report_keyboard(
            report_base_url, report_scenario, "person-result"
        ),
    )


@router.callback_query(F.data == "combined:ignored")
async def ignore_combined_search(callback: CallbackQuery) -> None:
    await callback.answer(
        "Комбинированный поиск отключён в тестовом сценарии.", show_alert=True
    )


async def report_page(request: web.Request) -> web.Response:
    scenario = request.match_info["scenario"]
    identifier = request.match_info["identifier"]
    try:
        report_text(scenario, identifier)
    except KeyError:
        raise web.HTTPNotFound() from None
    return web.Response(
        text="Добавьте /txt к адресу, чтобы получить тестовый отчёт.",
        content_type="text/plain",
        charset="utf-8",
    )


async def report_txt(request: web.Request) -> web.Response:
    scenario = request.match_info["scenario"]
    identifier = request.match_info["identifier"]
    try:
        text = report_text(scenario, identifier)
    except KeyError:
        raise web.HTTPNotFound() from None
    return web.Response(text=text, content_type="text/plain", charset="utf-8")


def create_report_app() -> web.Application:
    application = web.Application()
    application.router.add_get("/r/{scenario}/{identifier}", report_page)
    application.router.add_get("/r/{scenario}/{identifier}/txt", report_txt)
    return application


async def start_report_server(settings: StubSettings) -> web.AppRunner:
    runner = web.AppRunner(create_report_app())
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    await site.start()
    return runner


async def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная окружения TELEGRAM_BOT_TOKEN")

    settings = load_stub_settings()
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    report_runner = await start_report_server(settings)
    logging.info("Тестовые отчёты: %s", settings.public_base_url)

    try:
        async with Bot(token=token) as bot:
            await dispatcher.start_polling(
                bot,
                report_base_url=settings.public_base_url,
            )
    finally:
        await report_runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
