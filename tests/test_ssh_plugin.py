# =========================================================
# tests/test_ssh_plugin.py — core/ssh и универсальный плагин SSH
# =========================================================
#
# Подключение к настоящему SSH-серверу на 127.0.0.1
# (tests/fake_ssh.py): exec, интерактивная сессия, enable,
# пагинатор, ошибки входа и смена ключа хоста.
#
# =========================================================

import importlib.util
import json
import socket

import pytest

from core import host_keys, ssh
from core.context import PLUGINS_DIR
from core.plugin_api import PluginError
from tests.fake_ssh import FakeSSHServer, scp_source

CISCO_CONFIG = """Building configuration...

Current configuration : 1234 bytes
!
! Last configuration change at 10:00:00 UTC Mon Nov 16 2026 by admin
!
version 15.2
hostname R1
!
interface GigabitEthernet0/0
 ip address 192.0.2.1 255.255.255.0
!
ntp clock-period 17179870
end"""


@pytest.fixture
def plugin():
    path = PLUGINS_DIR / "SSH" / "plugin.py"
    spec = importlib.util.spec_from_file_location("nbm_test_ssh_plugin", path)
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
        "ssh_pass": srv.password, "ssh_key": str(tmp_path / "keys" / "id_rsa.key"),
        "backup_path": str(tmp_path / "backup"), "read_timeout": 10, **extra,
    }


# ---------------------------------------------------------
# Профили
# ---------------------------------------------------------

def test_shipped_profiles_are_valid(plugin, caplog):
    profiles = plugin.load_profiles([plugin.BUILTIN_PROFILES_DIR])
    files = sorted(p.stem for p in plugin.BUILTIN_PROFILES_DIR.glob("*.json"))
    assert sorted(profiles) == files and "cisco_ios" in profiles
    assert "пропущен" not in caplog.text


def test_user_profile_overrides_and_bad_profile_skipped(plugin, tmp_path, caplog):
    user = tmp_path / "ssh_profiles"
    user.mkdir()
    (user / "cisco_ios.json").write_text(json.dumps({
        "name": "Мой Cisco", "commands": [{"command": "show run", "file": "a.cfg"}]}), encoding="utf-8")
    (user / "broken.json").write_text(json.dumps({"mode": "telnet", "commands": []}), encoding="utf-8")
    (user / "custom.json").write_text("{}", encoding="utf-8")
    profiles = plugin.load_profiles([plugin.BUILTIN_PROFILES_DIR, user])
    assert profiles["cisco_ios"]["name"] == "Мой Cisco"
    assert "broken" not in profiles and "custom" not in profiles
    assert "broken.json пропущен" in caplog.text


@pytest.mark.parametrize("data, error", [
    ({"commands": [{"command": "x", "file": "../etc/passwd"}]}, "имя файла"),
    ({"commands": [{"command": "x", "file": "a"}, {"command": "y", "file": "a"}]}, "уже есть"),
    ({"commands": [{"command": "x", "file": "a", "start": "("}]}, "регулярное"),
    ({"commands": [{"file": "a"}]}, "одно из command или download"),
    ({"commands": [{"command": "x", "download": "/f", "file": "a"}]}, "одно из command или download"),
    ({"commands": [{"download": "/f", "file": "a", "drop": ["^#"]}]}, "используйте ignore"),
    ({"commands": [{"command": "x", "file": "a", "ignore": "^#"}]}, "не список"),
])
def test_parse_profile_errors(plugin, data, error):
    with pytest.raises(ValueError, match=error):
        plugin.parse_profile(data, "x")


def test_custom_commands_names(plugin):
    cmds = plugin.parse_custom_commands(
        "# комментарий\nshow running-config\n\nver.txt: show version\nshow running-config\n")
    assert [c["file"] for c in cmds] == ["show_running_config.txt", "ver.txt", "show_running_config_2.txt"]
    assert cmds[1]["command"] == "show version"
    with pytest.raises(PluginError, match="ни одной команды"):
        plugin.parse_custom_commands("# пусто\n")


def test_extract(plugin):
    spec = plugin.parse_profile({"commands": [{
        "command": "show", "file": "a", "start": "^Current", "end": "^end$", "drop": ["^!"]}]}, "x")["commands"][0]
    assert plugin.extract("junk\nCurrent conf\n! x\nhostname R1  \nend\nR1#", spec) == "Current conf\nhostname R1\nend\n"
    with pytest.raises(PluginError, match="оборван"):
        plugin.extract("Current conf\nhostname R1\n", spec)
    with pytest.raises(PluginError, match="нет начала"):
        plugin.extract("hostname R1\nend", spec)
    with pytest.raises(PluginError, match="не приняло"):
        plugin.extract("% Invalid input detected at '^' marker.", spec)


# ---------------------------------------------------------
# Бэкап через настоящий SSH
# ---------------------------------------------------------

