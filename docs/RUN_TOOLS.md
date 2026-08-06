# Памятка по запуску инструментов СБИС

Команды выполняются в терминале из корня проекта:

```text
D:\GIT\INNtoPhone
```

Перед запуском заполните локальный `.env`:

```dotenv
SBIS_BROWSER_COOKIE=строка_cookie_из_браузерного_запроса
SBIS_RPC_URL=https://online.sbis.ru/service/
```

Cookie является секретом пользовательской сессии. Не добавляйте его в команды,
исходный код, документацию, Git или сообщения об ошибках.

## 1. Проверить список клиентов

Инструмент полностью получает CRM-список, но показывает только первые 10 строк с
ИНН и названием организации.

```powershell
python -m src.cli.sbis_clients_console 99788
```

Где `99788` — `ListId`. Посмотреть справку:

```powershell
python -m src.cli.sbis_clients_console --help
```

Основная функция инструмента возвращает полный список клиентов:

```python
from src.cli.sbis_clients_console import printClientsTable


clients = await printClientsTable(99788)
```

## 2. Получить контакты по ИНН

Инструмент получает карточку одного клиента и выводит найденные телефоны и email:

```powershell
python -m src.cli.sbis_contacts_console 500100732259
```

ИНН передается строкой из 10 или 12 цифр и проверяется по контрольным цифрам до
сетевого запроса. Посмотреть справку:

```powershell
python -m src.cli.sbis_contacts_console --help
```

Использование функции без консольного вывода:

```python
from src.integrations.sbis import getContactsByInn


contacts = await getContactsByInn("500100732259")
```

## 3. Обойти список и получить контакты

Корневой `main.py` объединяет первые два сценария:

1. Полностью получает список по `ListId`.
2. Для каждого клиента берет ИНН.
3. Получает контакты по ИНН.
4. Печатает одну строку: ИНН, организация, контакты.
5. Возвращает список найденных ИНН.

Запуск:

```powershell
python -m main 99788
```

Равнозначный прямой запуск:

```powershell
python main.py 99788
```

Посмотреть справку:

```powershell
python -m main --help
```

Использование из другого асинхронного модуля:

```python
from main import debugList


inns = await debugList(99788)
```

## Отладка в VS Code

В `launch.json` для запуска модуля укажите:

```json
{
    "name": "СБИС: полный обход списка",
    "type": "debugpy",
    "request": "launch",
    "module": "main",
    "args": ["99788"],
    "cwd": "${workspaceFolder}",
    "console": "integratedTerminal",
    "justMyCode": true
}
```

Поставьте breakpoint слева от нужной строки и нажмите `F5`. Доступные аргументы
каждого инструмента всегда можно посмотреть через `--help`.

