# =========================================================
# core/ssh_keys.py — пара SSH-ключей для входа на устройство
# =========================================================
#
# Кнопка «Создать пару ключей» в форме устройства: закрытый
# ключ остаётся в data/keys/<тип>/<имя>, открытый (одна строка
# «ssh-rsa AAAA… комментарий») показывается, чтобы вставить его
# на устройство (pfSense: «Authorized SSH Keys»).
#
# RSA 2048: его понимают все поддерживаемые устройства, включая
# RouterOS 6; плагины pfSense и MikroTik читают именно RSA.
# Закрытый ключ — PEM без пароля: его читают и paramiko, и
# системный ssh (MikroTik на RouterOS 6).
#
# Существующий файл никогда не перезаписывается: для него
# только вычисляется открытый ключ.
#
# =========================================================

from __future__ import annotations

import os
import re
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core import devices

KEY_BITS = 2048
COMMENT = "network-backup-manager"

# Только имя файла: латиница, цифры, «.», «_», «-»
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SshKeyError(ValueError):
    """Понятная пользователю ошибка (имя, чужой формат ключа)."""


def key_path(device_type: str, name: str) -> Path:
    name = (name or devices.DEFAULT_KEY_NAME).strip()
    if not _NAME_RE.match(name) or name.endswith(".pub"):
        raise SshKeyError(
            "Имя файла ключа: латиница, цифры, «.», «_», «-» (до 64 символов), "
            "без пути и не .pub"
        )
    path = devices.local_key_path(device_type, name)
    if path is None:
        raise SshKeyError("Недопустимое имя файла ключа")
    return path


def _public_line(private_key) -> str:
    public = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return f"{public} {COMMENT}"


def public_key_of(path: Path) -> str:
    """Открытый ключ (строка OpenSSH) для существующего закрытого."""
    data = path.read_bytes()
    try:
        if b"OPENSSH PRIVATE KEY" in data:
            key = serialization.load_ssh_private_key(data, password=None)
        else:
            key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise SshKeyError(
            f"Файл {path.name} — не закрытый SSH-ключ без пароля ({exc})"
        ) from None
    return _public_line(key)


def ensure_key_pair(device_type: str, name: str) -> tuple[bool, Path, str]:
    """
    (создан ли сейчас, путь закрытого ключа, открытый ключ).
    Есть файл — новый не создаётся.
    """
    path = key_path(device_type, name)
    if path.exists():
        return False, path, public_key_of(path)

    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_BITS)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public = _public_line(key)

    path.parent.mkdir(parents=True, exist_ok=True)
    # Создаём сразу с правами 600 (Linux/Docker); «x» — не затереть
    # файл, появившийся между проверкой и записью
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    pub = path.with_name(path.name + ".pub")
    if not pub.exists():
        pub.write_text(public + "\n", encoding="ascii")
    return True, path, public
