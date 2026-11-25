# =========================================================
# tests/test_plugin_api.py — контракт плагинов v1
# =========================================================

import json
import re
import types

import pytest

from core import context, plugin_api
from core.plugin_api import PluginContractError


def module(**attrs):
    m = types.SimpleNamespace(run_backup=lambda device: [])
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


GOOD = {
    "api": 1, "title": "Тест", "auth": ["password"], "default_port": "2222",
    "fields": [
        {"name": "profile", "label": "Профиль", "type": "select", "choices": [["a", "A"], "b"]},
        {"name": "secret", "label": "Секрет", "type": "password"},
        {"name": "count", "label": "Число", "type": "number", "required": True},
        {"name": "flag", "label": "Флаг", "type": "checkbox"},
        {"name": "notes", "type": "textarea"},
    ],
}


def test_load_info():
    info = plugin_api.load_info(module(PLUGIN=GOOD), "My_Plugin")
    assert info.type == "my_plugin" and info.title == "Тест" and info.default_port == 2222
    assert info.uses_password and not info.uses_key
    f = info.get_field("profile")
    assert f.choices == (("a", "A"), ("b", "b")) and f.default == "a"
    assert info.get_field("secret").secret and info.get_field("flag").default is False
    assert info.get_field("notes").label == "notes"


@pytest.mark.parametrize("meta, error", [
    (None, "нет описания PLUGIN"),
    ({"api": 2}, "версии 2"),
    ({"api": 1, "auth": ["telnet"]}, "способ входа"),
    ({"api": 1, "fields": [{"name": "host"}]}, "занято ядром"),
    ({"api": 1, "fields": [{"name": "Bad-Name"}]}, "имя поля"),
    ({"api": 1, "fields": [{"name": "x", "type": "color"}]}, "неизвестный тип"),
    ({"api": 1, "fields": [{"name": "x", "type": "select"}]}, "нет вариантов"),
    ({"api": 1, "fields": [{"name": "x"}, {"name": "x"}]}, "повторяются"),
])
def test_contract_errors(meta, error):
    m = module() if meta is None else module(PLUGIN=meta)
    with pytest.raises(PluginContractError, match=error):
        plugin_api.load_info(m, "P")


def test_no_run_backup():
    with pytest.raises(PluginContractError, match="run_backup"):
        plugin_api.load_info(types.SimpleNamespace(PLUGIN={"api": 1}), "P")


def test_parse_field_values():
    info = plugin_api.load_info(module(PLUGIN=GOOD), "P")
    enc = lambda v: "enc:" + v
    form = {"opt_profile": "b", "opt_secret": "s3", "opt_count": "5", "opt_flag": "on",
            "opt_notes": "a\r\nb\r\n"}
    assert plugin_api.parse_field_values(info, form, {}, enc) == {
        "profile": "b", "secret": "enc:s3", "count": 5, "flag": True, "notes": "a\nb"}

    # Пустой пароль — прежнее значение; снятый флажок — False
    kept = plugin_api.parse_field_values(info, {"opt_count": "1"}, {"secret": "enc:old"}, enc)
    assert kept["secret"] == "enc:old" and kept["flag"] is False and "profile" not in kept

    with pytest.raises(ValueError, match="Не заполнено: Число"):
        plugin_api.parse_field_values(info, {}, {}, enc)
    with pytest.raises(ValueError, match="целое число"):
        plugin_api.parse_field_values(info, {"opt_count": "x"}, {}, enc)
    with pytest.raises(ValueError, match="недопустимое значение"):
        plugin_api.parse_field_values(info, {"opt_count": "1", "opt_profile": "zzz"}, {}, enc)


def test_decrypt_fields():
    info = plugin_api.load_info(module(PLUGIN=GOOD), "P")
    dev = {"secret": "enc:x", "profile": "a"}
    plugin_api.decrypt_fields(info, dev, lambda v: v.removeprefix("enc:"))
    assert dev == {"secret": "x", "profile": "a"}


# ---------------------------------------------------------
# Загрузка плагинов ядром
# ---------------------------------------------------------

def test_manager_loads_valid_and_reports_broken(tmp_path, monkeypatch):
    from core.plugin_manager import PluginManager

    monkeypatch.setattr(context, "DEVICES_FILE", tmp_path / "devices.json")
    plugins = tmp_path / "plugins"
    for name, body in {
        "Good": "PLUGIN = {'api': 1, 'title': 'Хороший'}\ndef run_backup(d): return []\n",
        "Old": "def run_backup(d): return []\n",
        "Crash": "raise ImportError('нет модуля foo')\n",
    }.items():
        (plugins / name).mkdir(parents=True)
        (plugins / name / "plugin.py").write_text(body, encoding="utf-8")

    pm = PluginManager()
    pm.plugins_dir = str(plugins)
    pm.discover_and_load_plugins()
    assert list(pm._plugins) == ["good"] and pm.plugin_info("good").title == "Хороший"
    assert set(pm.plugin_errors) == {"Old", "Crash"}
    assert "нет описания PLUGIN" in pm.plugin_errors["Old"]
    assert "нет модуля foo" in pm.plugin_errors["Crash"]


