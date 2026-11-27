# =========================================================
# tests/test_security.py — защита веб-интерфейса
# =========================================================
#
# Заголовки против кликджекинга, ограничение подбора
# пароля, проверка адреса и логина устройства (они идут
# аргументами ssh/scp), права на secret.key.
#
# =========================================================

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
s = pytest.importorskip("web.server")

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_auth_and_download import _hash  # noqa: E402

HTML = {"Accept": "text/html"}


@pytest.fixture
def auth(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"username": "admin", "password_hash": _hash("pw")}),
                    encoding="utf-8")
    monkeypatch.setattr(s, "AUTH_FILE", path)
    calls = []
    real = s.verify_password

    def counting(password, stored):
        calls.append(password)
        return real(password, stored)

    monkeypatch.setattr(s, "verify_password", counting)
    return TestClient(s.app), calls


# ---- заголовки ----

def test_security_headers_on_pages_errors_and_csrf(auth):
    c, _ = auth
    for r in (
        c.get("/api/health"),
        c.get("/no/such", auth=("admin", "pw"), headers=HTML),
        c.post("/settings/general/save", headers={"Origin": "http://evil.example"}),
    ):
        assert r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"


# ---- подбор пароля ----

def test_lockout_after_failures_and_reset_on_success(auth):
    c, calls = auth
    for _ in range(s.AUTH_MAX_FAILURES - 1):
        assert c.get("/api/backup-status", auth=("admin", "bad")).status_code == 401
    assert c.get("/api/backup-status", auth=("admin", "pw")).status_code == 200   # сброс

    for _ in range(s.AUTH_MAX_FAILURES):
        assert c.get("/api/backup-status", auth=("admin", "bad")).status_code == 401
    before = len(calls)
    r = c.get("/api/backup-status", auth=("admin", "pw"))
    assert r.status_code == 429 and r.headers["retry-after"] == str(s.AUTH_WINDOW_S)
    assert len(calls) == before                        # пароль даже не проверялся
    r = c.get("/", auth=("admin", "pw"), headers=HTML)
    assert r.status_code == 429 and "Вход временно заблокирован" in r.text


def test_lockout_expires(auth, monkeypatch):
    c, _ = auth
    now = [1000.0]
    monkeypatch.setattr(s.time, "monotonic", lambda: now[0])
    for _ in range(s.AUTH_MAX_FAILURES):
        c.get("/api/backup-status", auth=("admin", "bad"))
    assert c.get("/api/backup-status", auth=("admin", "pw")).status_code == 429
    now[0] += s.AUTH_WINDOW_S + 1
    assert c.get("/api/backup-status", auth=("admin", "pw")).status_code == 200


def test_correct_password_is_hashed_once(auth):
    c, calls = auth
    for _ in range(5):
        assert c.get("/api/backup-status", auth=("admin", "pw")).status_code == 200
    assert calls == ["pw"]


def test_wrong_username_still_hashes(auth):
    c, calls = auth
    assert c.get("/api/backup-status", auth=("root", "pw")).status_code == 401
    assert calls == ["pw"]                             # то же время ответа, что для admin


def test_password_change_invalidates_cache(auth):
    c, _ = auth
    assert c.get("/api/backup-status", auth=("admin", "pw")).status_code == 200
    s.AUTH_FILE.write_text(json.dumps({"username": "admin", "password_hash": _hash("new")}),
                           encoding="utf-8")
    assert c.get("/api/backup-status", auth=("admin", "pw")).status_code == 401
    assert c.get("/api/backup-status", auth=("admin", "new")).status_code == 200


# ---- адрес и логин устройства ----

@pytest.mark.parametrize("ip, username", [
    ("-oProxyCommand=calc", "admin"),
    ("192.0.2.1", "-oProxyCommand=calc"),
    ("192.0.2.1 -p 1", "admin"),
    ("192.0.2.1", "ad\nmin"),
    ("root@192.0.2.1", "admin"),
])
def test_bad_host_or_user_rejected(ip, username):
    form = {"device_type": "ssh", "ip": ip, "username": username, "backup_path": "x"}
    with pytest.raises(HTTPException) as exc:
        s._build_new_router(form)
    assert exc.value.status_code == 400


def test_normal_host_and_user_pass():
    for ip in ("192.0.2.1", "router.example.org", "2001:db8::1"):
        s._check_host_and_user(ip, "backup.user")


def test_mikrotik_ros6_refuses_option_like_values(tmp_path):
    path = Path(__file__).resolve().parent.parent / "plugins" / "Mikrotik" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_mt_plugin_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(ValueError, match="Недопустимый логин"):
        mod.backup_router_ros6({"host": "192.0.2.1", "username": "-oProxyCommand=x"}, str(tmp_path))
    with pytest.raises(ValueError, match="Недопустимый адрес"):
        mod.backup_router_ros6({"host": "-oProxyCommand=x"}, str(tmp_path))


# ---- secret.key ----

def test_secret_key_created_private(tmp_path):
    from core.secure_storage import SecureStorage

    st = SecureStorage(tmp_path)
    assert st.decrypt(st.encrypt("секрет")) == "секрет"
    assert SecureStorage(tmp_path).decrypt(st.encrypt("x")) == "x"   # тот же ключ
    if os.name != "nt":
        assert stat.S_IMODE((tmp_path / "secret.key").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="права POSIX")
def test_old_secret_key_gets_private(tmp_path):
    from cryptography.fernet import Fernet
    from core.secure_storage import SecureStorage

    key = tmp_path / "secret.key"
    key.write_bytes(Fernet.generate_key())
    key.chmod(0o644)
    SecureStorage(tmp_path)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
