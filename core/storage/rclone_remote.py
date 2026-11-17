# =========================================================
# core/storage/rclone_remote.py
# =========================================================
#
# Внешнее хранилище через rclone — один экземпляр на одно
# настроенное хранилище (их может быть несколько одного типа).
#
# Описание в config.json → "remotes":
#
#     {
#         "id": "webdav",                # ключ в routers.json
#         "name": "Nextcloud офис",
#         "type": "webdav",              # тип rclone
#         "options": {"url": ..., "user": ..., "pass": <enc>},
#         "base_dir": "/",
#         "mode": "changes_only"
#     }
#
# Параметры (options) — ровно те, что описывает
# `rclone config providers` для типа. Пароли (IsPassword)
# хранятся зашифрованными нашим ключом (SecureStorage), при
# запуске расшифровываются и передаются rclone «obscured»
# через переменные окружения — на диск не попадают.
#
# Папка устройства = <base_dir>/<имя локальной папки>
# (core.storage.base.device_remote_dir).
#
# =========================================================

from __future__ import annotations

import logging

from core import rclone as rclone_mod
from core.storage.base import (
    PushContext,
    PushResult,
    StorageBackend,
    device_remote_dir,
)

logger = logging.getLogger(__name__)


# Типы, где ведущий '/' в пути значим (абсолютный путь на
# сервере, а не относительно домашней папки). Для остальных
# (webdav, s3, облака) rclone ждёт путь без ведущего '/'.
_ABSOLUTE_PATH_TYPES = frozenset({"ftp", "sftp", "local"})

_ICONS = {
    "webdav": "☁️", "ftp": "📡", "sftp": "🔐", "s3": "🪣",
    "smb": "🗂", "onedrive": "☁️", "drive": "☁️",
    "local": "💾", "dropbox": "☁️", "yandex": "☁️",
}

# Ограничения rclone, чтобы зависший сервер не держал бэкап
_PUSH_FLAGS = [
    "--contimeout", "15s", "--timeout", "60s",
    "--retries", "2", "--low-level-retries", "3",
]
_TEST_FLAGS = [
    "--contimeout", "10s", "--timeout", "20s",
    "--retries", "1", "--low-level-retries", "1",
]


def _secure_storage():
    from core.context import BASE_DIR
    from core.secure_storage import SecureStorage
    return SecureStorage(BASE_DIR)


def password_option_names(type_name: str) -> set[str]:
    """Параметры типа, которые rclone считает паролями."""
    info = rclone_mod.provider(type_name) or {}
    return {
        o["Name"] for o in info.get("Options", [])
        if o.get("IsPassword")
    }


def required_option_names(type_name: str) -> set[str]:
    info = rclone_mod.provider(type_name) or {}
    return {
        o["Name"] for o in info.get("Options", [])
        if o.get("Required") and not o.get("Hide")
    }


def rclone_path(type_name: str, base_dir: str, remote_dir: str) -> str:
    """
    Путь для rclone ("NBM:<путь>"). Ведущий '/' сохраняется
    только там, где он значим и пользователь его указал.
    """
    if type_name in _ABSOLUTE_PATH_TYPES and str(base_dir or "").startswith("/"):
        return remote_dir
    return remote_dir.lstrip("/")


