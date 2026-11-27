# =========================================================
# core/secure_storage.py — шифрование паролей и секретов
# =========================================================
#
# Ключ Fernet — data/secret.key (core.context.SECRET_KEY_FILE).
# Без него зашифрованные пароли устройств и хранилищ не
# расшифровать: его нужно сохранять вместе с data/.
#
# В приложении используйте get_secure_storage() — один
# экземпляр на процесс.
#
# =========================================================

import logging
import os
import shutil
import threading

from cryptography.fernet import Fernet


logger = logging.getLogger(__name__)


class SecureStorage:
    def __init__(self, storage_dir=None):
        if storage_dir is None:
            from core.context import SECRET_KEY_FILE
            self.storage_dir = str(SECRET_KEY_FILE.parent)
        else:
            self.storage_dir = str(storage_dir)

        if self.storage_dir.endswith("secret.key"):
            self.storage_dir = os.path.dirname(self.storage_dir)

        self.key_file = os.path.join(self.storage_dir, "secret.key")
        self._cipher = self._load_or_create_key()

    def _load_or_create_key(self) -> Fernet:
        os.makedirs(os.path.dirname(self.key_file), exist_ok=True)

        if os.path.exists(self.key_file):
            with open(self.key_file, "rb") as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            # 0600 с момента создания: ключ расшифровывает все
            # пароли устройств (на Windows права даёт папка data\).
            fd = os.open(
                self.key_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as f:
                f.write(key)

        # Ключ, созданный прежними версиями, — тоже только владельцу.
        if os.name != "nt":
            try:
                if os.stat(self.key_file).st_mode & 0o077:
                    os.chmod(self.key_file, 0o600)
            except OSError as exc:
                logger.warning("secret.key: не удалось выставить права 600: %s", exc)

        return Fernet(key)

    def encrypt(self, plain_text: str) -> str:
        if not plain_text:
            return ""
        try:
            return self._cipher.encrypt(
                plain_text.encode("utf-8")
            ).decode("utf-8")
        except Exception as exc:
            logger.warning("SecureStorage.encrypt: %s", exc)
            return ""

    def decrypt(self, cipher_text: str) -> str:
        if not cipher_text:
            return ""
        try:
            return self._cipher.decrypt(
                cipher_text.encode("utf-8")
            ).decode("utf-8")
        except Exception as exc:
            logger.warning(
                "SecureStorage.decrypt: не удалось расшифровать "
                "значение: %s", exc,
            )
            return ""


# =========================================================
# Один экземпляр на процесс
# =========================================================

_instances: dict[str, SecureStorage] = {}
_instances_lock = threading.Lock()


def _adopt_legacy_key(key_file) -> None:
    """
    Прежние версии держали secret.key в корне программы.
    Если в data/ ключа ещё нет — копируем оттуда (копия, а не
    перенос: запущенный старый процесс не создаст себе новый,
    чужой ключ на месте пропавшего).
    """
    from core.context import BASE_DIR

    legacy = BASE_DIR / "secret.key"
    if key_file.exists() or not legacy.is_file() or legacy == key_file:
        return
    key_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, key_file)
    logger.info("🔑 secret.key скопирован в %s", key_file)


def get_secure_storage() -> SecureStorage:
    """Общий SecureStorage с ключом data/secret.key."""
    from core.context import SECRET_KEY_FILE

    key = str(SECRET_KEY_FILE)
    with _instances_lock:
        inst = _instances.get(key)
        if inst is None:
            _adopt_legacy_key(SECRET_KEY_FILE)
            inst = SecureStorage(SECRET_KEY_FILE.parent)
            _instances[key] = inst
        return inst
