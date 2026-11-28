# =========================================================
# core/auth.py — пользователь веб-интерфейса (data/auth.json)
# =========================================================
#
#     {"username": "admin", "password_hash": "pbkdf2_sha256$..."}
#
# Создаётся: окном --create-user (Windows), в консоли
# (--create-user без графики) или из окружения
# NBM_ADMIN_USER / NBM_ADMIN_PASSWORD при первом запуске
# (Docker).
#
# =========================================================

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets

from core import context

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 260000


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return (
        "pbkdf2_sha256$"
        f"{PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode()}$"
        f"{base64.b64encode(key).decode()}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, key_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        test = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            base64.b64decode(salt_b64), int(iterations),
        )
        return secrets.compare_digest(test, base64.b64decode(key_b64))
    except Exception:
        return False


# Одно правило для всех способов создать пользователя: окно и
# консоль --create-user, установщики, NBM_ADMIN_PASSWORD
MIN_PASS_CHARS = 8


def user_exists() -> bool:
    return context.AUTH_FILE.exists()


def save_user(username: str, password: str) -> None:
    context.AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    context.AUTH_FILE.write_text(
        json.dumps(
            {"username": username, "password_hash": hash_password(password)},
            ensure_ascii=False, indent=4,
        ),
        encoding="utf-8",
    )


def ensure_user_from_env() -> bool:
    """
    Нет data/auth.json, а в окружении есть NBM_ADMIN_PASSWORD —
    создаём пользователя. Существующего не меняем: пароль,
    сменённый потом в программе, переменная не перетрёт.
    """
    from core import settings

    creds = settings.admin_from_env()
    if creds is None or user_exists():
        return False
    if len(creds[1]) < MIN_PASS_CHARS:
        logger.error(
            "NBM_ADMIN_PASSWORD короче %s символов — пользователь не создан. "
            "Задайте пароль длиннее или создайте пользователя командой --create-user",
            MIN_PASS_CHARS,
        )
        return False
    save_user(*creds)
    logger.info("Пользователь %r создан из NBM_ADMIN_USER/NBM_ADMIN_PASSWORD", creds[0])
    return True
