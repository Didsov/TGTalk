# Шпаргалка: проход по спискам в API СБИС / Wasaby

## 1. Как устроены записи СБИС

Во многих внутренних API СБИС записи передаются не как обычные JSON-объекты, а через два параллельных массива:

```json
{
  "d": [
    "forward",
    true,
    25,
    null
  ],
  "s": [
    {
      "n": "Direction",
      "t": "Строка"
    },
    {
      "n": "HasMore",
      "t": "Логическое"
    },
    {
      "n": "Limit",
      "t": "Число целое"
    },
    {
      "n": "Position",
      "t": "Строка"
    }
  ],
  "_type": "record",
  "f": 0
}
```

Где:

```text
d — значения полей;
s — названия и типы полей.
```

Значения связаны по индексам:

```text
d[0] соответствует s[0]
d[1] соответствует s[1]
d[2] соответствует s[2]
d[3] соответствует s[3]
```

В примере:

```text
Direction = "forward"
HasMore = true
Limit = 25
Position = null
```

Главное правило:

```text
Тип значения в d должен совпадать с типом поля в s.
```

---

# 2. Первый запрос

Первый запрос списка отправляется без позиции:

```json
"Навигация": {
  "d": [
    "forward",
    true,
    25,
    null
  ],
  "s": [
    {
      "t": "Строка",
      "n": "Direction"
    },
    {
      "t": "Логическое",
      "n": "HasMore"
    },
    {
      "t": "Число целое",
      "n": "Limit"
    },
    {
      "t": "Строка",
      "n": "Position"
    }
  ],
  "_type": "record",
  "f": 0
}
```

Для первого запроса:

```text
Position = null
```

Поэтому тип `Position` может быть указан как:

```json
{
  "t": "Строка",
  "n": "Position"
}
```

---

# 3. Где находится курсор следующей страницы

Если сервер допускает продолжение списка, в ответе появляется объект:

```json
"m": {
  "f": 0,
  "d": [
    [
      "{\"d\":[124190002],\"s\":[{\"n\":\"cursor\",\"t\":\"Число целое\"}],\"f\":0,\"_type\":\"record\"}"
    ]
  ],
  "s": [
    {
      "n": "nextPosition",
      "t": "JSON-объект"
    }
  ],
  "_type": "record"
}
```

`m` — это метаданные результата.

В них находится поле:

```text
nextPosition
```

Курсор извлекается так:

```python
next_position = response_data["result"]["m"]["d"][0][0]
```

В некоторых методах `m` может находиться глубже, например:

```python
next_position = response_data["result"]["r"]["m"]["d"][0][0]
```

Поэтому путь нужно проверять по фактическому ответу конкретного метода.

---

# 4. Что представляет собой `nextPosition`

Значение `nextPosition` обычно является строкой:

```python
'{"d":[124190002],"s":[{"n":"cursor","t":"Число целое"}],"f":0,"_type":"record"}'
```

Хотя внутри строки находится JSON, для Python это:

```python
str
```

Проверка:

```python
print(type(next_position))
# <class 'str'>
```

При необходимости внутренний JSON можно разобрать:

```python
import json

cursor_record = json.loads(next_position)
```

Получится:

```python
{
    "d": [124190002],
    "s": [
        {
            "n": "cursor",
            "t": "Число целое"
        }
    ],
    "f": 0,
    "_type": "record"
}
```

Но для следующего запроса обычно нужно передавать исходную строку, не разбирая её.

---

# 5. Нельзя вставлять весь объект `m` в `Position`

Неправильно:

```python
payload["params"]["Навигация"]["d"][3] = response_data["result"]["m"]
```

Объект `m` — это контейнер метаданных ответа.

Следующий запрос ожидает другую структуру:

```text
Position.CompositeKey
```

Из `m` нужно извлечь только строку `nextPosition`.

---

# 6. Формирование `Position`

Курсор нужно завернуть в запись:

```json
{
  "d": [
    "{\"d\":[124190002],\"s\":[{\"n\":\"cursor\",\"t\":\"Число целое\"}],\"f\":0,\"_type\":\"record\"}"
  ],
  "s": [
    {
      "n": "CompositeKey",
      "t": "Строка"
    }
  ],
  "_type": "record",
  "f": 0
}
```

То есть логически:

```text
ответ:  nextPosition
запрос: Position.CompositeKey
```

Функция Python:

```python
def create_position(composite_key: str) -> dict:
    return {
        "d": [
            composite_key
        ],
        "s": [
            {
                "n": "CompositeKey",
                "t": "Строка"
            }
        ],
        "_type": "record",
        "f": 0
    }
```

