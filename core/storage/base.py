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

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


# =========================================================
# NORMALIZE — типовая функция сравнения
# =========================================================
#
# Плагин может объявить функцию normalize_for_compare,
# которая получает (имя_файла, содержимое) и возвращает
# содержимое, приведённое к «сравнимому» виду.
#
# Применяется ТОЛЬКО для решения «изменилось или нет».
# В git (или другое хранилище) попадает исходное
# содержимое без изменений.
#
# Пример: MikroTik .rsc начинается со строки
# "# <дата> <время> by RouterOS", которая меняется
# при каждом экспорте. Плагин удаляет эту строку —
# и два последовательных бэкапа считаются идентичными,
# если конфиг не менялся.
#
# =========================================================

NormalizeFn = Callable[[str, bytes], bytes]


def identity_normalize(filename: str, data: bytes) -> bytes:
    """Нормализатор по умолчанию — возвращает данные как есть."""
    return data


# =========================================================
# REMOTE DIR — шаблоны {host}, {name}, {type}
# =========================================================
#
# Каталог на внешнем сервере может содержать подстановки,
# чтобы у каждого устройства была своя папка:
#
#     /network_backups/{host}  →  /network_backups/192.168.88.1
#
# Значения очищаются от символов, недопустимых в имени
# папки (в т.ч. '/'), — подстановка не может «выйти»
# в соседний каталог.
#
# =========================================================

_UNSAFE_SEGMENT_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_segment(value) -> str:
    text = _UNSAFE_SEGMENT_CHARS.sub("_", str(value or ""))
    return text.strip().strip(".")


class _KeepUnknown(dict):
    """Неизвестные {placeholder} остаются как есть."""

    def __missing__(self, key):
        return "{" + key + "}"


def expand_remote_dir(
    raw,
    *,
    host: str = "",
    name: str = "",
    device_type: str = "",
) -> str:
    """
    Подставляет {host}, {name}, {type} в путь на сервере.

    {name} без названия устройства = {host}. Строка с
    некорректными фигурными скобками возвращается как есть.
    """
    raw = str(raw or "")
    if "{" not in raw:
        return raw

    safe_host = _safe_segment(host) or "device"
    values = _KeepUnknown(
        host=safe_host,
        name=_safe_segment(name) or safe_host,
        type=_safe_segment(device_type),
    )
    try:
        return raw.format_map(values)
    except Exception:
        return raw


def normalize_remote_dir(path: str) -> str:
    """'/a//b/' и 'a\\b' → '/a/b' — для сравнения каталогов."""
    parts = [p for p in str(path or "").replace("\\", "/").split("/") if p]
    return "/" + "/".join(parts)


