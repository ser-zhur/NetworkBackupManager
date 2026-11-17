# =========================================================
# tests/test_oauth.py
# =========================================================
#
# Вход в учётную запись (OneDrive, Google Drive, ...) через
# rclone: поток входа, мастер rclone config, сохранение
# обновлённого токена, веб-форма.
#
# Настоящий rclone, но фальшивый токен — к Microsoft/Google
# не обращаемся (мастер Google Drive до выбора общего диска
# сеть не использует). config.json — временный.
#
# =========================================================

import glob
import json
import os
import tempfile

import pytest

from core import rclone as rclone_mod
from core import rclone_oauth
from core.storage import global_config
from core.storage.rclone_remote import RcloneRemote, _secure_storage

pytestmark = pytest.mark.skipif(
    not (rclone_mod.is_available() and rclone_mod.provider("drive")),
    reason="rclone не найден",
)

FAKE_TOKEN = ('{"access_token":"fake","token_type":"Bearer",'
              '"refresh_token":"fake-r","expiry":"2030-01-01T00:00:00Z"}')


def _oauth_tmpdirs():
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "nbm_oauth_*")))


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(global_config, "CONFIG_FILE", path)
    return path


# =========================================================
# Поток входа
# =========================================================

def test_is_oauth_type():
    assert rclone_mod.is_oauth_type("onedrive")
    assert rclone_mod.is_oauth_type("drive")
    assert not rclone_mod.is_oauth_type("webdav")


def test_pasted_token_runs_rclone_wizard_to_the_end():
    before = _oauth_tmpdirs()       # чужие (например, запущенной службы) не в счёт
    flow = rclone_oauth.start("drive", {}, token=FAKE_TOKEN)

    v = flow.view()
    assert v["state"] == "question"                      # общий диск?
    assert v["question"]["name"] == "config_change_team_drive"
    assert [e["value"] for e in v["question"]["examples"]] == ["true", "false"]

    flow.answer("false")
    v = flow.view()
    assert v["state"] == "done"
    assert json.loads(flow.result["token"])["refresh_token"] == "fake-r"
    assert _oauth_tmpdirs() <= before                    # временный конфиг удалён


def test_invalid_token_is_rejected():
    flow = rclone_oauth.start("drive", {}, token="не json")
    assert flow.view()["state"] == "error"
    assert "токен" in flow.view()["message"]


def test_browser_login_gives_link_and_can_be_cancelled():
    flow = rclone_oauth.start("onedrive", {})
    try:
        v = flow.view()
        assert v["state"] == "authorizing"
        assert v["url"].startswith("http://127.0.0.1:53682/auth?state=")
    finally:
        flow.cancel()
    assert flow.view()["state"] == "error"
    assert flow._proc is None


# =========================================================
# Работа хранилища: токен через временный конфиг
# =========================================================

def test_refreshed_token_is_persisted_encrypted(cfg_file):
    storage = _secure_storage()
    remote = {"id": "od", "name": "OD", "type": "drive", "base_dir": "/",
              "options": {"token": storage.encrypt(FAKE_TOKEN), "scope": "drive"}}
    global_config.save_remotes([remote])
    backend = RcloneRemote(remote)

    with backend._rclone_env(remote) as env:
        conf = env["RCLONE_CONFIG"]
        assert "RCLONE_CONFIG_NBM_TYPE" not in env           # через файл, не переменные
        section = rclone_mod.read_remote_section(conf)
        assert section["type"] == "drive" and section["token"] == FAKE_TOKEN
        # rclone обновил токен в своём файле
        new_token = FAKE_TOKEN.replace("fake-r", "fresh-r")
        text = open(conf, encoding="utf-8").read().replace(FAKE_TOKEN, new_token)
        open(conf, "w", encoding="utf-8").write(text)

    assert not os.path.exists(conf)                          # файл удалён
    saved = global_config.load_global()["od"]["options"]["token"]
    assert "fresh-r" not in saved                            # зашифрован
    assert storage.decrypt(saved) == new_token


def test_oauth_remote_needs_token_to_be_configured():
    b = RcloneRemote({"id": "od", "type": "onedrive"})
    assert b.is_configured_cfg({"type": "onedrive", "options": {}}) is False
    assert b.is_configured_cfg({"type": "onedrive", "options": {"token": "x"}}) is True


# =========================================================
# Веб
# =========================================================

@pytest.fixture
def client(cfg_file, monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s

    s.storage_manager.reload_remotes()
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app)
    s.app.dependency_overrides.clear()
    monkeypatch.undo()
    s.storage_manager.reload_remotes()


def test_edit_page_shows_login_block(client):
    s, c = client
    page = c.get("/settings/storage/edit?type=onedrive").text
    assert 'id="oauth-block"' in page and "Войти через браузер" in page
    assert 'rclone authorize "onedrive"' in page
    # client_secret — поле пароля (секрет не показывается)
    import re
    tag = re.search(r'<input[^>]*name="opt_client_secret"[^>]*>', page, re.S).group(0)
    assert 'type="password"' in tag