---

# 7. Тип `Position` обязательно меняется

В первом запросе значение `Position` равно `null`.

Во втором запросе `Position` уже является записью:

```json
{
  "d": [...],
  "s": [...],
  "_type": "record",
  "f": 0
}
```

Поэтому описание поля должно измениться.

Неправильно:

```json
{
  "t": "Строка",
  "n": "Position"
}
```

Правильно:

```json
{
  "t": "Запись",
  "n": "Position"
}
```

Иначе сервер пытается преобразовать объект в строку и возвращает ошибку:

```text
Unable to parse value of field "Position"

Тип не преобразовывается в строку
```

---

# 8. Рабочий блок навигации для следующей страницы

```json
"Навигация": {
  "d": [
    "forward",
    true,
    25,
    {
      "d": [
        "{\"d\":[124190002],\"s\":[{\"n\":\"cursor\",\"t\":\"Число целое\"}],\"f\":0,\"_type\":\"record\"}"
      ],
      "s": [
        {
          "n": "CompositeKey",
          "t": "Строка"
        }
      ],
      "_type": "record",
      "f": 0
    }
  ],
  "s": [
    {
      "t": "Строка",
      "n": "Direction"
    },
    {
      "t": "Логическое",
      "n": "HasMore"
    },
    {
      "t": "Число целое",
      "n": "Limit"
    },
    {
      "t": "Запись",
      "n": "Position"
    }
  ],
  "_type": "record",
  "f": 0
}
```

---

# 9. Поле `f`

В записях встречается:

```json
"f": 0
```

или:

```json
"f": 1
```

Это внутренний служебный флаг сериализации СБИС / Wasaby.

На текущем примере выяснилось:

```text
f не являлся причиной ошибки пагинации.
```

Запрос работал с:

```json
"f": 0
```

Критически важны:

```json
"_type": "record"
```

и соответствие типа `Position` фактическому значению:

```json
{
  "t": "Запись",
  "n": "Position"
}
```

При копировании браузерного запроса лучше сохранять исходное значение `f`, но не строить на нём логику пагинации.

---

# 10. Как понять, что список закончился

При наличии следующей страницы сервер обычно возвращает:

```json
"n": true
```

и метаданные:

```json
"m": {
  ...
}
```

В `m` находится `nextPosition`.

Когда список заканчивается, ответ может выглядеть так:

```json
{
  "result": {
    "f": 0,
    "d": [],
    "s": [...],
    "_type": "recordset",
    "n": false,
    "r": {...}
  }
}
```

Признаки конца:

```text
result.n == false
result.m отсутствует
result.d может быть пустым
```

Практическая схема:

```text
n = true  и есть m  → продолжать
n = false и нет m   → список закончился
n = true  и нет m   → аномальный ответ, лучше остановиться
```

---

# 11. Что считать главным условием остановки

Главное условие:

```python
metadata = result.get("m")

if not metadata:
    break
```

Дополнительная проверка:

```python
if result.get("n") is False:
    break
```

Надёжный вариант:

```python
has_more = result.get("n")
metadata = result.get("m")

if has_more is False or not metadata:
    print("Список закончился")
    break
```

---

# 12. Нельзя полагаться только на количество строк

Не стоит завершать цикл только так:

```python
if len(rows) < limit:
    break
```

Некоторые методы СБИС могут вернуть количество записей, не совпадающее с `Limit`.

Например:

```text
Limit = 1
```

но метод вернул сразу 11 объектов.

Следовательно, `Limit` может означать:

- размер внутренней страницы;
- количество групп;
- размер навигационного блока;
- другую внутреннюю единицу метода.

Поэтому основными признаками продолжения являются:

```text
result.n
result.m
nextPosition
```

---

# 13. Защита от бесконечного цикла

Даже при правильной пагинации нужно защищаться от повторного курсора.

```python
seen_positions: set[str] = set()
```

После получения курсора:

```python
if next_position in seen_positions:
    raise RuntimeError(
        "Сервер повторно вернул уже использованный курсор"
    )

seen_positions.add(next_position)
```

Можно отдельно проверять текущий курсор:

```python
if next_position == previous_position:
    raise RuntimeError(
        "Сервер вернул тот же курсор повторно"
    )
```

Это защитит от:

- ошибок сервера;
- неправильного формирования `Position`;
- случайного повторного запроса одной страницы;
- бесконечного цикла.

