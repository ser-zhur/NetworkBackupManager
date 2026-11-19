# =========================================================
# plugins/SSH/plugin.py — универсальный SSH-плагин
# =========================================================
#
# Бэкап любого устройства, которое отдаёт конфигурацию
# командой по SSH. Что выполнять — описывает профиль:
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
import re
from pathlib import Path

from core import context
from core import ssh
from core.plugin_api import PluginError

BUILTIN_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
CUSTOM = "custom"
MODES = ("shell", "exec")

_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
# «файл.txt: команда» в своих командах
_NAMED_CMD_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\.[A-Za-z0-9]{1,8})\s*:\s*(\S.*)$")
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
        if not isinstance(c, dict) or not str(c.get("command") or "").strip():
            raise ValueError(f"commands[{i}]: нет command")
        name = str(c.get("file") or "").strip()
        if not _FILE_RE.match(name):
            raise ValueError(f"commands[{i}]: недопустимое имя файла {name!r}")
        if name in files:
            raise ValueError(f"commands[{i}]: файл {name} уже есть")
        files.add(name)
        parsed.append({
            "command": str(c["command"]).strip(),
            "file": name,
            "start": _regex(c.get("start"), f"commands[{i}].start"),
            "end": _regex(c.get("end"), f"commands[{i}].end"),
            "drop": [_regex(d, f"commands[{i}].drop") for d in c.get("drop") or []],
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
    имя из текста команды.
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
            command = line
            slug = re.sub(r"[^A-Za-z0-9]+", "_", command).strip("_").lower()[:60]
            name = f"{slug or 'output'}.txt"
        base, n = name, 2
        while name in used:
            stem, dot, ext = base.rpartition(".")
            name = f"{stem}_{n}.{ext}" if dot else f"{base}_{n}"
            n += 1
        used.add(name)
        result.append({"command": command, "file": name, "start": None, "end": None, "drop": []})
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
        "Любое устройство, которое показывает конфигурацию командой по SSH: "
        "выберите профиль или задайте свои команды."
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
            "placeholder": "show running-config\nversion.txt: show version",
            "help": "Только для «Свои команды»: по одной в строке. Вывод каждой — в свой файл; "
                    "имя можно задать так: «config.txt: show running-config».",
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

    client = ssh.connect(device, default_username=profile["default_username"] or "admin")
    outputs = []
    try:
        if profile["mode"] == "exec":
            for spec in profile["commands"]:
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
                for spec in profile["commands"]:
                    outputs.append((spec, sh.send(spec["command"])))
    finally:
        client.close()

    # Файлы пишутся только когда все команды выполнены: при
    # ошибке на середине в каталоге не окажется смеси версий.
    contents = [(spec["file"], extract(text, spec)) for spec, text in outputs]
    paths = [ssh.write_text(device["backup_path"], name, body) for name, body in contents]
    logging.info("✅ SSH (%s): сохранено файлов: %d", profile["name"], len(paths))
    return paths
