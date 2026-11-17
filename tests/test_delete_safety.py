# =========================================================
# tests/test_delete_safety.py
# =========================================================
#
# Удаление устройства: какие каталоги бэкапов и какие
# SSH-ключи можно удалять.
#
# Список устройств подменяется фиктивным, state.json —
# временным файлом. Реальные данные не трогаются.
#
# =========================================================

import json

import pytest

s = pytest.importorskip("web.server")


@pytest.fixture
def fake_devices(monkeypatch, tmp_path):
    """Подменяет список устройств; возвращает функцию-конструктор."""
    if s.plugin_manager is None:
        pytest.skip("PluginManager не загружен")

    plugin_dir = tmp_path / "FakePlugin"
    plugin_dir.mkdir()
    plugins = {
        "fake": {
            "func": lambda r: [],
            "routers": [],
            "normalize": None,
            "dir": str(plugin_dir),
        }
    }
    monkeypatch.setattr(s.plugin_manager, "_plugins", plugins)

    def add(name, backup_path, **extra):
        router = {"host": name, "name": name, "backup_path": str(backup_path), **extra}
        plugins["fake"]["routers"].append(router)
        return router

    add.plugin_dir = plugin_dir
    return add


# =========================================================
# _is_safe_to_delete
# =========================================================

def test_unrelated_dir_is_safe(fake_devices, tmp_path):
    d = tmp_path / "dev"
    d.mkdir()
    assert s._is_safe_to_delete(d)[0] is True


@pytest.mark.parametrize("target", [
    lambda: s.BASE_DIR,
    lambda: s.BASE_DIR.parent,          # родитель приложения
    lambda: s.DATA_DIR,
    lambda: s.PLUGINS_DIR,
])
def test_app_dirs_and_parents_unsafe(fake_devices, target):
    path = target()
    if not path.exists():
        pytest.skip(f"{path} нет на диске")
    safe, reason = s._is_safe_to_delete(path)
    assert safe is False, reason


def test_shared_backups_root_unsafe(fake_devices):
    if not s.BACKUPS_DIR.exists():
        pytest.skip("нет общей папки бэкапов")
    assert s._is_safe_to_delete(s.BACKUPS_DIR)[0] is False


def test_same_dir_as_other_device_unsafe(fake_devices, tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    me = fake_devices("A", d)
    fake_devices("B", d)
    safe, reason = s._is_safe_to_delete(d, exclude_router=me)
    assert safe is False
    assert "«B»" in reason


def test_parent_of_other_device_unsafe(fake_devices, tmp_path):
    parent = tmp_path / "parent"
    (parent / "child").mkdir(parents=True)
    me = fake_devices("A", parent)
    fake_devices("B", parent / "child")
    assert s._is_safe_to_delete(parent, exclude_router=me)[0] is False


def test_inside_other_device_unsafe(fake_devices, tmp_path):
    parent = tmp_path / "parent"
    (parent / "child").mkdir(parents=True)
    fake_devices("A", parent)
    me = fake_devices("B", parent / "child")
    assert s._is_safe_to_delete(parent / "child", exclude_router=me)[0] is False


def test_own_dir_is_safe_when_excluded(fake_devices, tmp_path):
    d = tmp_path / "own"
    d.mkdir()
    me = fake_devices("A", d)
    assert s._is_safe_to_delete(d, exclude_router=me)[0] is True


# =========================================================
# _local_key_path
# =========================================================

def test_key_inside_plugin_dir(fake_devices):
    assert s._local_key_path("fake", "id_rsa.key") == (
        fake_devices.plugin_dir / "id_rsa.key"
    ).resolve()
    assert s._local_key_path("fake", "keys/../id.key") == (
        fake_devices.plugin_dir / "id.key"
    ).resolve()


@pytest.mark.parametrize("name", [
    "../x.key",
    "../../secret.key",
    "..",
    "",
    "plugin.py",
    "routers.json",
])
def test_key_outside_or_not_a_key_rejected(fake_devices, name):
    assert s._local_key_path("fake", name) is None


def test_absolute_key_rejected(fake_devices, tmp_path):
    assert s._local_key_path("fake", str(tmp_path / "k.key")) is None


# =========================================================
# Удаление через HTTP
# =========================================================

@pytest.fixture
def client(monkeypatch, tmp_path, fake_devices):
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(s, "STATE_FILE", state)
    monkeypatch.setattr(s.plugin_manager, "save_routers", lambda t: None)
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        yield TestClient(s.app)
    finally:
        s.app.dependency_overrides.clear()


def test_delete_keeps_dir_containing_other_device(client, fake_devices, tmp_path):
    parent = tmp_path / "parent"
    (parent / "child").mkdir(parents=True)
    (parent / "child" / "config.rsc").write_text("x")
    fake_devices("A", parent)
    fake_devices("B", parent / "child")

    r = client.post("/delete-device/fake/0", data={}, follow_redirects=False)

    assert r.status_code == 303
    assert (parent / "child" / "config.rsc").exists()     # бэкапы B целы
    assert [d["name"] for d in s.plugin_manager._plugins["fake"]["routers"]] == ["B"]


def test_delete_removes_own_dir_and_local_key(client, fake_devices, tmp_path):
    own = tmp_path / "own"
    own.mkdir()
    (own / "config.rsc").write_text("x")
    key = fake_devices.plugin_dir / "dev.key"
    key.write_text("k")
    fake_devices("A", own, ssh_key="dev.key")

    r = client.post(
        "/delete-device/fake/0", data={"remove_key": "true"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert not own.exists()
    assert not key.exists()


def test_delete_does_not_remove_key_outside_plugin(client, fake_devices, tmp_path):
    outside = tmp_path / "outside.key"
    outside.write_text("k")
    own = tmp_path / "own2"
    own.mkdir()
    fake_devices("A", own, ssh_key="../outside.key")

    r = client.post(
        "/delete-device/fake/0", data={"remove_key": "true"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert outside.exists()
