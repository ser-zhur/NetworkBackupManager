# Network Backup Manager в Docker

Нужен сервер с Docker и плагином Compose: команда `sudo docker compose version` должна работать. Подходят процессоры x86-64 (amd64) и ARM64, например Raspberry Pi 4/5.

> Команды ниже — с `sudo`: Docker доступен только root. `sudo` действует на одну команду, поэтому в цепочке через `&&` он нужен перед каждой: `sudo docker compose pull && sudo docker compose up -d`. Если ваш пользователь в группе `docker`, `sudo` можно не писать (но эта группа даёт права, равные root).

Программу можно поставить двумя способами, дальше они работают одинаково:

| | А. Готовый образ | Б. Сборка на сервере |
|---|---|---|
| Откуда берётся | `ghcr.io/ser-zhur/networkbackupmanager` (GitHub Container Registry) | собирается из исходников на сервере |
| Обновление | `sudo docker compose pull && sudo docker compose up -d` (~30 с) | `sudo git pull && sudo docker compose up -d --build` (2–5 мин) |
| Откат | указать прежнюю версию в `.env` | `git checkout v<версия>` и пересборка |
| Нужно на сервере | Docker | Docker и git |

## Что где лежит

```
/opt/nbm/                  папка установки (имя любое)
  docker-compose.yml       описание контейнера
  .env                     ваши настройки (образец — .env.example)
  nbm/                     ДАННЫЕ — всё, что нужно сохранять
    data/                  устройства, пароли (зашифрованы), настройки,
                           SSH-ключи (data/keys/<тип>/), ключ шифрования
    backups/               выгрузки устройств и их история (git)
    logs/                  журналы
```

Контейнер можно удалять, пересоздавать и обновлять: всё ценное лежит в `nbm/` на сервере. Резервная копия программы — это копия папки `nbm/`.

> `nbm/data/secret.key` — ключ, которым зашифрованы пароли устройств и хранилищ. Без него пароли не расшифровать. Храните копию отдельно от бэкапов.

## Установка

### А. Готовый образ

```bash
sudo mkdir -p /opt/nbm && cd /opt/nbm
sudo curl -fLO https://github.com/ser-zhur/NetworkBackupManager/releases/latest/download/docker-compose.yml
sudo curl -fL -o .env https://github.com/ser-zhur/NetworkBackupManager/releases/latest/download/env.example
sudo nano .env          # NBM_ADMIN_PASSWORD, NBM_UID/NBM_GID, TZ
sudo docker compose up -d
```