def test_save_without_login_is_refused(client):
    s, c = client
    r = c.post("/settings/storage/save", data={
        "id": "", "type": "drive", "name": "Диск", "base_dir": "/", "mode": "changes_only",
    })
    assert r.status_code == 400 and "нужен вход" in r.text
    assert global_config.load_remotes() == []


def test_api_flow_and_save_with_login_result(client):
    s, c = client
    v = c.post("/api/oauth/start", json={"type": "drive", "options": {}, "token": FAKE_TOKEN}).json()
    assert v["state"] == "question"
    v = c.post(f"/api/oauth/{v['id']}/answer", json={"result": "false"}).json()
    assert v["state"] == "done"

    r = c.post("/settings/storage/save", data={
        "id": "", "type": "drive", "name": "Google", "base_dir": "/Backups",
        "mode": "changes_only", "oauth_flow": v["id"],
        "opt_client_id": "my-id", "opt_client_secret": "my-secret",
    }, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]

    remote = global_config.load_remotes()[0]
    opts = remote["options"]
    storage = _secure_storage()
    assert storage.decrypt(opts["token"]) == json.dumps(json.loads(FAKE_TOKEN), separators=(",", ":"))
    assert storage.decrypt(opts["client_secret"]) == "my-secret"
    assert opts["client_id"] == "my-id"
    assert s.storage_manager.get(remote["id"]) is not None

    # Повторное сохранение формы токен не теряет
    rid = remote["id"]
    c.post("/settings/storage/save", data={
        "id": rid, "type": "drive", "name": "Google 2", "base_dir": "/Backups",
        "mode": "changes_only",
    }, follow_redirects=False)
    again = global_config.load_global()[rid]["options"]
    assert again["token"] == opts["token"] and again["client_secret"] == opts["client_secret"]

    page = c.get(f"/settings/storage/edit?id={rid}").text
    assert "Вход выполнен" in page and "my-secret" not in page


def test_api_rejects_non_oauth_type(client):
    s, c = client
    v = c.post("/api/oauth/start", json={"type": "webdav", "options": {}}).json()
    assert v["state"] == "error"


# =========================================================
# Ограничение частоты запросов (Google: rateLimitExceeded)
# =========================================================

_GOOGLE_RATE_LIMIT = """\
2026/11/17 19:35:05 ERROR : error listing: couldn't list directory: googleapi: Error 403: Quota exceeded for quota metric 'Queries' of service 'drive.googleapis.com'.
Details:
[
  {
    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
    "reason": "RATE_LIMIT_EXCEEDED"
  }
]
, rateLimitExceeded
2026/11/17 19:35:05 NOTICE: Failed to lsf with 2 errors: last error was: error in ListJSON: couldn't list directory: googleapi: Error 403: Quota exceeded for quota metric 'Queries' of service 'drive.googleapis.com'.
Details:
[
  {
    "reason": "RATE_LIMIT_EXCEEDED"
  }
]
, rateLimitExceeded
"""


def test_error_text_skips_json_details():
    from core.storage.rclone_remote import _last_lines
    text = _last_lines(_GOOGLE_RATE_LIMIT)
    assert text.startswith("error in ListJSON: couldn't list directory: googleapi: Error 403: Quota exceeded")
    assert "]" not in text and "2026/" not in text


def test_rate_limit_hint_suggests_own_client_id():
    from core.storage.rclone_remote import _hint
    assert "client_id" in _hint(_GOOGLE_RATE_LIMIT, "drive", {})
    assert "client_id" not in _hint(_GOOGLE_RATE_LIMIT, "drive", {"client_id": "x"})
    assert _hint("connection refused", "drive", {}) == ""


def test_cloud_types_get_more_retries():
    from core.storage.rclone_remote import _TEST_FLAGS, _flags
    assert _flags(_TEST_FLAGS, "webdav") == _TEST_FLAGS
    cloud = _flags(_TEST_FLAGS, "drive")
    assert cloud[cloud.index("--low-level-retries") + 1] == "10"
    assert _TEST_FLAGS[_TEST_FLAGS.index("--low-level-retries") + 1] == "1"   # исходный не тронут


def test_stale_wizard_tmpdirs_are_swept(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / "nbm_oauth_old"
    fresh = tmp_path / "nbm_oauth_new"
    other = tmp_path / "something_else"
    for p in (stale, fresh, other):
        p.mkdir()
        (p / "rclone.conf").write_text("token", encoding="utf-8")
    old = time.time() - rclone_oauth.FLOW_TTL_SECONDS - 60
    os.utime(stale, (old, old))
    os.utime(other, (old, old))

    rclone_oauth._gc()
    assert not stale.exists() and fresh.exists() and other.exists()
