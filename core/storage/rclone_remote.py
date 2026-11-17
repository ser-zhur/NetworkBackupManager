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

import contextlib
import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path

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

# Облака с входом (Google Drive, OneDrive, ...) отвечают
# «слишком много запросов» (403 rateLimitExceeded / 429), особенно
# через общий ключ rclone. Это не отказ: rclone ждёт и повторяет
# (low-level retries) — даём ему на это попытки и время.
_CLOUD_LOW_LEVEL_RETRIES = "10"
_CLOUD_TEST_TIMEOUT = 150


def _flags(base: list[str], type_name: str) -> list[str]:
    if not rclone_mod.is_oauth_type(type_name):
        return list(base)
    flags = list(base)
    flags[flags.index("--low-level-retries") + 1] = _CLOUD_LOW_LEVEL_RETRIES
    return flags


def _secure_storage():
    from core.context import BASE_DIR
    from core.secure_storage import SecureStorage
    return SecureStorage(BASE_DIR)


def password_option_names(type_name: str) -> set[str]:
    """Параметры типа, которые rclone считает паролями (obscure)."""
    info = rclone_mod.provider(type_name) or {}
    return {
        o["Name"] for o in info.get("Options", [])
        if o.get("IsPassword")
    }


# Секреты, которые rclone паролями не считает (передаются как
# есть), но храним их всё равно зашифрованными.
_EXTRA_SECRETS = frozenset({"token", "client_secret"})


def secret_option_names(type_name: str) -> set[str]:
    """Что хранится зашифрованным: пароли rclone + токены OAuth."""
    info = rclone_mod.provider(type_name) or {}
    names = {o["Name"] for o in info.get("Options", [])}
    return password_option_names(type_name) | (names & _EXTRA_SECRETS)


