# =========================================================
# storages/git.py
# =========================================================
#
# Локальное git-версионирование.
# Authority — единственный, кто решает «изменилось или нет».
#
# Нормализация содержимого файлов для сравнения приходит
# из плагина через ctx.normalize. Сам storage про форматы
# ничего не знает: что передали — то и сравнивает.
#
# =========================================================

from core.storage.base import (
    StorageBackend,
    StorageField,
    PushContext,
    PushResult,
    identity_normalize,
)
from core.git_storage import (
    GitStorage,
    is_available as git_available,
)


# MikroTik .backup: двоичный, с меткой времени внутри.
_VOLATILE_EXTS = (".backup",)


class GitStorageBackend(StorageBackend):

    id = "git"
    name = "Локальная история"
    description = "Хранение версий конфигурации в git-репозитории рядом с приложением."
    icon = "🗄"

    fields = [
        StorageField(
            name="mode",
            label="Режим",
            type="select",
            default="changes_only",
            choices=[
                ("changes_only", "Только изменённые"),
                ("all", "Все бэкапы"),
            ],
            help="Коммит создаётся только при изменении конфига.",
        ),
    ]

    is_authority = True
    is_local = True
    always_enabled = True
    stores_history = True
    supports_download = True
    supports_delete = True
    supports_notes = True

    def test(self, config: dict) -> PushResult:
        if not git_available():
            return PushResult.failure("dulwich не установлен")
        return PushResult.success("git доступен")

    def push(self, config: dict, ctx: PushContext) -> PushResult:
        if not git_available():
            return PushResult.failure("dulwich не установлен")

        storage = GitStorage(ctx.backup_path)

        # Сравниваются все файлы, кроме двоичных, которые
        # меняются при каждой выгрузке (внутренние метки
        # времени, шифрование): по ним «изменилось» было бы
        # всегда. Такие файлы коммитятся вместе с остальными,
        # но решение не принимают. Если сравнимых нет совсем —
        # коммит создаётся всегда (см. commit_if_changed).
        text_files = [
            name for name in ctx.files
            if not name.lower().endswith(_VOLATILE_EXTS)
        ]

        from datetime import datetime as _dt
        msg = f"Backup {_dt.now().strftime('%Y-%m-%d %H:%M')}"

        # Нормализатор из плагина. Если плагин его не объявил
        # — identity (сравнение по сырым байтам).
        normalize_fn = ctx.normalize or identity_normalize

        ok, created, message = storage.commit_if_changed(
            files=ctx.files,
            text_files=text_files,
            message=msg,
            normalize=normalize_fn,
        )

        if not ok:
            # Сравнить не удалось — «неизвестно» (None), а не
            # «нет изменений»: иначе хранилища в режиме «только
            # изменения» пропустили бы выгрузку, и новая
            # конфигурация не попала бы никуда.
            return PushResult(
                ok=False,
                message=message,
                details={"changed": None},
            )

        if not created:
            return PushResult(
                ok=True,
                message="Изменений не обнаружено",
                details={"changed": False, "skipped": True},
            )

        return PushResult(
            ok=True,
            message=message,
            details={"changed": True},
        )