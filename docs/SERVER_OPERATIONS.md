# Эксплуатация INNtoPhone на Linux-сервере

Руководство рассчитано на первый запуск Ubuntu/Debian и размещение проекта в
`/opt/inntophone`. Команды с приглашением `root` выполняются администратором,
остальные — отдельным пользователем `inntophone`.

## 1. Пользователи и каталоги

Проверить текущего пользователя:

```bash
whoami
```

Переключиться с `root` на пользователя приложения:

```bash
su - inntophone
```

Вернуться к `root`:

```bash
exit
```

Если пользователь и каталог еще не созданы:

```bash
adduser --disabled-password --gecos "" inntophone
mkdir -p /opt/inntophone/data
chown -R inntophone:inntophone /opt/inntophone
```

## 2. Проверка перенесенных файлов

После загрузки `.env`, Telegram-сессии и SQLite-базы выполнить под `root`:

```bash
chown inntophone:inntophone /opt/inntophone/.env
chown inntophone:inntophone /opt/inntophone/telegram_test_user.session
chown inntophone:inntophone /opt/inntophone/data/clients.db

chmod 600 /opt/inntophone/.env
chmod 600 /opt/inntophone/telegram_test_user.session
chmod 600 /opt/inntophone/data/clients.db
```

Проверить владельца и права:

```bash
stat -c '%A %U %G %n' \
  /opt/inntophone/.env \
  /opt/inntophone/telegram_test_user.session \
  /opt/inntophone/data/clients.db
```

Ожидаемый результат:

```text
-rw------- inntophone inntophone /opt/inntophone/.env
-rw------- inntophone inntophone /opt/inntophone/telegram_test_user.session
-rw------- inntophone inntophone /opt/inntophone/data/clients.db
```

Убедиться, что приложение может читать файлы:

```bash
runuser -u inntophone -- test -r /opt/inntophone/.env && echo '.env: OK'
runuser -u inntophone -- test -r /opt/inntophone/telegram_test_user.session && echo 'session: OK'
runuser -u inntophone -- test -r /opt/inntophone/data/clients.db && echo 'database: OK'
```

Содержимое `.env`, токены и cookie командами `cat`, `head` или журналированием
не выводить.

## 3. Получение проекта и Python-окружение

Установить системные пакеты под `root`:

```bash
apt update
apt install -y git python3 python3-venv python3-pip sqlite3
```

Если репозиторий еще не клонирован:

```bash
su - inntophone
cd /opt/inntophone
git clone https://github.com/Didsov/TGTalk.git .
```

Создать virtualenv и установить зависимости:

```bash
cd /opt/inntophone
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Проверить импорт исходников и справку CLI без внешних запросов:

```bash
.venv/bin/python -m compileall -q src
.venv/bin/python -m src.cli.report_bot --help
.venv/bin/python -m src.cli.healthcheck --help
.venv/bin/python -m src.cli.daily_pipeline --help
```

Полный офлайн-набор тестов:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Интеграционные тесты проекта по умолчанию не должны обращаться к production.

## 4. Первый ручной запуск

### Проверка интеграций

Команда выполняет read-only запрос списка СБИС, проверяет Telethon-сессию и через
`/menu → Мой профиль` бесплатно получает остаток запросов поискового бота:

```bash
cd /opt/inntophone
.venv/bin/python -m src.cli.healthcheck \
  --database /opt/inntophone/data/clients.db \
  --no-notify
```

Она не запускает поиск по ИНН, почте или ФИО. Код завершения `0` означает, что
все проверки успешны; `1` — хотя бы одна интеграция требует внимания.

### Бот отчетов

Для первого запуска оставить процесс в терминале:

```bash
cd /opt/inntophone
.venv/bin/python -m src.cli.report_bot \
  --database /opt/inntophone/data/clients.db
```

В Telegram выполнить `/start`, затем `/health` и нажать «Обновить состояние».
Остановить foreground-процесс можно сочетанием `Ctrl+C`.

### Ежедневный конвейер

Следующая команда выполняет настоящий сбор СБИС, Telegram-обогащение с записью в
БД и рассылку отчетов. Она может расходовать запросы поискового бота:

```bash
cd /opt/inntophone
.venv/bin/python -m src.cli.daily_pipeline \
  --database /opt/inntophone/data/clients.db \
  --limit 1000 \
  --timeout 45