def test_exec_profile_openwrt(plugin, tmp_path):
    handler = lambda cmd: ("package network\n\nconfig interface 'lan'\n", 0) if cmd == "uci export" else ("", 127)
    with FakeSSHServer(username="root", exec_handler=handler) as srv:
        paths = plugin.run_backup(device(srv, tmp_path, profile="openwrt"))
    assert [p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in paths] == ["uci-export.txt"]
    assert open(paths[0], encoding="utf-8").read() == "package network\n\nconfig interface 'lan'\n"


def test_exec_error_code(plugin, tmp_path):
    with FakeSSHServer(exec_handler=lambda cmd: ("uci: not found\n", 127)) as srv:
        with pytest.raises(PluginError, match="кодом 127"):
            plugin.run_backup(device(srv, tmp_path, profile="openwrt"))
    assert not (tmp_path / "backup" / "uci-export.txt").exists()


def test_cisco_profile_with_enable(plugin, tmp_path):
    cfg = lambda privileged: CISCO_CONFIG if privileged else "% Invalid input"
    with FakeSSHServer(commands={"show running-config": cfg}, enable_password="secret") as srv:
        paths = plugin.run_backup(device(srv, tmp_path, profile="cisco_ios", enable_pass="secret"))
        assert "terminal length 0" in srv.received
    text = open(paths[0], encoding="utf-8").read()
    assert text.startswith("!\n!\nversion 15.2\n") and text.endswith("end\n")
    assert "Last configuration change" not in text and "ntp clock-period" not in text
    assert "Current configuration" not in text and "--More--" not in text


def test_cisco_needs_enable_password(plugin, tmp_path):
    with FakeSSHServer(commands={"show running-config": CISCO_CONFIG}, enable_password="secret") as srv:
        with pytest.raises(PluginError, match="пароль enable"):
            plugin.run_backup(device(srv, tmp_path, profile="cisco_ios"))
        with pytest.raises(PluginError, match="не принят"):
            plugin.run_backup(device(srv, tmp_path, profile="cisco_ios", enable_pass="wrong"))


def test_custom_shell_commands_with_pager(plugin, tmp_path):
    long_output = "\n".join(f"line {i}" for i in range(1, 18))
    with FakeSSHServer(commands={"show log": long_output, "show version": "Fake OS 1.0"}) as srv:
        paths = plugin.run_backup(device(
            srv, tmp_path, profile="custom", mode="shell", commands="show log\nver.txt: show version"))
    log = open(paths[0], encoding="utf-8").read()
    assert log == long_output + "\n"                       # пагинатор пролистан, следов нет
    assert open(paths[1], encoding="utf-8").read() == "Fake OS 1.0\n"


def test_truncated_output_writes_nothing(plugin, tmp_path):
    truncated = CISCO_CONFIG.rsplit("\n", 1)[0]            # без «end»
    with FakeSSHServer(commands={"show running-config": truncated}) as srv:
        with pytest.raises(PluginError, match="оборван"):
            plugin.run_backup(device(srv, tmp_path, profile="cisco_ios"))
    assert not (tmp_path / "backup" / "running-config.cfg").exists()


# ---------------------------------------------------------
# Скачивание файлов (download, «file:»)
# ---------------------------------------------------------

OPNSENSE_XML = (
    b'<?xml version="1.0"?>\n<opnsense>\n  <system><hostname>fw</hostname></system>\n'
    b'  <revision><time>1764000000.12</time></revision>\n</opnsense>\n'
)


