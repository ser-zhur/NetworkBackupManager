# Network Backup Manager на Linux (без Docker)

Скрипт `install.sh` устанавливает программу как службу systemd. Повторный запуск того же скрипта обновляет программу, `--uninstall` удаляет её.

## Требования

- Linux с systemd и доступ `root` (через `sudo`).
- Python 3.11 или новее:

| Система | Python | Подходит |
|---|---|---|
| Debian 12 / 13 | 3.11 / 3.13 | да |
| Ubuntu 24.04 и новее | 3.12+ | да |
| Ubuntu 22.04 | 3.10 | нет: используйте Docker ([DOCKER.md](DOCKER.md)) или Python 3.11 из PPA deadsnakes |
| RHEL / Rocky / Alma 9 | 3.9 (есть пакет `python3.11`) | да, после `dnf install python3.11` |

Недостающие пакеты скрипт ставит сам: `python3-venv`, `openssh-client` (для MikroTik на RouterOS 6), `curl`. Если пакеты ставить нельзя, запустите с `--no-deps` и установите их вручную. rclone для внешних хранилищ тоже скачивается автоматически, с проверкой контрольной суммы; не нужен — запустите с `--no-rclone`.

## Установка

```bash
git clone https://github.com/ser-zhur/NetworkBackupManager.git
cd NetworkBackupManager
git checkout "$(git describe --tags --abbrev=0)"    # последний выпуск
sudo ./install.sh
```

Ставьте выпущенную версию: ветка `main` — текущая разработка. Вместо `git clone` можно скачать и распаковать архив исходников («Source code (tar.gz)») со страницы [последнего выпуска](https://github.com/ser-zhur/NetworkBackupManager/releases/latest). В конце скрипт выводит адрес веб-интерфейса и **пароль первого входа** (пользователь `admin`). Сохраните пароль: он показывается один раз. Задать свой пароль при установке: `sudo NBM_ADMIN_PASSWORD='…' ./install.sh`.

Параметры:

| Параметр | Назначение |
|---|---|
| `--port 8090` | Порт веб-интерфейса (по умолчанию 8080) |
| `--host 127.0.0.1` | Адрес: только локальный доступ, например за обратным прокси. По умолчанию `0.0.0.0` — все интерфейсы |
| `--no-rclone` | Не устанавливать rclone |
| `--no-deps` | Не устанавливать пакеты ОС |

## Что где лежит

```
/opt/nbm/app          программа (только чтение); прежняя версия — /opt/nbm/app.prev
/opt/nbm/venv         Python-окружение
/var/lib/nbm/         ДАННЫЕ — всё, что нужно сохранять
  data/               устройства, настройки, пароли (зашифрованы),
                      SSH-ключи устройств: data/keys/<тип>/
  backups/            выгрузки и их история
  logs/               журналы
/etc/nbm/nbm.env      настройки запуска (порт, адрес и др.)
networkbackupmanager.service — служба systemd
```

Программа работает от системного пользователя `nbm`, а не от root. Писать она может только в `/var/lib/nbm`. Поэтому пути бэкапов устройств должны быть внутри этой папки; путь по умолчанию `backups/<имя>` там и находится.

> `/var/lib/nbm/data/secret.key` — ключ, которым зашифрованы пароли устройств. Без него пароли не расшифровать. Храните копию отдельно.

## Ежедневная работа

| Задача | Команда |
|---|---|
| Состояние | `systemctl status networkbackupmanager` |
| Журнал в реальном времени | `journalctl -u networkbackupmanager -f` |
| Журнал программы | `/var/lib/nbm/logs/app.log` |
| Перезапуск | кнопка в **Настройки → Служба** или `sudo systemctl restart networkbackupmanager` |
| Остановка / запуск | `sudo systemctl stop` / `start networkbackupmanager` |
| Изменить порт и другие настройки | `sudo nano /etc/nbm/nbm.env`, затем `sudo systemctl restart networkbackupmanager` |

**SSH-ключи устройств** кладите в `/var/lib/nbm/data/keys/<тип>/`, например `/var/lib/nbm/data/keys/mikrotik/id_rsa.key`, и отдайте их пользователю `nbm`:

```bash
sudo install -o nbm -g nbm -m 600 id_rsa.key /var/lib/nbm/data/keys/mikrotik/
```

**Забыли пароль:**
```bash
sudo -u nbm env NBM_HOME=/var/lib/nbm /opt/nbm/venv/bin/python /opt/nbm/app/main.py --create-user
```

## Обновление

```bash
cd NetworkBackupManager
git fetch --tags
git checkout "$(git tag --sort=-v:refname | head -1)"    # новый выпуск
sudo ./install.sh
```

Если ставили из архива — распакуйте архив новой версии и запустите `sudo ./install.sh` из него.

Настройки (`/etc/nbm/nbm.env`) и данные сохраняются, прежняя версия программы остаётся в `/opt/nbm/app.prev`. **Если новая версия не запустилась, скрипт сам возвращает прежнюю** и сообщает об ошибке; неудачная версия остаётся в `/opt/nbm/app.failed` для разбора.

Вернуться к прежней версии вручную:
```bash
git checkout v2.1.0      # нужная версия
sudo ./install.sh
```

## Удаление

```bash
sudo ./install.sh --uninstall           # программа и служба; данные /var/lib/nbm остаются
sudo ./install.sh --uninstall --purge   # и данные, и пользователь nbm
```

## Переезд с Windows

1. На Windows остановите службу.
2. Установите программу на Linux (`sudo ./install.sh`) и остановите её: `sudo systemctl stop networkbackupmanager`.
3. Скопируйте с Windows в `/var/lib/nbm/`:
   - `data\` — целиком, вместе с `secret.key` и `keys\`;
   - `backups\` — если нужна история версий;
   - `logs\` — по желанию.
4. Снова запустите `sudo ./install.sh`: он поправит владельца файлов и запустит службу.

Пути к бэкапам вида `D:\…\backups\R1` программа сама находит в `/var/lib/nbm/backups/R1`. Пароль входа переезжает вместе с `data/auth.json`.

## HTTPS

**Свой сертификат.** Положите `cert.pem` и `key.pem` в `/var/lib/nbm/data/tls/`, владелец — пользователь службы. Затем добавьте в `/etc/nbm/nbm.env` строки `NBM_TLS_CERT=tls/cert.pem` и `NBM_TLS_KEY=tls/key.pem` и выполните `sudo systemctl restart networkbackupmanager`. Подробнее — в [README](../README.md#https).

**Обратный прокси.** Установите с `--host 127.0.0.1` и добавьте в `/etc/nbm/nbm.env` строку `NBM_TRUSTED_PROXIES=127.0.0.1`. Пример настройки nginx — в [DOCKER.md](DOCKER.md#обратный-прокси), там `proxy_pass http://127.0.0.1:8080`.

## Неполадки

| Признак | Что делать |
|---|---|
| Скрипт: «Нужен Python 3.11 или новее» | Таблица в разделе «Требования», или Docker |
| Служба не запускается | `journalctl -u networkbackupmanager -n 50` |
| «Нет прав на чтение SSH-ключа» | `sudo chown nbm:nbm` и `chmod 600` для файла ключа |
| Бэкап: «Read-only file system» | Путь бэкапа устройства вне `/var/lib/nbm`: исправьте его в настройках устройства |
| Порт ниже 1024 (например, 80) | Работает: скрипт выдаёт службе право на такие порты. Но лучше использовать обратный прокси |
