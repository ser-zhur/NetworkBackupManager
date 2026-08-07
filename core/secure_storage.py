import os
from pathlib import Path
from cryptography.fernet import Fernet


class SecureStorage:
    def __init__(self, storage_dir=None):
        if storage_dir is None:
            # Определяем корень проекта относительно папки core
            base_dir = os.path.dirname(os.path.abspath(__file__))
            self.storage_dir = os.path.dirname(base_dir)
        else:
            self.storage_dir = str(storage_dir)

        # Если случайно передали путь с именем файла "secret.key" в конце, убираем его
        if self.storage_dir.endswith("secret.key"):
            self.storage_dir = os.path.dirname(self.storage_dir)

        self.key_file = os.path.join(self.storage_dir, "secret.key")
        self._cipher = self._load_or_create_key()

    def _load_or_create_key(self) -> Fernet:
        """Загружает существующий локальный ключ или создает новый с гарнтией наличия папки"""
        # Гарантируем создание директории, если ее нет
        os.makedirs(os.path.dirname(self.key_file), exist_ok=True)

        if os.path.exists(self.key_file):
            with open(self.key_file, "rb") as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            with open(self.key_file, "wb") as f:
                f.write(key)

        return Fernet(key)

    def encrypt(self, plain_text: str) -> str:
        """Шифрует строку"""
        if not plain_text:
            return ""
        try:
            return self._cipher.encrypt(plain_text.encode("utf-8")).decode("utf-8")
        except Exception:
            return ""

    def decrypt(self, cipher_text: str) -> str:
        """Расшифровывает строку"""
        if not cipher_text:
            return ""
        try:
            return self._cipher.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
        except Exception:
            # Если текст не зашифрован или зашифрован другим ключом, возвращаем как есть или пустую строку
            return ""