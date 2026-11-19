# =========================================================
# tests/fake_ssh.py — SSH-сервер для тестов плагинов
# =========================================================
#
# Настоящий SSH (paramiko) на 127.0.0.1 со случайным портом:
#
#   • exec — exec_handler(command) -> (вывод, код);
#   • shell — CLI в стиле Cisco: приглашение, эхо ввода,
#     enable с паролем, пагинатор «--More--» до
#     «terminal length 0», ответы из словаря commands.
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
        return "password"

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


class FakeSSHServer:
    def __init__(
        self,
        *,
        username="admin",
        password="pw",
        commands=None,
        exec_handler=None,
        hostname="R1",
        enable_password=None,
        page_lines=5,
        key_index=0,
    ):
        self.username = username
        self.password = password
        self.commands = commands or {}
        self.exec_handler = exec_handler
        self.hostname = hostname
        self.enable_password = enable_password
        self.page_lines = page_lines
        self.key = host_key(key_index)
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
            t = paramiko.Transport(conn)
            t.add_server_key(self.key)
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
