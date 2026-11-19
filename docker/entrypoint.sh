#!/bin/sh
# =========================================================
# docker/entrypoint.sh — запуск в контейнере
# =========================================================
#
# Запущен от root (обычный случай):
#   • uid/gid пользователя nbm → NBM_UID/NBM_GID (по умолчанию
#     1000) — под владельца папки данных на хосте;
#   • создаёт data/, logs/, backups/ в $NBM_HOME и отдаёт nbm
#     файлы с чужим владельцем (например, ключи, скопированные
#     на хост от root);
#   • запускает программу от nbm.
#
# Запущен не от root (docker run --user …): ничего не меняет,
# права на папку данных — забота администратора.
#
# =========================================================
set -eu

NBM_HOME="${NBM_HOME:-/nbm}"

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

uid="${NBM_UID:-1000}"
gid="${NBM_GID:-1000}"
case "$uid$gid" in
    *[!0-9]*) echo "NBM_UID и NBM_GID — числа (сейчас: '$uid', '$gid')" >&2; exit 1 ;;
esac
if [ "$uid" = "0" ]; then
    echo "NBM_UID=0 не поддерживается: программа не работает от root" >&2
    exit 1
fi

[ "$(id -g nbm)" = "$gid" ] || groupmod -o -g "$gid" nbm
[ "$(id -u nbm)" = "$uid" ] || usermod -o -u "$uid" nbm

mkdir -p "$NBM_HOME/data" "$NBM_HOME/logs" "$NBM_HOME/backups"
# Только файлы с чужим владельцем: при большой истории бэкапов
# полный chown -R на каждом старте был бы долгим.
find "$NBM_HOME" \( ! -user "$uid" -o ! -group "$gid" \) -exec chown -h "$uid:$gid" {} +
# Ключи и секреты — только владельцу (ssh отказывается работать
# с ключом, доступным другим).
chmod 700 "$NBM_HOME/data"

export HOME=/home/nbm
exec setpriv --reuid="$uid" --regid="$gid" --init-groups "$@"
