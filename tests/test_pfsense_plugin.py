# =========================================================
# tests/test_pfsense_plugin.py
# =========================================================
#
# Плагин pfSense: соединение закрывается при любом исходе,
# оборванная загрузка не портит config.xml.
#
# SSH подменяется заглушкой — к роутерам не подключаемся.
#
# =========================================================

import importlib.util
import socket

import pytest

from core.context import PLUGINS_DIR


@pytest.fixture
def plugin(monkeypatch):
    path = PLUGINS_DIR / "Pfsense" / "plugin.py"
    if not path.exists():
        pytest.skip("плагин pfSense не найден")
    spec = importlib.util.spec_from_file_location("nbm_test_pfsense", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Ключ не читаем — подменяем загрузку
    monkeypatch.setattr(
        module.paramiko.RSAKey, "from_private_key_file",
        staticmethod(lambda p: object()),
    )
    return module


class FakeSFTP:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, remote, local):
        if self.behaviour == "partial":
            with open(local, "wb") as f:
                f.write(b"<pfsense><trunc")
            raise socket.error("connection reset")
        with open(local, "wb") as f:
            f.write(b"<pfsense>new</pfsense>")


def make_ssh(behaviour, closed):
    import paramiko

    class FakeSSH:
        def __init__(self): self._keys = paramiko.HostKeys()
        def get_host_keys(self): return self._keys
        def set_missing_host_key_policy(self, p): self.policy = p
        def connect(self, **kw):
            if behaviour == "connect_fail":
                raise socket.timeout("timed out")
        def open_sftp(self): return FakeSFTP(behaviour)
        def close(self): closed.append(True)
    return FakeSSH


@pytest.fixture
def router(tmp_path):
    key = tmp_path / "id.key"
    key.write_text("k")
    return {"host": "10.0.0.1", "ssh_key": str(key), "backup_path": str(tmp_path / "bk")}


def test_success_replaces_config_and_closes(plugin, router, monkeypatch, tmp_path):
    closed = []
    monkeypatch.setattr(plugin.paramiko, "SSHClient", make_ssh("ok", closed))

    files = plugin.run_backup(router)

    cfg = tmp_path / "bk" / "config.xml"
    assert files == [str(cfg)]
    assert cfg.read_bytes() == b"<pfsense>new</pfsense>"
    assert not (tmp_path / "bk" / "config.xml.part").exists()
    assert closed == [True]


def test_partial_download_keeps_old_config_and_closes(plugin, router, monkeypatch, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "config.xml").write_bytes(b"<pfsense>old</pfsense>")
    closed = []
    monkeypatch.setattr(plugin.paramiko, "SSHClient", make_ssh("partial", closed))

    with pytest.raises(socket.error):
        plugin.run_backup(router)

    assert (bk / "config.xml").read_bytes() == b"<pfsense>old</pfsense>"
    assert not (bk / "config.xml.part").exists()
    assert closed == [True]


def test_connect_failure_closes(plugin, router, monkeypatch):
    closed = []
    monkeypatch.setattr(plugin.paramiko, "SSHClient", make_ssh("connect_fail", closed))

    with pytest.raises(socket.error):
        plugin.run_backup(router)

    assert closed == [True]
