# =========================================================
# tests/test_csv_import.py — импорт устройств из CSV
# =========================================================

import base64
import re

import pytest

from core import csv_import
from core.plugin_api import PluginInfo, DeviceField


SSH = PluginInfo(
    type="ssh", title="Cisco IOS / SSH",
    fields=(
        DeviceField("profile", "Профиль", "select", default="cisco_ios",
                    choices=(("cisco_ios", "Cisco IOS"), ("vyos", "VyOS"))),
        DeviceField("legacy", "Старые шифры", "checkbox", default=False),
    ),
)


# ---------------------------------------------------------- разбор файла

def test_semicolon_cp1251_aliases_and_comments():
    text = (
        "# список офиса\n"
        "Тип;Название;Адрес;Логин;Пароль;Теги\n"
        "\n"
        "ssh;Ядро;192.0.2.10;root;p;a|b\n"
        "# ssh;пропуск;192.0.2.99\n"
    )
    rows = csv_import.read_rows(csv_import.decode(text.encode("cp1251")))
    assert rows == [(4, {"type": "ssh", "name": "Ядро", "ip": "192.0.2.10",
                         "username": "root", "password": "p", "tags": "a|b"})]


def test_comma_utf8_bom_and_quoted():
    data = '﻿type,ip,tags,opt_profile\nssh,192.0.2.1,"x, y",vyos\n'.encode("utf-8")
    ((line, row),) = csv_import.read_rows(csv_import.decode(data))
    assert row == {"type": "ssh", "ip": "192.0.2.1", "tags": "x, y", "profile": "vyos"}


def test_missing_header_and_limits():
    with pytest.raises(csv_import.CsvImportError):
        csv_import.read_rows("192.0.2.1;ssh\n")
    with pytest.raises(csv_import.CsvImportError):
        csv_import.read_rows("")
    with pytest.raises(csv_import.CsvImportError):
        csv_import.decode(b"x" * (csv_import.MAX_BYTES + 1))
    many = "type;ip\n" + "ssh;192.0.2.1\n" * (csv_import.MAX_ROWS + 1)
    with pytest.raises(csv_import.CsvImportError):
        csv_import.read_rows(many)


def test_find_type_by_key_or_title():
    assert csv_import.find_type("SSH", [SSH]) == "ssh"
    assert csv_import.find_type("cisco ios / ssh", [SSH]) == "ssh"
    assert csv_import.find_type("juniper", [SSH]) is None


def test_to_form_defaults_and_choices():
    form = csv_import.to_form(
        {"ip": "192.0.2.5", "name": "Склад №2", "profile": "VyOS", "legacy": "да",
         "tags": "a|b"},
        SSH, {"git": {"enabled": True}, "ftp": {"enabled": False}},
    )
    assert form["opt_profile"] == "vyos"            # по подписи варианта
    assert form["opt_legacy"] == "on"
    assert form["backup_path"] == "backups/Склад_2"
    assert form["port"] == "22" and form["username"] == "admin"
    assert form["tags"] == "a,b"
    assert form["storage_git_enabled"] == "true" and "storage_ftp_enabled" not in form

    form = csv_import.to_form({"ip": "192.0.2.5", "profile": ""}, SSH, {})
    assert form["opt_profile"] == "cisco_ios" and "opt_legacy" not in form
    assert form["backup_path"] == "backups/192.0.2.5"


def test_template_has_columns_and_examples():
    text = csv_import.template_csv([SSH])
    header, example = text.splitlines()[:2]
    assert header.split(";")[:2] == ["type", "ip"]
    assert "profile" in header.split(";")
    rows = csv_import.read_rows(text)
    assert rows[0][1]["type"] == "ssh"


# ---------------------------------------------------------- веб

