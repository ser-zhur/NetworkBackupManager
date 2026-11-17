# =========================================================
# tests/test_tag_rows.py
# =========================================================
#
# Настройка «Строк тегов в фильтре» (config.json → tag_rows).
# Раскладку по строкам считает браузер; здесь — сервер:
# валидация, сохранение, передача в главную страницу.
#
# config.json подменяется временным файлом.
#
# =========================================================

import json
import re

import pytest

s = pytest.importorskip("web.server")


@pytest.mark.parametrize("raw, expected", [
    ("3", 3), (3, 3), ("0", 0), ("-5", 0), ("99", 20),
    ("", 2), (None, 2), ("abc", 2),
])
def test_clamp(raw, expected):
    assert s._clamp_tag_rows(raw) == expected


@pytest.fixture
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"retention": {"enabled": True}}), encoding="utf-8")
    monkeypatch.setattr(s, "CONFIG_FILE", cfg)
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield TestClient(s.app), cfg
    s.app.dependency_overrides.clear()


def _tag_rows_in_page(html: str) -> int:
    return int(re.search(r"const TAG_ROWS = (\d+);", html).group(1))


def test_saved_and_used_on_main_page(client):
    c, cfg = client

    r = c.post("/settings/general/save", data={
        "host": "127.0.0.1", "port": "8080",
        "max_parallel_backups": "5", "backup_timeout_seconds": "300",
        "tag_rows": "4",
    }, follow_redirects=False)
    assert r.status_code == 303

    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["tag_rows"] == 4
    assert saved["retention"] == {"enabled": True}       # остальное не потеряно

    assert _tag_rows_in_page(c.get("/").text) == 4
    assert 'name="tag_rows"' in c.get("/settings?tab=general").text


def test_default_when_not_configured(client):
    c, _ = client
    assert _tag_rows_in_page(c.get("/").text) == 2


def test_invalid_value_clamped_on_save(client):
    c, cfg = client
    c.post("/settings/general/save", data={
        "host": "127.0.0.1", "port": "8080",
        "max_parallel_backups": "5", "backup_timeout_seconds": "300",
        "tag_rows": "500",
    })
    assert json.loads(cfg.read_text(encoding="utf-8"))["tag_rows"] == 20
