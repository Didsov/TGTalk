# Telegram-бот-заглушка

Бот нужен для разработки интеграции без обращения к реальному сервису поиска
контактов. Он работает через long polling и отвечает на каждое сообщение:

- число умножает на два: `123` -> `246`, `2.5` -> `5`;
- текст разворачивает: `Привет` -> `тевирП`;
- для нетекстового сообщения сообщает, что поддерживается только текст.

## Подготовка

1. Откройте в Telegram бота `@BotFather`.
2. Выполните команду `/newbot` и следуйте его инструкциям.
3. Запишите полученный токен в локальный файл `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=токен-от-BotFather
```

Файл `.env` исключен из Git и не должен передаваться другим пользователям.

4. Установите зависимости:

```powershell
python -m pip install -r requirements.txt
```

## Запуск в PowerShell

```powershell
python -m src.integrations.telegram.stub_bot
```

После появления сообщения о запуске откройте своего бота в Telegram, нажмите
`Start` и отправьте ему число или текст. Остановить процесс можно клавишами
`Ctrl+C`.

## Проверка

```powershell
python -m unittest discover -s tests -v
```

Тесты проверяют преобразование сообщений локально и не обращаются к Telegram.

## Интерактивный клиент

Telegram Bot API не доставляет личные сообщения от одного бота другому. Поэтому
для проверки используется отдельный пользовательский аккаунт через Telegram API.

1. Войдите на `https://my.telegram.org` тестовым аккаунтом.
2. Откройте `API development tools` и создайте приложение.
3. Заполните в `.env` полученные параметры и username заглушки:

```dotenv
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=секретный_api_hash
TELEGRAM_PHONE=+79990000000
TELEGRAM_TARGET_BOT=@username_бота
```

Не используйте чужие `api_id` и `api_hash`. Для production необходимо отдельно
подтвердить допустимость автоматизации целевого пользовательского аккаунта.

Сначала запустите бота-заглушку в одном терминале:

```powershell
python -m src.integrations.telegram.stub_bot
```

Во втором терминале запустите интерактивный клиент:

```powershell
python -m src.cli.telegram_bot_console
```

При первом запуске Telegram пришлет код авторизации. После успешного входа рядом
с проектом появится локальный файл `telegram_test_user.session`; он исключен из
Git. Команда `/exit` завершает консольный клиент.

## Вызов из кода проекта

Соединение открывается один раз, после чего один клиент можно использовать для
нескольких запросов:

```python
from src.integrations.telegram.bot_client import (
    closeTg,
    openTg,
    sendMessageAndWait,
)


client = await openTg()
try:
    first_answer = await sendMessageAndWait(client, "@test_bot", "123")
    second_answer = await sendMessageAndWait(client, "@test_bot", "Привет")
finally:
    await closeTg(client)
```

`openTg` читает параметры пользовательского аккаунта из `.env`, создает клиент и
авторизует его. `sendMessageAndWait` использует переданный клиент и возвращает
текст ответа. `closeTg` завершает соединение; вызывайте ее в `finally`, чтобы
клиент закрылся даже после ошибки или таймаута.
