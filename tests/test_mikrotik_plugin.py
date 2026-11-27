# =========================================================
# tests/test_mikrotik_plugin.py — плагин MikroTik (путь paramiko)
# =========================================================
#
# Вход по паролю без ключа, ошибки RouterOS в выводе команд,
# таймаут зависшей команды. Устройство — tests/fake_ssh.py.
#
# =========================================================

import importlib.util
import time

import paramiko
import pytest

from core import host_keys
from core.context import PLUGINS_DIR
from tests.fake_ssh import FakeSSHServer, scp_source

RSC = b"# 2026-11-27 10:00:00 by RouterOS 7.20\n/system identity set name=R1\n"
BACKUP = b"\x00binary-backup"


@pytest.fixture
def plugin():
    path = PLUGINS_DIR / "Mikrotik" / "plugin.py"
    spec = importlib.util.spec_from_file_location("nbm_test_mikrotik_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def known_hosts(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    monkeypatch.setattr(host_keys, "KNOWN_HOSTS_FILE", path)
    return path


def router_os(outputs=None, delay=None):
    """exec: команды RouterOS (вывод из outputs) и scp -f для файлов."""
    outputs = outputs or {}
    scp = scp_source({"auto-backup.backup": BACKUP, "auto-export.rsc": RSC})

    def handler(channel, command):
        if command.startswith("scp "):
            return scp(channel, command)
        if delay and command in delay:
            time.sleep(delay[command])
        channel.sendall(outputs.get(command, "").encode())
        return 0
    return handler


def device(srv, tmp_path, **extra):
    return {
        "host": "127.0.0.1", "port": srv.port, "username": srv.username,
        "ssh_pass": srv.password, "ssh_key": str(tmp_path / "no-such.key"),
        "backup_path": str(tmp_path / "backup"), **extra,
    }


def test_password_login_without_key(plugin, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(plugin, "_do_ros6", lambda *a: called.append("ros6"))
    with FakeSSHServer(exec_channel=router_os()) as srv:
        dev = device(srv, tmp_path)
        files = plugin.run_backup(dev)
    assert called == []                                  # ssh.exe без ключа не пробуем
    assert dev["ros_version"] == "7"
    assert sorted(p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in files) == ["config.backup", "config.rsc"]
    assert (tmp_path / "backup" / "config.rsc").read_bytes() == RSC
    assert "/system backup save name=auto-backup" in srv.received


def test_no_key_and_no_password_is_an_error(plugin, tmp_path):
    with FakeSSHServer(exec_channel=router_os()) as srv:
        with pytest.raises(FileNotFoundError, match="SSH-ключ не найден"):
            plugin.run_backup(device(srv, tmp_path, ssh_pass=""))


def test_wrong_password(plugin, tmp_path):
    with FakeSSHServer(exec_channel=router_os()) as srv:
        with pytest.raises(Exception) as exc:
            plugin.run_backup(device(srv, tmp_path, ssh_pass="bad"))
    assert "auth" in str(exc.value).lower() or "Authentication" in str(exc.value)


def test_routeros_failure_in_output(plugin, tmp_path):
    out = {"/system backup save name=auto-backup": "failure: not enough disk space\n"}
    with FakeSSHServer(exec_channel=router_os(out)) as srv:
        client = plugin._connect_ros7("127.0.0.1", srv.port, srv.username, None, srv.password)
        with pytest.raises(RuntimeError, match="not enough disk space"):
            plugin.backup_router_ros7(device(srv, tmp_path), str(tmp_path / "b"), client=client)
        client.close()


def test_hung_command_times_out(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "COMMAND_TIMEOUT", 1)
    slow = {"/export file=auto-export": 5}
    with FakeSSHServer(exec_channel=router_os(delay=slow)) as srv:
        client = plugin._connect_ros7("127.0.0.1", srv.port, srv.username, None, srv.password)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="не (ответил|завершил)"):
            plugin.backup_router_ros7(device(srv, tmp_path), str(tmp_path / "b"), client=client)
        assert time.monotonic() - started < 4
        client.close()


def test_ros6_macs_keep_modern_algorithms(plugin, tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, timeout=60, verbose=False):
        seen.append(cmd)
        raise RuntimeError("stop")

    monkeypatch.setattr(plugin, "_run_command", fake_run)
    key = tmp_path / "id_rsa.key"
    key.write_text("x")
    with pytest.raises(RuntimeError):
        plugin.backup_router_ros6({"host": "192.0.2.1", "ssh_key": str(key)}, str(tmp_path))
    assert "-oMACs=+hmac-sha1,hmac-md5" in seen[-1]
    assert seen[-1].index("--") == seen[-1].index("admin@192.0.2.1") - 1


# ---- ключ хоста другого типа ----

def test_other_host_key_type_is_rejected(known_hosts):
    rsa = paramiko.RSAKey.generate(1024)
    ecdsa = paramiko.ECDSAKey.generate()
    host_keys.check_and_remember("192.0.2.5", 22, rsa)
    host_keys.check_and_remember("192.0.2.5", 22, rsa)            # тот же — ок
    with pytest.raises(paramiko.BadHostKeyException):
        host_keys.check_and_remember("192.0.2.5", 22, ecdsa)
    host_keys.check_and_remember("192.0.2.6", 22, ecdsa)          # новый хост — запомнить


def test_known_key_type_is_preferred(known_hosts):
    import socket

    host_keys.check_and_remember("192.0.2.7", 2222, paramiko.ECDSAKey.generate())
    a, b = socket.socketpair()
    try:
        t = paramiko.Transport(a)
        host_keys.prefer_known_key_types(t, "192.0.2.7", 2222)
        assert t.get_security_options().key_types[0].startswith("ecdsa-sha2-")

        host_keys.check_and_remember("192.0.2.8", 22, paramiko.RSAKey.generate(1024))
        t2 = paramiko.Transport(b)
        host_keys.prefer_known_key_types(t2, "192.0.2.8", 22)
        first = t2.get_security_options().key_types[0]
        assert first.startswith("rsa-sha2-") or first == "ssh-rsa"
    finally:
        a.close()
        b.close()