_token_lock = threading.Lock()


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
        # OAuth: без входа (токена) хранилище не работает
        if rclone_mod.is_oauth_type(cfg["type"]) and not options.get("token"):
            return False
        return True

    # -----------------------------------------------------
    # Окружение rclone
    # -----------------------------------------------------

    def _plain_options(self, cfg: dict) -> dict:
        """Параметры с расшифрованными секретами."""
        type_name = cfg.get("type") or self.type
        secrets_ = secret_option_names(type_name)
        storage = _secure_storage()
        options = {}
        for name, value in (cfg.get("options") or {}).items():
            if name in secrets_ and value:
                value = storage.decrypt(value)
                if not value:
                    raise RuntimeError(
                        f"не удалось расшифровать «{name}» "
                        f"(ключ secret.key сменился?)"
                    )
            options[name] = value
        return options

    def _env(self, cfg: dict) -> dict:
        """Окружение для хранилищ без OAuth (всё — переменными)."""
        type_name = cfg.get("type") or self.type
        return rclone_mod.remote_env(
            type_name, self._plain_options(cfg), password_option_names(type_name),
        )

    @contextlib.contextmanager
    def _rclone_env(self, cfg: dict):
        """
        Окружение на время работы rclone.

        OAuth-хранилище (есть token) — через временный файл
        конфига: rclone обновляет в нём токен, после работы
        новый токен сохраняется (зашифрованным) в настройки.
        Файл удаляется сразу. Остальные — переменными
        окружения, без файла.
        """
        type_name = cfg.get("type") or self.type
        options = self._plain_options(cfg)

        if not options.get("token"):
            yield rclone_mod.remote_env(
                type_name, options, password_option_names(type_name),
            )
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="nbm_rclone_"))
        conf = tmpdir / "rclone.conf"
        try:
            rclone_mod.write_remote_config(
                conf, type_name, options, password_option_names(type_name),
            )
            yield rclone_mod.config_env(conf)

            new_token = rclone_mod.read_remote_section(conf).get("token")
            if new_token and new_token != options.get("token"):
                self._persist_token(new_token)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _persist_token(self, token: str) -> None:
        """Сохранить обновлённый rclone токен в config.json."""
        from core.storage import global_config

        with _token_lock:
            remotes = global_config.load_remotes()
            for r in remotes:
                if r.get("id") == self.id:
                    r.setdefault("options", {})["token"] = _secure_storage().encrypt(token)
                    global_config.save_remotes(remotes)
                    self.remote = r
                    logger.info("storage[%s]: токен OAuth обновлён", self.id)
                    return
        # Хранилище ещё не сохранено (проверка из формы) — некуда

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
            with self._rclone_env(config) as env:
                return self._test_with_env(config, env)
        except Exception as exc:
            return PushResult.failure(f"Ошибка: {exc}")

    def _test_with_env(self, config: dict, env: dict) -> PushResult:
        type_name = config.get("type") or self.type
        base_dir = config.get("base_dir") or ""
        base = rclone_path(type_name, base_dir, device_remote_dir(base_dir, folder=""))
        target = f"{rclone_mod.REMOTE_NAME}:{'' if base in ('/', '') else base}"

        cloud = rclone_mod.is_oauth_type(type_name)

        def lsf(path):
            return rclone_mod.run(
                ["lsf", "--max-depth", "1", *_flags(_TEST_FLAGS, type_name), path],
                env=env, timeout=_CLOUD_TEST_TIMEOUT if cloud else 40,
            )

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
            + _hint(err, type_name, config.get("options") or {})
        )

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
            with self._rclone_env(config) as env:
                return self._push_with_env(config, ctx, env)
        except Exception as exc:
            return PushResult.failure(f"Ошибка: {exc}")

    def _push_with_env(self, config: dict, ctx: PushContext, env: dict) -> PushResult:
        type_name = config.get("type") or self.type
        base_dir = config.get("base_dir") or ""
        remote_dir = rclone_path(type_name, base_dir, ctx.remote_dir(base_dir))

        uploaded = 0
        errors: list[str] = []
        for name, local_path in ctx.files.items():
            if not local_path.is_file():
                continue
            target = f"{rclone_mod.REMOTE_NAME}:{remote_dir}/{name}" if remote_dir                 else f"{rclone_mod.REMOTE_NAME}:{name}"
            try:
                r = rclone_mod.run(
                    ["copyto", *_flags(_PUSH_FLAGS, type_name), str(local_path), target],
                    env=env, timeout=300,
                )
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
            if r.returncode != 0:
                err = r.stderr or r.stdout or ""
                errors.append(f"{name}: {_last_lines(err)}"
                              + _hint(err, type_name, config.get("options") or {}))
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


_LOG_PREFIX_RE = re.compile(
    r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+(?:[A-Z]+\s*:\s*)?"
)
_MAX_ERROR_LEN = 300


def _last_lines(text: str | None, n: int = 2) -> str:
    """
    Суть ошибки из вывода rclone: последние строки журнала без
    даты и уровня. Многострочные подробности (JSON от Google и
    т.п.) пропускаем; «Failed to ...: last error was: X» → X.
    """
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or raw[:1].isspace() or line[0] in '[]{}",' \
                or line.startswith("Details:"):
            continue
        lines.append(_LOG_PREFIX_RE.sub("", line))
    if not lines:
        return ""

    last = lines[-1]
    if "last error was:" in last:
        picked = [last.split("last error was:", 1)[1].strip()]
    else:
        picked = lines[-n:]
    result = " | ".join(picked)
    if len(result) > _MAX_ERROR_LEN:
        result = result[:_MAX_ERROR_LEN - 1] + "…"
    return result


def _hint(err: str, type_name: str, options: dict) -> str:
    """Пояснение к известным ошибкам облаков."""
    low = (err or "").lower()
    if not any(s in low for s in ("ratelimitexceeded", "rate_limit_exceeded",
                                  "userratelimitexceeded", "quota exceeded",
                                  "too many requests")):
        return ""
    if type_name == "drive" and not options.get("client_id"):
        return (
            ". Google ограничил число запросов через общий ключ rclone "
            "(им пользуются тысячи людей). Повторите позже или создайте "
            "свой client_id (https://rclone.org/drive/#making-your-own-client-id), "
            "укажите client_id и client_secret в настройках хранилища "
            "и войдите заново"
        )
    return ". Сервис временно ограничил число запросов — повторите позже"
