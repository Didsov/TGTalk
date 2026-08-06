"""Интерактивная консоль для ручного диалога с Telegram-ботом."""

import asyncio

from telethon import TelegramClient

from src.config import requireSetting
from src.integrations.telegram.bot_client import closeTg, openTg, sendMessageAndWait


async def run_console(client: TelegramClient, bot_username: str) -> None:
    """Запрашивать сообщения в цикле и печатать ответы бота."""
    print("Введите сообщение. Для выхода напишите /exit.")

    while True:
        # input блокирует поток, поэтому переносим его из цикла asyncio.
        text = await asyncio.to_thread(input, "Вы: ")
        if text.strip().lower() == "/exit":
            break
        if not text:
            print("Пустое сообщение не отправлено.")
            continue

        try:
            response = await sendMessageAndWait(
                client,
                bot_username,
                text,
            )
        except TimeoutError:
            print("Бот не ответил за 30 секунд.")
        else:
            print(f"Бот: {response}")


async def main() -> None:
    client = await openTg()
    try:
        bot_username = requireSetting("TELEGRAM_TARGET_BOT")
        await run_console(client, bot_username)
    finally:
        await closeTg(client)


if __name__ == "__main__":
    asyncio.run(main())