def name_of(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def test_custom_commands_with_files(plugin):
    cmds = plugin.parse_custom_commands(
        "file:/conf/config.xml\nfortigate.conf: file:sys_config\nget system status\nfile:/tmp/a b\n")
    assert [(c["file"], c["command"], c["download"]) for c in cmds] == [
        ("config.xml", "", "/conf/config.xml"),
        ("fortigate.conf", "", "sys_config"),
        ("get_system_status.txt", "get system status", ""),   # команда CLI, не файл
        ("tmp_a_b.txt", "", "/tmp/a b"),
    ]


def test_opnsense_profile_via_sftp(plugin, tmp_path):
    with FakeSSHServer(username="root", sftp_files={"/conf/config.xml": OPNSENSE_XML}) as srv:
        paths = plugin.run_backup(device(srv, tmp_path, profile="opnsense"))
        assert srv.received == []                           # ни одной команды, только SFTP
    saved = tmp_path / "backup" / "config.xml"
    assert paths == [str(saved.resolve())]
    assert saved.read_bytes() == OPNSENSE_XML               # байт в байт


def test_scp_fallback_with_shell_command(plugin, tmp_path):
    files = {"sys_config": b"#config-version=FGT\nconfig system global\nend\n"}
    with FakeSSHServer(commands={"get system status": "Version: FortiGate v7.2"},
                       exec_channel=scp_source(files)) as srv:
        paths = plugin.run_backup(device(
            srv, tmp_path, profile="custom", mode="shell",
            commands="status.txt: get system status\nfortigate.conf: file:sys_config"))
        assert "scp -f sys_config" in srv.received
    backup = tmp_path / "backup"
    assert [name_of(p) for p in paths] == ["status.txt", "fortigate.conf"]
    assert (backup / "fortigate.conf").read_bytes() == files["sys_config"]
    assert (backup / "status.txt").read_text(encoding="utf-8") == "Version: FortiGate v7.2\n"
    assert not list(backup.glob("*.part"))


def test_download_fails_both_ways(plugin, tmp_path):
    with FakeSSHServer(sftp_files={}, exec_channel=scp_source({})) as srv:
        with pytest.raises(PluginError) as err:
            plugin.run_backup(device(srv, tmp_path, profile="custom", commands="file:/nope.cfg"))
    text = str(err.value)
    assert "SFTP:" in text and "SCP:" in text and "/nope.cfg" in text
    assert not list((tmp_path / "backup").glob("*"))


@pytest.mark.parametrize("body, error", [
    (b"<html>Login</html>\n", "не похож"),
    (OPNSENSE_XML.replace(b"</opnsense>\n", b""), "оборван"),
    (b"  \n", "пустой"),
])
def test_bad_file_keeps_previous(plugin, tmp_path, body, error):
    saved = tmp_path / "backup" / "config.xml"
    saved.parent.mkdir()
    saved.write_bytes(b"previous")
    with FakeSSHServer(username="root", sftp_files={"/conf/config.xml": body}) as srv:
        with pytest.raises(PluginError, match=error):
            plugin.run_backup(device(srv, tmp_path, profile="opnsense"))
    assert saved.read_bytes() == b"previous"
    assert not list(saved.parent.glob("*.part"))


def test_failed_command_discards_downloaded_file(plugin, tmp_path):
    files = {"sys_config": b"#config-version=FGT\n"}
    with FakeSSHServer(exec_channel=scp_source(files)) as srv:
        with pytest.raises(PluginError, match="не приняло"):
            plugin.run_backup(device(
                srv, tmp_path, profile="custom", mode="shell",
                commands="show nothing\nfortigate.conf: file:sys_config"))
    assert not list((tmp_path / "backup").glob("*"))


def test_compare_normalizer_ignore(plugin, monkeypatch):
    profile = plugin.parse_profile({"commands": [
        {"download": "/conf/config.xml", "file": "config.xml", "ignore": [r"^\s*<time>"]},
        {"command": "show version", "file": "ver.txt"},
    ]}, "opn_test")
    monkeypatch.setitem(plugin.PROFILES, "opn_test", profile)
    norm = plugin.compare_normalizer({"profile": "opn_test"})
    a = b"<opnsense>\n  <time>1</time>\n  <x>1</x>\n</opnsense>\n"
    assert norm("config.xml", a) == norm("config.xml", a.replace(b"<time>1", b"<time>2"))
    assert norm("config.xml", a) != norm("config.xml", a.replace(b"<x>1", b"<x>2"))
    assert norm("ver.txt", a) == a
    # нет правил ignore или профиля — нормализатор не нужен
    assert plugin.compare_normalizer({"profile": "openwrt"}) is None
    assert plugin.compare_normalizer({"profile": "nope"}) is None


def test_unknown_profile(plugin, tmp_path):
    with pytest.raises(PluginError, match="Нет SSH-профиля «nope»"):
        plugin.run_backup({"profile": "nope", "host": "x", "backup_path": str(tmp_path)})


# ---------------------------------------------------------
# core/ssh: вход и ключ хоста
# ---------------------------------------------------------

def test_wrong_password(tmp_path):
    with FakeSSHServer() as srv:
        with pytest.raises(PluginError, match="неверный логин, пароль или ключ"):
            ssh.connect(device(srv, tmp_path, ssh_pass="bad"))


def test_no_password_no_key(tmp_path):
    with pytest.raises(PluginError, match="Нет ни пароля, ни SSH-ключа.*keys"):
        ssh.connect({"host": "127.0.0.1", "ssh_key": str(tmp_path / "keys" / "id_rsa.key")})


def test_no_connection(tmp_path):
    # Свободный порт, который никто не слушает
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(PluginError, match="нет связи|нет ответа"):
        ssh.connect({"host": "127.0.0.1", "port": port, "ssh_pass": "pw", "timeout": 3})


def test_host_key_change_rejected(tmp_path, known_hosts):
    with FakeSSHServer(key_index=0) as srv:
        ssh.connect(device(srv, tmp_path)).close()
        port = srv.port
    assert known_hosts.exists()
    # Тот же адрес и порт, другой ключ хоста
    srv2 = FakeSSHServer(key_index=1)
    try:
        dev = device(srv2, tmp_path)
        known = known_hosts.read_text().replace(f"[127.0.0.1]:{port}", f"[127.0.0.1]:{srv2.port}")
        known_hosts.write_text(known)
        with pytest.raises(PluginError, match="изменился"):
            ssh.connect(dev)
    finally:
        srv2.close()


def test_clean_terminal_codes():
    assert ssh.clean("\x1b[1;32mok\x1b[0m\r\nnext --More-- \x08\x08\x08   \x08\x08x") == "ok\nnext --More--x"
