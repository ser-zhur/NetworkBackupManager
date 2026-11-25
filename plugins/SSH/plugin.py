# =========================================================
# plugins/SSH/plugin.py — универсальный SSH-плагин
# =========================================================
#
# Бэкап любого устройства, которое отдаёт конфигурацию
# командой по SSH или файлом (SFTP, затем SCP). Что
# выполнять и что скачивать — описывает профиль:
#
#   plugins/SSH/profiles/<id>.json   — профили из поставки
#   data/ssh_profiles/<id>.json      — свои (заменяют поставочные
#                                      с тем же id)
#
# Формат профиля — docs/PLUGINS.md. Профиль «Свои команды»
# берёт команды из настроек устройства.
#
# Профили читаются при запуске программы.
#
# =========================================================

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path, PurePosixPath

from core import context
from core import ssh
from core.plugin_api import PluginError

BUILTIN_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
CUSTOM = "custom"
MODES = ("shell", "exec")

_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
# «файл.txt: команда» в своих командах
_NAMED_CMD_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\.[A-Za-z0-9]{1,8})\s*:\s*(\S.*)$")
# «file:<путь>» в своих командах — скачать файл, а не выполнить
# команду («get …» не годится: это команда CLI FortiGate)
_FILE_CMD_RE = re.compile(r"^file:\s*(\S.*)$")
# Устройство не поняло команду
_CLI_ERROR_RE = re.compile(
    r"^\s*(% ?Invalid|% ?Unknown command|% ?Unrecognized|Unrecognized command|"
    r"Error: Unrecognized|syntax error|Invalid input|Unknown command|-bash: .*: command not found)",
    re.I,
)


# =========================================================
# Профили
# =========================================================

def _regex(value, where: str):
    if value in (None, ""):
        return None
    try:
        return re.compile(str(value))
    except re.error as exc:
        raise ValueError(f"{where}: неверное регулярное выражение {value!r}: {exc}") from None


def parse_profile(data: dict, profile_id: str) -> dict:
    """Проверить профиль; ValueError — описание ошибки."""
    if not isinstance(data, dict):
        raise ValueError("профиль — не объект JSON")
    mode = data.get("mode", "shell")
    if mode not in MODES:
        raise ValueError(f"mode: {mode!r} (допустимо: shell, exec)")
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError("нет списка commands")

    parsed, files = [], set()
    for i, c in enumerate(commands, 1):
        if not isinstance(c, dict):
            raise ValueError(f"commands[{i}]: не объект JSON")
        command = str(c.get("command") or "").strip()
        download = str(c.get("download") or "").strip()
        if bool(command) == bool(download):
            raise ValueError(f"commands[{i}]: нужно одно из command или download")
        if "\n" in download or "\r" in download:
            raise ValueError(f"commands[{i}]: download — путь в одну строку")
        if download and c.get("drop"):
            raise ValueError(
                f"commands[{i}]: drop не применяется к download "
                f"(файл сохраняется как есть) — используйте ignore"
            )
        name = str(c.get("file") or "").strip()
        if not _FILE_RE.match(name):
            raise ValueError(f"commands[{i}]: недопустимое имя файла {name!r}")
        if name in files:
            raise ValueError(f"commands[{i}]: файл {name} уже есть")
        files.add(name)
        for key in ("drop", "ignore"):
            if not isinstance(c.get(key) or [], list):
                raise ValueError(f"commands[{i}].{key} — не список")
        parsed.append({
            "command": command,
            "download": download,
            "file": name,
            "start": _regex(c.get("start"), f"commands[{i}].start"),
            "end": _regex(c.get("end"), f"commands[{i}].end"),
            "drop": [_regex(d, f"commands[{i}].drop") for d in c.get("drop") or []],
            "ignore": [_regex(d, f"commands[{i}].ignore") for d in c.get("ignore") or []],
        })

    setup = data.get("setup") or []
    if not isinstance(setup, list):
        raise ValueError("setup — не список")
    return {
        "id": profile_id,
        "name": str(data.get("name") or profile_id),
        "description": str(data.get("description") or ""),
        "mode": mode,
        "setup": [str(s) for s in setup],
        "enable": str(data.get("enable") or ""),
        "pager": data.get("pager", ssh.DEFAULT_PAGER),
        "default_username": str(data.get("default_username") or ""),
        "commands": parsed,
    }


def load_profiles(dirs=None) -> dict:
    """{id: профиль}; ошибочные профили пропускаются с записью в лог."""
    if dirs is None:
        dirs = [BUILTIN_PROFILES_DIR, context.DATA_DIR / "ssh_profiles"]
    profiles = {}
    for directory in dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            pid = path.stem.lower()
            if pid == CUSTOM:
                logging.warning("SSH-профиль %s: id «custom» зарезервирован", path)
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                profiles[pid] = parse_profile(data, pid)
            except (OSError, ValueError) as exc:
                logging.error("SSH-профиль %s пропущен: %s", path, exc)
    return profiles


