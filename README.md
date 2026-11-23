# Network Backup Manager

Резервное копирование конфигураций сетевых устройств по SSH с веб-интерфейсом.

- **Устройства:** MikroTik (RouterOS 6/7), pfSense, D-Link DGS-1510 и любое устройство, которое показывает конфигурацию командой по SSH: Cisco IOS, Huawei, HPE/H3C, Aruba, Juniper, VyOS, OpenWrt — готовыми профилями или своими командами. Новые типы добавляются плагинами ([docs/PLUGINS.md](docs/PLUGINS.md)).
- **История:** каждая выгрузка сохраняется в git-репозиторий устройства. В интерфейсе можно посмотреть изменения между версиями.
- **Внешние хранилища:** копии через rclone (WebDAV, облака и т. д.), с шифрованием 7z AES-256.
- **Расписание и хранение:** автоматический запуск по расписанию, очистка старых версий.
- **Запуск:** служба Windows ([docs/WINDOWS.md](docs/WINDOWS.md)), служба systemd в Linux ([docs/LINUX.md](docs/LINUX.md)) или Docker ([docs/DOCKER.md](docs/DOCKER.md)).
- **Уведомления:** Telegram, почта, webhook — об ошибках, восстановлении и изменениях конфигурации.
- **Мониторинг:** `/api/health` и `/api/status` для Zabbix и аналогов, готовый шаблон Zabbix, журнал событий.

## Установка

| Где | Как | Подробно |
|---|---|---|
| Windows 10/11, Windows Server 2016+ | Установщик, служба Windows | [docs/WINDOWS.md](docs/WINDOWS.md) |
| Linux с Docker (amd64, arm64) | Готовый образ | [docs/DOCKER.md](docs/DOCKER.md) |
| Linux без Docker | `install.sh`, служба systemd | [docs/LINUX.md](docs/LINUX.md) |

