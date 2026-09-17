# =========================================================
# core/storage/base.py
# =========================================================
#
# Базовая абстракция хранилища.
#
# Каждый backend — это класс-наследник StorageBackend,
# лежащий в каталоге storages/ рядом с EXE. Имя файла
# совпадает с атрибутом id класса.
#
# Backend сам описывает свои поля формы через список
# StorageField — UI рендерит форму динамически, без
# правок шаблонов.
#
# =========================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# =========================================================
# FIELD — описание поля формы
# =========================================================

@dataclass
class StorageField:
    """
    Одно поле в форме настройки хранилища.

    Атрибуты:
        name        — ключ в JSON-конфиге (без префикса)
        label       — человекочитаемая подпись
        type        — text | password | number | bool | select | path
        required    — обязательное ли поле
        default     — значение по умолчанию
        placeholder — placeholder для input
        choices     — [(value, label), ...] для select
        help        — подсказка под полем
    """

    name: str
    label: str
    type: str = "text"
    required: bool = False
    default: Any = ""
    placeholder: str = ""
    choices: list[tuple[str, str]] | None = None
    help: str = ""

    def __post_init__(self):
        # Валидация типа на этапе объявления — лучше упасть
        # при импорте backend'а, чем потом в UI.
        allowed = {
            "text", "password", "number", "bool",
            "select", "path",
        }
        if self.type not in allowed:
            raise ValueError(
                f"StorageField '{self.name}': "
                f"неизвестный type={self.type!r}, "
                f"допустимо: {sorted(allowed)}"
            )


# =========================================================
# PUSH RESULT
# =========================================================

@dataclass
class PushResult:
    """
    Результат операции push/test.

    ok      — успех
    message — короткое человекочитаемое сообщение для UI
    details — произвольные данные backend'а
              (например, {"changed": True} для git)
    """

    ok: bool
    message: str = ""
    details: dict | None = None

    @classmethod
    def success(cls, message: str = "", **details) -> "PushResult":
        return cls(True, message, details or None)

    @classmethod
    def failure(cls, message: str, **details) -> "PushResult":
        return cls(False, message, details or None)

    @classmethod
    def skipped(cls, message: str = "Пропущено") -> "PushResult":
        # ok=False, но это не «ошибка» — backend сообщает,
        # что операция намеренно не выполнена.
        return cls(True, message, {"skipped": True})


# =========================================================
# PUSH CONTEXT
# =========================================================

@dataclass
class PushContext:
    """
    Контекст одной операции резервного копирования,
    передаваемый в StorageBackend.push().

    Включает и метаданные устройства, и сами файлы,
    и вердикт git-хранилища (изменилось ли что-то).
    """

    device_id: str            # "mikrotik/0"
    device_host: str          # "192.168.88.1"
    device_type: str          # "mikrotik"
    backup_path: Path         # локальный рабочий каталог
    files: dict[str, Path]    # {"config.rsc": Path(...), ...}
    changed: bool | None      # вердикт authority (git); None если git выключен
    run_time: datetime


# =========================================================
# STORAGE BACKEND
# =========================================================

class StorageBackend:
    """
    Базовый класс. Наследник должен переопределить:

        id, name, description, icon
        fields
        test(config) -> PushResult
        push(config, ctx) -> PushResult

    Плюс — при необходимости:

        history(...), download(...), delete(...), set_note(...)

    и выставить соответствующие capabilities (флаги).
    """

    # ---- Идентификация ----
    id: str = "base"
    name: str = "Base Storage"
    description: str = ""
    icon: str = "📦"

    # ---- Описание формы ----
    fields: list[StorageField] = []

    # ---- Capabilities ----
    #
    # is_authority — единственный backend, который решает
    #                «изменилось или нет» (git). Остальные
    #                видят вердикт в ctx.changed.
    # is_local     — работает с локальной ФС, не требует
    #                сети.
    # stores_history, supports_download, supports_delete,
    # supports_notes — что умеет backend сверх push.
    #
    is_authority: bool = False
    is_local: bool = False
    stores_history: bool = False
    supports_download: bool = False
    supports_delete: bool = False
    supports_notes: bool = False

    # =====================================================
    # Обязательные методы — переопределяются наследниками
    # =====================================================

    def test(self, config: dict) -> PushResult:
        """Проверить соединение / доступность хранилища."""
        raise NotImplementedError(
            f"{self.__class__.__name__}.test() не реализован"
        )

    def push(
        self,
        config: dict,
        ctx: PushContext,
    ) -> PushResult:
        """
        Выгрузить файлы в хранилище.

        Возвращает PushResult. Если backend обнаружил,
        что загружать нечего (например, changes_only и
        не было изменений), возвращает PushResult.skipped().
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.push() не реализован"
        )

    # =====================================================
    # Опциональные методы — по умолчанию заглушки
    # =====================================================

    def history(
        self,
        config: dict,
        ctx: PushContext,
        limit: int = 50,
    ) -> list[dict]:
        """
        Список версий (свежие сверху). Только если
        stores_history == True.
        """
        return []

    def download(
        self,
        config: dict,
        ctx: PushContext,
        version_id: str,
        filename: str,
    ) -> bytes | None:
        """Байты конкретной версии файла."""
        return None

    def delete(
        self,
        config: dict,
        ctx: PushContext,
        version_id: str,
    ) -> PushResult:
        """Удалить версию."""
        return PushResult.failure("Удаление не поддерживается")

    def set_note(
        self,
        config: dict,
        ctx: PushContext,
        version_id: str,
        note: str,
    ) -> PushResult:
        """Установить пользовательский комментарий."""
        return PushResult.failure(
            "Комментарии не поддерживаются"
        )

    # =====================================================
    # Утилиты для наследников
    # =====================================================

    def _should_skip_for_changes(
        self,
        config: dict,
        ctx: PushContext,
    ) -> bool:
        """
        Общая логика для mode == "changes_only".

        Если git выключен (ctx.changed is None) — не
        пропускаем (дружелюбное поведение, п.3).

        Если git включён и изменений нет — пропускаем.
        """
        if config.get("mode") != "changes_only":
            return False
        if ctx.changed is None:
            return False
        return ctx.changed is False

    def log(self, level: str, msg: str, *args):
        """
        Удобный лог внутри backend'а.

        Формат уже учитывается глобальным logger'ом,
        device context берётся из thread-local.
        """
        import logging
        lg = logging.getLogger(f"storage.{self.id}")
        getattr(lg, level)(msg, *args)