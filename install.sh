#!/usr/bin/env bash
# =========================================================
# install.sh — установка и обновление на Linux (systemd)
# =========================================================
#
#   sudo ./install.sh                 установить или обновить
#   sudo ./install.sh --port 8090     … с другим портом
#   sudo ./install.sh --uninstall     удалить программу (данные остаются)
#   sudo ./install.sh --uninstall --purge   … и данные тоже
#
# Запускать из папки с исходниками (git clone или архив релиза).
#
#   /opt/nbm/app        код (прежняя версия — /opt/nbm/app.prev)
#   /opt/nbm/venv       Python-окружение
#   /var/lib/nbm        данные: data/, backups/, logs/
#   /etc/nbm/nbm.env    настройки (NBM_*)
#   networkbackupmanager.service — служба, пользователь nbm
#
# Повторный запуск из новой версии — обновление: настройки и
# данные сохраняются; если новая версия не запустилась —
# возвращается прежняя.
#
# Подробно — docs/LINUX.md.
# =========================================================
set -euo pipefail

PREFIX=/opt/nbm
DATA=/var/lib/nbm
ETC=/etc/nbm
ENV_FILE=$ETC/nbm.env
UNIT=networkbackupmanager.service
UNIT_FILE=/etc/systemd/system/$UNIT
USER_NAME=nbm
RCLONE_VERSION_PIN=v1.72.0
MIN_PY=(3 11)

PORT=""
HOST=""
WITH_RCLONE=1
WITH_DEPS=1
UNINSTALL=0
PURGE=0

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1;34m▶\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

Параметры:
  --port N         порт веб-интерфейса (по умолчанию 8080)
  --host ADDR      адрес (по умолчанию 0.0.0.0 — все интерфейсы;
                   127.0.0.1 — только локально, за обратным прокси)
  --no-rclone      не устанавливать rclone (внешние хранилища не нужны)
  --no-deps        не доустанавливать пакеты ОС (python3-venv, openssh-client)
  --uninstall      удалить программу; с --purge — и данные
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port)      PORT="${2:?--port: укажите номер}"; shift 2 ;;
        --host)      HOST="${2:?--host: укажите адрес}"; shift 2 ;;
        --no-rclone) WITH_RCLONE=0; shift ;;
        --no-deps)   WITH_DEPS=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --purge)     PURGE=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           usage; die "Неизвестный параметр: $1" ;;
    esac
done

[ "$(id -u)" = 0 ] || die "Нужны права root: sudo $0 $*"
[ "$(uname -s)" = Linux ] || die "Скрипт только для Linux"
[ -d /run/systemd/system ] || die "Нужен systemd (для Docker — docs/DOCKER.md)"

