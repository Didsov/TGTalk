# Резервные копии базы и ответов на локальный компьютер

## Как устроено резервирование

Резервная копия должна находиться вне сервера. Поэтому запуск выполняет
Windows-компьютер:

1. По SSH просит сервер создать согласованный SQLite-снимок.
2. Сервер добавляет снимок и `data/telegram_reports` в ZIP.
3. В ZIP записывается `manifest.json` с размером и SHA-256 каждого файла.
4. Сервер проверяет архив до публикации.
5. Windows скачивает ZIP через SCP во временный файл `.partial`.
6. Локальный Python повторно проверяет SHA-256 и `PRAGMA integrity_check`.
7. Только после успешной проверки файл получает окончательное имя.

По умолчанию сервер хранит последние 7 копий, компьютер — последние 30. Если
компьютер выключен, задача запустится при следующем включении, а созданные ранее
копии останутся на сервере как дополнительный кратковременный буфер.

Архив содержит персональные данные. Храните локальный каталог на диске с
BitLocker и не синхронизируйте его в публичное облако.

## Что входит в архив

- согласованный снимок `data/clients.db`;
- файлы из `data/telegram_reports`;
- manifest с контрольными суммами.

Не включаются `.env`, Telegram-сессия, cookie и токены. Их следует один раз
сохранить отдельно в зашифрованном хранилище секретов. Не добавляйте их в Git и
не помещайте в обычный незашифрованный ZIP.

## Первый ручной запуск

Сначала обновите проект на сервере и локальном компьютере до ветки с backup CLI.
Проверьте создание архива непосредственно на сервере под `inntophone`:

```bash
cd /opt/inntophone
.venv/bin/python -m src.cli.backup create \
  --database /opt/inntophone/data/clients.db \
  --responses /opt/inntophone/data/telegram_reports \
  --output /opt/inntophone/data/backups \
  --keep 7
```

Команда выводит одну JSON-строку с `"ok": true` и путём архива. Проверить его:

```bash
.venv/bin/python -m src.cli.backup verify \
  --archive /opt/inntophone/data/backups/ИМЯ-ФАЙЛА.zip
```

Работающие сервисы останавливать не нужно: база копируется штатным SQLite Backup
API. Архивы ответов создаются как неизменяемые файлы и копируются отдельно.

На Windows должен быть установлен OpenSSH Client:

```powershell
ssh -V
scp
```

Для автоматического запуска SSH не должен спрашивать пароль. Настройте отдельный
SSH-ключ пользователя `inntophone` и сначала вручную проверьте вход:

```powershell
ssh inntophone@АДРЕС_СЕРВЕРА
```

Затем из корня локального проекта выполните:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\pull_server_backup.ps1 `
  -Server АДРЕС_СЕРВЕРА `
  -RemoteUser inntophone `
  -LocalBackupDirectory D:\INNtoPhone-Backups `
  -RemoteKeep 7 `
  -LocalKeep 30
```

Успешный результат заканчивается строкой:

```text
Backup saved and verified: D:\INNtoPhone-Backups\inntophone-backup-....zip
```

## Ежедневный запуск через Планировщик Windows

Выберите время после завершения серверного ежедневного конвейера, например
`12:00`. Откройте PowerShell от имени своего обычного Windows-пользователя:

```powershell
$action = New-ScheduledTaskAction `
  -Execute "powershell.exe" `
  -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "D:\GIT\INNtoPhone\scripts\pull_server_backup.ps1" -Server "inntophone-server" -RemoteUser "inntophone" -LocalBackupDirectory "D:\INNtoPhone-Backups" -RemoteKeep 3 -LocalKeep 7'

$trigger = New-ScheduledTaskTrigger -Daily -At 12:00
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
  -TaskName "INNtoPhone local backup" `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Description "Скачать и проверить backup INNtoPhone"
```

`-StartWhenAvailable` запускает пропущенную задачу после включения компьютера.
Проверить её вручную:

```powershell
Start-ScheduledTask -TaskName "INNtoPhone local backup"
Start-Sleep -Seconds 10
Get-ScheduledTaskInfo -TaskName "INNtoPhone local backup"
Get-ChildItem D:\INNtoPhone-Backups
```

Код `LastTaskResult = 0` означает успешный запуск.

## Регулярная проверка

Раз в месяц вручную проверьте последнюю локальную копию:

```powershell
cd D:\GIT\INNtoPhone
python -m src.cli.backup verify `
  --archive D:\INNtoPhone-Backups\ИМЯ-ФАЙЛА.zip
```

Периодически выполняйте тестовое восстановление в отдельный временный каталог.
Наличие ZIP без успешной проверки не считается резервной копией.

## Восстановление после потери сервера

1. Разверните чистый сервер и установите проект.
2. Загрузите выбранный ZIP в `/home/inntophone/restore.zip` через SCP/FileZilla.
3. Проверьте архив до извлечения:

```bash
cd /opt/inntophone
.venv/bin/python -m src.cli.backup verify \
  --archive /home/inntophone/restore.zip
```

4. Остановите процессы, способные писать в базу:

```bash
sudo systemctl stop inntophone-daily.timer
sudo systemctl stop inntophone-daily.service
sudo systemctl stop inntophone-report-bot.service
```

5. Установите `unzip`, создайте новый временный каталог, извлеките проверенный
   архив и сохраните текущую базу как возвратную копию:

```bash
sudo apt install -y unzip
mkdir -m 700 /home/inntophone/restore-work
unzip /home/inntophone/restore.zip -d /home/inntophone/restore-work
if [ -f /opt/inntophone/data/clients.db ]; then
  mv /opt/inntophone/data/clients.db \
    /opt/inntophone/data/clients.before-restore.db
fi
cp /home/inntophone/restore-work/database/clients.db \
  /opt/inntophone/data/clients.db
mkdir -p /opt/inntophone/data/telegram_reports
if [ -d /home/inntophone/restore-work/responses ]; then
  cp -a /home/inntophone/restore-work/responses/. \
    /opt/inntophone/data/telegram_reports/
fi
```

Каталог `restore-work` должен отсутствовать перед выполнением `mkdir`. Для новой
попытки используйте другое явное имя, например `restore-work-2`; не очищайте
широкие каталоги `/home` или `/opt` рекурсивными командами.

6. Восстановите владельца и права, проверьте базу и запустите сервисы:

```bash
sudo chown -R inntophone:inntophone /opt/inntophone/data
sudo chmod 600 /opt/inntophone/data/clients.db
sudo -u inntophone sqlite3 /opt/inntophone/data/clients.db 'PRAGMA integrity_check;'
sudo systemctl start inntophone-report-bot.service
sudo systemctl start inntophone-daily.timer
```

Ожидаемый результат SQLite — `ok`. Файл `clients.before-restore.db` удаляйте
только после полной проверки восстановленного приложения.
