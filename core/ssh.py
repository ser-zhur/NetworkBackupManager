# =========================================================
# core/ssh.py — SSH для плагинов устройств
# =========================================================
#
#   client = ssh.connect(device)            # пароль и/или ключ, TOFU
#   text = ssh.run(client, "show config")   # exec: одна команда
#
#   with ssh.Shell(client) as sh:           # интерактивная сессия
#       sh.send("terminal length 0")
#       text = sh.send("show running-config")
#
#   ssh.write_text(device["backup_path"], "config.txt", text)
#
# Ошибки — PluginError с понятным текстом: «неверный логин»,
# «нет связи», «ключ хоста изменился», «нет ответа за N с».
#
# =========================================================

from __future__ import annotations

import logging
import os
import re
import socket
import time
from pathlib import Path

import paramiko

from core import host_keys
from core.plugin_api import PluginError

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15.0
COMMAND_TIMEOUT = 60.0

# Управляющие последовательности терминала: цвета, курсор,
# режимы (ESC [ ... буква, ESC ] ... BEL, одиночные ESC x).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]|\x1b[=>78]")
# Пагинатор стирает свою надпись backspace'ами и пробелами
_BACKSPACE_RE = re.compile(r" *\x08+ *")
DEFAULT_PAGER = r"--\s*More\s*--|---- More ----|<--- More --->|Press any key to continue"


def device_host(device: dict) -> str:
    host = (device.get("host") or device.get("ip") or "").strip()
    if not host:
        raise PluginError("Не задан адрес устройства")
    return host


def device_port(device: dict, default: int = 22) -> int:
    try:
        return int(device.get("port") or default)
    except (TypeError, ValueError):
        raise PluginError(f"Порт SSH — не число: {device.get('port')!r}") from None


def connect(
    device: dict,
    *,
    default_username: str = "admin",
    default_port: int = 22,
    timeout: float | None = None,
) -> paramiko.SSHClient:
    """
    Подключение по SSH с данными устройства.

    Ключ (device["ssh_key"], если файл есть) и пароль
    (device["ssh_pass"]) пробуются оба. Ключ хоста — TOFU
    (core.host_keys): изменённый ключ — отказ.
    """
    host = device_host(device)
    port = device_port(device, default_port)
    username = device.get("username") or default_username
    password = device.get("ssh_pass") or None
    key_path = device.get("ssh_key") or ""
    key_file = key_path if key_path and os.path.isfile(key_path) else None
    timeout = float(timeout or device.get("timeout") or CONNECT_TIMEOUT)
    where = f"{username}@{host}:{port}"

    if key_file is None and password is None:
        hint = f" Положите ключ в {key_path} или" if key_path else ""
        raise PluginError(f"Нет ни пароля, ни SSH-ключа для {where}.{hint} задайте пароль.")

    client = paramiko.SSHClient()
    host_keys.prepare_client(client, port)
    logger.info("📡 SSH %s (%s)", where, "ключ" if key_file else "пароль")
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            key_filename=key_file,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
    except paramiko.BadHostKeyException:
        client.close()
        raise PluginError(
            f"Ключ хоста {host}:{port} изменился — подключение отклонено. "
            f"Если устройство переустановили, удалите его строку из "
            f"{host_keys.KNOWN_HOSTS_FILE}."
        ) from None
    except paramiko.AuthenticationException:
        client.close()
        raise PluginError(f"SSH {where}: неверный логин, пароль или ключ") from None
    except (socket.timeout, TimeoutError):
        client.close()
        raise PluginError(f"SSH {where}: нет ответа за {timeout:g} с") from None
    except (paramiko.ssh_exception.NoValidConnectionsError, ConnectionError, socket.gaierror, OSError) as exc:
        client.close()
        raise PluginError(f"SSH {where}: нет связи — {exc}") from None
    except paramiko.SSHException as exc:
        client.close()
        raise PluginError(f"SSH {where}: {exc}") from None
    return client


def clean(text: str) -> str:
    """Убрать управляющие последовательности терминала и \\r."""
    text = _ANSI_RE.sub("", text)
    text = _BACKSPACE_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "")
    return text


def run(client: paramiko.SSHClient, command: str, timeout: float = COMMAND_TIMEOUT) -> str:
    """
    Одна команда через exec. Ненулевой код возврата — ошибка
    (часть устройств кода не сообщает — тогда -1, это не ошибка).
    """
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read()
        err = stderr.read()
        code = stdout.channel.recv_exit_status()
    except (socket.timeout, TimeoutError):
        raise PluginError(f"Нет ответа на «{command}» за {timeout:g} с") from None
    except paramiko.SSHException as exc:
        raise PluginError(f"«{command}»: {exc}") from None
    if code not in (0, -1):
        detail = clean(err.decode("utf-8", "replace")).strip()[:300]
        raise PluginError(f"«{command}» завершилась с кодом {code}" + (f": {detail}" if detail else ""))
    return clean(out.decode("utf-8", "replace"))


