"""Клиент для отправки сообщения Telegram-боту от пользовательского аккаунта."""

from telethon import TelegramClient

from src.config import PROJECT_ROOT, loadEnvironment, requireSetting


SESSION_PATH = PROJECT_ROOT / "telegram_test_user"


async def openTg() -> TelegramClient:
    """Создать Telegram-клиент, авторизовать его и вернуть вызывающему коду."""
    loadEnvironment()

    api_id_text = requireSetting("TELEGRAM_API_ID")
    try:
        api_id = int(api_id_text)
    except ValueError as error:
        raise RuntimeError("TELEGRAM_API_ID должен быть целым числом") from error

    api_hash = requireSetting("TELEGRAM_API_HASH")
    phone = requireSetting("TELEGRAM_PHONE")

    # При первом запуске start запросит код и пароль 2FA, если он включен.
    client = TelegramClient(SESSION_PATH, api_id, api_hash)
    await client.start(phone=phone)
    return client


async def closeTg(client: TelegramClient) -> None:
    """Закрыть соединение Telegram-клиента и освободить сетевые ресурсы."""
    await client.disconnect()


async def sendMessageAndWait(
    client: TelegramClient,
    bot_username: str,
    text: str,
    timeout: float = 30,
) -> str:
    """Отправить текст боту и вернуть следующее полученное от него сообщение."""
    if not text:
        raise ValueError("Нельзя отправить пустое сообщение")

    # Conversation связывает отправку и ожидание ответа в одном диалоге.
    async with client.conversation(bot_username, timeout=timeout) as conversation:
        await conversation.send_message(text)
        response = await conversation.get_response()

    return response.raw_text