```

Без явного `--date` сбор выполняется за вчерашний день. Перед первым production-
запуском рекомендуется проверить остаток запросов через `/health_refresh`.

## 5. Постоянный запуск через systemd

Ручной запуск Python занимает текущий терминал, потому что процесс работает в
foreground. Это нормально. Для временной проверки можно открыть второе SSH-окно.
Остановить foreground-процесс следует через `Ctrl+C`; `Ctrl+Z` использовать не
нужно, потому что он лишь приостанавливает процесс.

Для постоянной работы используются systemd units. Их создание, включение и
просмотр выполняются под `root`. Если бот отчетов сейчас запущен вручную, сначала
остановить его через `Ctrl+C`, иначе два процесса с одним Bot API token будут
конфликтовать.

### Бот отчетов

Подключиться отдельным SSH-сеансом как `root` и открыть файл:

```bash
nano /etc/systemd/system/inntophone-report-bot.service
```

Вставить:

```ini
[Unit]
Description=INNtoPhone reporting Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=inntophone
Group=inntophone
WorkingDirectory=/opt/inntophone
ExecStart=/opt/inntophone/.venv/bin/python -m src.cli.report_bot --database /opt/inntophone/data/clients.db
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
```

Сохранить в `nano`: `Ctrl+O`, `Enter`, затем выйти через `Ctrl+X`.

Загрузить конфигурацию, включить автозапуск и запустить бота:

```bash
systemctl daemon-reload
systemctl enable --now inntophone-report-bot.service
systemctl status inntophone-report-bot.service
```

В исправном состоянии будет показано `active (running)`. Из просмотра статуса
выйти клавишей `q`. Теперь SSH-терминал можно закрыть — бот продолжит работать.

Проверить последние логи:

```bash
journalctl -u inntophone-report-bot.service -n 100 --no-pager
```

### Ежедневный конвейер

Конвейер не должен работать постоянно. `service` выполняет один обход и
завершается, а `timer` запускает его ежедневно.

Открыть service-файл:

```bash
nano /etc/systemd/system/inntophone-daily.service
```

Вставить:

```ini
[Unit]
Description=INNtoPhone daily pipeline
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=inntophone
Group=inntophone
WorkingDirectory=/opt/inntophone
ExecStart=/opt/inntophone/.venv/bin/python -m src.cli.daily_pipeline --database /opt/inntophone/data/clients.db --limit 1000 --timeout 45
UMask=0077
```

Сохранить через `Ctrl+O`, `Enter`, `Ctrl+X`. Затем открыть timer-файл:

```bash
nano /etc/systemd/system/inntophone-daily.timer
```

Вставить:

```ini
[Unit]
Description=Run INNtoPhone daily pipeline

[Timer]
OnCalendar=*-*-* 08:00:00
Persistent=true
Unit=inntophone-daily.service

[Install]
WantedBy=timers.target
```

Сохранить файл. В этом примере запуск назначен на `08:00` по часовому поясу
сервера. Проверить его:


```bash
timedatectl
```

Если работа должна идти по Владивостоку:

```bash
timedatectl set-timezone Asia/Vladivostok
```

Активировать таймер:

```bash
systemctl daemon-reload
systemctl enable --now inntophone-daily.timer
systemctl list-timers --all | grep inntophone
```

В списке должна присутствовать строка `inntophone-daily.timer` со временем
следующего запуска. Сам `inntophone-daily.service` между запусками обычно имеет
состояние `inactive (dead)` — для oneshot-сервиса это нормально.

Без ожидания расписания выполнить один настоящий конвейер можно командой:

```bash
systemctl start inntophone-daily.service
journalctl -u inntophone-daily.service -f
```

Команда запускает сбор и может расходовать запросы поискового бота. Из просмотра
логов выйти через `Ctrl+C`; это не останавливает уже запущенный systemd-сервис.

### Мониторинг cookie, сессии и баланса

Healthcheck также является oneshot-сервисом: он выполняет одну проверку и
завершается. Таймер повторяет её каждые 30 минут. Не назначать проверку на время
ежедневного конвейера: оба процесса используют одну Telethon-сессию.

Открыть service-файл:

```bash
nano /etc/systemd/system/inntophone-health.service
```

Вставить:

```ini
[Unit]
Description=INNtoPhone integration healthcheck
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=inntophone
Group=inntophone
WorkingDirectory=/opt/inntophone
ExecStart=/opt/inntophone/.venv/bin/python -m src.cli.healthcheck --database /opt/inntophone/data/clients.db
UMask=0077
```

Сохранить файл и открыть таймер:

```bash
nano /etc/systemd/system/inntophone-health.timer
```

Вставить:

```ini
[Unit]
Description=Run INNtoPhone healthcheck every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true
Unit=inntophone-health.service

