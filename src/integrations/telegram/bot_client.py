"""Клиент для отправки сообщения Telegram-боту от пользовательского аккаунта."""

from __future__ import annotations

import asyncio
import re
from enum import Enum
from typing import Any

from telethon import TelegramClient
from telethon.tl.custom import Message

from src.config import PROJECT_ROOT, loadEnvironment, requireSetting


SESSION_PATH = PROJECT_ROOT / "telegram_test_user"


class BotResponseKind(str, Enum):
    """Явные терминальные варианты текстового ответа поискового бота."""

    NOT_FOUND = "not_found"
    RETRYABLE_ERROR = "retryable_error"
    UNKNOWN = "unknown"
    OTHER = "other"


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


async def sendQueryAndWait(
    conversation: Any,
    text: str,
    timeout: float = 30,
) -> Message:
    """Отправить запрос и дождаться содержательного ответа либо его правки."""
    if not text:
        raise ValueError("Нельзя отправить пустое сообщение")

    sent_message = await conversation.send_message(text)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    waiters = _responseOrEditWaiters(conversation, sent_message, timeout)
    try:
        response = await _waitForFirstMessage(waiters, timeout)
    finally:
        await _cancelWaiters(conversation, waiters)

    while not await _isQueryResponseReady(response):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError("Telegram-бот не прислал итоговый ответ")
        waiters = _responseOrEditWaiters(conversation, response, remaining)
        try:
            response = await _waitForFirstMessage(waiters, remaining)
        finally:
            await _cancelWaiters(conversation, waiters)
    return response


async def sendMessageAndGetResponse(
    client: TelegramClient,
    bot_username: str,
    text: str,
    timeout: float = 30,
) -> Message:
    """Отправить запрос в отдельном диалоге и вернуть полный ответ Telegram."""
    if not text:
        raise ValueError("Нельзя отправить пустое сообщение")

    async with client.conversation(bot_username, timeout=timeout) as conversation:
        return await sendQueryAndWait(conversation, text, timeout=timeout)


def classifyBotResponse(message: Message | str) -> BotResponseKind:
    """Распознать явные ответы «не найдено» и «неизвестный формат»."""
    if isinstance(message, str):
        text = message
    else:
        text = getattr(message, "raw_text", None) or getattr(message, "text", "")

    normalized = " ".join(str(text).casefold().split())
    if "ничего не найдено" in normalized:
        return BotResponseKind.NOT_FOUND
    if any(
        marker in normalized
        for marker in (
            "слишком много запросов",
            "лимит запросов",
            "попробуйте позже",
            "повторите позже",
            "временно недоступ",
            "временная ошибка",
            "техническая ошибка",
        )
    ):
        return BotResponseKind.RETRYABLE_ERROR
    if "формат результата неизвестен" in normalized:
        return BotResponseKind.UNKNOWN
    return BotResponseKind.OTHER


def _iterMessageButtons(message: Message) -> Any:
    rows = getattr(message, "buttons", None) or ()
    for row in rows:
        if isinstance(row, (list, tuple)):
            yield from row
        else:
            yield row


def extractReportUrl(message: Message) -> str | None:
    """Вернуть URL отчёта из inline-кнопки, не нажимая и не открывая её."""
    url_buttons: list[tuple[str, str]] = []
    for button in _iterMessageButtons(message):
        url = getattr(button, "url", None)
        if not isinstance(url, str) or not url:
            continue
        text = str(getattr(button, "text", ""))
        url_buttons.append((text, url))

    if not url_buttons:
        return None

    for text, url in url_buttons:
        normalized = text.casefold()
        if "отчёт" in normalized or "отчет" in normalized:
            return url
    return url_buttons[0][1]


async def extractReportUrlAsync(message: Message) -> str | None:
    """Извлечь URL, при необходимости асинхронно загрузив inline-кнопки."""
    buttons = await _messageButtons(message)
    return _reportUrlFromButtons(buttons)


