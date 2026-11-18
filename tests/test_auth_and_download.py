# =========================================================
# tests/test_auth_and_download.py
# =========================================================
#
# • Вход с не-ASCII логином/паролем (раньше: невозможен
#   или 500 вместо 401).
# • Скачивание файла с не-ASCII именем (раньше: 500 из-за
#   заголовка Content-Disposition).
#
# auth.json подменяется временным файлом.
#
# =========================================================

import base64
import hashlib
import json
import os

import pytest

pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
s = pytest.importorskip("web.server")

from fastapi.testclient import TestClient  # noqa: E402


def _hash(password: str) -> str:
    """Тот же формат, что у main.hash_password."""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 1000)
    return (
        f"pbkdf2_sha256$1000$"
        f"{base64.b64encode(salt).decode()}$"
        f"{base64.b64encode(key).decode()}"
    )


def _basic(raw: bytes) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    def make(username: str, password: str):
        auth = tmp_path / "auth.json"
        auth.write_text(json.dumps({
            "username": username,
            "password_hash": _hash(password),
        }, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(s, "AUTH_FILE", auth)
        return TestClient(s.app)
    return make


URL = "/api/backup-status"


# =========================================================
# Вход
# =========================================================

def test_cyrillic_login_and_password(client):
    c = client("админ", "пароль123")
    r = c.get(URL, headers=_basic("админ:пароль123".encode("utf-8")))
    assert r.status_code == 200


def test_ascii_login_still_works(client):
    c = client("admin", "secret")
    assert c.get(URL, headers=_basic(b"admin:secret")).status_code == 200


def test_wrong_password_is_401_with_charset(client):
    c = client("админ", "пароль123")
    r = c.get(URL, headers=_basic("админ:не тот".encode("utf-8")))
    assert r.status_code == 401
    assert 'charset="UTF-8"' in r.headers["www-authenticate"]


def test_ascii_login_against_cyrillic_user_is_401_not_500(client):
    c = client("админ", "пароль123")
    r = c.get(URL, headers=_basic(b"admin:x"))
    assert r.status_code == 401


def test_latin1_fallback(client):
    # Старые клиенты шлют Latin-1 — не UTF-8-последовательность
    c = client("café", "pw")
    r = c.get(URL, headers=_basic("café:pw".encode("latin-1")))
    assert r.status_code == 200


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer abc"},
    {"Authorization": "Basic !!!not-base64!!!"},
    _basic(b"no-colon-here"),
])
def test_malformed_auth_is_401(client, headers):
    c = client("admin", "secret")
    r = c.get(URL, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")


# =========================================================
# Content-Disposition
# =========================================================

def test_attachment_header_ascii():
    h = s._attachment_header("config.rsc")
    assert h == "attachment; filename=\"config.rsc\"; filename*=UTF-8''config.rsc"


def test_attachment_header_non_ascii_and_quotes():
    h = s._attachment_header('конфиг "1".rsc')
    h.encode("latin-1")                      # заголовок кодируем — не падает
    assert 'filename="______ _1_.rsc"' in h
    assert "filename*=UTF-8''%D0%BA%D0%BE%D0%BD%D1%84%D0%B8%D0%B3%20%221%22.rsc" in h


def test_download_non_ascii_file_from_history(tmp_path, monkeypatch):
    from core.git_storage import GitStorage

    repo = tmp_path / "repo"
    repo.mkdir()
    f = repo / "конфиг.rsc"
    f.write_bytes(b"/system identity\n")
    gs = GitStorage(repo)
    ok, created, _ = gs.commit_if_changed(
        files={"конфиг.rsc": f}, text_files=["конфиг.rsc"], message="t",
    )
    assert ok and created
    sha = gs.get_history()[0]["sha"]

    monkeypatch.setattr(s.plugin_manager, "_plugins", {
        "mikrotik": {"func": None, "routers": [
            {"id": "00000000000a", "host": "10.0.0.1", "backup_path": str(repo)},
        ], "dir": str(tmp_path)},
    })
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        r = TestClient(s.app).get(f"/device/00000000000a/git/download/{sha}/конфиг.rsc")
    finally:
        s.app.dependency_overrides.clear()

    assert r.status_code == 200
    assert r.content == b"/system identity\n"
    assert "filename*=UTF-8''%D0%BA" in r.headers["content-disposition"]