[Install]
WantedBy=timers.target
```

Сохранить и активировать:

```bash
systemctl daemon-reload
systemctl enable --now inntophone-health.timer
systemctl list-timers --all | grep inntophone
```

Запустить вне расписания и проверить результат:

```bash
systemctl start inntophone-health.service
journalctl -u inntophone-health.service -n 100 --no-pager
```

Если хотя бы одна интеграция недоступна, healthcheck завершается с кодом `1`, и
systemd показывает запуск как failed. Детали при этом сохраняются в SQLite и
доступны администратору через `/health`.

Дополнительное описание статусов находится в
[`HEALTH_MONITORING.md`](HEALTH_MONITORING.md).

### Изменение времени и параметров запуска

Время ежедневного сбора задается в `inntophone-daily.timer`, а не в service-
файле. Все следующие команды выполнять под `root`.

Открыть таймер в `nano`:

```bash
EDITOR=nano systemctl edit --full inntophone-daily.timer
```

Например, ежедневный запуск в `10:30`:

```ini
[Timer]
OnCalendar=*-*-* 10:30:00
Persistent=true
Unit=inntophone-daily.service
```

Сохранить через `Ctrl+O`, `Enter`, `Ctrl+X`, затем применить изменение:

```bash
systemctl daemon-reload
systemctl restart inntophone-daily.timer
systemctl list-timers --all | grep inntophone
```

Примеры `OnCalendar`:

```ini
# Каждый день в 07:00
OnCalendar=*-*-* 07:00:00

# Каждый день в 23:30
OnCalendar=*-*-* 23:30:00