---

# 14. Работа с JSON через `requests`

`requests` умеет принимать обычный Python-словарь:

```python
payload = {
    "jsonrpc": "2.0",
    "protocol": 7,
    "method": "CrmClients.ListClients",
    "params": {},
    "id": 1
}
```

Отправка:

```python
response = requests.post(
    url,
    json=payload,
    headers=headers,
    cookies=cookies,
    timeout=30
)
```

Нужно использовать:

```python
json=payload
```

Не рекомендуется для JSON-запроса:

```python
data=payload
```

При использовании `json=` библиотека сама:

- сериализует словарь через JSON;
- добавляет `Content-Type: application/json`;
- преобразует Python-значения.

Соответствия:

```text
Python True  → JSON true
Python False → JSON false
Python None  → JSON null
```

Ответ:

```python
response_data = response.json()
```

После этого `response_data` — обычный Python-словарь.

---

# 15. Функция получения поля из записи `d/s`

Чтобы не обращаться постоянно через индексы, можно искать поле по имени.

```python
from typing import Any


def get_sbis_field(
    record: dict[str, Any],
    field_name: str,
    default: Any = None
) -> Any:
    values = record.get("d", [])
    schema = record.get("s", [])

    for index, field in enumerate(schema):
        if field.get("n") != field_name:
            continue

        if index >= len(values):
            return default

        return values[index]

    return default
```

Пример:

```python
metadata = response_data["result"]["m"]

next_position = get_sbis_field(
    metadata,
    "nextPosition"
)
```

В ответе `nextPosition` обычно завернут в список:

```python
[
    '{"d":[124190002], ... }'
]
```

Поэтому:

```python
if isinstance(next_position, list) and next_position:
    next_position = next_position[0]
```

---

# 16. Универсальное извлечение `nextPosition`

```python
def extract_next_position(
    response_data: dict
) -> str | None:
    result = response_data.get("result")

    if not isinstance(result, dict):
        return None

    metadata = result.get("m")

    if not isinstance(metadata, dict):
        return None

    next_position = get_sbis_field(
        metadata,
        "nextPosition"
    )

    if isinstance(next_position, list):
        if not next_position:
            return None

        next_position = next_position[0]

    if not isinstance(next_position, str):
        return None

    next_position = next_position.strip()

    return next_position or None
```

---

# 17. Установка позиции в запрос

```python
def set_navigation_position(
    payload: dict,
    next_position: str | None
) -> None:
    navigation = payload["params"]["Навигация"]

    if next_position is None:
        navigation["d"][3] = None
        navigation["s"][3] = {
            "t": "Строка",
            "n": "Position"
        }
        return

    navigation["d"][3] = create_position(
        next_position
    )

    navigation["s"][3] = {
        "t": "Запись",
        "n": "Position"
    }
```

---

# 18. Полный безопасный цикл пагинации

```python
import copy
import requests


def get_sbis_field(
    record: dict,
    field_name: str,
    default=None
):
    values = record.get("d", [])
    schema = record.get("s", [])

    for index, field in enumerate(schema):
        if field.get("n") == field_name:
            if index < len(values):
                return values[index]

            return default

    return default


def extract_next_position(
    response_data: dict
) -> str | None:
    result = response_data.get("result")

    if not isinstance(result, dict):
        return None

    metadata = result.get("m")

    if not isinstance(metadata, dict):
        return None

    next_position = get_sbis_field(
        metadata,
        "nextPosition"
    )

    if isinstance(next_position, list):
        if not next_position:
            return None

        next_position = next_position[0]

    if not isinstance(next_position, str):
        return None

    next_position = next_position.strip()

    return next_position or None


def create_position(
    composite_key: str
) -> dict:
    return {
        "d": [
            composite_key
        ],
        "s": [
            {
                "n": "CompositeKey",
                "t": "Строка"
            }
        ],
        "_type": "record",
        "f": 0
    }


def set_navigation_position(
    payload: dict,
    next_position: str | None
) -> None:
    navigation = payload["params"]["Навигация"]

    if next_position is None:
        navigation["d"][3] = None
        navigation["s"][3] = {
            "t": "Строка",
            "n": "Position"
        }
    else:
        navigation["d"][3] = create_position(
            next_position
        )

        navigation["s"][3] = {
            "t": "Запись",
            "n": "Position"
        }


def load_all_pages(
    url: str,
    base_payload: dict,
    headers: dict,
    cookies: dict
) -> list:
    all_rows = []

    current_position = None
    seen_positions: set[str] = set()

    page_number = 1

    while True:
        payload = copy.deepcopy(base_payload)

        set_navigation_position(
            payload,
            current_position
        )

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            cookies=cookies,
            timeout=30
        )

        response.raise_for_status()
        response_data = response.json()

        error = response_data.get("error")

        if error:
            raise RuntimeError(
                error.get("details")
                or error.get("message")
                or str(error)
            )

        result = response_data.get("result", {})

        rows = result.get("d", [])

        if not isinstance(rows, list):
            raise RuntimeError(
                "Поле result.d не является списком"
            )

        print(
            f"Страница {page_number}: "
            f"получено {len(rows)} записей"
        )

        all_rows.extend(rows)

        has_more = result.get("n")
        next_position = extract_next_position(
            response_data
        )

        if has_more is False:
            print(
                "result.n = false — "
                "список закончился"
            )
            break

        if next_position is None:
            print(
                "nextPosition отсутствует — "
                "список закончился"
            )
            break

        if next_position == current_position:
            raise RuntimeError(
                "Сервер повторно вернул "
                "текущий курсор"
            )

        if next_position in seen_positions:
            raise RuntimeError(
                "Обнаружен циклический курсор"
            )

        seen_positions.add(next_position)

        current_position = next_position
        page_number += 1

    return all_rows
```