@pytest.fixture
def web(tmp_path, monkeypatch):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    if not s.plugin_manager or "ssh" not in s.plugin_manager._plugins:
        pytest.skip("плагин ssh не загружен")

    saved = []
    monkeypatch.setattr(s.plugin_manager, "save_routers", saved.append)
    existing = {"host": "192.0.2.1", "ip": "192.0.2.1", "name": "Old",
                "backup_path": str(tmp_path / "backups" / "Old")}
    for pd in s.plugin_manager._plugins.values():
        monkeypatch.setitem(pd, "routers", [])
    monkeypatch.setitem(s.plugin_manager._plugins["ssh"], "routers", [existing])
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app), saved
    s.app.dependency_overrides.clear()


CSV = (
    "type;name;ip;username;password;profile;tags;disabled\n"
    "ssh;SW1;192.0.2.10;admin;secret;cisco_ios;Офис|Ядро;\n"
    "ssh;Dup;192.0.2.1;admin;x;;;\n"                 # уже есть
    "unknown;X;192.0.2.11;;;;;\n"                     # нет такого типа
    "ssh;SW2;192.0.2.12;admin;;bad_profile;;\n"       # плохой профиль
    "ssh;SW3;192.0.2.13;admin;p2;;;да\n"
    "ssh;SW3copy;192.0.2.13;admin;p2;;;\n"            # повтор в файле
)


def test_preview_then_apply_selected(web):
    s, client, saved = web
    r = client.post("/import/preview", files={"file": ("d.csv", CSV.encode("utf-8"), "text/csv")})
    assert r.status_code == 200
    page = r.text
    assert "Будет добавлено" in page
    assert "Уже есть: Old" in page
    assert "Неизвестный тип «unknown»" in page
    assert "bad_profile" in page
    assert "Повторяется в файле (строка 6)" in page
    lines = re.findall(r'name="line" value="(\d+)"', page)
    assert lines == ["2", "6"]
    # Ничего не сохранено до подтверждения
    assert saved == [] and len(s.plugin_manager._plugins["ssh"]["routers"]) == 1

    b64 = re.search(r'name="csv_b64" value="([^"]+)"', page).group(1)
    r = client.post("/import/apply", data={"csv_b64": b64, "line": ["2", "6", "3", "4"]})
    assert r.status_code == 200 and "Добавлено устройств: 2" in r.text

    routers = s.plugin_manager._plugins["ssh"]["routers"]
    assert [x.get("name") for x in routers] == ["Old", "SW1", "SW3"]
    sw1, sw3 = routers[1], routers[2]
    assert sw1["ssh_pass"] and sw1["ssh_pass"] != "secret"          # зашифрован
    assert s.storage.decrypt(sw1["ssh_pass"]) == "secret"
    assert sw1["tags"] == ["Офис", "Ядро"] and sw1["profile"] == "cisco_ios"
    assert sw1["backup_path"].replace("\\", "/").endswith("backups/SW1")
    assert sw3.get("disabled") is True and not sw1.get("disabled")
    assert saved == ["ssh"]

    # Повторный импорт того же файла ничего не добавляет
    r = client.post("/import/apply", data={"csv_b64": b64, "line": ["2", "6"]})
    assert "Ничего не добавлено" in r.text
    assert len(routers) == 3


def test_pasted_text_and_bad_file(web):
    s, client, saved = web
    r = client.post("/import/preview", data={"text": "type;ip\nssh;192.0.2.20\n"})
    assert 'name="line" value="2"' in r.text
    r = client.post("/import/preview", data={"text": "просто текст"})
    assert "минимум type и ip" in r.text
    r = client.post("/import/apply", data={"csv_b64": base64.b64encode(b"\xff\xfe").decode()})
    assert "Не удалось прочитать список" in r.text


def test_pages(web):
    s, client, saved = web
    page = client.get("/import").text
    assert 'action="/import/preview"' in page and "profile" in page
    r = client.get("/import/template.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert r.content.startswith(b"\xef\xbb\xbftype;ip;")
    assert 'href="/import"' in client.get("/add").text