class RcloneRemote(StorageBackend):
    """Одно настроенное хранилище rclone."""

    is_authority = False
    is_local = False
    always_enabled = False
    stores_history = False

    # Для проверки пересечений папок и формы устройства
    remote_dir_field = "base_dir"

    def __init__(self, remote: dict):
        self.remote = dict(remote)
        self.id = str(remote["id"])
        self.type = str(remote.get("type") or "")
        self.name = str(remote.get("name") or self.type or self.id)
        self.icon = _ICONS.get(self.type, "🗄")
        info = rclone_mod.provider(self.type) or {}
        self.description = (
            f"rclone · {self.type}"
            + (f" — {info.get('Description')}" if info.get("Description") else "")
        )
        # Поля формы у rclone-хранилища строятся из описания
        # rclone (см. web), а не из StorageField.
        self.fields = []

    # -----------------------------------------------------
    # Настроено ли
    # -----------------------------------------------------

    def is_configured_cfg(self, cfg: dict | None) -> bool:
        cfg = cfg or {}
        if not cfg.get("type"):
            return False
        options = cfg.get("options") or {}
        for name in required_option_names(cfg["type"]):
            if str(options.get(name, "")).strip() == "":
                return False
        return True

    # -----------------------------------------------------
    # Окружение rclone
    # -----------------------------------------------------

    def _env(self, cfg: dict) -> dict:
        type_name = cfg.get("type") or self.type
        passwords = password_option_names(type_name)
        storage = _secure_storage()
        options = {}
        for name, value in (cfg.get("options") or {}).items():
            if name in passwords and value:
                value = storage.decrypt(value)
                if not value:
                    raise RuntimeError(
                        f"не удалось расшифровать «{name}» "
                        f"(ключ secret.key сменился?)"
                    )
            options[name] = value
        return rclone_mod.remote_env(type_name, options, passwords)

    # -----------------------------------------------------
    # Пересечения папок
    # -----------------------------------------------------

    def target_key(
        self,
        config: dict,
        *,
        host: str,
        name: str = "",
        device_type: str = "",
        folder: str = "",
    ) -> tuple | None:
        directory = device_remote_dir(
            config.get("base_dir") or "",
            folder=folder, host=host, name=name, device_type=device_type,
        ).lower()
        return (self.id, (), directory)

    # -----------------------------------------------------
    # TEST
    # -----------------------------------------------------

    def test(self, config: dict) -> PushResult:
        if not rclone_mod.is_available():
            return PushResult.failure(
                "rclone не найден (Настройки → Хранилища → путь к rclone)"
            )
        try:
            env = self._env(config)
        except Exception as exc:
            return PushResult.failure(str(exc))

        type_name = config.get("type") or self.type
        base_dir = config.get("base_dir") or ""
        base = rclone_path(type_name, base_dir, device_remote_dir(base_dir, folder=""))
        target = f"{rclone_mod.REMOTE_NAME}:{'' if base in ('/', '') else base}"

        def lsf(path):
            return rclone_mod.run(
                ["lsf", "--max-depth", "1", *_TEST_FLAGS, path],
                env=env, timeout=40,
            )

        try:
            r = lsf(target)
            if r.returncode == 0:
                return PushResult.success(
                    f"Подключение установлено, базовая папка доступна ({type_name})"
                )
            err = (r.stderr or r.stdout or "").strip()

            # Базовой папки ещё нет — проверим сам сервер
            if base not in ("/", "") and "not found" in err.lower():
                root = lsf(f"{rclone_mod.REMOTE_NAME}:")
                if root.returncode == 0:
                    return PushResult.success(
                        "Подключение установлено; базовой папки пока "
                        "нет — она будет создана при первой выгрузке"
                    )
            return PushResult.failure(
                "Ошибка подключения: " + (_last_lines(err) or "неизвестная")
            )
        except Exception as exc:
            return PushResult.failure(f"Ошибка запуска rclone: {exc}")

    # -----------------------------------------------------
    # PUSH
    # -----------------------------------------------------

    def push(self, config: dict, ctx: PushContext) -> PushResult:
        if self._should_skip_for_changes(config, ctx):
            return PushResult.skipped("Изменений нет — выгрузка пропущена")

        if not rclone_mod.is_available():
            return PushResult.failure(
                "rclone не найден (Настройки → Хранилища → путь к rclone)"
            )
        try:
            env = self._env(config)
        except Exception as exc:
            return PushResult.failure(str(exc))

        type_name = config.get("type") or self.type
        base_dir = config.get("base_dir") or ""
        remote_dir = rclone_path(type_name, base_dir, ctx.remote_dir(base_dir))

        uploaded = 0
        errors: list[str] = []
        for name, local_path in ctx.files.items():
            if not local_path.is_file():
                continue
            target = f"{rclone_mod.REMOTE_NAME}:{remote_dir}/{name}" if remote_dir \
                else f"{rclone_mod.REMOTE_NAME}:{name}"
            try:
                r = rclone_mod.run(
                    ["copyto", *_PUSH_FLAGS, str(local_path), target],
                    env=env, timeout=300,
                )
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
            if r.returncode != 0:
                errors.append(f"{name}: {_last_lines(r.stderr or r.stdout)}")
                continue
            uploaded += 1

        if uploaded == 0 and errors:
            return PushResult.failure("Ни один файл не загружен: " + "; ".join(errors[:2]))
        if uploaded == 0:
            return PushResult.failure("Нечего загружать")
        if errors:
            return PushResult(
                ok=True,
                message=(
                    f"Загружено {uploaded} из {len(ctx.files)} файлов; "
                    f"ошибки: {'; '.join(errors[:2])}"
                ),
            )
        return PushResult.success(f"Загружено файлов: {uploaded}")


def _last_lines(text: str | None, n: int = 2) -> str:
    """Последние строки вывода rclone — там суть ошибки."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return " | ".join(lines[-n:])