def _reportUrlFromButtons(buttons: list[Any]) -> str | None:
    url_buttons: list[tuple[str, str]] = []
    for button in buttons:
        url = getattr(button, "url", None)
        if not isinstance(url, str) or not url:
            continue
        url_buttons.append((str(getattr(button, "text", "")), url))
    for text, url in url_buttons:
        normalized = text.casefold()
        if "отчёт" in normalized or "отчет" in normalized:
            return url
    return url_buttons[0][1] if url_buttons else None


async def getLatestIncomingMessage(
    client: TelegramClient,
    bot_username: str,
    *,
    limit: int = 20,
) -> Message:
    """Прочитать последнее входящее сообщение, ничего не отправляя боту."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit должен быть положительным целым числом")
    messages = await client.get_messages(bot_username, limit=limit)
    for message in messages:
        if not bool(getattr(message, "out", False)):
            return message
    raise LookupError("В диалоге с ботом нет входящих сообщений")


async def _messageButtons(message: Message) -> list[Any]:
    buttons = list(_iterMessageButtons(message))
    if buttons:
        return buttons

    get_buttons = getattr(message, "get_buttons", None)
    if not callable(get_buttons):
        return []

    rows = await get_buttons()
    result: list[Any] = []
    for row in rows or ():
        if isinstance(row, (list, tuple)):
            result.extend(row)
        else:
            result.append(row)
    return result


async def getMessageButtons(message: Message) -> list[Any]:
    """Вернуть плоский список кнопок, включая лениво загружаемые."""
    return await _messageButtons(message)


async def clickRussiaCallback(message: Message) -> Any:
    """Нажать callback-кнопку России, находя её по тексту, а не позиции."""
    for button in await _messageButtons(message):
        text = str(getattr(button, "text", ""))
        if "россия" not in text.casefold():
            continue
        if getattr(button, "data", None) is None:
            raise ValueError("Кнопка «Россия» не является callback-кнопкой")
        return await button.click()

    raise LookupError("В ответе бота нет callback-кнопки «Россия»")


async def clickProfileCallback(message: Message) -> Any:
    """Нажать callback-кнопку «Мой профиль», находя её по тексту."""
    for button in await _messageButtons(message):
        text = " ".join(str(getattr(button, "text", "")).casefold().split())
        if "мой профиль" not in text:
            continue
        if getattr(button, "data", None) is None:
            raise ValueError("Кнопка «Мой профиль» не является callback-кнопкой")
        return await button.click()
    raise LookupError("В меню бота нет callback-кнопки «Мой профиль»")


def parseAvailableQueries(message: Message | str) -> int:
    """Извлечь неотрицательное число из поля «Доступно запросов»."""
    if isinstance(message, str):
        text = message
    else:
        text = getattr(message, "raw_text", None) or getattr(message, "text", "")
    match = re.search(
        r"доступно\s+запросов[*_\s]*:\s*[*_\s]*(\d+)",
        str(text),
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError("Ответ профиля не содержит поле «Доступно запросов»")
    return int(match.group(1))


async def getAvailableQueries(
    client: TelegramClient,
    bot_username: str,
    timeout: float = 30,
) -> int:
    """Получить остаток запросов через /menu и callback «Мой профиль»."""
    async with client.conversation(bot_username, timeout=timeout) as conversation:
        menu_message = await sendQueryAndWait(
            conversation,
            "/menu",
            timeout=timeout,
        )
        waiters = _responseOrEditWaiters(conversation, menu_message, timeout)
        click_task = asyncio.create_task(clickProfileCallback(menu_message))
        profile_task = asyncio.create_task(
            _waitForFirstMessage(waiters, timeout)
        )
        await asyncio.sleep(0)
        try:
            while True:
                done, _ = await asyncio.wait(
                    (profile_task, click_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if profile_task in done:
                    profile_message = profile_task.result()
                    break
                # Успешный callback-ответ ещё не означает, что сообщение
                # профиля уже пришло. Ошибку клика при этом не скрываем.
                click_task.result()
                profile_message = await profile_task
                break
        finally:
            for task in (click_task, profile_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                click_task,
                profile_task,
                return_exceptions=True,
            )
            await _cancelWaiters(conversation, waiters)
    return parseAvailableQueries(profile_message)


def _responseOrEditWaiters(
    conversation: Any,
    message: Message,
    timeout: float,
) -> tuple[asyncio.Future[Any], asyncio.Future[Any]]:
    return (
        asyncio.ensure_future(
            conversation.get_response(message, timeout=timeout)
        ),
        # get_edit отсчитывает входящие сообщения от последнего исходящего.
        # Передача самого редактируемого Message не поймает edit с тем же id.
        asyncio.ensure_future(conversation.get_edit(timeout=timeout)),
    )


async def _cancelWaiters(
    conversation: Any,
    waiters: tuple[asyncio.Future[Any], ...],
) -> None:
    for waiter in waiters:
        if not waiter.done():
            waiter.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)

    # Telethon Conversation leaves a cancelled inner future registered in its
    # private pending maps. A later update would otherwise try to resolve that
    # cancelled future and could break the conversation event handler.
    for attribute in ("_pending_responses", "_pending_edits"):
        pending = getattr(conversation, attribute, None)
        if not isinstance(pending, dict):
            continue
        for key, future in tuple(pending.items()):
            if future.cancelled():
                pending.pop(key, None)


async def _waitForFirstMessage(
    waiters: tuple[asyncio.Future[Any], ...],
    timeout: float,
) -> Message:
    pending = set(waiters)
    errors: list[BaseException] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while pending:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                "Telegram-бот не прислал ответ и не изменил сообщение"
            )

        done, pending = await asyncio.wait(
            pending,
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError(
                "Telegram-бот не прислал ответ и не изменил сообщение"
            )

        # Если оба события пришли одновременно, новый ответ имеет приоритет.
        for waiter in waiters:
            if waiter not in done:
                continue
            try:
                return waiter.result()
            except asyncio.CancelledError:
                continue
            except Exception as error:
                errors.append(error)

    if errors:
        raise errors[0]
    raise asyncio.TimeoutError(
        "Telegram-бот не прислал ответ и не изменил сообщение"
    )


async def waitForResponseOrEdit(
    conversation: Any,
    message: Message,
    timeout: float = 30,
) -> Message:
    """Дождаться первого успешного нового ответа или редактирования сообщения."""
    waiters = _responseOrEditWaiters(conversation, message, timeout)
    try:
        return await _waitForFirstMessage(waiters, timeout)
    finally:
        await _cancelWaiters(conversation, waiters)


async def clickRussiaAndWait(
    conversation: Any,
    message: Message,
    timeout: float = 30,
) -> Message:
    """Нажать «Россия» и дождаться нового либо отредактированного ответа."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    waiters = _responseOrEditWaiters(conversation, message, timeout)
    # AsyncMock и другие coroutine-based адаптеры регистрируются на этом такте.
    await asyncio.sleep(0)

    try:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError("Истекло время на нажатие кнопки «Россия»")
        await asyncio.wait_for(clickRussiaCallback(message), timeout=remaining)

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                "Telegram-бот не прислал ответ и не изменил сообщение"
            )
        response = await _waitForFirstMessage(waiters, remaining)
    finally:
        await _cancelWaiters(conversation, waiters)

    while not await _isTerminalBotResponse(response):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError(
                "Telegram-бот не прислал итоговый ответ"
            )
        waiters = _responseOrEditWaiters(conversation, response, remaining)
        try:
            response = await _waitForFirstMessage(waiters, remaining)
        finally:
            await _cancelWaiters(conversation, waiters)
    return response


async def _isTerminalBotResponse(message: Message) -> bool:
    return (
        classifyBotResponse(message) is not BotResponseKind.OTHER
        or await extractReportUrlAsync(message) is not None
    )


async def _isQueryResponseReady(message: Message) -> bool:
    """Отличить итоговый ответ от промежуточного сообщения «поиск идёт»."""
    if classifyBotResponse(message) is not BotResponseKind.OTHER:
        return True
    buttons = await _messageButtons(message)
    return bool(buttons)
