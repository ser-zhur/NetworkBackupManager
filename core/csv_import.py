# =========================================================
# core/csv_import.py — список устройств из CSV
# =========================================================
#
# Только разбор файла: строки CSV → «формы» в том же виде,
# что присылает страница «Добавить устройство». Проверку и
# сохранение делает web/server.py — тем же кодом, что и для
# формы, поэтому правила у импорта и у ручного добавления
# одинаковые.
#
# Формат:
#   • первая строка — заголовки, порядок колонок любой;
#   • разделитель «;», «,» или табуляция (определяется сам;
#     Excel с русскими настройками сохраняет с «;»);
#   • кодировка UTF-8 (с BOM или без) или Windows-1251;
#   • строки, где первая непустая ячейка начинается с «#», и
#     пустые строки пропускаются.
#
# =========================================================

from __future__ import annotations

import csv
import io
import re

MAX_BYTES = 1024 * 1024
MAX_ROWS = 1000

# Колонки ядра: (имя, описание). Имя колонки сравнивается без
# учёта регистра; синонимы — в _ALIASES.
COLUMNS = (
    ("type", "Тип устройства: ssh, mikrotik, … или название типа, как в форме добавления. Обязательно"),
    ("ip", "IP-адрес или имя узла. Обязательно"),
    ("name", "Название в списке"),
    ("port", "Порт SSH (по умолчанию — порт типа устройства)"),
    ("username", "Логин (по умолчанию — логин типа устройства)"),
    ("password", "Пароль; пусто — вход по ключу"),
    ("key", "Файл SSH-ключа в data/keys/<тип>/ (по умолчанию id_rsa.key)"),
    ("backup_path", "Папка бэкапов (по умолчанию backups/<название или IP>)"),
    ("max_backups", "Сколько версий хранить в папке (по умолчанию 5)"),
    ("tags", "Теги через запятую или «|»"),
    ("disabled", "1, да или yes — устройство отключено"),
)

_ALIASES = {
    "device_type": "type", "тип": "type",
    "host": "ip", "address": "ip", "адрес": "ip", "ip-адрес": "ip",
    "название": "name", "имя": "name",
    "порт": "port",
    "user": "username", "login": "username", "логин": "username",
    "пользователь": "username",
    "pass": "password", "ssh_pass": "password", "пароль": "password",
    "ssh_key": "key", "private_key": "key", "ключ": "key",
    "папка": "backup_path", "path": "backup_path",
    "versions": "max_backups", "версий": "max_backups",
    "теги": "tags", "tag": "tags",
    "отключено": "disabled", "отключен": "disabled",
}

_YES = {"1", "yes", "y", "true", "да", "д", "+", "x"}


class CsvImportError(ValueError):
    """Файл целиком не читается — понятный текст для пользователя."""


def decode(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise CsvImportError(f"Файл больше {MAX_BYTES // 1024} КБ")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def _column(name: str) -> str:
    key = (name or "").strip().lower().lstrip("﻿")
    if key.startswith("opt_"):
        return key[4:]          # явно поле плагина — без синонимов
    return _ALIASES.get(key, key)


def _delimiter(header: str) -> str:
    counts = {d: header.count(d) for d in (";", ",", "\t")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ";"


def read_rows(text: str) -> list[tuple[int, dict]]:
    """
    CSV → [(номер строки в файле, {колонка: значение})].
    Имена колонок приведены к именам ядра (type, ip, …) или
    к именам полей плагина.
    """
    lines = text.splitlines()
    first = next((ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")), "")
    if not first:
        raise CsvImportError("Файл пуст")

    reader = csv.reader(io.StringIO(text), delimiter=_delimiter(first))
    header: list[str] | None = None
    rows: list[tuple[int, dict]] = []
    for cells in reader:
        line = reader.line_num
        if not any(c.strip() for c in cells):
            continue
        if next(c for c in cells if c.strip()).lstrip().startswith("#"):
            continue
        if header is None:
            header = [_column(c) for c in cells]
            if "type" not in header or "ip" not in header:
                raise CsvImportError(
                    "В первой строке нужны заголовки колонок, "
                    "минимум type и ip (скачайте образец)"
                )
            continue
        row = {}
        for name, value in zip(header, cells):
            if name and name not in row:
                row[name] = value.strip()
        rows.append((line, row))
        if len(rows) > MAX_ROWS:
            raise CsvImportError(f"Больше {MAX_ROWS} устройств в одном файле")
    return rows


def find_type(value: str, infos) -> str | None:
    """Тип устройства по имени (ssh) или названию (Cisco IOS / SSH)."""
    v = (value or "").strip().lower()
    if not v:
        return None
    for info in infos:
        if v == info.type or v == info.title.lower():
            return info.type
    return None


def folder_name(name: str, ip: str) -> str:
    """Имя папки бэкапов по умолчанию: название или IP."""
    base = re.sub(r"[^\w.-]+", "_", (name or "").strip()).strip("._")
    return base or re.sub(r"[^\w.-]+", "_", ip.strip()) or "device"


def is_yes(value) -> bool:
    return str(value or "").strip().lower() in _YES


def to_form(row: dict, info, storage_defaults: dict) -> dict:
    """
    Строка CSV → поля формы «Добавить устройство»
    (имена как в add.html).
    """
    form = {
        "device_type": info.type,
        "name": row.get("name", ""),
        "ip": row.get("ip", ""),
        "port": row.get("port") or str(info.default_port),
        "username": row.get("username") or info.default_username,
        "ssh_pass": row.get("password", ""),
        "private_key": row.get("key", ""),
        "backup_path": row.get("backup_path")
        or "backups/" + folder_name(row.get("name", ""), row.get("ip", "")),
        "max_backups": row.get("max_backups") or "5",
        "tags": row.get("tags", "").replace("|", ","),
    }
    for f in info.fields:
        # Пустая ячейка — значение по умолчанию, как в форме
        if row.get(f.name, "") != "":
            value = row[f.name]
            if f.type == "checkbox":
                value = "on" if is_yes(value) else ""
            elif f.type == "select" and value:
                # Можно писать и значение, и подпись варианта
                for key, label in f.choices:
                    if value.lower() in (key.lower(), label.lower()):
                        value = key
                        break
            form[f"opt_{f.name}"] = value
        elif f.type == "checkbox" and f.default:
            form[f"opt_{f.name}"] = "on"
        elif f.type != "checkbox" and f.default not in ("", None):
            form[f"opt_{f.name}"] = str(f.default)
    for backend_id, cfg in (storage_defaults or {}).items():
        if cfg.get("enabled"):
            form[f"storage_{backend_id}_enabled"] = "true"
    return form


def template_csv(infos) -> str:
    """Образец файла: все колонки ядра и поля плагинов."""
    extra = []
    for info in infos:
        for f in info.fields:
            if f.name not in extra:
                extra.append(f.name)
    header = [c for c, _ in COLUMNS] + extra
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(header)
    examples = {
        "ssh": {"name": "SW-Core", "ip": "192.0.2.10", "username": "admin",
                "password": "secret", "tags": "Офис|Ядро",
                "profile": "cisco_ios"},
        "mikrotik": {"name": "R-Office", "ip": "192.0.2.1", "key": "id_rsa.key",
                     "tags": "Офис"},
    }
    types = {i.type for i in infos}
    for t, values in examples.items():
        if t in types:
            w.writerow([t if c == "type" else values.get(c, "") for c in header])
    return out.getvalue()