if [ -n "$PORT" ]; then
    case "$PORT" in ''|*[!0-9]*) die "--port: нужно число" ;; esac
    [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port: 1…65535"
fi

# =========================================================
# Удаление
# =========================================================
if [ "$UNINSTALL" = 1 ]; then
    say "Удаление Network Backup Manager"
    if [ -f "$UNIT_FILE" ]; then
        systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
        rm -f "$UNIT_FILE"
        systemctl daemon-reload
        ok "Служба $UNIT удалена"
    fi
    rm -rf "$PREFIX" "$ETC"
    ok "Удалены $PREFIX и $ETC"
    if [ "$PURGE" = 1 ]; then
        rm -rf "$DATA"
        id "$USER_NAME" >/dev/null 2>&1 && userdel "$USER_NAME" 2>/dev/null || true
        ok "Удалены данные $DATA и пользователь $USER_NAME"
    else
        ok "Данные сохранены: $DATA (удалить: sudo $0 --uninstall --purge)"
    fi
    exit 0
fi

[ -f "$SRC/main.py" ] && [ -d "$SRC/core" ] || die "Запускайте install.sh из папки с исходниками программы"

# =========================================================
# Пакеты ОС
# =========================================================
pkg_install() {
    # $1 — пакет apt, $2 — пакет dnf/yum, $3 — пакет zypper
    if command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q "$1" >/dev/null
    elif command -v dnf >/dev/null; then
        dnf install -y -q "$2" >/dev/null
    elif command -v yum >/dev/null; then
        yum install -y -q "$2" >/dev/null
    elif command -v zypper >/dev/null; then
        zypper --non-interactive install "$3" >/dev/null
    else
        return 1
    fi
}

APT_UPDATED=0
need_pkg() {
    # need_pkg "<описание>" <apt> <dnf> <zypper>
    [ "$WITH_DEPS" = 1 ] || die "Не хватает: $1. Установите вручную или запустите без --no-deps"
    if command -v apt-get >/dev/null && [ "$APT_UPDATED" = 0 ]; then
        apt-get update -q >/dev/null; APT_UPDATED=1
    fi
    say "Устанавливаю $1"
    pkg_install "$2" "$3" "$4" || die "Не удалось установить $1 — поставьте пакет вручную"
}

py_ok() {
    "$1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (${MIN_PY[0]}, ${MIN_PY[1]}) else 1)" 2>/dev/null
}

find_python() {
    local c
    for c in python3.14 python3.13 python3.12 python3.11 python3; do
        if command -v "$c" >/dev/null && py_ok "$(command -v "$c")"; then
            command -v "$c"; return 0
        fi
    done
    return 1
}

say "Проверка системы"
PY="$(find_python)" || {
    command -v python3 >/dev/null || need_pkg "Python 3" python3 python3 python3
    PY="$(find_python)" || die "Нужен Python ${MIN_PY[0]}.${MIN_PY[1]} или новее (есть: $(python3 --version 2>&1)). Обновите ОС или используйте Docker (docs/DOCKER.md)"
}
PY_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

# venv + ensurepip (в Debian/Ubuntu — отдельный пакет)
if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
    need_pkg "модуль venv для Python $PY_VER" "python${PY_VER}-venv" python3 python3
    "$PY" -c 'import venv, ensurepip' 2>/dev/null || {
        need_pkg "модуль venv для Python" python3-venv python3 python3
        "$PY" -c 'import venv, ensurepip' 2>/dev/null || die "Нет модуля venv для $PY"
    }
fi
# ssh и scp — MikroTik RouterOS 6 выгружается системным клиентом
command -v ssh >/dev/null && command -v scp >/dev/null || need_pkg "OpenSSH-клиент" openssh-client openssh-clients openssh
command -v curl >/dev/null || command -v wget >/dev/null || need_pkg "curl" curl curl curl
ok "Python $PY_VER ($PY), ssh, scp"

download() {  # download <url> <файл>
    if command -v curl >/dev/null; then curl -fsSL "$1" -o "$2"; else wget -q "$1" -O "$2"; fi
}

# =========================================================
# rclone
# =========================================================
if [ "$WITH_RCLONE" = 1 ]; then
    if command -v rclone >/dev/null; then
        ok "rclone уже есть: $(rclone version 2>/dev/null | head -1)"
    else
        case "$(uname -m)" in
            x86_64|amd64)  arch=amd64 ;;
            aarch64|arm64) arch=arm64 ;;
            armv7l)        arch=arm-v7 ;;
            armv6l)        arch=arm ;;
            i?86)          arch=386 ;;
            *) die "rclone: неизвестная архитектура $(uname -m) — поставьте rclone вручную и запустите с --no-rclone" ;;
        esac
        say "Устанавливаю rclone $RCLONE_VERSION_PIN ($arch)"
        tmp="$(mktemp -d)"
        zip="rclone-${RCLONE_VERSION_PIN}-linux-${arch}.zip"
        download "https://downloads.rclone.org/${RCLONE_VERSION_PIN}/${zip}" "$tmp/$zip"
        download "https://downloads.rclone.org/${RCLONE_VERSION_PIN}/SHA256SUMS" "$tmp/SHA256SUMS"
        (cd "$tmp" && grep " ${zip}\$" SHA256SUMS | sha256sum -c - >/dev/null) || die "rclone: контрольная сумма не совпала"
        "$PY" -m zipfile -e "$tmp/$zip" "$tmp"
        install -m 0755 "$tmp/rclone-${RCLONE_VERSION_PIN}-linux-${arch}/rclone" /usr/local/bin/rclone
        rm -rf "$tmp"
        ok "rclone: /usr/local/bin/rclone"
    fi
fi

# =========================================================
# Пользователь и папки
# =========================================================
if ! id "$USER_NAME" >/dev/null 2>&1; then
    nologin="$(command -v nologin || echo /usr/sbin/nologin)"
    useradd --system --user-group --home-dir "$DATA" --no-create-home --shell "$nologin" "$USER_NAME"
    ok "Создан системный пользователь $USER_NAME"