Устанавливайте выпущенные версии со страницы [Releases](https://github.com/ser-zhur/NetworkBackupManager/releases). Ветка `main` — текущая разработка. Что изменилось — [CHANGELOG.md](CHANGELOG.md).

### Windows

1. Скачайте `NetworkBackupManager-<версия>-Setup.exe` из [последнего выпуска](https://github.com/ser-zhur/NetworkBackupManager/releases/latest) и запустите. Программа установится как служба Windows.
2. Создайте пользователя веб-интерфейса — в командной строке **от имени администратора**:
   ```bat
   "C:\Program Files\NetworkBackupManager\NetworkBackupManager.exe" --create-user
   ```
3. Откройте `http://127.0.0.1:8080`.

Доступ с других компьютеров, обновление и удаление — [docs/WINDOWS.md](docs/WINDOWS.md).

### Docker

```bash
sudo mkdir -p /opt/nbm && cd /opt/nbm
sudo curl -fLO https://github.com/ser-zhur/NetworkBackupManager/releases/latest/download/docker-compose.yml
sudo curl -fL -o .env https://github.com/ser-zhur/NetworkBackupManager/releases/latest/download/env.example
sudo nano .env          # NBM_ADMIN_PASSWORD — пароль первого входа, TZ — часовой пояс
sudo docker compose up -d
```

Интерфейс — `http://<сервер>:8080`, пользователь `admin` и пароль из `.env`. Данные лежат в `/opt/nbm/nbm`. Обновление: `docker compose pull && docker compose up -d`. Сборка образа на сервере, откат, переезд с Windows — [docs/DOCKER.md](docs/DOCKER.md).

### Linux (служба systemd)

```bash
git clone https://github.com/ser-zhur/NetworkBackupManager.git
cd NetworkBackupManager
git checkout "$(git describe --tags --abbrev=0)"    # последний выпуск
sudo ./install.sh
```

Скрипт ставит недостающие пакеты и rclone, создаёт пользователя `nbm` и службу `networkbackupmanager`, а в конце выводит адрес и **пароль первого входа**. Данные лежат в `/var/lib/nbm`. Обновление — тот же скрипт из новой версии; если она не запустилась, возвращается прежняя. Подробно — [docs/LINUX.md](docs/LINUX.md).

### Из исходников (для разработки)

Нужен Python 3.11 или новее.

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
| `NBM_API_TOKEN` | Токен мониторинга для `/api/status` (дополнительно к созданному в интерфейсе) |
| `NBM_CONTAINER` | `1` — запуск в контейнере: службой управляет не программа |

## Устройства по SSH без своего плагина

Тип **«SSH (универсальный)»** выполняет команды по SSH и сохраняет их вывод. Что выполнять, задаёт профиль:

| Профиль | Команда |
|---|---|
| Cisco IOS / IOS-XE | `show running-config` (при приглашении `>` — `enable` с паролем из настроек) |
| D-Link DGS | `show running-config` |
| Huawei VRP | `display current-configuration` |
| HPE Comware / H3C | `display current-configuration` |
| HPE Aruba OS-Switch / ProCurve | `show running-config` |
| Juniper Junos | `show configuration \| display set` |
| VyOS / Ubiquiti EdgeOS | `show configuration commands` |
| OpenWrt | `uci export` |
| Свои команды | список команд из настроек устройства |

Профили проверены на имитации устройств, а не на реальном оборудовании. Если профиль не подошёл, пришлите текст ошибки из карточки устройства.

Свой профиль — JSON-файл в `data/ssh_profiles/` (формат — [docs/PLUGINS.md](docs/PLUGINS.md#профили-ssh)). Файл с тем же именем, что у поставочного, заменяет его. Профили читаются при запуске программы.

Вывод проверяется перед сохранением. Если у вывода нет начала или конца конфигурации (обрыв связи, мало времени на ответ) или устройство не приняло команду, бэкап считается неудачным, и прежняя версия не затирается.

## Уведомления

**Настройки → Уведомления**: каналы Telegram (бот), почта (SMTP) и webhook (POST JSON; поле `text` подходит для входящих webhook Slack, Mattermost, Rocket.Chat). Для каждого канала выбирается, о чём сообщать:

- ошибки бэкапа, выгрузки в хранилище и планировщика;
- восстановление после ошибки;
- изменения конфигурации устройства: файлы и число строк, по желанию — с текстом изменений. В тексте могут оказаться хэши паролей, поэтому по умолчанию он не отправляется;
- итог прогона всех устройств (всегда, только при ошибках или никогда);
- запуск программы.

Всё, что случилось за прогон всех устройств, приходит одним сообщением вместе с его итогом. Кнопка «Проверить» отправляет пробное сообщение. Токены и пароли каналов хранятся зашифрованными (`data/notifications.json`).

## Мониторинг

| Адрес | Доступ | Что отдаёт |
|---|---|---|
| `GET /api/health` | без пароля | `200` — работает, `503` — планировщик остановлен или нельзя писать в папку данных |
| `GET /api/status` | токен или логин/пароль | Устройства и их состояние, планировщик, последний прогон, список проблем (JSON) |
| `GET /api/events` | токен или логин/пароль | Последние события: бэкап не удался или восстановился, ошибки хранилищ, итоги прогонов |

Токен создаётся в **Настройки → Мониторинг**. Он даёт только чтение состояния и передаётся заголовком `Authorization: Bearer <токен>`.

Шаблон Zabbix (6.0 и новее) — [web/static/monitoring/zbx_nbm_template.json](web/static/monitoring/zbx_nbm_template.json). Его также можно скачать со вкладки «Мониторинг». После импорта задайте макросы узла `{$NBM.URL}` и `{$NBM.TOKEN}`. Шаблон собирается скриптом `tools/build_zabbix_template.py`, руками его не правят.

## Где лежат данные

```
<NBM_HOME>/
  data/devices.json   устройства (пароли зашифрованы ключом data/secret.key)
  data/keys/<тип>/    SSH-ключи устройств
  data/auth.json      пользователь веб-интерфейса
  data/state.json     результаты последних бэкапов
  data/events.jsonl   журнал событий
  data/notifications.json  каналы уведомлений
  data/ssh_profiles/  свои профили SSH-устройств
  data/known_hosts    запомненные ключи SSH-хостов
  backups/            выгрузки и их git-история
  logs/
```

Папка `data/` содержит секреты, в репозиторий она не попадает (`.gitignore`).

Папку данных можно перенести в другое место, на другой компьютер или в Docker. Пути к бэкапам устройств внутри неё хранятся относительными, а пути прежней установки (`D:\…\backups\R1`) программа сама находит в `backups/`.

## Разработка

```bash
pip install -r requirements-dev.txt
pytest -q
python tools/build_css.py    # после правки шаблонов: пересборка Tailwind CSS
```

Как написать плагин для нового типа устройств — [docs/PLUGINS.md](docs/PLUGINS.md).

Сборка EXE и установщика для Windows — `build_exe.bat` (нужны PyInstaller и Inno Setup 6.3+ или 7). GitHub Actions собирает установщик при каждом изменении и проверяет установку, обновление и удаление (workflow `windows-installer`); готовый `NetworkBackupManager-Setup.exe` — в артефактах прогона.

Скрипты в `tools/debug/` — ручная проверка SSH-подключения к устройству. Перед запуском впишите в них свой адрес и логин.

Участие в разработке — [CONTRIBUTING.md](CONTRIBUTING.md). Об уязвимостях сообщайте закрыто — [SECURITY.md](SECURITY.md).

## Лицензия

© 2026 Жураченко.

Программа распространяется по лицензии [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only). Её можно бесплатно использовать, в том числе в коммерческих организациях, изучать, изменять и распространять. Условие: изменённую версию, которую вы распространяете или к которой даёте доступ по сети, нужно предоставлять её пользователям с исходным кодом под той же лицензией.

Если условия AGPL вам не подходят, например вы хотите встроить программу в закрытый продукт, возможна отдельная коммерческая лицензия. Напишите автору через [GitHub](https://github.com/ser-zhur).

Программа предоставляется «как есть», без каких-либо гарантий; подробно — разделы 15 и 16 лицензии.

Сторонние компоненты и их лицензии перечислены в файле `THIRD_PARTY_LICENSES.txt` установщика и Docker-образа и на странице «Сторонние компоненты» в интерфейсе (`python tools/third_party_licenses.py` — для запуска из исходников).

## Торговые марки

MikroTik, RouterOS, Cisco, Huawei, D-Link, HPE, H3C, Aruba, Juniper, Junos, pfSense, VyOS, OpenWrt и другие названия продуктов являются торговыми марками или зарегистрированными торговыми марками их владельцев. Они упоминаются только для указания совместимости. Проект не связан с владельцами этих марок и не одобрен ими.