def test_shipped_plugins_follow_contract():
    from core.plugin_manager import PluginManager

    pm = PluginManager()
    pm.discover_and_load_plugins()
    assert pm.plugin_errors == {}
    assert {"mikrotik", "pfsense", "ssh", "fortigate"} <= set(pm._plugins)
    assert pm._plugins["ssh"]["normalizer_factory"] is not None


def test_normalizer_for_device(caplog):
    from core.plugin_manager import PluginManager
    from core.storage.base import identity_normalize

    general = lambda name, data: b"general"
    special = lambda name, data: b"special"

    def factory(device):
        if device.get("boom"):
            raise RuntimeError("сломано")
        return special if device.get("rules") else None

    plugin = {"normalize": general, "normalizer_factory": factory}
    pick = PluginManager._normalizer_for
    assert pick(plugin, {"rules": 1}) is special
    assert pick(plugin, {}) is general                      # фабрика вернула None
    assert pick(plugin, {"boom": 1, "name": "fw"}) is general
    assert "compare_normalizer (fw): сломано" in caplog.text
    assert pick({"normalize": general}, {}) is general
    assert pick(None, {}) is identity_normalize


# ---------------------------------------------------------
# Формы добавления и правки
# ---------------------------------------------------------

@pytest.fixture
def web(tmp_path, monkeypatch):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    monkeypatch.setattr(context, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setitem(s.plugin_manager._plugins["ssh"], "routers", [])
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app)
    s.app.dependency_overrides.clear()


FORM = {"device_type": "ssh", "ip": "192.0.2.10", "port": "2222", "username": "admin",
        "ssh_pass": "pw", "backup_path": "backups/sw1", "max_backups": "5",
        "opt_profile": "huawei_vrp", "opt_enable_pass": "EN-SECRET", "opt_mode": "shell"}


def test_add_page_lists_plugins_and_fields(web):
    _, client = web
    page = client.get("/add").text
    assert "SSH (универсальный)" in page and "MikroTik RouterOS" in page
    assert 'name="opt_profile"' in page and "Huawei VRP" in page
    assert 'data-plugin-block="ssh"' in page
    # Скрипт формы скрывает все [data-auth] не того способа входа:
    # на пунктах списка типов этого атрибута быть не должно, иначе
    # в списке остаётся только выбранный тип.
    assert not re.search(r"<option[^>]*\bdata-auth=", page)
    assert page.count("data-methods=") == len(re.findall(r'<option value="[^"]*" data-methods', page)) >= 4


def test_save_and_edit_ssh_device(web, tmp_path):
    s, client = web
    r = client.post("/save-router", data=FORM, follow_redirects=False)
    assert r.status_code == 303, r.text
    (dev,) = [d for d in json.loads((tmp_path / "devices.json").read_text(encoding="utf-8"))["devices"]
              if d["type"] == "ssh"]
    assert dev["profile"] == "huawei_vrp" and dev["port"] == 2222
    assert dev["enable_pass"] and "EN-SECRET" not in json.dumps(dev)
    assert s.storage.decrypt(dev["enable_pass"]) == "EN-SECRET"

    edit = client.get(f"/device/{dev['id']}/edit").text
    assert "SSH (универсальный)" in edit and "задан — оставьте пустым" in edit
    assert "EN-SECRET" not in edit and dev["enable_pass"] not in edit

    r = client.post(f"/device/{dev['id']}/update",
                    data={**FORM, "opt_enable_pass": "", "opt_profile": "cisco_ios", "active_field": "1", "active": "true"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    (dev2,) = [d for d in json.loads((tmp_path / "devices.json").read_text(encoding="utf-8"))["devices"]
               if d["type"] == "ssh"]
    assert dev2["profile"] == "cisco_ios" and dev2["enable_pass"] == dev["enable_pass"]


@pytest.mark.parametrize("bad, error", [
    ({"opt_profile": "nope"}, "недопустимое значение"),
    ({"port": "70000"}, "Порт SSH"),
])
def test_save_rejects_bad_values(web, bad, error):
    _, client = web
    r = client.post("/save-router", data={**FORM, **bad})
    assert r.status_code == 400 and error in r.text
