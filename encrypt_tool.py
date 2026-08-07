import os
import sys

# Добавляем текущую директорию в путь, чтобы можно было импортировать core
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.secure_storage import SecureStorage

def main():
    print("🔐 Утилита первичного шифрования паролей для портативного бэкапа")
    print("-" * 60)
    
    # Инициализируем хранилище (оно автоматически найдет или создаст secret.key в корне проекта)
    try:
        storage = SecureStorage()
    except Exception as e:
        print(f"❌ Ошибка инициализации хранилища ключей: {e}")
        sys.exit(1)

    print(f"📁 Файл ключа шифрования: {storage.key_file}")
    print("💡 Подсказка: этот файл (secret.key) должен лежать рядом с программой при переносе.")
    print("-" * 60)

    while True:
        try:
            password = input("\nВведите пароль для шифрования (или 'q' для выхода): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\кВыход...")
            break

        if password.lower() == 'q':
            print("👋 Завершение работы утилиты.")
            break
            
        if not password:
            print("⚠️ Пароль не может быть пустым.")
            continue

        # Шифруем пароль с помощью нашего модуля
        encrypted_text = storage.encrypt(password)

        print("\n✅ Успешно зашифровано!")
        print("Скопируйте эту строку в файл routers.json (в поле 'pass'):")
        print(f"\033[92m{encrypted_text}\033[0m")
        print("-" * 60)

if __name__ == "__main__":
    main()