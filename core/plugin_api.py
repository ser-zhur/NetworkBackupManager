# =========================================================
# core/plugin_api.py — контракт плагинов устройств, версия 1
# =========================================================
#
# Плагин — папка plugins/<Имя>/ с файлом plugin.py. Тип
# устройства = имя папки в нижнем регистре. Подробно —
# docs/PLUGINS.md.
#
# plugin.py объявляет:
#
#   PLUGIN = {
#       "api": 1,                       # версия контракта
#       "title": "Cisco IOS",           # имя в интерфейсе
#       "description": "…",
#       "auth": ["password", "key"],    # чем входит на устройство
#       "default_username": "admin",
#       "default_port": 22,
#       "fields": [                     # свои поля устройства
#           {"name": "profile", "label": "Профиль",
#            "type": "select", "choices": [["ios", "Cisco IOS"]]},
#       ],
#   }
#
#   def run_backup(device: dict) -> list[str]:
#       ...
#
#   def normalize_for_compare(filename, data: bytes) -> bytes  # необязательно
#
# run_backup получает копию устройства: пароль (ssh_pass) и
# поля type="password" уже расшифрованы, ssh_key — абсолютный
# путь в data/keys/<тип>/ (файла может не быть), backup_path —
# абсолютный путь к рабочему каталогу.
#
#   • вернуть список путей к файлам конфигурации в backup_path;
#   • [] — успех, выгружать нечего;
#   • ошибка — исключение. PluginError — с понятным
#     пользователю текстом (показывается как есть).
#
# =========================================================

from __future__ import annotations

import re
from dataclasses import dataclass

API_VERSION = 1

FIELD_TYPES = ("text", "password", "number", "select", "textarea", "checkbox")
AUTH_METHODS = ("password", "key")

# Поля устройства, которыми управляет ядро: плагин не может
# объявить свои с такими именами.
RESERVED_FIELDS = frozenset({
    "id", "type", "name", "host", "ip", "port", "username", "ssh_pass",
    "ssh_key", "private_key", "backup_path", "max_backups", "storage",
    "tags", "disabled", "debug_connection",
    # колонки импорта из CSV (core/csv_import.py)
    "password", "key",
})

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class PluginError(Exception):
    """Ошибка бэкапа с понятным пользователю текстом."""


class PluginContractError(Exception):
    """plugin.py не соответствует контракту — плагин не загружается."""


@dataclass(frozen=True)
class DeviceField:
    name: str
    label: str
    type: str = "text"
    required: bool = False
    default: object = ""
    choices: tuple = ()          # ((значение, подпись), ...)
    help: str = ""
    placeholder: str = ""

    @property
    def secret(self) -> bool:
        return self.type == "password"


@dataclass(frozen=True)
class PluginInfo:
    type: str                    # имя папки в нижнем регистре
    title: str
    description: str = ""
    auth: tuple = AUTH_METHODS
    default_username: str = "admin"
    default_port: int = 22
    fields: tuple = ()

    def get_field(self, name: str) -> DeviceField | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def uses_key(self) -> bool:
        return "key" in self.auth

    @property
    def uses_password(self) -> bool:
        return "password" in self.auth


def _field(raw, plugin: str) -> DeviceField:
    if not isinstance(raw, dict):
        raise PluginContractError(f"{plugin}: описание поля — не словарь: {raw!r}")
    name = str(raw.get("name") or "")
    if not _NAME_RE.match(name):
        raise PluginContractError(
            f"{plugin}: недопустимое имя поля {name!r} "
            f"(латиница в нижнем регистре, цифры, _)"
        )
    if name in RESERVED_FIELDS:
        raise PluginContractError(f"{plugin}: поле {name!r} занято ядром")
    ftype = raw.get("type", "text")
    if ftype not in FIELD_TYPES:
        raise PluginContractError(
            f"{plugin}: поле {name}: неизвестный тип {ftype!r} "
            f"(допустимо: {', '.join(FIELD_TYPES)})"
        )
    choices = tuple(
        (str(c[0]), str(c[1])) if isinstance(c, (list, tuple)) else (str(c), str(c))
        for c in raw.get("choices") or ()
    )
    if ftype == "select" and not choices:
        raise PluginContractError(f"{plugin}: поле {name}: у списка нет вариантов")
    default = raw.get("default", False if ftype == "checkbox" else "")
    if ftype == "select" and default == "":
        default = choices[0][0]
    return DeviceField(
        name=name,
        label=str(raw.get("label") or name),
        type=ftype,
        required=bool(raw.get("required")),
        default=default,
        choices=choices,
        help=str(raw.get("help") or ""),
        placeholder=str(raw.get("placeholder") or ""),
    )


