# Network Backup Manager

Резервное копирование конфигураций сетевых устройств по SSH с веб-интерфейсом.

- **Устройства:** MikroTik (RouterOS 6/7), pfSense, D-Link DGS-1510. Новые типы добавляются плагинами в `plugins/`.
- **История:** каждая выгрузка сохраняется в git-репозиторий устройства. В интерфейсе можно посмотреть изменения между версиями.
- **Внешние хранилища:** копии через rclone (WebDAV, облака и т. д.), с шифрованием 7z AES-256.
- **Расписание и хранение:** автоматический запуск по расписанию, очистка старых версий.
- **Запуск:** служба Windows, systemd в Linux или контейнер.

## Быстрый старт (из исходников)

Нужен Python 3.12 или новее.

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt     # Linux: venv/bin/pip
venv/Scripts/python main.py --create-user        # логин и пароль веб-интерфейса
venv/Scripts/python main.py --foreground
```

Затем откройте http://127.0.0.1:8080.

Если подходит rclone из `PATH`, внешние хранилища работают сразу. Иначе положите `rclone(.exe)` в `bin/` или укажите путь в `NBM_RCLONE`.

## Параметры запуска

| Ключ | Назначение |
|---|---|
| `--foreground` | Только веб-сервер, без установки службы |
| `--create-user` | Создать или сменить пользователя веб-интерфейса |
| `--install-service` / `--uninstall-service` | Установить или удалить службу (Windows / systemd) |
| `--start-service` / `--stop-service` / `--restart-service` | Управление службой |
| `--version` | Показать версию |

## Переменные окружения

Переменные окружения важнее `config.json`.

| Переменная | Назначение |
|---|---|
| `NBM_HOME` | Папка данных: `data/`, `logs/`, `backups/`. По умолчанию — папка программы |
| `NBM_HOST`, `NBM_PORT` | Адрес и порт веб-интерфейса (по умолчанию `127.0.0.1:8080`) |
| `NBM_OPEN_BROWSER` | `0`/`1` — открывать ли браузер при запуске |
| `NBM_TRUSTED_PROXIES` | Адреса обратных прокси через запятую или `*` |
| `NBM_ADMIN_USER`, `NBM_ADMIN_PASSWORD` | Первый пользователь, если `data/auth.json` ещё нет |
| `NBM_RCLONE` | Путь к rclone |
| `NBM_CONTAINER` | `1` — запуск в контейнере: службой управляет не программа |

## Где лежат данные

```
<NBM_HOME>/
  data/devices.json   устройства (пароли зашифрованы ключом data/secret.key)
  data/keys/<тип>/    SSH-ключи устройств
  data/auth.json      пользователь веб-интерфейса
  backups/            выгрузки и их git-история
  logs/
```

Папка `data/` содержит секреты, в репозиторий она не попадает (`.gitignore`).

## Разработка

```bash
pip install -r requirements-dev.txt
pytest -q
python tools/build_css.py    # после правки шаблонов: пересборка Tailwind CSS
```

Сборка EXE и установщика для Windows — `build_exe.bat` (нужны PyInstaller и Inno Setup).

Скрипты в `tools/debug/` — ручная проверка SSH-подключения к устройству. Перед запуском впишите в них свой адрес и логин.