PROFILES = load_profiles()


def parse_custom_commands(text: str) -> list[dict]:
    """
    Свои команды: по одной в строке, «#» — комментарий.
    «имя.txt: команда» — вывод в файл с этим именем, иначе
    имя из текста команды. «file:<путь>» (тоже можно с
    «имя:») — скачать файл с устройства.
    """
    result, used = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _NAMED_CMD_RE.match(line)
        if m:
            name, command = m.group(1), m.group(2).strip()
        else:
            name, command = "", line
        f = _FILE_CMD_RE.match(command)
        download = f.group(1).strip() if f else ""
        if download:
            command = ""
            if not name:
                base = PurePosixPath(download.replace("\\", "/")).name
                name = base if _FILE_RE.match(base) else ""
        if not name:
            slug = re.sub(r"[^A-Za-z0-9]+", "_", download or command).strip("_").lower()[:60]
            name = f"{slug or 'output'}.txt"
        base, n = name, 2
        while name in used:
            stem, dot, ext = base.rpartition(".")
            name = f"{stem}_{n}.{ext}" if dot else f"{base}_{n}"
            n += 1
        used.add(name)
        result.append({
            "command": command, "download": download, "file": name,
            "start": None, "end": None, "drop": [], "ignore": [],
        })
    if not result:
        raise PluginError("Профиль «Свои команды»: не задано ни одной команды")
    return result


# =========================================================
# Описание для ядра (контракт v1)
# =========================================================

PLUGIN = {
    "api": 1,
    "title": "SSH (универсальный)",
    "description": (
        "Любое устройство, которое показывает конфигурацию командой по SSH "
        "или отдаёт её файлом (SFTP, SCP): выберите профиль или задайте свои команды."
    ),
    "auth": ["password", "key"],
    "default_username": "admin",
    "fields": [
        {
            "name": "profile", "label": "Профиль устройства", "type": "select",
            "required": True,
            "choices": sorted(((p["id"], p["name"]) for p in PROFILES.values()), key=lambda c: c[1].lower())
            + [(CUSTOM, "Свои команды")],
            "help": "Свои профили — JSON-файлы в data/ssh_profiles/ (формат — docs/PLUGINS.md).",
        },
        {
            "name": "commands", "label": "Свои команды", "type": "textarea",
            "placeholder": "show running-config\nversion.txt: show version\nconfig.xml: file:/conf/config.xml",
            "help": "Только для «Свои команды»: по одной в строке. Вывод каждой — в свой файл; "
                    "имя можно задать так: «config.txt: show running-config». "
                    "«file:<путь>» — скачать файл с устройства (SFTP, затем SCP).",
        },
        {
            "name": "mode", "label": "Режим для своих команд", "type": "select",
            "choices": [["shell", "интерактивная сессия (коммутаторы, роутеры)"],
                        ["exec", "exec — каждая команда отдельно (Linux, OpenWrt)"]],
        },
        {
            "name": "enable_pass", "label": "Пароль enable", "type": "password",
            "help": "Нужен, если после входа приглашение заканчивается на «>» "
                    "(Cisco: пользователь без privilege 15).",
        },
        {
            "name": "read_timeout", "label": "Ожидание вывода команды, с", "type": "number",
            "placeholder": "60",
        },
    ],
}


# =========================================================
# Бэкап
# =========================================================

def extract(text: str, spec: dict) -> str:
    """Вырезать конфигурацию из вывода команды по start/end/drop."""
    command = spec["command"]
    lines = [line.rstrip() for line in text.split("\n")]

    for line in lines[:3]:
        if _CLI_ERROR_RE.search(line):
            raise PluginError(f"Устройство не приняло «{command}»: {line.strip()[:200]}")

    begin = 0
    if spec["start"] is not None:
        begin = next((i for i, l in enumerate(lines) if spec["start"].search(l)), None)
        if begin is None:
            raise PluginError(
                f"В выводе «{command}» нет начала конфигурации "
                f"(/{spec['start'].pattern}/)"
            )
    stop = len(lines)
    if spec["end"] is not None:
        end = next((i for i in range(begin, len(lines)) if spec["end"].search(lines[i])), None)
        if end is None:
            raise PluginError(
                f"Вывод «{command}» оборван: нет конца конфигурации "
                f"(/{spec['end'].pattern}/) — увеличьте ожидание вывода"
            )
        stop = end + 1

    kept = [l for l in lines[begin:stop] if not any(d.search(l) for d in spec["drop"])]
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    if not kept:
        raise PluginError(f"Пустой вывод «{command}»")
    return "\n".join(kept) + "\n"


