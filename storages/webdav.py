# =========================================================
# storages/webdav.py
# =========================================================
#
# Выгрузка резервных копий на WebDAV-сервер через rclone.
#
# Поддерживает Nextcloud, ownCloud, SharePoint, rclone
# serve webdav и любой стандартный WebDAV.
#
# Требует установленный rclone — путь настраивается
# на странице /storage-settings.
#
# =========================================================

from __future__ import annotations

import logging
import os

from core import rclone as rclone_mod

from core.storage.base import (
    StorageBackend,
    StorageField,
    PushContext,
    PushResult,
)


logger = logging.getLogger(__name__)


_REMOTE_NAME = "nbm_webdav"


class WebDavStorageBackend(StorageBackend):

    id = "webdav"
    name = "WebDAV (через rclone)"
    description = (
        "Выгрузка на WebDAV-сервер (Nextcloud, ownCloud, "
        "SharePoint, rclone serve webdav)."
    )
    icon = "☁️"

    fields = [
        StorageField(
            name="url",
            label="URL сервера",
            required=True,
            placeholder=(
                "https://cloud.example.com/remote.php/dav/files/user/"
            ),
            help="Полный URL WebDAV-эндпоинта.",
        ),
        StorageField(
            name="vendor",
            label="Тип сервера",
            type="select",
            default="other",
            choices=[
                ("other", "Другой (стандартный WebDAV)"),
                ("nextcloud", "Nextcloud"),
                ("owncloud", "ownCloud"),
                ("sharepoint", "SharePoint Online"),
                ("sharepoint-ntlm", "SharePoint (NTLM)"),
                ("rclone", "rclone serve webdav"),
            ],
        ),
        StorageField(
            name="user",
            label="Пользователь",
            required=True,
        ),
        StorageField(
            name="pass",
            label="Пароль",
            type="password",
        ),
        StorageField(
            name="remote_dir",
            label="Каталог на сервере",
            default="/network_backups",
            placeholder="/network_backups",
            help=(
                "Абсолютный путь от корня WebDAV. "
                "Будет создан при необходимости."
            ),
        ),
        StorageField(
            name="mode",
            label="Режим",
            type="select",
            default="changes_only",
            choices=[
                ("changes_only", "Только изменённые"),
                ("all", "Все бэкапы"),
            ],
            help=(
                "При режиме «только изменённые» выгрузка "
                "пропускается, если git не увидел изменений."
            ),
        ),
    ]

    # -----------------------------------------------------
    # REMOTE CONFIG
    # -----------------------------------------------------

    @staticmethod
    def _build_remote_config(config: dict) -> dict | None:
        """
        Формирует секцию [remote] для rclone.conf.

        None — если не заданы обязательные поля или
        не удалось зашифровать пароль.
        """
        url = (config.get("url") or "").strip()
        user = (config.get("user") or "").strip()
        password = config.get("pass") or ""
        vendor = config.get("vendor") or "other"

        if not url or not user:
            return None

        obscured = rclone_mod.obscure(password) if password else ""
        if password and obscured is None:
            return None

        cfg = {
            "type": "webdav",
            "url": url,
            "vendor": vendor,
            "user": user,
        }
        if obscured:
            cfg["pass"] = obscured

        return cfg

    @staticmethod
    def _remote_dir(config: dict) -> str:
        """
        Нормализует путь на сервере.

        rclone плохо переносит ведущий '/' после ':'.
        """
        raw = (config.get("remote_dir") or "").strip()
        return raw.strip("/")

    # -----------------------------------------------------
    # TEST
    # -----------------------------------------------------

    def test(self, config: dict) -> PushResult:
        if not rclone_mod.is_available():
            return PushResult.failure(
                "rclone не найден. Укажите путь к бинарнику "
                "в /storage-settings."
            )

        remote_cfg = self._build_remote_config(config)
        if remote_cfg is None:
            return PushResult.failure(
                "Заполнены не все обязательные поля "
                "(URL, пользователь) или не удалось "
                "зашифровать пароль."
            )

        env = None
        cfg_path = None
        try:
            env, cfg_path = rclone_mod.build_temp_config(
                _REMOTE_NAME, remote_cfg
            )

            try:
                # lsd на корне remote — тест связи/авторизации.
                result = rclone_mod.run(
                    ["lsd", f"{_REMOTE_NAME}:"],
                    env=env,
                    timeout=20,
                )
            except Exception as exc:
                return PushResult.failure(
                    f"Ошибка запуска rclone: {exc}"
                )

            if result.returncode != 0:
                err = (
                    result.stderr or result.stdout or ""
                ).strip()
                return PushResult.failure(
                    f"Ошибка подключения: {err or 'неизвестная'}"
                )

            return PushResult.success(
                f"Подключение установлено "
                f"({remote_cfg['vendor']})"
            )
        finally:
            if cfg_path is not None:
                try:
                    os.unlink(cfg_path)
                except OSError:
                    pass

    # -----------------------------------------------------
    # PUSH
    # -----------------------------------------------------

    def push(self, config: dict, ctx: PushContext) -> PushResult:
        if self._should_skip_for_changes(config, ctx):
            return PushResult.skipped(
                "Изменений нет — выгрузка пропущена"
            )

        if not rclone_mod.is_available():
            return PushResult.failure(
                "rclone не найден. Укажите путь к бинарнику "
                "в /storage-settings."
            )

        remote_cfg = self._build_remote_config(config)
        if remote_cfg is None:
            return PushResult.failure(
                "Заполнены не все обязательные поля "
                "(URL, пользователь) или не удалось "
                "зашифровать пароль."
            )

        remote_dir = self._remote_dir(config)

        env = None
        cfg_path = None
        try:
            env, cfg_path = rclone_mod.build_temp_config(
                _REMOTE_NAME, remote_cfg
            )

            uploaded = 0
            errors: list[str] = []

            for name, local_path in ctx.files.items():
                if not local_path.is_file():
                    continue

                target = (
                    f"{_REMOTE_NAME}:{remote_dir}/{name}"
                    if remote_dir
                    else f"{_REMOTE_NAME}:{name}"
                )

                try:
                    result = rclone_mod.run(
                        ["copyto", str(local_path), target],
                        env=env,
                        timeout=120,
                    )
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                    continue

                if result.returncode != 0:
                    err = (
                        result.stderr or result.stdout or ""
                    ).strip()
                    errors.append(f"{name}: {err}")
                    continue

                uploaded += 1

            if uploaded == 0 and errors:
                return PushResult.failure(
                    "Ни один файл не загружен: "
                    + "; ".join(errors[:2])
                )

            if errors:
                return PushResult(
                    ok=True,
                    message=(
                        f"Загружено {uploaded} из "
                        f"{len(ctx.files)} файлов; "
                        f"ошибки: {'; '.join(errors[:2])}"
                    ),
                )

            return PushResult.success(
                f"Загружено файлов: {uploaded}"
            )

        finally:
            if cfg_path is not None:
                try:
                    os.unlink(cfg_path)
                except OSError:
                    pass