fi

FIRST_INSTALL=0
[ -f "$UNIT_FILE" ] || FIRST_INSTALL=1

mkdir -p "$DATA/data" "$DATA/logs" "$DATA/backups" "$ETC" "$PREFIX"
# Только чужие файлы (например, скопированные при переезде от root)
find "$DATA" \( ! -user "$USER_NAME" -o ! -group "$USER_NAME" \) -exec chown -h "$USER_NAME:$USER_NAME" {} +
chmod 750 "$DATA"
chmod 700 "$DATA/data"

# =========================================================
# Настройки /etc/nbm/nbm.env
# =========================================================
set_env() {  # set_env NAME VALUE — заменить или добавить строку
    # Значение — через окружение awk: без интерпретации & | \ (пароль)
    local tmp
    tmp="$(mktemp)"
    NBM_SET_NAME="$1" NBM_SET_VALUE="$2" awk '
        BEGIN { n = ENVIRON["NBM_SET_NAME"]; v = ENVIRON["NBM_SET_VALUE"]; done = 0 }
        index($0, n "=") == 1 { if (!done) print n "=" v; done = 1; next }
        { print }
        END { if (!done) print n "=" v }
    ' "$ENV_FILE" > "$tmp"
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
}

NEW_PASSWORD=""
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# Настройки Network Backup Manager (служба $UNIT).
# После правки: sudo systemctl restart $UNIT
# Все переменные — README, раздел «Переменные окружения».
NBM_HOME=$DATA
NBM_HOST=${HOST:-0.0.0.0}
NBM_PORT=${PORT:-8080}
NBM_MANAGED_BY=systemd
NBM_OPEN_BROWSER=0
# NBM_TRUSTED_PROXIES=127.0.0.1
EOF
fi
# Обязательные — и при обновлении старой установки
set_env NBM_HOME "$DATA"
set_env NBM_MANAGED_BY systemd
[ -z "$PORT" ] || set_env NBM_PORT "$PORT"
[ -z "$HOST" ] || set_env NBM_HOST "$HOST"

# Первый вход: пароль один раз, пока пользователь не создан
if [ ! -f "$DATA/data/auth.json" ]; then
    NEW_PASSWORD="${NBM_ADMIN_PASSWORD:-$("$PY" -c 'import secrets; print(secrets.token_urlsafe(12))')}"
    set_env NBM_ADMIN_USER "${NBM_ADMIN_USER:-admin}"
    set_env NBM_ADMIN_PASSWORD "\"$(printf '%s' "$NEW_PASSWORD" | sed 's/[\\"]/\\&/g')\""
fi
chown root:"$USER_NAME" "$ENV_FILE"
chmod 640 "$ENV_FILE"

PORT_NOW="$(sed -n 's/^NBM_PORT=//p' "$ENV_FILE" | tail -1)"
HOST_NOW="$(sed -n 's/^NBM_HOST=//p' "$ENV_FILE" | tail -1)"
PORT_NOW="${PORT_NOW:-8080}"

# =========================================================
# Код и Python-окружение
# =========================================================
say "Копирую программу в $PREFIX/app"
rm -rf "$PREFIX/app.new"
mkdir -p "$PREFIX/app.new"
tar -C "$SRC" \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='plugins/*/routers.json' --exclude='plugins/*/*.key' \
    --exclude='plugins/*/*.pem' --exclude='plugins/*/*.ppk' \
    -cf - main.py requirements.txt core web plugins storages \
    | tar -C "$PREFIX/app.new" -xf -

VENV="$PREFIX/venv"
if [ -x "$VENV/bin/python" ] && [ "$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" != "$PY_VER" ]; then
    warn "Python изменился — пересоздаю окружение"
    rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python" ]; then
    say "Создаю Python-окружение $VENV"
    "$PY" -m venv "$VENV"
fi
say "Устанавливаю зависимости (pip)…"
"$VENV/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip
"$VENV/bin/python" -m pip install -q --disable-pip-version-check -r "$PREFIX/app.new/requirements.txt"
"$VENV/bin/python" -m compileall -q "$PREFIX/app.new" >/dev/null || true
VERSION="$("$VENV/bin/python" "$PREFIX/app.new/main.py" --version 2>/dev/null | head -1 | awk '{print $NF}' || true)"
VERSION="${VERSION:-?}"
ok "Версия: $VERSION"