---

# 19. Диагностика страницы

Полезно выводить:

```python
print("Страница:", page_number)
print("Количество записей:", len(rows))
print("result.n:", result.get("n"))
print("Есть m:", "m" in result)
print("nextPosition:", next_position)
```

Пример:

```text
Страница: 1
Количество записей: 25
result.n: True
Есть m: True
nextPosition: {"d":[124190002],...}
```

Последняя страница:

```text
Страница: 5
Количество записей: 0
result.n: False
Есть m: False
nextPosition: None
```

---

# 20. Частые ошибки

## Ошибка 1. Передан весь объект `m`

Неправильно:

```python
position = result["m"]
```

Правильно:

```python
next_position = result["m"]["d"][0][0]
```

---

## Ошибка 2. `Position` объявлен строкой

Неправильно:

```json
{
  "t": "Строка",
  "n": "Position"
}
```

при фактическом значении:

```json
{
  "d": [...],
  "s": [...],
  "_type": "record"
}
```

Правильно:

```json
{
  "t": "Запись",
  "n": "Position"
}
```

---

## Ошибка 3. В `Position` передан `nextPosition` без `CompositeKey`

Неправильно:

```json
{
  "d": [
    ["<nextPosition>"]
  ],
  "s": [
    {
      "n": "nextPosition",
      "t": "JSON-объект"
    }
  ]
}
```

Правильно:

```json
{
  "d": [
    "<nextPosition>"
  ],
  "s": [
    {
      "n": "CompositeKey",
      "t": "Строка"
    }
  ],
  "_type": "record",
  "f": 0
}
```

---

## Ошибка 4. Разобранный курсор передан как объект

Для запроса СБИС обычно ожидает:

```python
str
```

То есть:

```python
next_position
```

а не:

```python
json.loads(next_position)
```

---

## Ошибка 5. Нет защиты от повторного курсора

Нужно обязательно проверять:

```python
if next_position in seen_positions:
    break
```

---

## Ошибка 6. Остановка только по `len(d)`

Ненадёжно:

```python
if len(rows) < limit:
    break
```

Надёжнее:

```python
if result.get("n") is False:
    break

if not result.get("m"):
    break
```

---

# 21. Короткий алгоритм

```text
1. Отправить первый запрос с Position = null.

2. Получить result.d и обработать записи.

3. Проверить result.n.

4. Проверить наличие result.m.

5. Извлечь result.m.nextPosition.

6. Вставить nextPosition в Position.CompositeKey.

7. Поменять тип Position со «Строка» на «Запись».

8. Отправить следующий запрос.

9. Остановиться, когда:
   - result.n == false;
   - result.m отсутствует;
   - nextPosition отсутствует;
   - курсор повторился.
```

---

# 22. Самое главное

```text
Первый запрос:
Position = null
Position type = Строка
```

```text
Следующие запросы:
Position = record
Position type = Запись
```

```text
Курсор берётся из:
result.m.nextPosition
```

```text
Курсор передаётся как:
Position.CompositeKey
```

```text
Конец списка:
result.n == false
или отсутствует result.m
```

```text
Никогда не передавать весь объект m в Position.
```

```text
Всегда защищаться от повторного курсора.
```