# =========================================================
# tests/test_dlink_plugin.py — плагин D-Link DGS-1510
# =========================================================
#
# Старая прошивка предлагает ключ хоста только ssh-rsa
# (подпись SHA-1), который paramiko 5 не принимает.
#
# =========================================================

import importlib.util

import paramiko
import pytest

from core import host_keys, ssh
from core.context import PLUGINS_DIR
from tests.fake_ssh import FakeSSHServer

CONFIG = """#-------------------------------------------------------------------
#                       DGS-1510-28X Gigabit Ethernet SmartPro Switch
#-------------------------------------------------------------------
Current configuration : 2048 bytes
!
snmp-server name SW1
interface ethernet 1/0/1
 description uplink
!
end"""


@pytest.fixture
def plugin():
    path = PLUGINS_DIR / "D-Link_DGS-1510" / "plugin.py"
    spec = importlib.util.spec_from_file_location("nbm_test_dlink_plugin", path)
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


def test_old_firmware_ssh_rsa_only(plugin, tmp_path, known_hosts):
    with FakeSSHServer(commands={"show running-config": CONFIG}, old_host_key=True) as srv:
        files = plugin.run_backup(device(srv, tmp_path))
        assert srv.received[:2] == ["terminal length 0", "show running-config"]

        text = (tmp_path / "backup" / "running-config.cfg").read_text(encoding="utf-8")
        assert files == [str((tmp_path / "backup" / "running-config.cfg").resolve())]
        assert text.startswith("Current configuration") and text.endswith("end\n")
        # Ключ запомнен (TOFU) и при повторе сверяется
        assert "ssh-rsa" in known_hosts.read_text()
        plugin.run_backup(device(srv, tmp_path))


def test_old_firmware_changed_key_rejected(plugin, tmp_path):
    with FakeSSHServer(commands={"show running-config": CONFIG}, old_host_key=True) as srv:
        plugin.run_backup(device(srv, tmp_path))
    with FakeSSHServer(commands={"show running-config": CONFIG}, old_host_key=True,
                       key_index=1) as srv2:
        # Другой порт — другая запись: переносим на неё ключ
        # первого сервера, будто устройство подменили
        known = paramiko.HostKeys(str(host_keys.KNOWN_HOSTS_FILE))
        old_key = next(iter(known.values()))["ssh-rsa"]
        known.add(f"[127.0.0.1]:{srv2.port}", "ssh-rsa", old_key)
        known.save(str(host_keys.KNOWN_HOSTS_FILE))
        with pytest.raises(paramiko.BadHostKeyException):
            plugin.run_backup(device(srv2, tmp_path))


def test_plain_paramiko_rejects_old_firmware(tmp_path):
    """Почему нужен LegacyRSATransport: без него — ошибка из журнала."""
    with FakeSSHServer(old_host_key=True) as srv:
        with pytest.raises(Exception, match="no acceptable host key"):
            ssh.connect(device(srv, tmp_path)).close()


def test_legacy_transport_prefers_modern_keys(tmp_path):
    """С обычным сервером LegacyRSATransport выбирает ключ rsa-sha2."""
    with FakeSSHServer() as srv:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=srv.port, username=srv.username,
                       password=srv.password, allow_agent=False, look_for_keys=False,
                       transport_factory=ssh.LegacyRSATransport)
        try:
            assert client.get_transport().host_key_type.startswith("rsa-sha2-")
        finally:
            client.close()
