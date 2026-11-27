# =========================================================
# tests/test_host_keys.py
# =========================================================
#
# Ключи хоста SSH (TOFU): запомнить при первом подключении,
# отказать при изменённом. К роутерам не подключаемся —
# Transport / ssh.exe подменены.
#
# =========================================================

import importlib.util
import threading

import paramiko
import pytest

from core import host_keys
from core.context import PLUGINS_DIR


@pytest.fixture(scope="module")
def keys():
    """Два разных RSA-ключа «сервера» (генерация — один раз)."""
    return paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)


@pytest.fixture(autouse=True)
def known_hosts(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    monkeypatch.setattr(host_keys, "KNOWN_HOSTS_FILE", path)
    monkeypatch.setattr(host_keys, "SSH_EXE_KNOWN_HOSTS_FILE", tmp_path / "known_hosts_ssh")
    return path


# =========================================================
# check_and_remember
# =========================================================

def test_first_use_remembers_then_accepts(keys, known_hosts):
    a, _ = keys
    host_keys.check_and_remember("10.0.0.1", 22, a)
    assert known_hosts.exists()
    host_keys.check_and_remember("10.0.0.1", 22, a)  # тот же — ок


def test_changed_key_rejected(keys):
    a, b = keys
    host_keys.check_and_remember("10.0.0.1", 22, a)
    with pytest.raises(paramiko.BadHostKeyException):
        host_keys.check_and_remember("10.0.0.1", 22, b)


def test_port_is_part_of_identity(keys, known_hosts):
    a, b = keys
    host_keys.check_and_remember("10.0.0.1", 22, a)
    host_keys.check_and_remember("10.0.0.1", 2222, b)   # другой порт — другая запись
    text = known_hosts.read_text()
    assert "[10.0.0.1]:2222" in text


def test_parallel_remember_keeps_all_hosts(keys, known_hosts):
    a, _ = keys
    hosts = [f"10.0.1.{i}" for i in range(20)]
    threads = [
        threading.Thread(target=host_keys.check_and_remember, args=(h, 22, a))
        for h in hosts
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    saved = paramiko.HostKeys(str(known_hosts))
    assert all(saved.lookup(h) for h in hosts)


def test_policy_handles_bracketed_hostname(keys, known_hosts):
    a, b = keys
    policy = host_keys.TofuPolicy(port=22)
    policy.missing_host_key(None, "[10.0.0.5]:2200", a)
    with pytest.raises(paramiko.BadHostKeyException):
        host_keys.check_and_remember("10.0.0.5", 2200, b)


def test_prepare_client_loads_known_keys(keys):
    a, _ = keys
    host_keys.check_and_remember("10.0.0.1", 22, a)
    client = paramiko.SSHClient()
    host_keys.prepare_client(client)
    assert client.get_host_keys().lookup("10.0.0.1")
    assert isinstance(client._policy, host_keys.TofuPolicy)


def test_ssh_exe_options(known_hosts):
    opts = host_keys.ssh_exe_options()
    assert "-oStrictHostKeyChecking=accept-new" in opts
    assert any(o.startswith('-oUserKnownHostsFile="') for o in opts)
    assert "-oStrictHostKeyChecking=no" not in opts


def test_is_host_key_error():
    assert host_keys.is_host_key_error(RuntimeError(
        "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\n"
        "Host key verification failed."
    ))
    assert not host_keys.is_host_key_error(RuntimeError("Permission denied"))


# =========================================================
# Плагин MikroTik
# =========================================================

@pytest.fixture
def mikrotik(monkeypatch, tmp_path):
    path = PLUGINS_DIR / "Mikrotik" / "plugin.py"
    if not path.exists():
        pytest.skip("плагин MikroTik не найден")
    spec = importlib.util.spec_from_file_location("nbm_test_mikrotik", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.paramiko.RSAKey, "from_private_key_file",
        staticmethod(lambda p: paramiko.RSAKey.generate(1024)),
    )
    key = tmp_path / "id.key"
    key.write_text("k")
    module._test_router = {
        "host": "10.0.0.9", "ssh_key": str(key),
        "backup_path": str(tmp_path / "bk"), "ros_version": "7",
    }
    return module


def _fake_transport(server_key, authed):
    class FakeTransport:
        def __init__(self, sock, disabled_algorithms=None): pass
        def start_client(self, timeout=None): pass
        def get_remote_server_key(self): return server_key
        def auth_publickey(self, username, key): authed.append(True)
        def is_authenticated(self): return True
        def close(self): pass
    return FakeTransport


def test_ros7_remembers_and_rejects_changed_key(mikrotik, keys, monkeypatch):
    a, b = keys
    authed = []

    monkeypatch.setattr(mikrotik, "CompatTransport", _fake_transport(a, authed))
    mikrotik._connect_ros7("10.0.0.9", 22, "admin", "k")
    assert authed == [True]

    authed.clear()
    monkeypatch.setattr(mikrotik, "CompatTransport", _fake_transport(b, authed))
    with pytest.raises(paramiko.BadHostKeyException):
        mikrotik._connect_ros7("10.0.0.9", 22, "admin", "k")
    assert authed == []          # наш ключ подменённому серверу не предъявлен


def test_changed_key_does_not_fall_back_to_ros6(mikrotik, keys, monkeypatch):
    a, b = keys
    host_keys.check_and_remember("10.0.0.9", 22, a)
    monkeypatch.setattr(mikrotik, "CompatTransport", _fake_transport(b, []))

    ros6_called = []
    monkeypatch.setattr(
        mikrotik, "backup_router_ros6",
        lambda r, p: ros6_called.append(True) or [],
    )

    with pytest.raises(paramiko.BadHostKeyException):
        mikrotik.backup_router(mikrotik._test_router)
    assert ros6_called == []


def test_ros6_uses_accept_new(mikrotik, monkeypatch):
    commands = []

    def fake_run(cmd, timeout=60, verbose=False):
        commands.append(cmd)
        raise RuntimeError("stop")

    monkeypatch.setattr(mikrotik, "_run_command", fake_run)
    with pytest.raises(RuntimeError):
        mikrotik.backup_router_ros6(mikrotik._test_router, mikrotik._test_router["backup_path"])

    ssh_cmd = commands[0]
    assert "-oStrictHostKeyChecking=accept-new" in ssh_cmd
    assert "-oStrictHostKeyChecking=no" not in ssh_cmd
    assert "-oUserKnownHostsFile=/dev/null" not in ssh_cmd
