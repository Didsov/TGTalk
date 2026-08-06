"""Telegram-бот для имитации внешнего сервиса поиска контактов."""

import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from dotenv import load_dotenv


router = Router()


def transform_message(text: str) -> str:
    """Умножить число на два или развернуть нечисловой текст."""
    value = text.strip()

    # Decimal точно обрабатывает десятичные числа без погрешностей float.
    try:
        number = Decimal(value)
    except InvalidOperation:
        return text[::-1]

    # NaN и Infinity формально распознаются Decimal, но обычными числами не являются.
    if not number.is_finite():
        return text[::-1]

    doubled = number * 2
    if doubled == doubled.to_integral_value():
        return str(int(doubled))

    return format(doubled.normalize(), "f")


# Декоратор без фильтров направляет сюда сообщения любого типа.
@router.message()
async def reply_to_message(message: Message) -> None:
    if message.text is None:
        await message.answer("Поддерживаются только текстовые сообщения.")
        return

    await message.answer(transform_message(message.text))


async def main() -> None:
    # По умолчанию load_dotenv ищет файл .env от текущего рабочего каталога вверх.
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задана переменная окружения TELEGRAM_BOT_TOKEN")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    # Контекстный менеджер гарантированно закрывает HTTP-сессию бота при остановке.
    async with Bot(token=token) as bot:
        await dispatcher.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
