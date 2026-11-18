# =========================================================
# tests/test_home_dir.py — папка данных (NBM_HOME) и secret.key
# =========================================================

import json
import os
import subprocess
import sys
from pathlib import Path

from core import context
from core import secure_storage as ss

ROOT = Path(__file__).resolve().parent.parent

_PROBE = (
    "import json; from core import context as c; "
    "print(json.dumps({k: str(getattr(c, k)) for k in "
    "('BASE_DIR','HOME_DIR','DATA_DIR','LOGS_DIR','BACKUPS_DIR',"
    "'PLUGINS_DIR','WEB_DIR','SECRET_KEY_FILE')} | "
    "{'rel': str(c.resolve_backup_path('backups/x'))}))"
)


def _probe(env_home):
    env = dict(os.environ)
    env.pop("NBM_HOME", None)
    if env_home is not None:
        env["NBM_HOME"] = env_home
    r = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return {k: Path(v) for k, v in json.loads(r.stdout).items()}


def test_default_home_is_app_dir():
    p = _probe(None)
    assert p["HOME_DIR"] == p["BASE_DIR"]
    assert p["DATA_DIR"] == p["BASE_DIR"] / "data"
    assert p["SECRET_KEY_FILE"] == p["BASE_DIR"] / "data" / "secret.key"


def test_nbm_home_moves_data_not_code(tmp_path):
    home = tmp_path / "nbm home"
    p = _probe(str(home))
    home = home.resolve()
    assert p["HOME_DIR"] == home
    assert p["DATA_DIR"] == home / "data"
    assert p["LOGS_DIR"] == home / "logs"
    assert p["BACKUPS_DIR"] == home / "backups"
    assert p["SECRET_KEY_FILE"] == home / "data" / "secret.key"
    assert p["rel"] == home / "backups" / "x"
    # Код — по-прежнему в папке программы
    assert p["PLUGINS_DIR"] == p["BASE_DIR"] / "plugins"
    assert p["WEB_DIR"] == p["BASE_DIR"] / "web"


def _isolate(monkeypatch, tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    key_file = tmp_path / "home" / "data" / "secret.key"
    monkeypatch.setattr(context, "BASE_DIR", app)
    monkeypatch.setattr(context, "SECRET_KEY_FILE", key_file)
    monkeypatch.setattr(ss, "_instances", {})
    return app, key_file


def test_get_secure_storage_is_shared_and_creates_key(monkeypatch, tmp_path):
    _, key_file = _isolate(monkeypatch, tmp_path)

    a = ss.get_secure_storage()
    assert ss.get_secure_storage() is a
    assert key_file.is_file()
    assert a.decrypt(a.encrypt("пароль")) == "пароль"


def test_legacy_key_is_copied_to_data(monkeypatch, tmp_path):
    app, key_file = _isolate(monkeypatch, tmp_path)
    old = ss.SecureStorage(app)                # старый ключ в корне
    secret = old.encrypt("пароль устройства")

    new = ss.get_secure_storage()

    assert key_file.read_bytes() == (app / "secret.key").read_bytes()
    assert new.decrypt(secret) == "пароль устройства"
    # Копия, не перенос
    assert (app / "secret.key").is_file()


def test_existing_data_key_wins_over_legacy(monkeypatch, tmp_path):
    app, key_file = _isolate(monkeypatch, tmp_path)
    ss.SecureStorage(app)
    current = ss.SecureStorage(key_file.parent)
    before = key_file.read_bytes()

    st = ss.get_secure_storage()

    assert key_file.read_bytes() == before
    assert st.decrypt(current.encrypt("x")) == "x"
