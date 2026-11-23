# =========================================================
# tests/test_init_user.py — main.py --init-user (установщик Windows)
# =========================================================

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(home: Path, **env) -> int:
    e = {k: v for k, v in os.environ.items() if not k.startswith("NBM_")}
    e.update(NBM_HOME=str(home), PYTHONIOENCODING="utf-8", **env)
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--init-user"],
        cwd=ROOT, env=e, capture_output=True, timeout=60,
    ).returncode


def test_init_user(tmp_path):
    from core import auth

    auth_file = tmp_path / "data" / "auth.json"

    # Пароль не задан — пользователя нет, код 2
    assert run(tmp_path) == 2
    assert not auth_file.exists()

    assert run(tmp_path, NBM_ADMIN_USER="ops", NBM_ADMIN_PASSWORD="s3cret pass") == 0
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["username"] == "ops"
    assert auth.verify_password("s3cret pass", saved["password_hash"])
    assert "s3cret" not in auth_file.read_text(encoding="utf-8")

    # Повторный запуск (обновление) существующего не меняет
    assert run(tmp_path, NBM_ADMIN_USER="x", NBM_ADMIN_PASSWORD="other") == 0
    assert json.loads(auth_file.read_text(encoding="utf-8")) == saved
