# =========================================================
# tests/test_ssh_keys.py — «Создать пару ключей» и шаги подготовки
# =========================================================

import paramiko
import pytest

from core import context, plugin_api, ssh_keys


@pytest.fixture
def keys_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "KEYS_DIR", tmp_path / "keys")
    return tmp_path / "keys"


def test_creates_pair_readable_by_paramiko(keys_dir):
    created, path, public = ssh_keys.ensure_key_pair("pfsense", "id_rsa.key")
    assert created and path == (keys_dir / "pfsense" / "id_rsa.key").resolve()
    assert path.read_bytes().startswith(b"-----BEGIN RSA PRIVATE KEY-----")

    key = paramiko.RSAKey.from_private_key_file(str(path))       # так читает плагин
    assert key.get_bits() == ssh_keys.KEY_BITS
    assert public == f"ssh-rsa {key.get_base64()} {ssh_keys.COMMENT}"
    assert (path.parent / "id_rsa.key.pub").read_text().strip() == public


def test_existing_key_is_never_overwritten(keys_dir):
    _, path, public = ssh_keys.ensure_key_pair("mikrotik", "r1.key")
    before = path.read_bytes()
    created, path2, public2 = ssh_keys.ensure_key_pair("mikrotik", "r1.key")
    assert not created and path2 == path
    assert path.read_bytes() == before and public2 == public


def test_existing_foreign_file_gives_clear_error(keys_dir):
    (keys_dir / "pfsense").mkdir(parents=True)
    (keys_dir / "pfsense" / "id_rsa.key").write_text("не ключ", encoding="utf-8")
    with pytest.raises(ssh_keys.SshKeyError, match="не закрытый SSH-ключ"):
        ssh_keys.ensure_key_pair("pfsense", "id_rsa.key")
    assert (keys_dir / "pfsense" / "id_rsa.key").read_text(encoding="utf-8") == "не ключ"


@pytest.mark.parametrize("name", ["../evil.key", "a/b.key", "C:\\x.key", "id_rsa.key.pub", ".hidden", "x" * 70])
def test_bad_names_rejected(keys_dir, name):
    with pytest.raises(ssh_keys.SshKeyError):
        ssh_keys.ensure_key_pair("pfsense", name)
    assert not keys_dir.exists() or not any(keys_dir.rglob("*"))


def test_default_name(keys_dir):
    _, path, _ = ssh_keys.ensure_key_pair("pfsense", "")
    assert path.name == "id_rsa.key"


def test_setup_steps_contract():
    class M:
        PLUGIN = {"api": 1, "setup": ["Шаг `один`", "  ", "Два"]}

        @staticmethod
        def run_backup(device):
            return []

    assert plugin_api.load_info(M, "X").setup == ("Шаг `один`", "Два")
    M.PLUGIN = {"api": 1, "setup": "строка, а не список"}
    with pytest.raises(plugin_api.PluginContractError):
        plugin_api.load_info(M, "X")


# ---------------------------------------------------------- веб

@pytest.fixture
def client(keys_dir):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    if not s.plugin_manager or "pfsense" not in s.plugin_manager._plugins:
        pytest.skip("плагин pfsense не загружен")
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app)
    s.app.dependency_overrides.clear()


def test_api_creates_then_shows_existing(client, keys_dir):
    s, c = client
    r = c.post("/api/ssh-keys", json={"type": "pfsense", "name": "fw1.key"}).json()
    assert r["ok"] and r["created"] and r["name"] == "fw1.key"
    assert r["public_key"].startswith("ssh-rsa ")
    assert (keys_dir / "pfsense" / "fw1.key").is_file()

    again = c.post("/api/ssh-keys", json={"type": "pfsense", "name": "fw1.key"}).json()
    assert again["ok"] and not again["created"] and again["public_key"] == r["public_key"]

    assert not c.post("/api/ssh-keys", json={"type": "nope", "name": "a.key"}).json()["ok"]
    assert not c.post("/api/ssh-keys", json={"type": "pfsense", "name": "../a"}).json()["ok"]


def test_add_page_shows_pfsense_setup_and_keygen(client):
    s, c = client
    page = c.get("/add").text
    assert "Как подготовить pfSense" in page
    assert "User - System: Shell account access" in page
    assert "<code" in page and "data/keys/pfsense/" in page
    assert "Создать пару ключей" in page and "/api/ssh-keys" in page


def test_inline_code_escapes_html(client):
    s, _ = client
    out = str(s._inline_code("<b>x</b> `<i>` y"))
    assert "&lt;b&gt;" in out and "<b>" not in out
    assert "<code" in out and "&lt;i&gt;" in out


def test_add_page_shows_mikrotik_setup(client):
    s, c = client
    if "mikrotik" not in s.plugin_manager._plugins:
        pytest.skip("плагин mikrotik не загружен")
    page = c.get("/add").text
    assert "Как подготовить MikroTik" in page
    assert "policy=ssh,ftp,read,write,sensitive,policy,test" in page
    assert "ssh-keys import" in page and "Скачать .pub" in page
