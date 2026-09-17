# =========================================================
# storages/git.py
# =========================================================
#
# Локальное git-версионирование.
# Authority — единственный, кто решает «изменилось или нет».
#
# =========================================================

from core.storage.base import (
    StorageBackend,
    StorageField,
    PushContext,
    PushResult,
)
from core.git_storage import (
    GitStorage,
    is_available as git_available,
)


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
    stores_history = True
    supports_download = True
    supports_delete = True
    supports_notes = True

    def test(self, config: dict) -> PushResult:
        if not git_available():
            return PushResult.failure(
                "dulwich не установлен"
            )
        return PushResult.success("git доступен")

    def push(
        self,
        config: dict,
        ctx: PushContext,
    ) -> PushResult:
        if not git_available():
            return PushResult.failure("dulwich не установлен")

        storage = GitStorage(ctx.backup_path)

        # Файлы для сравнения — текстовые
        text_files = [
            name for name in ctx.files
            if name.endswith((".rsc", ".xml"))
        ]

        from datetime import datetime as _dt
        msg = f"Backup {_dt.now().strftime('%Y-%m-%d %H:%M')}"

        ok, created, message = storage.commit_if_changed(
            files=ctx.files,
            text_files=text_files,
            message=msg,
        )

        # Реальная ошибка git (не «нет изменений»)
        if not ok:
            return PushResult(
                ok=False,
                message=message,
                details={"changed": False},
            )

        # Изменений нет — нормальный пропуск
        if not created:
            return PushResult(
                ok=True,
                message="Изменений не обнаружено",
                details={"changed": False, "skipped": True},
            )

        # Коммит создан
        return PushResult(
            ok=True,
            message=message,
            details={"changed": True},
        )