def load_info(module, folder: str) -> PluginInfo:
    """
    Описание плагина из модуля plugin.py.

    PluginContractError — плагин не соответствует контракту
    (его нельзя загружать: ядро не знает, как с ним работать).
    """
    if not callable(getattr(module, "run_backup", None)):
        raise PluginContractError(f"{folder}: нет функции run_backup(device)")

    meta = getattr(module, "PLUGIN", None)
    if not isinstance(meta, dict):
        raise PluginContractError(
            f"{folder}: нет описания PLUGIN = {{'api': {API_VERSION}, ...}} "
            f"(см. docs/PLUGINS.md)"
        )
    api = meta.get("api")
    if api != API_VERSION:
        raise PluginContractError(
            f"{folder}: плагин написан для контракта версии {api!r}, "
            f"программа поддерживает версию {API_VERSION}"
        )

    auth = tuple(meta.get("auth") or AUTH_METHODS)
    unknown = [a for a in auth if a not in AUTH_METHODS]
    if unknown:
        raise PluginContractError(f"{folder}: неизвестный способ входа {unknown}")

    fields = tuple(_field(f, folder) for f in meta.get("fields") or ())
    names = [f.name for f in fields]
    if len(names) != len(set(names)):
        raise PluginContractError(f"{folder}: имена полей повторяются")

    try:
        port = int(meta.get("default_port", 22))
    except (TypeError, ValueError):
        raise PluginContractError(f"{folder}: default_port — не число") from None

    return PluginInfo(
        type=folder.lower(),
        title=str(meta.get("title") or folder),
        description=str(meta.get("description") or ""),
        auth=auth,
        default_username=str(meta.get("default_username") or "admin"),
        default_port=port,
        fields=fields,
    )


# =========================================================
# Значения полей из формы
# =========================================================

def parse_field_values(info: PluginInfo, form, previous: dict, encrypt) -> dict:
    """
    Поля плагина из формы (имена opt_<поле>) → значения для
    devices.json. Пароли шифруются (encrypt); пустой пароль —
    прежнее значение из previous.

    ValueError с понятным текстом — значение не подходит.
    """
    values: dict = {}
    missing = []
    for f in info.fields:
        raw = form.get(f"opt_{f.name}")
        if f.type == "checkbox":
            values[f.name] = raw in ("on", "true", "1")
            continue
        raw = (raw or "").strip() if f.type != "textarea" else (raw or "").replace("\r\n", "\n").strip()

        if f.secret:
            if raw:
                values[f.name] = encrypt(raw)
            elif previous.get(f.name):
                values[f.name] = previous[f.name]
            elif f.required:
                missing.append(f.label)
            continue

        if not raw:
            if f.required:
                missing.append(f.label)
            continue

        if f.type == "number":
            try:
                values[f.name] = int(raw)
            except ValueError:
                raise ValueError(f"{f.label}: нужно целое число") from None
        elif f.type == "select":
            if raw not in {c[0] for c in f.choices}:
                raise ValueError(f"{f.label}: недопустимое значение {raw!r}")
            values[f.name] = raw
        else:
            values[f.name] = raw

    if missing:
        raise ValueError("Не заполнено: " + ", ".join(missing))
    return values


def decrypt_fields(info: PluginInfo | None, device: dict, decrypt) -> None:
    """Расшифровать поля type="password" в рабочей копии устройства."""
    if info is None:
        return
    for f in info.fields:
        if f.secret and device.get(f.name):
            device[f.name] = decrypt(device[f.name])