class Shell:
    """
    Интерактивная сессия (invoke_shell) для устройств, у которых
    exec не работает (коммутаторы, многие маршрутизаторы).

    Приглашение определяется само: последняя строка после входа.
    send(команда) ждёт, пока приглашение не появится снова, и
    возвращает вывод без эха команды и приглашения. Пагинатор
    («--More--») пролистывается пробелом.
    """

    def __init__(
        self,
        client: paramiko.SSHClient,
        *,
        timeout: float = COMMAND_TIMEOUT,
        pager: str | None = DEFAULT_PAGER,
        idle: float = 1.0,
    ):
        self.client = client
        self.timeout = timeout
        self.pager = re.compile(pager) if pager else None
        self.idle = idle
        self.channel = None
        self.prompt = ""

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def open(self) -> None:
        try:
            self.channel = self.client.invoke_shell(term="vt100", width=511, height=1000)
        except paramiko.SSHException as exc:
            raise PluginError(f"Устройство не открыло интерактивную сессию: {exc}") from None
        self.channel.settimeout(0.2)
        self.prompt = self.detect_prompt()

    def close(self) -> None:
        if self.channel is not None:
            try:
                self.channel.close()
            except Exception:
                pass
            self.channel = None

    # ---------------------------------------------------------

    def _recv(self) -> str:
        try:
            data = self.channel.recv(65535)
        except socket.timeout:
            return ""
        if not data:
            if self.channel.closed or self.channel.exit_status_ready():
                raise PluginError("Устройство закрыло сессию")
            return ""
        return data.decode("utf-8", "replace")

    def _page(self, buf: str) -> str:
        """Пролистать пагинатор в хвосте буфера; вернуть буфер без него."""
        if self.pager is None:
            return buf
        tail_start = max(0, len(buf) - 200)
        m = None
        for m in self.pager.finditer(buf, tail_start):
            pass
        if m is not None and not buf[m.end():].strip():
            self.channel.send(" ")
            return buf[:m.start()]
        return buf

    def read_quiet(self, timeout: float | None = None) -> str:
        """Читать, пока данные идут; вернуть, когда устройство замолчит."""
        deadline = time.monotonic() + (timeout or self.timeout)
        buf, last = "", time.monotonic()
        while time.monotonic() < deadline:
            chunk = self._recv()
            if chunk:
                buf = self._page(clean(buf + chunk))
                last = time.monotonic()
            elif buf and time.monotonic() - last >= self.idle:
                break
        return buf

    def detect_prompt(self) -> str:
        for _ in range(3):
            text = self.read_quiet(timeout=15)
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            if lines:
                return lines[-1]
            self.channel.send("\n")
        raise PluginError("Не удалось определить приглашение командной строки устройства")

    def read_until(self, pattern: re.Pattern | None = None, timeout: float | None = None, what: str = "") -> str:
        """
        Читать до приглашения (или до pattern в последней строке).
        Возвращает весь прочитанный текст.
        """
        timeout = timeout or self.timeout
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            chunk = self._recv()
            if not chunk:
                continue
            buf = self._page(clean(buf + chunk))
            last = buf.rstrip(" ").rsplit("\n", 1)[-1].strip()
            if pattern is not None and pattern.search(last):
                return buf
            if pattern is None and last == self.prompt and "\n" in buf:
                return buf
        raise PluginError(f"Нет ответа на «{what}» за {timeout:g} с")

    def send(self, command: str, timeout: float | None = None) -> str:
        """Выполнить команду; вывод без эха и приглашения."""
        self.channel.send(command + "\n")
        buf = self.read_until(timeout=timeout, what=command)
        lines = buf.split("\n")
        if lines and command.strip() and command.strip() in lines[0]:
            lines = lines[1:]
        if lines and lines[-1].strip() == self.prompt:
            lines = lines[:-1]
        return "\n".join(lines)

    def enable(self, command: str, password: str, prompt: str = r"[Pp]ass(word|wd)?:\s*$") -> None:
        """Привилегированный режим (Cisco enable и т.п.)."""
        self.channel.send(command + "\n")
        pw = re.compile(prompt)
        deadline = time.monotonic() + 15
        buf = ""
        while True:
            if time.monotonic() >= deadline:
                raise PluginError(f"Нет ответа на «{command}» за 15 с")
            buf = self._page(clean(buf + self._recv()))
            last = buf.rstrip(" ").rsplit("\n", 1)[-1].strip()
            if pw.search(last):
                break
            if "\n" in buf and last and last != self.prompt and last.endswith(("#", ">", "]", "$")):
                self.prompt = last      # пароль не спросили — режим уже сменился
                return
        self.channel.send(password + "\n")
        text = self.read_quiet(timeout=15)
        if re.search(r"denied|incorrect|bad secret|fail|invalid", text, re.I):
            raise PluginError(f"«{command}»: пароль не принят")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        self.prompt = lines[-1] if lines else self.detect_prompt()


def write_text(directory, name: str, text: str) -> str:
    """Записать файл атомарно (tmp → replace); вернуть абсолютный путь."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return str(path.resolve())
