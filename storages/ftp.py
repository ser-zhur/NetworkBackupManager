# =========================================================
# storages/ftp.py
# =========================================================
#
# Выгрузка резервных копий на внешний FTP-сервер.
# Поддерживает:
#   - plain FTP
#   - FTPS (explicit, через FTP_TLS)
#
# =========================================================

import logging
import ssl
from ftplib import FTP, FTP_TLS, error_perm

from core.storage.base import (
    StorageBackend,
    StorageField,
    PushContext,
    PushResult,
)

logger = logging.getLogger(__name__)


class FtpStorageBackend(StorageBackend):

    id = "ftp"
    name = "FTP-сервер"
    description = "Выгрузка резервных копий на внешний FTP-сервер."
    icon = "📡"

    fields = [
        StorageField(
            name="host",
            label="Хост",
            required=True,
            placeholder="192.168.88.254",
        ),
        StorageField(
            name="port",
            label="Порт",
            type="number",
            default=21,
        ),
        StorageField(
            name="remote_dir",
            label="Каталог на сервере",
            default="/network_backups",
            placeholder="/network_backups",
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
            name="use_tls",
            label="Использовать TLS (FTPS)",
            type="bool",
            default=False,
            help="Включите, если сервер требует шифрованное соединение.",
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
            help="При режиме «только изменённые» выгрузка "
                 "пропускается, если git не увидел изменений.",
        ),
    ]

    # -----------------------------------------------------
    # CONNECT
    # -----------------------------------------------------

    @staticmethod
    def _connect(config: dict):
        """
        Возвращает подключённый и залогиненный FTP-клиент.

        Если use_tls=True — использует FTP_TLS и включает
        защиту канала данных (PROT P).
        """
        host = (config.get("host") or "").strip()
        if not host:
            raise ValueError("Не указан хост")

        try:
            port = int(config.get("port") or 21)
        except (TypeError, ValueError):
            raise ValueError("Некорректный порт")

        user = config.get("user", "") or ""
        password = config.get("pass", "") or ""
        use_tls = bool(config.get("use_tls"))

        if use_tls:
            # Explicit FTPS. Проверка сертификата отключена —
            # на локальных/корпоративных серверах сертификаты
            # часто самоподписанные.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            ftp = FTP_TLS(context=ctx)
            ftp.connect(host, port, timeout=10)
            ftp.login(user=user, passwd=password)
            ftp.prot_p()
        else:
            ftp = FTP()
            ftp.connect(host, port, timeout=10)
            ftp.login(user=user, passwd=password)

        return ftp

    # -----------------------------------------------------
    # REMOTE DIR NAVIGATION
    # -----------------------------------------------------

    @staticmethod
    def _ensure_remote_dir(ftp, remote_dir: str) -> None:
        """
        Переходит в целевой каталог.

        Стратегия:
          1. cwd(remote_dir) как есть.
          2. Если не помогло — от корня сегмент за сегментом
             с mkd для отсутствующих каталогов.
        """

        remote_dir = (remote_dir or "/").strip() or "/"

        # 1) пробуем как указал пользователь
        try:
            ftp.cwd(remote_dir)
            logger.info(
                "FTP: cwd(%r) успешно, текущий каталог %s",
                remote_dir,
                FtpStorageBackend._safe_pwd(ftp),
            )
            return
        except error_perm:
            pass

        # 2) идём от корня по сегментам
        try:
            ftp.cwd("/")
        except error_perm:
            pass

        for part in remote_dir.strip("/").split("/"):
            if not part:
                continue
            try:
                ftp.cwd(part)
            except error_perm:
                try:
                    ftp.mkd(part)
                except error_perm as exc:
                    raise RuntimeError(
                        f"Каталог {part!r} недоступен и не может "
                        f"быть создан. Текущий каталог: "
                        f"{FtpStorageBackend._safe_pwd(ftp)}. "
                        f"Ответ сервера: {exc}"
                    )
                ftp.cwd(part)

        logger.info(
            "FTP: текущий каталог после навигации: %s",
            FtpStorageBackend._safe_pwd(ftp),
        )

    @staticmethod
    def _safe_pwd(ftp) -> str:
        try:
            return ftp.pwd()
        except Exception:
            return "?"

    # -----------------------------------------------------
    # TEST
    # -----------------------------------------------------

    def test(self, config: dict) -> PushResult:
        try:
            ftp = self._connect(config)
        except Exception as exc:
            return PushResult.failure(f"Ошибка подключения: {exc}")

        try:
            try:
                ftp.pwd()
            except Exception:
                pass

            mode_label = "FTPS" if config.get("use_tls") else "FTP"
            return PushResult.success(
                f"Подключение установлено ({mode_label})"
            )
        except Exception as exc:
            return PushResult.failure(f"Ошибка: {exc}")
        finally:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    # -----------------------------------------------------
    # PUSH
    # -----------------------------------------------------

    def push(self, config: dict, ctx: PushContext) -> PushResult:

        if self._should_skip_for_changes(config, ctx):
            return PushResult.skipped(
                "Изменений нет — выгрузка пропущена"
            )

        try:
            ftp = self._connect(config)
        except Exception as exc:
            return PushResult.failure(f"Ошибка подключения: {exc}")

        remote_dir = (config.get("remote_dir") or "/").strip()

        try:
            self._ensure_remote_dir(ftp, remote_dir)

            uploaded = 0
            for name, path in ctx.files.items():
                if not path.is_file():
                    continue
                with open(path, "rb") as fp:
                    ftp.storbinary(f"STOR {name}", fp)
                uploaded += 1

        except Exception as exc:
            try:
                ftp.close()
            except Exception:
                pass
            return PushResult.failure(f"Ошибка FTP: {exc}")

        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

        if uploaded == 0:
            return PushResult.failure("Нечего загружать")

        return PushResult.success(
            f"Загружено файлов: {uploaded}"
        )