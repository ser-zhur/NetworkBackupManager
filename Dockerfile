# =========================================================
# Network Backup Manager — образ Docker
# =========================================================
#
#   docker compose up -d          (см. docker-compose.yml, README)
#
# Данные — в томе /nbm (NBM_HOME): data/, logs/, backups/.
# Процесс работает от пользователя nbm (uid/gid задаются
# NBM_UID/NBM_GID — под владельца папки на хосте).
#
# =========================================================

ARG PYTHON_VERSION=3.14

# ---------------------------------------------------------
# rclone — выгрузка во внешние хранилища
# ---------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS rclone

# Не RCLONE_*: аргумент виден в RUN как переменная окружения,
# а rclone читает RCLONE_* как свои ключи (RCLONE_VERSION = --version).
ARG NBM_RCLONE_VERSION=v1.72.0
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/rclone
RUN set -eux; \
    arch="${TARGETARCH:-amd64}"; \
    zip="rclone-${NBM_RCLONE_VERSION}-linux-${arch}.zip"; \
    curl -fsSLO "https://downloads.rclone.org/${NBM_RCLONE_VERSION}/${zip}"; \
    curl -fsSL "https://downloads.rclone.org/${NBM_RCLONE_VERSION}/SHA256SUMS" -o SHA256SUMS; \
    grep " ${zip}\$" SHA256SUMS | sha256sum -c -; \
    python -m zipfile -e "${zip}" .; \
    install -m 0755 "rclone-${NBM_RCLONE_VERSION}-linux-${arch}/rclone" /usr/local/bin/rclone; \
    /usr/local/bin/rclone version

# ---------------------------------------------------------
# Приложение
# ---------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

ARG VERSION=dev
LABEL org.opencontainers.image.title="Network Backup Manager" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.description="Резервное копирование конфигураций сетевых устройств по SSH" \
      org.opencontainers.image.source="https://github.com/ser-zhur/NetworkBackupManager" \
      org.opencontainers.image.version="${VERSION}"

# openssh-client — MikroTik RouterOS 6 выгружается системным ssh/scp;
# tini — корректная остановка и сбор завершённых дочерних процессов;
# tzdata — часовой пояс из TZ (без неё — молча UTC, и расписание
# сдвигается).
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client tini tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --user-group --create-home --home-dir /home/nbm --shell /usr/sbin/nologin nbm

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY main.py ./
COPY core ./core
COPY web ./web
COPY plugins ./plugins
COPY storages ./storages
COPY LICENSE ./LICENSE
# Лицензии сторонних компонентов образа (генератор после
# запуска удаляется)
COPY tools/third_party_licenses.py ./tools/
COPY tools/licenses ./tools/licenses
RUN python tools/third_party_licenses.py THIRD_PARTY_LICENSES.txt --target docker \
 && rm -rf tools
COPY docker/entrypoint.sh /usr/local/bin/nbm-entrypoint
COPY docker/healthcheck.py /usr/local/bin/nbm-healthcheck
RUN chmod 0755 /usr/local/bin/nbm-entrypoint

# NBM_RCLONE важнее config.json: путь rclone.exe, перенесённый
# вместе с data/ из установки на Windows, здесь не сработал бы.
ENV NBM_HOME=/nbm \
    NBM_HOST=0.0.0.0 \
    NBM_PORT=8080 \
    NBM_CONTAINER=1 \
    NBM_RCLONE=/usr/local/bin/rclone \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME ["/nbm"]
EXPOSE 8080

# /api/health: 200 — работает, 503 — планировщик остановлен
# или нельзя писать в папку данных.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python /usr/local/bin/nbm-healthcheck

ENTRYPOINT ["tini", "--", "nbm-entrypoint"]
CMD ["python", "main.py", "--foreground"]
