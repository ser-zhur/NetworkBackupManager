# =========================================================
# tests/test_ssh_compat.py — старые прошивки и paramiko 5
# =========================================================
#
# paramiko 5 убрал ключ хоста ssh-rsa (SHA-1) и обмен
# ключами на SHA-1. core.ssh.CompatTransport возвращает их
# последними. Проверяем на сервере, который умеет только
# старое, все места, где плагины подключаются через paramiko.
#
# =========================================================

import importlib.util

import paramiko
import pytest

from core import host_keys, ssh
from core.context import PLUGINS_DIR
from tests.fake_ssh import FakeSSHServer

OLD_KEX = [
    "diffie-hellman-group-exchange-sha1",
    "diffie-hellman-group14-sha1",
    "diffie-hellman-group1-sha1",
]

CISCO_CONFIG = "Building configuration...\n\nCurrent configuration : 99 bytes\n!\nhostname OLD\n!\nend"


def load_plugin(folder):
    path = PLUGINS_DIR / folder / "plugin.py"
    spec = importlib.util.spec_from_file_location(f"nbm_test_compat_{folder}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def known_hosts(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    monkeypatch.setattr(host_keys, "KNOWN_HOSTS_FILE", path)
    return path


def device(srv, tmp_path, **extra):
    return {
        "host": "127.0.0.1", "port": srv.port, "username": srv.username,
        "ssh_pass": srv.password, "backup_path": str(tmp_path / "backup"),
        "read_timeout": 10, **extra,
    }


def plain_connect(srv):
    """Обычная paramiko без CompatTransport."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect("127.0.0.1", port=srv.port, username=srv.username,
                   password=srv.password, allow_agent=False, look_for_keys=False,
                   timeout=10)
    client.close()


# ---------------------------------------------------------
# core.ssh
# ---------------------------------------------------------

def test_plain_paramiko_cannot_connect_to_old_firmware():
    """Без CompatTransport — ошибки из журнала пользователя."""
    with FakeSSHServer(old_host_key=True) as srv:
        with pytest.raises(paramiko.SSHException, match="no acceptable host key"):
            plain_connect(srv)
    with FakeSSHServer(kex=["diffie-hellman-group14-sha1"]) as srv:
        with pytest.raises(paramiko.SSHException, match="no acceptable kex"):
            plain_connect(srv)


@pytest.mark.parametrize("kex", OLD_KEX)
def test_connect_old_kex_and_host_key(tmp_path, known_hosts, kex):
    with FakeSSHServer(old_host_key=True, kex=[kex]) as srv:
        client = ssh.connect(device(srv, tmp_path))
        try:
            t = client.get_transport()
            assert t.is_authenticated() and t.host_key_type == "ssh-rsa"
        finally:
            client.close()
    assert " ssh-rsa " in known_hosts.read_text()


def test_compat_prefers_modern(tmp_path):
    """Современный сервер — современные алгоритмы, не SHA-1."""
    with FakeSSHServer() as srv:
        client = ssh.connect(device(srv, tmp_path))
        try:
            t = client.get_transport()
            assert t.host_key_type.startswith("rsa-sha2-")
        finally:
            client.close()
    pref = ssh.CompatTransport._preferred_kex
    assert pref[-3:] == tuple(OLD_KEX)
    assert ssh.CompatTransport._preferred_keys[-1] == "ssh-rsa"
    # Сам paramiko не изменён: на остальных подключениях SHA-1 нет
    assert "ssh-rsa" not in paramiko.Transport._preferred_keys
    assert not set(OLD_KEX) & set(paramiko.Transport._preferred_kex)


def test_changed_host_key_still_rejected(tmp_path):
    with FakeSSHServer(old_host_key=True, kex=["diffie-hellman-group1-sha1"]) as srv:
        ssh.connect(device(srv, tmp_path)).close()
        port = srv.port
    known = paramiko.HostKeys(str(host_keys.KNOWN_HOSTS_FILE))
    old_key = next(iter(known.values()))["ssh-rsa"]
    with FakeSSHServer(old_host_key=True, kex=["diffie-hellman-group1-sha1"], key_index=1) as srv2:
        known.add(f"[127.0.0.1]:{srv2.port}", "ssh-rsa", old_key)
        known.save(str(host_keys.KNOWN_HOSTS_FILE))
        with pytest.raises(Exception, match="изменился"):
            ssh.connect(device(srv2, tmp_path)).close()
    assert port != srv2.port


# ---------------------------------------------------------
# Плагины
# ---------------------------------------------------------

def test_ssh_plugin_old_cisco(tmp_path):
    plugin = load_plugin("SSH")
    with FakeSSHServer(commands={"show running-config": CISCO_CONFIG}, old_host_key=True,
                       kex=["diffie-hellman-group1-sha1"]) as srv:
        paths = plugin.run_backup(device(srv, tmp_path, profile="cisco_ios"))
    assert "hostname OLD" in open(paths[0], encoding="utf-8").read()


def test_fortigate_old_firmware(tmp_path):
    plugin = load_plugin("Fortigate")
    body = "#config-version=FGT60D-5.4.0-FW-build1011:opmode=0:vdom=0:user=nbm\nconfig system global\nend\n"

    def handler(channel, command):
        channel.recv(1)
        data = body.encode()
        channel.sendall(f"C0644 {len(data)} sys_config\n".encode())
        channel.recv(1)
        channel.sendall(data + b"\x00")
        channel.recv(1)
        return 0

    with FakeSSHServer(username="nbm-backup", exec_channel=handler, old_host_key=True,
                       kex=["diffie-hellman-group14-sha1"]) as srv:
        paths = plugin.run_backup(device(srv, tmp_path))
    assert "config system global" in open(paths[0], encoding="utf-8").read()


def test_mikrotik_paramiko_path_old_ros(tmp_path):
    plugin = load_plugin("Mikrotik")
    key = paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "id_rsa.key"
    key.write_private_key_file(str(key_path))
    with FakeSSHServer(old_host_key=True, kex=["diffie-hellman-group-exchange-sha1"],
                       authorized_key=key) as srv:
        client = plugin._connect_ros7("127.0.0.1", srv.port, srv.username, str(key_path))
        try:
            t = client.get_transport()
            assert t.is_authenticated() and t.host_key_type == "ssh-rsa"
        finally:
            client.close()


def test_mikrotik_ros6_allows_ssh_rsa_host_key(tmp_path, monkeypatch):
    """Путь ROS6 (системный ssh): ключ хоста ssh-rsa разрешён явно."""
    plugin = load_plugin("Mikrotik")
    calls = []

    def fake_run(cmd, timeout=None, verbose=False):
        calls.append(cmd)
        raise RuntimeError("stop")

    monkeypatch.setattr(plugin, "_run_command", fake_run)
    monkeypatch.setattr(plugin, "_get_key_path", lambda router: str(tmp_path / "k"))
    with pytest.raises(RuntimeError):
        plugin.backup_router_ros6({"host": "192.0.2.1", "username": "nbm"}, str(tmp_path))
    assert calls and "-oHostKeyAlgorithms=+ssh-rsa" in calls[-1]
