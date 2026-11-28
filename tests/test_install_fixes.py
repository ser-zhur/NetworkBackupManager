# =========================================================
# tests/test_install_fixes.py — установка и запуск
# =========================================================
#
# Запуск без прав на data/ и logs/ (ярлык на Windows); адрес
# 0.0.0.0; адрес службы для ярлыка; порт и пароль из
# --init-user; свои плагины в папке данных.
#
# =========================================================

import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core import auth, context, settings

ROOT = Path(__file__).resolve().parent.parent


def _env(home: Path, **extra) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith("NBM_")}
    e.update(NBM_HOME=str(home), PYTHONIOENCODING="utf-8", **extra)
    return e


# ---- 1. запуск без прав на папки данных ----

@pytest.mark.skipif(os.name != "nt", reason="права NTFS")
def test_version_without_access_to_data_and_logs(tmp_path):
    dirs = [tmp_path / d for d in ("data", "logs", "backups")]
    for d in dirs:
        d.mkdir()
    (tmp_path / "data" / "config.json").write_text("{}", encoding="utf-8")
    # Как делает установщик: только SYSTEM и Администраторы
    # (у процесса без повышения прав группа Администраторов не действует)
    for d in dirs:
        subprocess.run(["icacls", str(d), "/inheritance:r", "/grant:r",
                        "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "/Q"],
                       check=True, capture_output=True)
    try:
        try:
            (tmp_path / "logs" / "probe.txt").write_text("x")
            pytest.skip("процесс с правами администратора — проверка не имеет смысла")
        except PermissionError:
            pass
        r = subprocess.run([sys.executable, str(ROOT / "main.py"), "--version"],
                           cwd=ROOT, env=_env(tmp_path), capture_output=True, timeout=60)
        out = r.stdout.decode("utf-8", "replace")
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
        assert out.startswith("Network Backup Manager ")
    finally:
        for d in dirs:
            subprocess.run(["icacls", str(d), "/reset", "/T", "/C", "/Q"], capture_output=True)


def test_launcher_opens_address_written_by_service(tmp_path):
    """Ярлык без прав администратора: ничего не пишет в data/, открывает адрес службы."""
    addr = tmp_path / "web-address.txt"
    addr.write_text("https://127.0.0.1:8443\n", encoding="utf-8")
    code = textwrap.dedent(f"""
        import sys, json
        sys.argv = ["main.py"]
        import main
        from core import context
        context.WEB_ADDRESS_FILE = __import__("pathlib").Path({str(addr)!r})
        opened, elevated = [], []
        main.is_admin = lambda: False
        main.is_service_process = lambda: False
        main.is_service_registered = lambda: True
        import core.service_installer as si
        si.is_os_service_running = lambda: True
        main._url_reachable = lambda url: True
        main.webbrowser.open = lambda url, new=0: opened.append(url)
        main.relaunch_elevated = lambda args: elevated.append(args) or True
        main.ensure_default_files = lambda: (_ for _ in ()).throw(AssertionError("пишет в data"))
        rc = main.run_application()
        si.is_os_service_running = lambda: False
        rc2 = main.run_application()
        print(json.dumps([rc, opened, rc2, elevated]))
    """)
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=_env(tmp_path),
                       capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    rc, opened, rc2, elevated = json.loads(r.stdout.decode().strip().splitlines()[-1])
    assert rc == 0 and opened == ["https://127.0.0.1:8443"]
    assert rc2 == 0 and elevated == [["--start-service"]]       # служба стоит — запуск с UAC


def test_web_address_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "WEB_ADDRESS_FILE", tmp_path / "web-address.txt")
    assert settings.read_web_address() == ""
    settings.write_web_address("http://127.0.0.1:8090")
    assert settings.read_web_address() == "http://127.0.0.1:8090"
    (tmp_path / "web-address.txt").write_text("javascript:alert(1)", encoding="utf-8")
    assert settings.read_web_address() == ""


# ---- 2. 0.0.0.0 и :: ----

@pytest.mark.parametrize("host, url", [
    ("0.0.0.0", "http://127.0.0.1:8080"),
    ("", "http://127.0.0.1:8080"),
    ("::", "http://[::1]:8080"),
    ("192.0.2.5", "http://192.0.2.5:8080"),
    ("fe80::1", "http://[fe80::1]:8080"),
    ("localhost", "http://localhost:8080"),
])
def test_local_url(host, url):
    assert settings.local_url("http", host, 8080) == url


def test_wait_for_http_with_wildcard_host(monkeypatch):
    from core import service_installer

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    try:
        port = srv.getsockname()[1]
        monkeypatch.setattr(service_installer, "get_web_config", lambda: ("0.0.0.0", port))
        assert service_installer.wait_for_http(3) is True
    finally:
        srv.close()


# ---- 4, 9. --init-user: порт и длина пароля ----

def _init(home, **env):
    return subprocess.run([sys.executable, str(ROOT / "main.py"), "--init-user"],
                          cwd=ROOT, env=_env(home, **env), capture_output=True, timeout=60).returncode


def test_init_user_writes_port_and_checks_password(tmp_path):
    cfg = tmp_path / "data" / "config.json"
    assert _init(tmp_path, NBM_ADMIN_PASSWORD="short", NBM_PORT="8090") == 3
    assert not (tmp_path / "data" / "auth.json").exists()
    assert json.loads(cfg.read_text(encoding="utf-8"))["port"] == 8090

    assert _init(tmp_path, NBM_ADMIN_PASSWORD="long-enough", NBM_PORT="99999") == 4
    assert json.loads(cfg.read_text(encoding="utf-8"))["port"] == 8090
    assert _init(tmp_path, NBM_ADMIN_PASSWORD="long-enough") == 0
    assert (tmp_path / "data" / "auth.json").exists()


def test_short_env_password_not_used(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setenv("NBM_ADMIN_PASSWORD", "1234567")
    assert auth.ensure_user_from_env() is False
    assert not (tmp_path / "auth.json").exists()
    monkeypatch.setenv("NBM_ADMIN_PASSWORD", "12345678")
    assert auth.ensure_user_from_env() is True


# ---- 6. свои плагины в папке данных ----

def test_user_plugins_dir(tmp_path, monkeypatch):
    from core.plugin_manager import PluginManager

    monkeypatch.setattr(context, "DEVICES_FILE", tmp_path / "devices.json")
    body = "PLUGIN = {{'api': 1, 'title': '{}'}}\ndef run_backup(d): return []\n"
    builtin, user = tmp_path / "app" / "plugins", tmp_path / "home" / "plugins"
    for base, name, title in ((builtin, "Std", "встроенный"), (user, "Mine", "свой"),
                              (user, "STD", "подмена")):
        (base / name).mkdir(parents=True)
        (base / name / "plugin.py").write_text(body.format(title), encoding="utf-8")

    pm = PluginManager()
    pm.plugins_dir, pm.user_plugins_dir = str(builtin), str(user)
    pm.discover_and_load_plugins()
    assert sorted(pm._plugins) == ["mine", "std"]
    assert pm.plugin_info("std").title == "встроенный"
    assert list(pm.plugin_errors) == [str(user / "STD")]


def test_user_plugins_dir_only_when_data_separate():
    # В разработке и на Windows данные и код в одной папке
    if context.HOME_DIR == context.BASE_DIR:
        assert context.USER_PLUGINS_DIR is None
    else:
        assert context.USER_PLUGINS_DIR == context.HOME_DIR / "plugins"
