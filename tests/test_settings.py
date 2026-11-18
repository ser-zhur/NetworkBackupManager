# =========================================================
# tests/test_settings.py — настройки из окружения (NBM_*),
# пользователь из окружения, режим контейнера, systemd, rclone
# =========================================================

import json

import pytest

from core import auth, context, rclone, service_installer, settings


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"host": "0.0.0.0", "port": 9000}), encoding="utf-8")
    monkeypatch.setattr(context, "CONFIG_FILE", path)
    for name in ("NBM_HOST", "NBM_PORT", "NBM_OPEN_BROWSER", "NBM_CONTAINER",
                 "NBM_TRUSTED_PROXIES", "NBM_ADMIN_USER", "NBM_ADMIN_PASSWORD", "NBM_RCLONE"):
        monkeypatch.delenv(name, raising=False)
    return path


# ---- хост и порт ----

def test_web_config_from_file(cfg):
    assert settings.web_config() == ("0.0.0.0", 9000)


def test_env_overrides_file(cfg, monkeypatch):
    monkeypatch.setenv("NBM_HOST", "127.0.0.2")
    monkeypatch.setenv("NBM_PORT", "8181")
    assert settings.web_config() == ("127.0.0.2", 8181)


def test_bad_env_port_falls_back_to_file(cfg, monkeypatch):
    monkeypatch.setenv("NBM_PORT", "99999")
    assert settings.web_config() == ("0.0.0.0", 9000)


def test_defaults_without_config(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(context, "CONFIG_FILE", tmp_path / "none.json")
    assert settings.web_config() == ("127.0.0.1", 8080)


def test_service_installer_uses_same_config(cfg, monkeypatch):
    monkeypatch.setenv("NBM_PORT", "8282")
    assert service_installer.get_web_config() == ("0.0.0.0", 8282)


# ---- браузер и контейнер ----

def test_open_browser_flags(cfg, monkeypatch):
    monkeypatch.setenv("NBM_CONTAINER", "0")
    assert settings.open_browser() is True
    monkeypatch.setenv("NBM_OPEN_BROWSER", "0")
    assert settings.open_browser() is False
    monkeypatch.setenv("NBM_OPEN_BROWSER", "1")
    monkeypatch.setenv("NBM_CONTAINER", "1")
    assert settings.open_browser() is False            # в контейнере — никогда


def test_container_status_and_install(cfg, monkeypatch):
    monkeypatch.setenv("NBM_CONTAINER", "1")
    service_installer.invalidate_status_cache()
    try:
        st = service_installer.get_status(use_cache=False)
        assert st["container"] is True
        assert not any(st[k] for k in ("can_install", "can_start", "can_stop",
                                       "can_restart", "can_uninstall"))
        ok, msg = service_installer.install_service()
        assert ok is False and "Docker" in msg
    finally:
        service_installer.invalidate_status_cache()


def test_trusted_proxies(cfg, monkeypatch):
    assert settings.trusted_proxies() == ""
    monkeypatch.setenv("NBM_TRUSTED_PROXIES", " 10.0.0.1, 172.17.0.1 ,")
    assert settings.trusted_proxies() == "10.0.0.1,172.17.0.1"


# ---- пользователь из окружения ----

@pytest.fixture
def auth_file(tmp_path, monkeypatch):
    path = tmp_path / "data" / "auth.json"
    monkeypatch.setattr(context, "AUTH_FILE", path)
    return path


def test_user_from_env_created_once(cfg, auth_file, monkeypatch):
    assert auth.ensure_user_from_env() is False            # пароля нет — ничего

    monkeypatch.setenv("NBM_ADMIN_USER", "оператор")
    monkeypatch.setenv("NBM_ADMIN_PASSWORD", "секрет-1")
    assert auth.ensure_user_from_env() is True
    data = json.loads(auth_file.read_text(encoding="utf-8"))
    assert data["username"] == "оператор"
    assert auth.verify_password("секрет-1", data["password_hash"])
    assert not auth.verify_password("другой", data["password_hash"])

    monkeypatch.setenv("NBM_ADMIN_PASSWORD", "новый")       # существующего не трогаем
    assert auth.ensure_user_from_env() is False
    assert json.loads(auth_file.read_text(encoding="utf-8")) == data


def test_default_admin_login(cfg, auth_file, monkeypatch):
    monkeypatch.setenv("NBM_ADMIN_PASSWORD", "p")
    auth.ensure_user_from_env()
    assert json.loads(auth_file.read_text(encoding="utf-8"))["username"] == "admin"


# ---- systemd ----

def test_unit_environment_passes_nbm_vars_without_password(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("NBM_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NBM_HOME", "/var/lib/nbm")
    monkeypatch.setenv("NBM_X", 'a"b\\c%d')
    monkeypatch.setenv("NBM_ADMIN_PASSWORD", "secret")

    text = service_installer._unit_environment()

    assert 'Environment="NBM_HOME=/var/lib/nbm"\n' in text
    assert 'Environment="NBM_X=a\\"b\\\\c%%d"\n' in text
    assert "secret" not in text


# ---- rclone ----

def test_rclone_env_path_wins(cfg, monkeypatch, tmp_path):
    exe = tmp_path / "my-rclone"
    exe.write_text("")
    monkeypatch.setenv("NBM_RCLONE", str(exe))
    assert rclone.get_rclone_path() == str(exe)
    assert rclone.resolve_binary() == str(exe)


def test_rclone_bundled_in_bin_before_path(cfg, monkeypatch, tmp_path):
    import os

    name = "rclone.exe" if os.name == "nt" else "rclone"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / name).write_text("")
    monkeypatch.setattr(context, "BASE_DIR", tmp_path)
    assert rclone.resolve_binary() == str(tmp_path / "bin" / name)