def device_remote_dir(
    base,
    *,
    folder: str,
    host: str = "",
    name: str = "",
    device_type: str = "",
) -> str:
    """
    Папка устройства на внешнем хранилище:

        <базовая папка>/<имя локальной папки бэкапов>

    Имя совпадает с локальной папкой устройства
    (backup_path) — правило, а не настройка, чтобы локальное
    и удалённое хранилища не расходились. В базовой папке
    по-прежнему работают подстановки {host}/{name}/{type}.

    Результат — '/a/b/<folder>' (ведущий '/', без хвостового).
    """
    expanded = expand_remote_dir(
        base, host=host, name=name, device_type=device_type,
    )
    parts = [p for p in str(expanded or "").replace("\\", "/").split("/") if p]
    folder = str(folder or "").strip().strip("/\\")
    if folder:
        parts.append(folder)
    return "/" + "/".join(parts)


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
        # ok=True, но это не «успешная выгрузка» —
        # операция намеренно не выполнена.
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
    и вердикт git-хранилища (изменилось ли что-то),
    и нормализатор от плагина.
    """

    device_id: str            # "mikrotik/0"
    device_host: str          # "192.168.88.1"
    device_type: str          # "mikrotik"
    backup_path: Path         # локальный рабочий каталог
    files: dict[str, Path]    # {"config.rsc": Path(...), ...}
    changed: bool | None      # вердикт authority (git); None если git выключен
    run_time: datetime

    # Нормализатор, объявленный плагином. Используется
    # для сравнения «изменилось или нет». В git попадают
    # исходные данные без изменений.
    normalize: NormalizeFn = identity_normalize

    # Название устройства из routers.json (для {name}
    # в remote_dir). Пусто — {name} = {host}.
    device_name: str = ""

    def remote_dir(self, base) -> str:
        """
        Папка устройства на хранилище: базовая папка +
        имя локальной папки бэкапов (см. device_remote_dir).
        """
        return device_remote_dir(
            base,
            folder=Path(self.backup_path).name,
            host=self.device_host,
            name=self.device_name,
            device_type=self.device_type,
        )


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
    #                «изменилось или нет» (git).
    # is_local     — работает с локальной ФС, не требует сети.
    # always_enabled — backend нельзя отключить (git).
    # stores_history / supports_download / supports_delete /
    # supports_notes — что умеет backend сверх push.
    #
    is_authority: bool = False
    is_local: bool = False
    always_enabled: bool = False
    stores_history: bool = False
    supports_download: bool = False
    supports_delete: bool = False
    supports_notes: bool = False

    # =====================================================
    # Обязательные методы
    # =====================================================

    def test(self, config: dict) -> PushResult:
        raise NotImplementedError(
            f"{self.__class__.__name__}.test() не реализован"
        )

    def push(self, config: dict, ctx: PushContext) -> PushResult:
        raise NotImplementedError(
            f"{self.__class__.__name__}.push() не реализован"
        )

    # =====================================================
    # Опциональные методы — по умолчанию заглушки
    # =====================================================

    def history(self, config: dict, ctx: PushContext, limit: int = 50) -> list[dict]:
        return []

    def download(self, config: dict, ctx: PushContext, version_id: str, filename: str) -> bytes | None:
        return None

    def delete(self, config: dict, ctx: PushContext, version_id: str) -> PushResult:
        return PushResult.failure("Удаление не поддерживается")

    def set_note(self, config: dict, ctx: PushContext, version_id: str, note: str) -> PushResult:
        return PushResult.failure("Комментарии не поддерживаются")

    # =====================================================
    # Каталог на сервере: защита от пересечений
    # =====================================================
    #
    # remote_dir_field — имя поля с каталогом на сервере.
    # server_fields    — поля, определяющие «тот же сервер».
    # Если remote_dir_field пуст — проверка не делается.

    remote_dir_field: str | None = None
    server_fields: tuple[str, ...] = ()

    def target_key(
        self,
        config: dict,
        *,
        host: str,
        name: str = "",
        device_type: str = "",
        folder: str = "",
    ) -> tuple | None:
        """
        Куда фактически пишет устройство: (сервер, каталог).
        Каталог = базовая папка + folder (имя локальной папки
        бэкапов устройства).

        Два устройства с одинаковым ключом перезаписывали бы
        файлы друг друга (имена файлов фиксированные:
        config.rsc, config.backup, ...). None — backend
        не участвует в проверке.

        Сравнение без учёта регистра: на SMB/Windows это
        один и тот же каталог, а ложное срабатывание на
        «Dir» и «dir» безопаснее пропущенного.
        """
        if not self.remote_dir_field:
            return None

        server = tuple(
            str(config.get(f) or "").strip().rstrip("/").lower()
            for f in self.server_fields
        )
        directory = device_remote_dir(
            config.get(self.remote_dir_field) or "",
            folder=folder,
            host=host,
            name=name,
            device_type=device_type,
        ).lower()
        return (self.id, server, directory)

    # =====================================================
    # Утилиты
    # =====================================================

    def _should_skip_for_changes(self, config: dict, ctx: PushContext) -> bool:
        """
        Общая логика для mode == "changes_only".

        Если git выключен (ctx.changed is None) — не
        пропускаем. Если git включён и изменений нет —
        пропускаем.
        """
        if config.get("mode") != "changes_only":
            return False
        if ctx.changed is None:
            return False
        return ctx.changed is False

    def log(self, level: str, msg: str, *args):
        import logging
        lg = logging.getLogger(f"storage.{self.id}")
        getattr(lg, level)(msg, *args)