# Только по будням в 09:00
OnCalendar=Mon..Fri *-*-* 09:00:00
```

Проверить выражение и ближайшие срабатывания до редактирования таймера:

```bash
systemd-analyze calendar '*-*-* 10:30:00'
```

Расписание использует часовой пояс сервера. Проверить его:

```bash
timedatectl
```

Установить Владивосток и пересчитать следующее срабатывание:

```bash
timedatectl set-timezone Asia/Vladivostok
systemctl restart inntophone-daily.timer
```

Количество обрабатываемых клиентов и timeout задаются в service-файле:

```bash
EDITOR=nano systemctl edit --full inntophone-daily.service
```

Например, ограничение в 200 клиентов и timeout 45 секунд:

```ini
ExecStart=/opt/inntophone/.venv/bin/python -m src.cli.daily_pipeline --database /opt/inntophone/data/clients.db --limit 200 --timeout 45
```

После изменения service-файла:

```bash
systemctl daemon-reload
```

Постоянно работающий бот отчетов перезапускать из-за изменения daily service не
нужно. Новые параметры будут использованы при следующем запуске конвейера.

Частота мониторинга задается в `inntophone-health.timer`:

```bash
EDITOR=nano systemctl edit --full inntophone-health.timer
```

Например, проверка раз в час:

```ini
[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true
Unit=inntophone-health.service
```

Применить и проверить:

```bash
systemctl daemon-reload
systemctl restart inntophone-health.timer
systemctl list-timers --all | grep inntophone
```

## 6. Ручное управление

```bash
# Состояние бота
systemctl status inntophone-report-bot.service

# Перезапуск после обновления кода или .env
systemctl restart inntophone-report-bot.service

# Остановка и запуск
systemctl stop inntophone-report-bot.service
systemctl start inntophone-report-bot.service

# Состояние расписания
systemctl status inntophone-daily.timer
systemctl list-timers --all | grep inntophone

# Состояние расписания healthcheck
systemctl status inntophone-health.timer

# Запустить проверку интеграций прямо сейчас
systemctl start inntophone-health.service

# Запустить ежедневный конвейер прямо сейчас
systemctl start inntophone-daily.service

# Дождаться результата ручного запуска
systemctl status inntophone-daily.service
```

Не запускать вручную второй экземпляр бота отчетов, пока systemd-сервис активен:
два long-polling процесса с одним Bot API token конфликтуют.

## 7. Просмотр журналов

```bash
# Последние 100 строк бота
journalctl -u inntophone-report-bot.service -n 100 --no-pager

# Смотреть бот в реальном времени
journalctl -u inntophone-report-bot.service -f

# Последний ежедневный запуск
journalctl -u inntophone-daily.service -n 200 --no-pager

# Логи текущих суток
journalctl -u inntophone-daily.service --since today --no-pager
```

В Telegram администратору доступны `/pipeline_status`, `/report_status`,
`/health` и `/health_refresh`.

## 8. Обновление с GitHub

Сначала не останавливая рабочий сервис получить код и проверить его:

```bash
su - inntophone
cd /opt/inntophone
git status --short
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m compileall -q src
.venv/bin/python -m unittest discover -s tests -q
```

Если тесты прошли, вернуться к `root` и перезапустить бот:

```bash
exit
systemctl restart inntophone-report-bot.service
systemctl status inntophone-report-bot.service
```

`.env`, `*.session` и `data/clients.db` не хранятся в Git и при `git pull` не
должны изменяться.

## 9. Резервное копирование

Перед копированием остановить процессы, которые могут записывать БД:

```bash
systemctl stop inntophone-daily.timer
systemctl stop inntophone-daily.service
systemctl stop inntophone-report-bot.service
```

Создать согласованную SQLite-копию от имени приложения:

```bash
runuser -u inntophone -- /opt/inntophone/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('/opt/inntophone/data/clients.db'); d=sqlite3.connect('/opt/inntophone/data/clients.backup.db'); s.backup(d); d.close(); s.close()"
```

Скопировать в защищенное резервное хранилище нужно:

- `data/clients.backup.db`;
- `.env`;
- `telegram_test_user.session`.

После копирования:

```bash
rm /opt/inntophone/data/clients.backup.db
systemctl start inntophone-report-bot.service
systemctl start inntophone-daily.timer
```

Резервные копии содержат персональные данные и секреты. Для них нужны такие же
ограничения доступа и шифрование, как для рабочих файлов.

## 10. Частые проблемы

### FileZilla показывает только `/home/inntophone`

В поле «Удаленный сайт» вручную открыть `/opt/inntophone`. Проверить права:

```bash
chown -R inntophone:inntophone /opt/inntophone
```

### `Permission denied`

```bash
namei -l /opt/inntophone/data/clients.db
stat -c '%A %U %G %n' /opt/inntophone/data/clients.db
```

Все родительские каталоги должны быть доступны пользователю `inntophone`, а
рабочие файлы — принадлежать ему.

### Cookie СБИС или Telegram-сессия не работают

```bash
su - inntophone
cd /opt/inntophone
.venv/bin/python -m src.cli.healthcheck \
  --database /opt/inntophone/data/clients.db \
  --no-notify
```

`unauthorized` означает необходимость заменить cookie или повторно перенести
авторизованную Telegram-сессию. `rate_limited` — временное ограничение, а не
потеря авторизации.

### Бот сразу завершается

```bash
journalctl -u inntophone-report-bot.service -n 200 --no-pager
```

Чаще всего причина — отсутствующая переменная в `.env`, неверный Bot API token,
неустановленная зависимость либо недостаточные права на SQLite/session.

### Проверка диска

```bash
df -h
du -sh /opt/inntophone/data
```

Не публиковать вывод `.env`, cookie, токенов, `.session`, содержимое БД и полные
контактные данные в GitHub issues, чатах или журналах.
