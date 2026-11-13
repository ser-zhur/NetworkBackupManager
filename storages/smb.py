# =========================================================
# storages/smb.py
# =========================================================
#
# Выгрузка резервных копий на SMB (CIFS) сервер через rclone.
#
# Поддерживает:
#   • Windows Server, Samba, NAS
#   • NTLM-аутентификацию (по умолчанию)
#   • Kerberos (use_kerberos=true)
#
# Требует установленный rclone — путь настраивается
# на странице /storage-settings.
#
# Нюанс SMB по сравнению с WebDAV: первый сегмент
# remote_dir — это имя шары. То есть remote_dir
# "backup/mikrotik" превращается в путь
#     remote:backup/mikrotik/file.rsc
# где "backup" — SMB-шара. remote_dir обязателен.
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


_REMOTE_NAME = "nbm_smb"


class SmbStorageBackend(StorageBackend):

    id = "smb"
    name = "SMB (через rclone)"
    description = (
        "Выгрузка на SMB/CIFS-шару (Windows Server, Samba, NAS)."
    )
    icon = "🗂"

    fields = [
        StorageField(
            name="host",
            label="Хост",
            required=True,
            placeholder="fileserver.example.com или 192.168.1.10",
        ),
        StorageField(
            name="port",
            label="Порт",
            type="number",
            default=445,
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
            name="domain",
            label="Домен",
            placeholder="WORKGROUP",
            help=(
                "Для NTLM-аутентификации. Оставьте пустым, "
                "если вход локальный или используется Kerberos."
            ),
        ),
        StorageField(
            name="use_kerberos",
            label="Использовать Kerberos",
            type="bool",
            default=False,
            help="Требует настроенного krb5 и входа в домен.",
        ),
        StorageField(
            name="remote_dir",
            label="Каталог на сервере",
            required=True,
            default="",
            placeholder="backup/mikrotik",
            help=(
                "Первый сегмент — имя SMB-шары. Пример: "
                "backup/mikrotik. Подкаталоги создаются "
                "автоматически."
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
        Формирует секцию [nbm_smb] для rclone.conf.

        None — если не заданы обязательные поля или
        не удалось зашифровать пароль.
        """
        host = (config.get("host") or "").strip()
        user = (config.get("user") or "").strip()
        password = config.get("pass") or ""
        domain = (config.get("domain") or "").strip()
        use_kerberos = bool(config.get("use_kerberos"))

        if not host or not user:
            return None

        try:
            port = int(config.get("port") or 445)
        except (TypeError, ValueError):
            port = 445

        cfg = {
            "type": "smb",
            "host": host,
            "port": str(port),
            "user": user,
        }

        if use_kerberos:
            # При Kerberos пароль не обязателен —
            # rclone берёт тикет из системного кэша.
            cfg["use_kerberos"] = "true"
        else:
            if password:
                obscured = rclone_mod.obscure(password)
                if obscured is None:
                    return None
                cfg["pass"] = obscured

        if domain:
            cfg["domain"] = domain

        return cfg

    @staticmethod
    def _remote_dir(config: dict) -> str:
        """
        Нормализует путь на шаре.

        Первый сегмент — имя шары. Ведущие/хвостовые
        слэши убираем — rclone плохо переносит ":".
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
                "(хост, пользователь) или не удалось "
                "зашифровать пароль."
            )

        remote_dir = self._remote_dir(config)
        if not remote_dir:
            return PushResult.failure(
                "Не указан каталог на сервере. Первый "
                "сегмент должен быть именем SMB-шары "
                "(например, backup/mikrotik)."
            )

        share = remote_dir.split("/")[0]

        env = None
        cfg_path = None
        try:
            env, cfg_path = rclone_mod.build_temp_config(
                _REMOTE_NAME, remote_cfg
            )

            try:
                # lsd на шаре — проверяет авторизацию и
                # доступ к шаре. На корне (remote:) не
                # работаем: многие серверы запрещают
                # enumeration shares без отдельных прав.
                result = rclone_mod.run(
                    ["lsd", f"{_REMOTE_NAME}:{share}"],
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
                f"Подключение установлено (шара «{share}»)"
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
                "(хост, пользователь) или не удалось "
                "зашифровать пароль."
            )

        remote_dir = self._remote_dir(config)
        if not remote_dir:
            return PushResult.failure(
                "Не указан каталог на сервере "
                "(имя SMB-шары)."
            )

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

                target = f"{_REMOTE_NAME}:{remote_dir}/{name}"

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