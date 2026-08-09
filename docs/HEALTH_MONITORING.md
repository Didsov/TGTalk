# Мониторинг интеграций

Администратор отчётного бота может выполнить `/health` или нажать
«Состояние интеграций». Статус содержит:

- состояние cookie СБИС;
- состояние пользовательской Telegram-сессии;
- состояние токена отчётного бота;
- время последней проверки и последнего успешного ответа;
- безопасный код ошибки;
- количество доступных запросов из последнего ежедневного конвейера.

После `/start` бот показывает постоянную кнопку «Меню». Она и команда `/menu`
повторно проверяют whitelist и открывают актуальное inline-меню.

## Запуск проверки

```powershell
python -m src.cli.healthcheck --database .\data\clients.db
```

Для запуска без уведомлений администраторам:

```powershell
python -m src.cli.healthcheck --database .\data\clients.db --no-notify
```

Проверка СБИС читает только первую страницу существующего
`Contractor.ListCompany` и не запрашивает карточки. Проверка Telethon проверяет
авторизацию, затем вызывает существующую `getAvailableQueries`: отправляет
`/menu`, нажимает callback «Мой профиль» и получает текущий остаток. Поиск по
ИНН, почте или ФИО не запускается, поэтому платный запрос не расходуется.

Внутри отчётного бота команда `/health_refresh` и кнопка «Обновить состояние»
выполняют ту же проверку немедленно. Обычная `/health` показывает последний
сохранённый результат без ожидания внешних сервисов.

В SQLite сохраняются только статус, время, безопасный код ошибки и счётчик
последовательных сбоев. Cookie, токены, содержимое `.session` и ответы сервисов
не сохраняются. HTTP 429 и FloodWait считаются ограничением частоты, а не потерей
авторизации.

Уведомление отправляется при первой ошибке авторизации или rate limit, после
трёх последовательных сетевых/прочих сбоев и после восстановления. Одинаковое
состояние повторно не рассылается.

## systemd-таймер

Пример `/etc/systemd/system/inntophone-health.service`:

```ini
[Unit]
Description=INNtoPhone integration healthcheck

[Service]
Type=oneshot
User=inntophone
WorkingDirectory=/opt/inntophone
EnvironmentFile=/opt/inntophone/.env
ExecStart=/opt/inntophone/.venv/bin/python -m src.cli.healthcheck --database /opt/inntophone/data/clients.db
```

Пример `/etc/systemd/system/inntophone-health.timer`:

```ini
[Unit]
Description=Run INNtoPhone healthcheck every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

Активация и просмотр журнала:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now inntophone-health.timer
systemctl list-timers --all | grep inntophone
journalctl -u inntophone-health.service -n 100
```

Пути, пользователя и Python virtualenv в unit-файле нужно заменить на значения
конкретного сервера.
