#!/bin/sh
# =========================================================
# docker/smoke-test.sh — проверка собранного образа
# =========================================================
#
#     docker/smoke-test.sh <образ>
#
# Запускает контейнер с временной папкой данных и проверяет:
# здоровье, вход в веб-интерфейс, пользователя процесса,
# владельца файлов, часовой пояс, rclone и ssh, остановку.
# Используется в CI (.github/workflows/docker.yml).
#
# =========================================================
set -eu

IMAGE="${1:?укажите образ}"
NAME="nbm-smoke-$$"
PORT="${SMOKE_PORT:-18080}"
WORK="$(mktemp -d)"
DATA="$WORK/nbm"
UID_HOST="$(id -u)"
GID_HOST="$(id -g)"

fail() {
    echo "❌ $*" >&2
    echo "----- docker logs -----" >&2
    docker logs "$NAME" >&2 2>&1 || true
    exit 1
}

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    sudo rm -rf "$WORK" 2>/dev/null || rm -rf "$WORK" || true
}
trap cleanup EXIT

# Файл с чужим владельцем (root) — entrypoint должен его отдать nbm
mkdir -p "$DATA/data/keys/mikrotik"
echo "dummy" > "$DATA/data/keys/mikrotik/id_rsa.key"
sudo chown -R 0:0 "$DATA/data/keys"

docker run -d --name "$NAME" \
    -p "127.0.0.1:$PORT:8080" \
    -e NBM_ADMIN_PASSWORD=smoke-pass \
    -e NBM_UID="$UID_HOST" -e NBM_GID="$GID_HOST" \
    -e TZ=Europe/Kyiv \
    -v "$DATA:/nbm" \
    "$IMAGE" >/dev/null

echo "⏳ Жду статус healthy…"
status=""
for _ in $(seq 1 60); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo gone)"
    [ "$status" = "healthy" ] && break
    [ "$status" = "unhealthy" ] || [ "$status" = "gone" ] && fail "контейнер: $status"
    sleep 2
done
[ "$status" = "healthy" ] || fail "нет статуса healthy за 120 с (сейчас: $status)"
echo "✅ healthy"

curl -fsS "http://127.0.0.1:$PORT/api/health" | grep -q '"status"' || fail "/api/health"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/add")"
[ "$code" = "401" ] || fail "/add без пароля: $code (ожидался 401)"
curl -fsS -u admin:smoke-pass "http://127.0.0.1:$PORT/add" | grep -q "SSH (универсальный)" \
    || fail "страница добавления устройства (все плагины загружены?)"
echo "✅ веб-интерфейс и вход"

# Процесс программы — не root, а NBM_UID
docker top "$NAME" -eo uid,args | grep "main.py" | grep -v tini | awk '{print $1}' | grep -qx "$UID_HOST" \
    || { docker top "$NAME" -eo uid,args; fail "программа работает не от uid $UID_HOST"; }
[ "$(stat -c %u "$DATA/data/auth.json")" = "$UID_HOST" ] || fail "владелец data/auth.json"
[ "$(stat -c %u "$DATA/data/keys/mikrotik/id_rsa.key")" = "$UID_HOST" ] || fail "владелец ключа не исправлен"
[ "$(stat -c %a "$DATA/data")" = "700" ] || fail "права data/ не 700"
echo "✅ пользователь и права"

tz="$(docker exec "$NAME" python -c 'import time; print(time.strftime("%Z"))')"
case "$tz" in EET|EEST) ;; *) fail "часовой пояс: $tz (ожидался EET/EEST)" ;; esac
docker exec "$NAME" rclone version >/dev/null || fail "rclone"
docker exec "$NAME" ssh -V >/dev/null 2>&1 || fail "ssh"
docker exec "$NAME" scp 2>&1 | grep -qi usage || fail "scp"
echo "✅ часовой пояс ($tz), rclone, ssh, scp"

start=$(date +%s)
docker stop -t 20 "$NAME" >/dev/null
took=$(( $(date +%s) - start ))
[ "$took" -lt 15 ] || fail "остановка заняла $took с — сигнал не доходит до программы?"
echo "✅ остановка за $took с"

echo "🎉 Образ $IMAGE прошёл проверку"
