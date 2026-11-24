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
#         "id": "webdav",                # ключ в devices.json
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
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

from core import rclone as rclone_mod
from core.storage import encryption
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
    from core.secure_storage import get_secure_storage
    return get_secure_storage()


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

        if config.get("encrypt"):
            # Шифрование — через общий путь (временная папка с архивами)
            return self.push_many(config, [ctx])[0]

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
        dest = f"{rclone_mod.REMOTE_NAME}:{remote_dir}"
        options = config.get("options") or {}

        # Файлы с тем же именем, что и на хранилище, из одной
        # локальной папки — одним запуском rclone (на облаках
        # каждый запуск заново ищет путь до папки — это лишние
        # запросы и упор в лимиты). Остальные — по одному.
        groups: dict[Path, list[str]] = {}
        singles: list[tuple[str, Path]] = []
        for name, local_path in ctx.files.items():
            if not local_path.is_file():
                continue
            if name == local_path.name:
                groups.setdefault(local_path.parent, []).append(name)
            else:
                singles.append((name, local_path))

        uploaded = 0
        errors: list[str] = []
        for parent, names in groups.items():
            if len(names) == 1:
                singles.append((names[0], parent / names[0]))
                continue
            done, errs = self._copy_batch(
                parent, names, dest, type_name, options, env,
                ignore_existing=ctx.immutable,
            )
            uploaded += done
            errors += errs

        for name, local_path in singles:
            target = f"{dest}/{name}" if remote_dir else f"{dest}{name}"
            args = ["copyto", *_flags(_PUSH_FLAGS, type_name)]
            if ctx.immutable:
                args.append("--ignore-existing")
            try:
                r = rclone_mod.run(
                    [*args, str(local_path), target], env=env, timeout=300,
                )
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                continue
            if r.returncode != 0:
                err = r.stderr or r.stdout or ""
                errors.append(f"{name}: {_last_lines(err)}" + _hint(err, type_name, options))
                continue
            uploaded += 1

        return self._push_result(uploaded, errors, len(ctx.files))

    # -----------------------------------------------------
    # Один файл в заданную папку (копия настроек)
    # -----------------------------------------------------

    def put_file(self, config: dict, local_path: Path, directory: str) -> PushResult:
        """
        local_path → <directory>/<имя файла> на хранилище.
        directory — путь вида '/a/b' (от корня хранилища).
        """
        if not rclone_mod.is_available():
            return PushResult.failure(
                "rclone не найден (Настройки → Хранилища → путь к rclone)"
            )
        type_name = config.get("type") or self.type
        path = rclone_path(type_name, config.get("base_dir") or "", directory)
        target = f"{rclone_mod.REMOTE_NAME}:{path.rstrip('/')}/{local_path.name}"
        try:
            with self._rclone_env(config) as env:
                r = rclone_mod.run(
                    ["copyto", *_flags(_PUSH_FLAGS, type_name), "--", str(local_path), target],
                    env=env, timeout=300,
                )
        except Exception as exc:
            return PushResult.failure(f"Ошибка: {exc}")
        if r.returncode != 0:
            err = r.stderr or r.stdout or ""
            return PushResult.failure(
                (_last_lines(err) or "ошибка rclone")
                + _hint(err, type_name, config.get("options") or {})
            )
        return PushResult.success(f"Загружено: {directory.rstrip('/')}/{local_path.name}")

    @classmethod
    def _copy_batch(
        cls, parent: Path, names: list[str], dest: str,
        type_name: str, options: dict, env: dict,
        ignore_existing: bool = False,
    ) -> tuple[int, list[str]]:
        """Несколько файлов одной папки. → (загружено, ошибки)."""
        failed, general = cls._copy_files(
            parent, names, dest, type_name, options, env,
            ignore_existing=ignore_existing,
        )
        if general is not None:
            return 0, [f"{len(names)} файлов: {general}"]
        return len(names) - len(failed), [f"{n}: {msg}" for n, msg in failed.items()]

    @staticmethod
    def _copy_files(
        src_root: Path, rel_paths: list[str], dest: str,
        type_name: str, options: dict, env: dict,
        ignore_existing: bool = False,
    ) -> tuple[dict[str, str], str | None]:
        """
        `rclone copy --files-from-raw`: файлы (пути относительно
        src_root, через '/') за один запуск в dest с той же
        структурой. ignore_existing — уже лежащие на хранилище
        не перезаливать (неизменяемые версии).

        → (ошибки по файлам {путь: текст}, общая ошибка или None).
        Какие файлы не загрузились — по строкам «ERROR : <путь>:»
        журнала rclone; если таких нет, а rclone завершился с
        ошибкой (нет связи, вход) — общая ошибка, не загружено
        ничего.
        """
        args = ["copy", *_flags(_PUSH_FLAGS, type_name), "-v", "--stats-log-level", "DEBUG"]
        if ignore_existing:
            args.append("--ignore-existing")
        r, err = _run_with_list(args, rel_paths, [str(src_root), dest], env,
                                timeout=300 + 20 * len(rel_paths))
        if r is None:
            return {}, err
        if r.returncode == 0:
            return {}, None

        err = r.stderr or r.stdout or ""
        wanted = set(rel_paths)
        failed: dict[str, str] = {}
        for line in err.splitlines():
            m = _FILE_ERROR_RE.match(line.strip())
            if not m:
                continue
            # «b.rsc.27514f6a.partial» — временное имя при загрузке
            path = _PARTIAL_RE.sub("", m.group(1).replace("\\", "/"))
            if path in wanted and path not in failed:
                failed[path] = m.group(2).strip()
        hint = _hint(err, type_name, options)
        if not failed:
            return {}, (_last_lines(err) or "ошибка rclone") + hint
        return {p: f"{msg[:_MAX_ERROR_LEN]}{hint}" for p, msg in failed.items()}, None

    # -----------------------------------------------------
    # PUSH MANY — несколько устройств за один запуск
    # -----------------------------------------------------

    def push_many(self, config: dict, contexts: list[PushContext]) -> list[PushResult]:
        """
        Выгрузка нескольких устройств за одно подключение:
        файлы раскладываются во временное дерево
        <папка устройства>/<файл> (жёсткие ссылки, без
        копирования, где можно; при шифровании — архивы 7z) и
        уходят одним `rclone copy` в общую родительскую папку.
        Если из-за подстановок в базовой папке ({host}, {type})
        родители разные — один запуск на каждого родителя.

        → результаты в порядке contexts.
        """
        results: list[PushResult | None] = [None] * len(contexts)
        if not contexts:
            return []

        def fail_all(message: str) -> list[PushResult]:
            return [PushResult.failure(message)] * len(contexts)

        if not rclone_mod.is_available():
            return fail_all("rclone не найден (Настройки → Хранилища → путь к rclone)")

        password = ""
        if config.get("encrypt"):
            if not encryption.is_available():
                return fail_all(
                    "шифрование: не установлена библиотека py7zr "
                    "(pip install py7zr) — файлы не выгружены"
                )
            password = encryption.get_password()
            if not password:
                return fail_all(
                    "шифрование включено, но пароль не задан "
                    "(Настройки → Хранилища → Шифрование) — файлы не выгружены"
                )

        type_name = config.get("type") or self.type
        base_dir = config.get("base_dir") or ""

        # Группы по родительской папке на хранилище
        groups: list[tuple[str, dict[str, int]]] = []
        by_parent: dict[str, dict[str, int]] = {}
        no_folder: list[int] = []
        for i, ctx in enumerate(contexts):
            if self._should_skip_for_changes(config, ctx):
                results[i] = PushResult.skipped("Изменений нет — выгрузка пропущена")
                continue
            remote_dir = rclone_path(type_name, base_dir, ctx.remote_dir(base_dir))
            parent, _, folder = remote_dir.rpartition("/")
            if not folder:
                no_folder.append(i)
                continue
            if not parent and remote_dir.startswith("/"):
                parent = "/"
            group = by_parent.get(parent)
            if group is None:
                group = by_parent[parent] = {}
                groups.append((parent, group))
            if folder.lower() in {f.lower() for f in group}:
                groups.append((parent, {folder: i}))   # та же папка — отдельно
            else:
                group[folder] = i

        try:
            with self._rclone_env(config) as env:
                for parent, folders in groups:
                    self._push_group(config, contexts, parent, folders, env, results, password)
                for i in no_folder:
                    results[i] = (
                        PushResult.failure("шифрование: у устройства нет своей папки")
                        if password else self._push_with_env(config, contexts[i], env)
                    )
        except Exception as exc:
            fail = PushResult.failure(f"Ошибка: {exc}")
            results = [r if r is not None else fail for r in results]

        return [r if r is not None else PushResult.failure("Не выполнено") for r in results]

    def _push_group(
        self, config: dict, contexts: list[PushContext],
        parent: str, folders: dict[str, int], env: dict,
        results: list, password: str = "",
    ) -> None:
        type_name = config.get("type") or self.type
        options = config.get("options") or {}
        dest = f"{rclone_mod.REMOTE_NAME}:{parent}"
        immutable = any(contexts[i].immutable for i in folders.values())

        # Что уже лежит на хранилище (только синхронизация с
        # шифрованием: не шифровать заново уже выгруженное и
        # найти открытые копии).
        existing: set[str] | None = None
        if password and immutable:
            existing = self._list_files(dest, set(folders), type_name, env)

        stage = Path(tempfile.mkdtemp(prefix="nbm_stage_"))
        # rels[i] = [(путь на хранилище, имя файла устройства)]
        rels: dict[int, list[tuple[str, str]]] = {}
        present: dict[int, list[tuple[str, str]]] = {}
        stage_errors: dict[int, list[str]] = {}
        try:
            for folder, i in folders.items():
                (stage / folder).mkdir()
                rels[i], present[i], stage_errors[i] = [], [], []
                for name, local_path in contexts[i].files.items():
                    if not local_path.is_file():
                        continue
                    remote_name = encryption.archive_name(name) if password else name
                    rel = f"{folder}/{remote_name}"
                    if existing is not None and rel in existing:
                        present[i].append((rel, name))      # уже зашифровано
                        continue
                    target = stage / folder / remote_name
                    try:
                        if password:
                            encryption.make_archive(local_path, target, name, password)
                        else:
                            try:
                                os.link(local_path, target)
                            except OSError:
                                shutil.copy2(local_path, target)
                    except Exception as exc:
                        stage_errors[i].append(f"{name}: шифрование: {exc}")
                        continue
                    rels[i].append((rel, name))

            all_rels = [rel for items in rels.values() for rel, _ in items]
            if password and immutable and all_rels:
                (stage / encryption.README_NAME).write_text(
                    encryption.README_TEXT, encoding="utf-8",
                )
                all_rels.append(encryption.README_NAME)

            failed: dict[str, str] = {}
            general = None
            if all_rels:
                failed, general = self._copy_files(
                    stage, all_rels, dest, type_name, options, env,
                    ignore_existing=immutable,
                )
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        # Открытые копии — удалить, если рядом есть зашифрованная
        cleanup: dict[int, tuple[int, int]] = {}
        if password and general is None:
            cleanup = self._remove_plain_copies(
                dest, folders, rels, present, failed, existing, type_name, env,
            )

        for i in rels:
            total = len(contexts[i].files)
            errors = list(stage_errors[i])
            done = len(present[i])
            if general is not None:
                if rels[i]:
                    errors.append(f"{len(rels[i])} файлов: {general}")
            else:
                errors += [f"{name}: {failed[rel]}" for rel, name in rels[i] if rel in failed]
                done += sum(1 for rel, _ in rels[i] if rel not in failed)
            extra = {}
            if password:
                deleted, left = cleanup.get(i, (0, 0))
                extra = {"encrypted": True, "plain_deleted": deleted, "plain_left": left}
            results[i] = self._push_result(done, errors, total, extra)

    def _list_files(self, dest: str, folders: set[str], type_name: str, env: dict) -> set[str]:
        """Файлы в папках устройств: {'<папка>/<файл>'}; нет папки — пусто."""
        try:
            r = rclone_mod.run(
                ["lsf", "-R", "--files-only", "--max-depth", "2",
                 *_flags(_PUSH_FLAGS, type_name), dest],
                env=env, timeout=600,
            )
        except Exception as exc:
            logger.warning("storage[%s]: список файлов: %s", self.id, exc)
            return set()
        if r.returncode != 0:
            # Папки ещё нет — нормально; прочее — просто без пропусков
            return set()
        wanted = {f.lower() for f in folders}
        out = set()
        for line in (r.stdout or "").splitlines():
            path = line.strip().replace("\\", "/")
            folder, _, name = path.partition("/")
            if name and "/" not in name and folder.lower() in wanted:
                out.add(path)
        return out

    def _remove_plain_copies(
        self, dest: str, folders: dict[str, int],
        rels: dict, present: dict, failed: dict,
        existing: set[str] | None, type_name: str, env: dict,
    ) -> dict[int, tuple[int, int]]:
        """
        Удаляет открытые файлы, у которых на хранилище есть
        зашифрованная пара (X → X.7z). Открытые без пары
        (например, версии, удалённые из локальной истории)
        НЕ удаляются — иначе пропали бы совсем; их число
        возвращается.

        → {i: (удалено, оставлено без пары)}.
        """
        encrypted: set[str] = {
            rel for items in rels.values() for rel, _ in items if rel not in failed
        } | {rel for items in present.values() for rel, _ in items}
        if existing is not None:
            encrypted |= {p for p in existing if encryption.is_archive_name(p)}

        to_delete: dict[int, list[str]] = {}
        left: dict[int, int] = {}
        for folder, i in folders.items():
            if existing is not None:
                plain = [
                    p for p in existing
                    if p.split("/", 1)[0] == folder and not encryption.is_archive_name(p)
                ]
            else:
                # Обычный бэкап: открытые копии только что выгруженных файлов
                plain = [f"{folder}/{name}" for _, name in rels.get(i, [])]
            to_delete[i] = [p for p in plain if encryption.archive_name(p) in encrypted]
            left[i] = (
                sum(1 for p in plain if encryption.archive_name(p) not in encrypted)
                if existing is not None else 0
            )

        all_paths = [p for paths in to_delete.values() for p in paths]
        deleted_ok = True
        if all_paths:
            r, err = _run_with_list(
                ["delete", *_flags(_PUSH_FLAGS, type_name), "--max-depth", "2"],
                all_paths, [dest], env, timeout=300 + 5 * len(all_paths),
            )
            if r is None or r.returncode != 0:
                deleted_ok = False
                logger.warning(
                    "storage[%s]: не удалось удалить открытые копии: %s", self.id,
                    err if r is None else _last_lines(r.stderr or r.stdout),
                )
            elif existing is not None:
                logger.info(
                    "storage[%s]: удалено открытых копий (есть зашифрованные): %d",
                    self.id, len(all_paths),
                )

        return {
            i: (len(to_delete[i]) if deleted_ok else 0, left[i])
            for i in folders.values()
        }

    @staticmethod
    def _push_result(
        uploaded: int, errors: list[str], total: int, extra: dict | None = None,
    ) -> PushResult:
        extra = extra or {}
        if uploaded == 0 and errors:
            return PushResult.failure(
                "Ни один файл не загружен: " + "; ".join(errors[:2]), **extra,
            )
        if uploaded == 0:
            return PushResult.failure("Нечего загружать", **extra)
        what = "Зашифровано и загружено" if extra.get("encrypted") else "Загружено"
        if errors:
            return PushResult(
                ok=True,
                message=(
                    f"{what} {uploaded} из {total} файлов; "
                    f"ошибки: {'; '.join(errors[:2])}"
                ),
                details={"uploaded": uploaded, "failed": len(errors), **extra},
            )
        return PushResult.success(f"{what} файлов: {uploaded}", uploaded=uploaded, **extra)


def _run_with_list(args: list[str], paths: list[str], tail: list[str], env: dict, timeout: int):
    """
    rclone <args> --files-from-raw <список> <tail...>.
    → (CompletedProcess | None, текст исключения).
    """
    fd, list_path = tempfile.mkstemp(prefix="nbm_files_", suffix=".txt")
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(paths) + "\n")
        try:
            return rclone_mod.run(
                [*args, "--files-from-raw", list_path, *tail], env=env, timeout=timeout,
            ), ""
        except Exception as exc:
            return None, str(exc)
    finally:
        try:
            Path(list_path).unlink()
        except OSError:
            pass


_LOG_PREFIX_RE = re.compile(
    r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+(?:[A-Z]+\s*:\s*)?"
)
_MAX_ERROR_LEN = 300

# «2026/11/17 20:00:00 ERROR : file.rsc: Failed to copy: ...»
_FILE_ERROR_RE = re.compile(
    r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d\s+ERROR\s*:\s*(.+?): (.+)$"
)
_PARTIAL_RE = re.compile(r"\.[0-9a-f]{6,}\.partial$")


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