Файлы берутся из последнего выпуска; их же можно скачать вручную со страницы [Releases](https://github.com/ser-zhur/NetworkBackupManager/releases).

### Б. Сборка на сервере

```bash
sudo git clone https://github.com/ser-zhur/NetworkBackupManager.git /opt/nbm
cd /opt/nbm
sudo git checkout "$(sudo git describe --tags --abbrev=0)"    # последний выпуск
sudo cp .env.example .env
sudo nano .env          # NBM_ADMIN_PASSWORD и раскомментировать строку COMPOSE_FILE=…
sudo docker compose up -d --build
```

Строка `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` в `.env` включает сборку на месте. Образ получает локальное имя `networkbackupmanager:local`, поэтому вы не перепутаете его с образом из реестра.

### Первый запуск

- Откройте `http://<сервер>:8080` и войдите как `NBM_ADMIN_USER` / `NBM_ADMIN_PASSWORD` из `.env`.
- Пароль из `.env` используется один раз, при создании пользователя. Дальше пароль меняется в программе, и строку в `.env` можно очистить.
- `sudo docker compose ps` показывает состояние контейнера. Через 30–40 секунд после запуска в нём должно быть `healthy`.

## Настройки `.env`

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `NBM_ADMIN_USER`, `NBM_ADMIN_PASSWORD` | `admin`, пусто | Первый пользователь |
| `NBM_VERSION` | `latest` | Версия образа (вариант А): `latest` или точная, например `2.1.1` |
| `NBM_LISTEN` | `8080` | Порт на сервере. `127.0.0.1:8080` — доступ только с самого сервера, например за обратным прокси |
| `NBM_DATA` | `./nbm` | Папка данных на сервере |
| `NBM_UID`, `NBM_GID` | `1000` | Владелец файлов в папке данных; узнать: `id -u`, `id -g` |
| `TZ` | `Europe/Kyiv` | Часовой пояс: по нему работает расписание бэкапов |
| `NBM_TRUSTED_PROXIES` | пусто | Адрес обратного прокси, если он есть |
| `NBM_API_TOKEN` | пусто | Токен мониторинга, если не хотите создавать его в интерфейсе |

После правки `.env` выполните `sudo docker compose up -d`: контейнер пересоздастся с новыми настройками.

## Ежедневная работа

| Задача | Команда (в `/opt/nbm`) |
|---|---|
| Состояние | `sudo docker compose ps` |
| Журнал в реальном времени | `sudo docker compose logs -f` |
| Журнал программы | `nbm/logs/app.log` |
| Перезапуск | `sudo docker compose restart` |
| Остановка / запуск | `sudo docker compose stop` / `sudo docker compose up -d` |
| Командная строка внутри | `sudo docker compose exec nbm sh` |
| Проверка SSH до устройства | `sudo docker compose exec nbm ssh -p 22 admin@192.0.2.1` |

**SSH-ключи устройств** кладутся в `nbm/data/keys/<тип>/`, например `nbm/data/keys/mikrotik/id_rsa.key`. Владельца и права менять не нужно: при запуске контейнер сам отдаёт файлы пользователю `NBM_UID`.

**Автозапуск.** После перезагрузки сервера контейнер поднимается сам (`restart: unless-stopped`), если служба Docker включена: `systemctl enable docker`.

## Обновление и откат

**Вариант А:**
```bash
cd /opt/nbm
sudo docker compose pull && sudo docker compose up -d
```
Откат: в `.env` укажите `NBM_VERSION=2.1.0` (прежнюю версию), затем `sudo docker compose up -d`. Чтобы снова обновляться автоматически, верните `latest`.

**Вариант Б:**
```bash
cd /opt/nbm
sudo git fetch --tags && sudo git checkout "$(sudo git tag --sort=-v:refname | head -1)"
sudo docker compose up -d --build
```
Откат: `sudo git checkout v2.1.0 && sudo docker compose up -d --build`. Вернуться к последней версии — команды обновления выше.

Перед обновлением на новую основную версию (например, 2.x → 3.x) сделайте копию папки `nbm/`.

## Резервная копия программы

```bash
cd /opt/nbm
sudo docker compose stop
sudo tar czf /root/nbm-$(date +%F).tar.gz nbm
sudo docker compose up -d
```
Восстановление: распакуйте архив в `/opt/nbm` и выполните `sudo docker compose up -d`.

Без остановки контейнера настройки (папку `data/`, без бэкапов устройств) можно сохранить из интерфейса: **Настройки → Копия настроек** — скачать, выгрузить на хранилища (в том числе по расписанию) или восстановить из архива. После восстановления контейнер перезапускается сам.

## Переезд с Windows

1. На Windows остановите службу.
2. Скопируйте с Windows в `/opt/nbm/nbm/` на сервере:
   - `data\` — целиком, вместе с `secret.key` и `keys\`;
   - `backups\` — если хотите сохранить историю версий;
   - `logs\` — по желанию.
3. На сервере выполните `sudo docker compose up -d`.

Пути к бэкапам устройств вида `D:\…\backups\R1` программа сама находит в `nbm/backups/R1` и в `devices.json` записывает их уже относительными. Владельца файлов исправляет сама. Путь к `rclone.exe` из Windows-настроек не мешает: в образе свой rclone.

Пароль входа переезжает вместе с `data/auth.json`, поэтому `NBM_ADMIN_PASSWORD` не нужен.

## Обратный прокси (HTTPS)

Пример для nginx: в `.env` задайте `NBM_LISTEN=127.0.0.1:8080` и `NBM_TRUSTED_PROXIES=*`.

```nginx
server {
    listen 443 ssl;
    server_name nbm.example.com;
    # ssl_certificate …; ssl_certificate_key …;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Неполадки

| Признак | Что делать |
|---|---|
| `permission denied while trying to connect to the docker API` | Команда выполнена без `sudo`. В цепочке `… && …` `sudo` нужен перед каждой командой |
| `unhealthy` в `sudo docker compose ps` | `sudo docker compose logs --tail 50`; проверьте место на диске и права на `nbm/` |
| `permission denied` в журнале | Совпадают ли `NBM_UID`/`NBM_GID` с владельцем `nbm/` (`ls -ln`)? |
| В интерфейсе время на 2–3 часа отличается от местного | Проверьте `TZ` в `.env` |
| `sudo docker compose pull`: `denied` / `unauthorized` | Выполните `sudo docker logout ghcr.io`: образ публичный, а устаревший сохранённый токен мешает его скачать |
| «Ключ хоста изменился» после переустановки устройства | Удалите строку устройства из `nbm/data/known_hosts` |
