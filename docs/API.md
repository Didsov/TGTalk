# CrmClients
## .ListClients
CrmClients.ListClients	Клиентов CRM-списка	Найти организации для обработки 
Запрос возвращает страницу клиентов выбранного CRM-списка. Это стартовая точка
всего сбора. Помимо ИНН и названия, ответ уже содержит много полей, полезных для
прозвона и сегментации.

### Что передаётся

Фильтр текущего запроса:

| Поле | Текущее значение | Смысл |
|---|---|---|
| `HasListTheme` | `false` | Внутренняя настройка представления списка |
| `KindOf` | `[1, 4, 7]` | Внутренние типы записей; точная расшифровка не подтверждена |
| `ListId` | выбранный номер | Идентификатор CRM-списка |
| `Responsible` | `null` | Фильтр по ответственному сейчас не используется |
| `Result` | `["0"]` | Внутренний фильтр результата; значение нужно уточнять перехватом |
| `ShowOnly` | `"Clients"` | Возвращать клиентов |
| `Stage` | `null` | Фильтр по стадии сейчас не используется |

Навигация:

| Поле | Значение |
|---|---|
| `Direction` | `forward` |
| `HasMore` | `true` при запросе |
| `Limit` | размер страницы, сейчас обычно 100 |
| `Position` | `null` для первой страницы, затем курсор из метаданных ответа |

### Что реально приходит в ответе

Сохранённый ответ `dumps/crm_clients_raw.json` подтверждает следующие группы
полей.

Идентификация организации:

- `ID`, `CompositeKey`;
- `Контрагент`, `@Лицо`, `@КонтрагентКонтакт`;
- `Name`, `Название`;
- `ИНН`, `КПП`, `ОГРН`, `ОКПО`, `UUID`;
- тип организации, признак ИП, дата ликвидации.

Адрес и территория:

- `АдресФактический`, `АдресЮридический`;
- разобранные адреса `ParsedAddressFact` и `ParsedAddress`;
- `Регион`, `RegCode`, КЛАДР и ФИАС-поля.

CRM и прозвоны:

- `ПоследнийКонтакт`, `lastContact`, `last_event`;
- дата, тип и вид контакта;
- статус, результат, стадия и переход;
- примечание к последнему контакту;
- `НазваниеСтадии`, `НазваниеПерехода`;
- ответственный и подразделение;
- `ContactTypeIcon`, например признак события «Звонок»;
- `КонтактныеДанные` и `ContactData` предусмотрены схемой;
- теги и папки.




### Сырой CURL:
```
curl 'https://online.sbis.ru/service/?x_version=26.3248-150.6' \
  -H 'accept: application/json, text/javascript, */*; q=0.01' \
  -H 'accept-language: ru-RU;q=0.8,en-US;q=0.5,en;q=0.3' \
  -H 'content-type: application/json; charset=UTF-8' \
  -b '<БРАУЗЕРНЫЕ_COOKIE_УДАЛЕНЫ>' \
  -H 'origin: https://online.sbis.ru' \
  -H 'priority: u=1, i' \
  -H 'referer: https://online.sbis.ru/page/crm-client-lists' \
  -H 'sec-ch-ua: "Chromium";v="148", "YaBrowser";v="26.6", "Not/A)Brand";v="99", "Yowser";v="2.5"' \
  -H 'sec-ch-ua-mobile: ?0' \
  -H 'sec-ch-ua-platform: "Windows"' \
  -H 'sec-fetch-dest: empty' \
  -H 'sec-fetch-mode: cors' \
  -H 'sec-fetch-site: same-origin' \
  -H 'user-agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 YaBrowser/26.6.0.0 Safari/537.36' \
  -H 'x-adaptive: false' \
  -H 'x-calledmethod: CrmClients.ListClients' \
  -H 'x-originalmethodname: Q3JtQ2xpZW50cy5MaXN0Q2xpZW50cw==' \
  -H 'x-requested-with: XMLHttpRequest' \
  -H 'x-saby-appid: d7b1d6a5-e59d-4b03-b823-726fbdc35d09' \
  -H 'x-saby-appversion: 26.3248' \
  -H 'x-saby-cfgid: 2725babf-70e1-4f04-8e78-1b4272de7b21' \
  --data-raw '{"jsonrpc":"2.0","protocol":7,"method":"CrmClients.ListClients","params":{"Фильтр":{"d":[false,[1,4,7],100451,null,["0"],"Clients",null],"s":[{"t":"Логическое","n":"HasListTheme"},{"t":{"n":"Массив","t":"Число целое"},"n":"KindOf"},{"t":"Число целое","n":"ListId"},{"t":"Строка","n":"Responsible"},{"t":{"n":"Массив","t":"Строка"},"n":"Result"},{"t":"Строка","n":"ShowOnly"},{"t":"Строка","n":"Stage"}],"_type":"record","f":0},"Сортировка":null,"Навигация":{"d":["forward",true,25,null],"s":[{"t":"Строка","n":"Direction"},{"t":"Логическое","n":"HasMore"},{"t":"Число целое","n":"Limit"},{"t":"Строка","n":"Position"}],"_type":"record","f":0},"ДопПоля":[]},"id":1}'
```

### Тестовый вывод клиентов

Команда полностью загружает список, но выводит только первые 10 строк:

```powershell
python -m src.cli.sbis_clients_console 100451
```

Изменяемые значения фильтра вынесены в начало
`src/integrations/sbis/clients.py`. Меняйте по одной константе за эксперимент,
чтобы результат можно было связать с конкретным параметром.

## BillingContractor.ReadCard

Скрытый браузерный метод читает карточку контрагента по реквизитам. Текущий
сценарий передает только ИНН, а `KPP`, `CountryCode` и `BillingExtId` оставляет
пустыми.

```python
from src.integrations.sbis import getClientByInn


client = await getClientByInn("500100732259")
if client is None:
    print("Карточка не найдена")
else:
    print(client)
```

Функция проверяет контрольные цифры ИНН до сетевого запроса, использует cookie из
`SBIS_BROWSER_COOKIE` и декодирует protocol 7 в обычный словарь. Возврат `None`
для пустого `result` принят как рабочий контракт проекта; фактический ответ «не
найдено» еще нужно подтвердить перехватом или тестовым вызовом.

### Извлечение контактов по ИНН

```python
from src.integrations.sbis import getContactsByInn


contacts = await getContactsByInn("500100732259")
```

Функция находит вложенные строки с `ContactType="contact"`, берет отображаемое
значение из `RowTitle` и определяет тип по `Actions`: `copy_phone`/`tel_link` —
телефон, `copy_email`/`mail_client` — email. Результат имеет вид:

```python
[
    {
        "id": "идентификатор контакта",
        "type": "phone",
        "value": "+7 (900) 000-00-00",
        "masked": False,
    }
]
```

Повторяющиеся контакты удаляются без изменения исходного отображаемого значения.