def resolve_profile(device: dict) -> dict:
    pid = device.get("profile") or ""
    if pid == CUSTOM:
        mode = device.get("mode") or "shell"
        return {
            "id": CUSTOM, "name": "Свои команды", "mode": mode if mode in MODES else "shell",
            "setup": [], "enable": "", "pager": ssh.DEFAULT_PAGER, "default_username": "",
            "commands": parse_custom_commands(device.get("commands") or ""),
        }
    profile = PROFILES.get(pid)
    if profile is None:
        known = ", ".join(sorted(PROFILES)) or "—"
        raise PluginError(f"Нет SSH-профиля «{pid}» (есть: {known}, custom)")
    return profile


def run_backup(device: dict) -> list[str]:
    profile = resolve_profile(device)
    try:
        timeout = float(device.get("read_timeout") or ssh.COMMAND_TIMEOUT)
    except (TypeError, ValueError):
        timeout = ssh.COMMAND_TIMEOUT

    commands = [spec for spec in profile["commands"] if not spec["download"]]
    downloads = [spec for spec in profile["commands"] if spec["download"]]
    backup_dir = Path(device["backup_path"])
    if downloads:
        backup_dir.mkdir(parents=True, exist_ok=True)

    client = ssh.connect(device, default_username=profile["default_username"] or "admin")
    outputs = []
    fetched = []   # (spec, путь .part)
    try:
        if not commands:
            pass
        elif profile["mode"] == "exec":
            for spec in commands:
                outputs.append((spec, ssh.run(client, spec["command"], timeout)))
        else:
            with ssh.Shell(client, timeout=timeout, pager=profile["pager"]) as sh:
                if profile["enable"] and sh.prompt.endswith(">"):
                    if not device.get("enable_pass"):
                        raise PluginError(
                            f"Приглашение «{sh.prompt}» — нужен привилегированный режим: "
                            f"задайте пароль enable в настройках устройства"
                        )
                    sh.enable(profile["enable"], device["enable_pass"])
                for command in profile["setup"]:
                    sh.send(command)
                for spec in commands:
                    outputs.append((spec, sh.send(spec["command"])))
        for spec in downloads:
            part = backup_dir / (spec["file"] + ".part")
            fetched.append((spec, part))
            protocol = ssh.download(client, spec["download"], part, timeout)
            logging.info("SSH (%s): %s скачан по %s", profile["name"], spec["download"], protocol.upper())
            check_file(part.read_bytes(), spec)

        # Файлы пишутся только когда всё выполнено и скачано: при
        # ошибке на середине в каталоге не окажется смеси версий.
        contents = [(spec["file"], extract(text, spec)) for spec, text in outputs]
    except BaseException:
        _remove_parts(fetched)
        raise
    finally:
        client.close()

    paths = [ssh.write_text(device["backup_path"], name, body) for name, body in contents]
    for spec, part in fetched:
        final = backup_dir / spec["file"]
        os.replace(part, final)
        paths.append(str(final.resolve()))
    logging.info("✅ SSH (%s): сохранено файлов: %d", profile["name"], len(paths))
    return paths


def _remove_parts(fetched) -> None:
    for _, part in fetched:
        try:
            part.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logging.warning("SSH: не удалось удалить %s: %s", part, exc)


def check_file(data: bytes, spec: dict) -> None:
    """
    Скачанный файл: не пустой; start — первая непустая
    строка, end — последняя (проверка, файл не меняется).
    """
    remote = spec["download"]
    if not data.strip():
        raise PluginError(f"Файл «{remote}» пустой")
    if spec["start"] is None and spec["end"] is None:
        return
    lines = [l.strip() for l in data.decode("utf-8", "replace").splitlines() if l.strip()]
    if spec["start"] is not None and not spec["start"].search(lines[0]):
        raise PluginError(
            f"Файл «{remote}» не похож на конфигурацию: первая строка "
            f"{lines[0][:120]!r} не подходит под /{spec['start'].pattern}/"
        )
    if spec["end"] is not None and not spec["end"].search(lines[-1]):
        raise PluginError(
            f"Файл «{remote}» оборван: последняя строка не подходит под "
            f"/{spec['end'].pattern}/"
        )


def compare_normalizer(device: dict):
    """
    Для сравнения «изменилось или нет»: убрать строки,
    подходящие под ignore профиля устройства. Файл в
    бэкапе остаётся целым. Нет правил — None.
    """
    try:
        profile = resolve_profile(device)
    except PluginError:
        return None
    rules = {spec["file"]: spec["ignore"] for spec in profile["commands"] if spec["ignore"]}
    if not rules:
        return None

    def normalize(filename: str, data: bytes) -> bytes:
        patterns = rules.get(filename)
        if not patterns:
            return data
        kept = [
            line for line in data.split(b"\n")
            if not any(p.search(line.decode("utf-8", "replace").rstrip("\r")) for p in patterns)
        ]
        return b"\n".join(kept)

    return normalize