# =========================================================
# Служба systemd
# =========================================================
caps=""
if [ "$PORT_NOW" -lt 1024 ]; then
    caps="AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE"
fi

cat > "$UNIT_FILE" <<EOF
# Создано install.sh — правки затрутся при обновлении.
# Свои настройки — sudo systemctl edit $UNIT
[Unit]
Description=Network Backup Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
EnvironmentFile=$ENV_FILE
Environment=PYTHONDONTWRITEBYTECODE=1
WorkingDirectory=$PREFIX/app
ExecStart=$VENV/bin/python $PREFIX/app/main.py --foreground
# Кнопка «Перезапустить» в интерфейсе — выход с кодом 1
Restart=on-failure
RestartSec=3
TimeoutStopSec=20
UMask=0027
$caps

# Ограничения: писать можно только в папку данных
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

health_url="http://127.0.0.1:${PORT_NOW}/api/health"
case "$HOST_NOW" in ""|0.0.0.0|::|127.0.0.1|localhost) ;; *) health_url="http://${HOST_NOW}:${PORT_NOW}/api/health" ;; esac

wait_healthy() {
    local i
    for i in $(seq 1 60); do
        if "$PY" -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=3)" "$health_url" 2>/dev/null; then
            return 0
        fi
        systemctl is-failed --quiet "$UNIT" && return 1
        sleep 1
    done
    return 1
}

say "Запускаю службу"
systemctl stop "$UNIT" 2>/dev/null || true
if [ -d "$PREFIX/app" ]; then
    rm -rf "$PREFIX/app.prev"
    mv "$PREFIX/app" "$PREFIX/app.prev"
fi
mv "$PREFIX/app.new" "$PREFIX/app"
chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl reset-failed "$UNIT" 2>/dev/null || true
systemctl start "$UNIT"

if ! wait_healthy; then
    journalctl -u "$UNIT" -n 30 --no-pager >&2 || true
    if [ -d "$PREFIX/app.prev" ]; then
        warn "Новая версия не запустилась — возвращаю прежнюю"
        systemctl stop "$UNIT" || true
        rm -rf "$PREFIX/app.failed"
        mv "$PREFIX/app" "$PREFIX/app.failed"
        mv "$PREFIX/app.prev" "$PREFIX/app"
        systemctl reset-failed "$UNIT" 2>/dev/null || true
        systemctl start "$UNIT"
        wait_healthy && die "Обновление не удалось (журнал выше); работает прежняя версия. Новая — в $PREFIX/app.failed" \
                     || die "Не запускается и прежняя версия — см. journalctl -u $UNIT"
    fi
    die "Служба не запустилась — журнал выше (journalctl -u $UNIT)"
fi

# Пароль первого входа больше не нужен: пользователь создан
if [ -f "$DATA/data/auth.json" ]; then
    sed -i '/^NBM_ADMIN_PASSWORD=/d' "$ENV_FILE"
fi

ok "Служба $UNIT работает"
echo
if [ "$FIRST_INSTALL" = 1 ]; then echo "Network Backup Manager $VERSION установлен."; else echo "Network Backup Manager обновлён до $VERSION."; fi
shown_host="$HOST_NOW"
case "$shown_host" in ""|0.0.0.0|::) shown_host="$(hostname -I 2>/dev/null | awk '{print $1}')"; shown_host="${shown_host:-<адрес сервера>}" ;; esac
echo "  Веб-интерфейс:  http://${shown_host}:${PORT_NOW}"
if [ -n "$NEW_PASSWORD" ]; then
    echo "  Вход:           ${NBM_ADMIN_USER:-admin} / $NEW_PASSWORD"
    echo "                  (сохраните; сменить — в программе или: sudo -u $USER_NAME env NBM_HOME=$DATA $VENV/bin/python $PREFIX/app/main.py --create-user)"
fi
echo "  Данные:         $DATA   (SSH-ключи устройств — $DATA/data/keys/<тип>/)"
echo "  Настройки:      $ENV_FILE"
echo "  Журнал:         journalctl -u $UNIT -f   или   $DATA/logs/app.log"
echo "  Обновление:     sudo ./install.sh  (из папки новой версии)"
