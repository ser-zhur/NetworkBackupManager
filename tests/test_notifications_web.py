# =========================================================
# tests/test_notifications_web.py — вкладка «Уведомления»
# =========================================================

import json

import pytest

from core import context, notifications, notify_channels


@pytest.fixture
def client(tmp_path, monkeypatch):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    notifications.dispatcher.status.clear()
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield TestClient(s.app)
    s.app.dependency_overrides.clear()


def _stored(tmp_path):
    return json.loads((tmp_path / "notifications.json").read_text(encoding="utf-8"))["channels"]


FORM = {"type": "telegram", "id": "", "name": "Админы", "enabled": "on",
        "f_bot_token": "123:SECRET", "f_chat_id": "42",
        "errors": "on", "recovered": "on", "config": "on", "run": "always"}


def test_empty_tab_and_new_form(client):
    page = client.get("/settings?tab=notifications").text
    assert "Каналов пока нет" in page and "tab=notifications&new=telegram" in page
    form = client.get("/settings?tab=notifications&new=email").text
    assert 'name="f_host"' in form and 'name="f_password"' in form and "STARTTLS" in form


def test_validation_error_shows_form_again(client, tmp_path):
    r = client.post("/settings/notifications/save", data={**FORM, "f_chat_id": ""})
    assert r.status_code == 200 and "Не заполнено: ID чата" in r.text
    assert 'value="Админы"' in r.text
    assert "SECRET" not in r.text                     # секрет обратно не выводится
    assert not (tmp_path / "notifications.json").exists()


def test_save_edit_toggle_test_delete(client, tmp_path, monkeypatch):
    r = client.post("/settings/notifications/save", data=FORM, follow_redirects=False)
    assert r.status_code == 303
    (ch,) = _stored(tmp_path)
    assert ch["name"] == "Админы" and ch["settings"]["chat_id"] == "42"
    assert "SECRET" not in json.dumps(ch)            # токен зашифрован
    assert ch["rules"]["run"] == "always" and ch["rules"]["app"] is False

    page = client.get("/settings?tab=notifications").text
    assert "Админы" in page and "итог каждого прогона" in page

    edit = client.get(f"/settings?tab=notifications&edit={ch['id']}").text
    assert "задан — оставьте пустым" in edit and "SECRET" not in edit

    # Правка без токена — прежний токен сохраняется
    client.post("/settings/notifications/save",
                data={**FORM, "id": ch["id"], "f_bot_token": "", "name": "NOC"})
    (ch2,) = _stored(tmp_path)
    assert ch2["name"] == "NOC" and ch2["settings"]["bot_token"] == ch["settings"]["bot_token"]

    client.post(f"/settings/notifications/{ch['id']}/toggle")
    assert _stored(tmp_path)[0]["enabled"] is False

    sent = []
    monkeypatch.setattr(notify_channels, "_http_post",
                        lambda url, payload, headers=None: sent.append((url, payload)) or (200, b'{"ok":true}'))
    r = client.post(f"/settings/notifications/{ch['id']}/test")
    assert "отправлено" in r.text
    assert sent[0][0].endswith("/bot123:SECRET/sendMessage") and "Проверка" in sent[0][1]["text"]
    assert "✓ отправлено" in r.text                   # статус канала в списке

    monkeypatch.setattr(notify_channels, "_http_post",
                        lambda *a, **k: (401, b'{"ok":false}'))
    r = client.post(f"/settings/notifications/{ch['id']}/test")
    assert "неверный токен бота" in r.text

    client.post(f"/settings/notifications/{ch['id']}/delete")
    assert _stored(tmp_path) == []


def test_unknown_type_rejected(client):
    r = client.post("/settings/notifications/save", data={**FORM, "type": "pigeon"})
    assert r.status_code == 400
