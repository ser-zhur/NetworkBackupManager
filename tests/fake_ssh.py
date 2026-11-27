# =========================================================
# tests/fake_ssh.py — SSH-сервер для тестов плагинов
# =========================================================
#
# Настоящий SSH (paramiko) на 127.0.0.1 со случайным портом:
#
#   • exec — exec_handler(command) -> (вывод, код);
#     exec_channel(channel, command) -> код — диалог
#     с клиентом; scp_source(files) — готовый такой
#     обработчик: отдаёт файлы по «scp -f <путь>»;
#   • sftp_files={путь: байты} — подсистема SFTP (только
#     чтение);
#   • shell — CLI в стиле Cisco: приглашение, эхо ввода,
#     enable с паролем, пагинатор «--More--» до
#     «terminal length 0», ответы из словаря commands;
#   • old_host_key=True — как старая прошивка: ключ хоста
#     только ssh-rsa (подпись SHA-1); kex=(...) — только эти
#     алгоритмы обмена ключами (например, group1-sha1);
#   • authorized_key=PKey — вход и по этому ключу.
#
# =========================================================

from __future__ import annotations

import socket
import threading
import time

import paramiko

_HOST_KEYS: list = []


def host_key(n: int = 0) -> paramiko.PKey:
    while len(_HOST_KEYS) <= n:
        _HOST_KEYS.append(paramiko.RSAKey.generate(2048))
    return _HOST_KEYS[n]


class _Iface(paramiko.ServerInterface):
    def __init__(self, srv: "FakeSSHServer"):
        self.srv = srv

    def get_allowed_auths(self, username):
        return "password,publickey" if self.srv.authorized_key else "password"

    def check_auth_publickey(self, username, key):
        ok = username == self.srv.username and key == self.srv.authorized_key
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        if username == self.srv.username and password == self.srv.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *a):
        return True

    def check_channel_exec_request(self, channel, command):
        threading.Thread(target=self.srv._exec, args=(channel, command.decode()), daemon=True).start()
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self.srv._shell, args=(channel,), daemon=True).start()
        return True


def _attrs(data: bytes) -> paramiko.SFTPAttributes:
    attr = paramiko.SFTPAttributes()
    attr.st_size = len(data)
    attr.st_mode = 0o100644
    return attr


class _MemHandle(paramiko.SFTPHandle):
    def __init__(self, data: bytes):
        super().__init__()
        self.data = data

    def read(self, offset, length):
        return self.data[offset:offset + length]

    def stat(self):
        return _attrs(self.data)


class _MemSFTP(paramiko.SFTPServerInterface):
    """SFTP только для чтения из словаря {путь: байты}."""

    def __init__(self, iface, *args, **kwargs):
        super().__init__(iface, *args, **kwargs)
        self.files = iface.srv.sftp_files

    def open(self, path, flags, attr):
        data = self.files.get(path)
        return paramiko.SFTP_NO_SUCH_FILE if data is None else _MemHandle(data)

    def stat(self, path):
        data = self.files.get(path)
        return paramiko.SFTP_NO_SUCH_FILE if data is None else _attrs(data)

    lstat = stat


def scp_source(files: dict):
    """exec_channel: «scp -f <путь>» по протоколу SCP из словаря {путь: байты}."""
    def ack(channel):
        return channel.recv(1) == b"\x00"

    def handler(channel, command):
        if not command.startswith("scp -f "):
            channel.sendall(f"\x01scp: unexpected command {command}\n".encode())
            return 1
        path = command[len("scp -f "):].strip().strip("'")
        if not ack(channel):
            return 1
        data = files.get(path)
        if data is None:
            channel.sendall(f"\x01scp: {path}: No such file or directory\n".encode())
            return 1
        name = path.rsplit("/", 1)[-1]
        channel.sendall(f"C0644 {len(data)} {name}\n".encode())
        if not ack(channel):
            return 1
        channel.sendall(data + b"\x00")
        ack(channel)
        return 0
    return handler


