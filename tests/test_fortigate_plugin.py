# =========================================================
# tests/test_fortigate_plugin.py — плагин Fortinet FortiGate
# =========================================================
#
# Скачивание sys_config по SCP с настоящего SSH-сервера на
# 127.0.0.1 (tests/fake_ssh.py отвечает по протоколу scp -f),
# проверка содержимого, сохранение прежней версии при
# ошибке, нормализация для сравнения.
#
# =========================================================

import importlib.util

import pytest

from core import host_keys
from core.context import PLUGINS_DIR
from core.plugin_api import PluginError, load_info
from tests.fake_ssh import FakeSSHServer

CONFIG = (
    "#config-version=FGT60F-7.2.5-FW-build1517-230606:opmode=0:vdom=0:user=nbm-backup\n"
    "#conf_file_ver=123456789\n"
    "#buildno=1517\n"
    "#global_vdom=1\n"
    "config system global\n"
    "    set hostname \"FGT-TEST\"\n"
    "    set admin-scp enable\n"
    "end\n"
    "config system admin\n"
    "    edit \"admin\"\n"
    "        set password ENC SH2abcdefghijklmnopqrstuvwxyz0123456789=\n"
    "    next\n"
    "end\n"
)


@pytest.fixture
def plugin():
    path = PLUGINS_DIR / "Fortigate" / "plugin.py"
    spec = importlib.util.spec_from_file_location("nbm_test_fortigate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def known_hosts(tmp_path, monkeypatch):
    monkeypatch.setattr(host_keys, "KNOWN_HOSTS_FILE", tmp_path / "known_hosts")


def scp_server(body: str | None = None, error: str | None = None):
    """SSH-сервер, который на «scp -f sys_config» отдаёт body или ошибку."""
    def ack(channel):
        assert channel.recv(1) == b"\x00"

    def handler(channel, command):
        if command != "scp -f sys_config":
            channel.sendall(f"\x01scp: unexpected command {command}\n".encode())
            return 1
        ack(channel)
        if error is not None:
            channel.sendall(f"\x01scp: {error}\n".encode())
            return 1
        data = body.encode()
        channel.sendall(f"C0644 {len(data)} sys_config\n".encode())
        ack(channel)
        channel.sendall(data + b"\x00")
        ack(channel)
        return 0
    return FakeSSHServer(username="nbm-backup", exec_channel=handler)


def device(srv, tmp_path):
    return {
        "host": "127.0.0.1", "port": srv.port, "username": srv.username,
        "ssh_pass": srv.password, "ssh_key": str(tmp_path / "keys" / "id_rsa.key"),
        "backup_path": str(tmp_path / "backup"), "read_timeout": 10,
    }


def test_contract(plugin):
    info = load_info(plugin, "Fortigate")
    assert info.type == "fortigate"
    assert info.uses_password and info.uses_key
    assert info.default_username == "nbm-backup"
    assert info.setup


def test_downloads_sys_config(plugin, tmp_path):
    with scp_server(CONFIG) as srv:
        paths = plugin.run_backup(device(srv, tmp_path))
    assert srv.received == ["scp -f sys_config"]
    assert len(paths) == 1
    saved = tmp_path / "backup" / "fortigate.conf"
    assert paths[0] == str(saved.resolve())
    assert saved.read_bytes() == CONFIG.encode()
    assert not (tmp_path / "backup" / "fortigate.conf.part").exists()


@pytest.mark.parametrize("error, expected", [
    ("sys_config: Permission denied", "admin-scp"),
    ("sys_config: No such file or directory", "нет файла"),
])
def test_scp_error_keeps_previous(plugin, tmp_path, error, expected):
    saved = tmp_path / "backup" / "fortigate.conf"
    saved.parent.mkdir()
    saved.write_bytes(b"previous")
    with scp_server(error=error) as srv:
        with pytest.raises(PluginError, match=expected):
            plugin.run_backup(device(srv, tmp_path))
    assert saved.read_bytes() == b"previous"
    assert not saved.with_name("fortigate.conf.part").exists()


@pytest.mark.parametrize("body, expected", [
    ("Command fail. Return code -37\n", "не похож"),
    ("   \n", "пустой"),
])
def test_not_a_config_keeps_previous(plugin, tmp_path, body, expected):
    saved = tmp_path / "backup" / "fortigate.conf"
    saved.parent.mkdir()
    saved.write_bytes(b"previous")
    with scp_server(body) as srv:
        with pytest.raises(PluginError, match=expected):
            plugin.run_backup(device(srv, tmp_path))
    assert saved.read_bytes() == b"previous"
    assert not saved.with_name("fortigate.conf.part").exists()


def test_wrong_password(plugin, tmp_path):
    with scp_server(CONFIG) as srv:
        dev = device(srv, tmp_path)
        dev["ssh_pass"] = "wrong"
        with pytest.raises(PluginError, match="неверный логин"):
            plugin.run_backup(dev)


def test_bad_read_timeout(plugin, tmp_path):
    with scp_server(CONFIG) as srv:
        dev = device(srv, tmp_path)
        dev["read_timeout"] = "abc"
        with pytest.raises(PluginError, match="не число"):
            plugin.run_backup(dev)


def test_normalize_ignores_counter_and_ciphertext(plugin):
    other = (CONFIG.replace("#conf_file_ver=123456789", "#conf_file_ver=987654321")
             .replace("SH2abcdefghijklmnopqrstuvwxyz0123456789=", "SH2zyxw9876543210="))
    norm = plugin.normalize_for_compare
    assert norm("fortigate.conf", CONFIG.encode()) == norm("fortigate.conf", other.encode())

    changed = CONFIG.replace("FGT-TEST", "FGT-NEW")
    assert norm("fortigate.conf", CONFIG.encode()) != norm("fortigate.conf", changed.encode())
    # другие файлы не трогает
    assert norm("other.txt", CONFIG.encode()) == CONFIG.encode()