class FakeSSHServer:
    def __init__(
        self,
        *,
        username="admin",
        password="pw",
        commands=None,
        exec_handler=None,
        exec_channel=None,
        sftp_files=None,
        hostname="R1",
        enable_password=None,
        page_lines=5,
        key_index=0,
        old_host_key=False,
        kex=None,
        authorized_key=None,
    ):
        self.authorized_key = authorized_key
        self.username = username
        self.password = password
        self.commands = commands or {}
        self.exec_handler = exec_handler
        self.exec_channel = exec_channel
        self.sftp_files = sftp_files
        self.hostname = hostname
        self.enable_password = enable_password
        self.page_lines = page_lines
        self.key = host_key(key_index)
        self.transport_class = paramiko.Transport
        if old_host_key or kex:
            from core import ssh

            class OldFirmware(ssh.CompatTransport):
                pass

            if old_host_key:
                self.key = ssh._LegacyRSAKey(key=self.key.key)
                OldFirmware._preferred_keys = ("ssh-rsa",)
            if kex:
                OldFirmware._preferred_kex = tuple(kex)
                # group-exchange: серверу нужны группы (как /etc/ssh/moduli)
                from paramiko.primes import ModulusPack
                pack = ModulusPack()
                for k in (ssh._KexGroup1SHA1, ssh._KexGroup14SHA1):
                    bits = k.P.bit_length()
                    pack._parse_modulus(f"0 2 6 100 {bits - 1} 2 {k.P:X}")
                OldFirmware._modulus_pack = pack
            self.transport_class = OldFirmware
        self.received: list[str] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        # shutdown будит accept() в потоке (Linux: одного close мало)
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        for t in self._transports:
            t.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -----------------------------------------------------

    def _accept(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            t = self.transport_class(conn)
            t.add_server_key(self.key)
            if self.sftp_files is not None:
                t.set_subsystem_handler("sftp", paramiko.SFTPServer, _MemSFTP)
            self._transports.append(t)
            try:
                t.start_server(server=_Iface(self))
            except Exception:
                continue

    def _exec(self, channel, command):
        # paramiko отвечает на exec-запрос после возврата из
        # check_channel_exec_request: закрыть канал раньше —
        # клиент получит «Channel closed».
        time.sleep(0.1)
        self.received.append(command)
        if self.exec_channel:
            code = self.exec_channel(channel, command)
        else:
            out, code = self.exec_handler(command) if self.exec_handler else ("", 127)
            channel.sendall(out.encode())
        channel.send_exit_status(code)
        channel.close()

    def _readline(self, channel, echo=True) -> str | None:
        buf = b""
        while True:
            data = channel.recv(1)
            if not data:
                return None
            if data in (b"\n", b"\r"):
                if echo:
                    channel.sendall(b"\r\n")
                return buf.decode()
            buf += data
            if echo:
                channel.sendall(data)

    def _shell(self, channel):
        privileged = self.enable_password is None
        paging = True
        channel.sendall(b"\r\nWelcome to fake CLI\r\n\r\n")
        while True:
            channel.sendall(f"{self.hostname}{'#' if privileged else '>'}".encode())
            line = self._readline(channel)
            if line is None:
                return
            cmd = line.strip()
            self.received.append(cmd)
            if not cmd:
                continue
            if cmd == "enable":
                channel.sendall(b"Password: ")
                pw = self._readline(channel, echo=False)
                if pw == self.enable_password:
                    privileged = True
                else:
                    channel.sendall(b"% Access denied\r\n")
                continue
            if cmd == "terminal length 0":
                paging = False
                continue
            if cmd in ("exit", "quit"):
                channel.close()
                return
            out = self.commands.get(cmd)
            if out is None:
                channel.sendall(b"% Invalid input detected at '^' marker.\r\n")
                continue
            if callable(out):
                out = out(privileged)
            lines = out.split("\n")
            for i, text in enumerate(lines):
                channel.sendall(text.encode() + b"\r\n")
                if paging and (i + 1) % self.page_lines == 0 and i + 1 < len(lines):
                    channel.sendall(b" --More-- ")
                    while channel.recv(1) not in (b" ", b""):
                        pass
                    channel.sendall(b"\x08" * 10 + b" " * 10 + b"\x08" * 10)
