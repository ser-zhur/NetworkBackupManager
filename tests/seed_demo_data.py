# =========================================================
# tests/seed_demo_data.py
# =========================================================
#
# Генератор демонстрационных данных для проверки
# UI-функций (история версий, retention, очистка).
#
# Создаёт:
#   plugins/demo/plugin.py          минимальный плагин
#   data/devices.json               устройства типа demo (прежние demo заменяются)
#   backups/demo/device_N/.git      готовые репы с историей
#
# Запуск из корня проекта:
#
#     python tests/seed_demo_data.py                 # 3 устройства, 50 коммитов
#     python tests/seed_demo_data.py --devices 5
#     python tests/seed_demo_data.py --commits 100
#     python tests/seed_demo_data.py --age-days 800  # старше, чтобы retention сработал
#     python tests/seed_demo_data.py --clean         # удалить всё
#
# После запуска ПЕРЕЗАПУСТИТЕ сервер — плагины загружаются
# только при старте.
#
# =========================================================

import argparse
import shutil
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------
# Константы
# ---------------------------------------------------------

PLUGIN_DIR = PROJECT_ROOT / "plugins" / "demo"
BACKUP_ROOT = PROJECT_ROOT / "backups" / "demo"

PLUGIN_PY = '''# =========================================================
# plugins/demo/plugin.py
# =========================================================
#
# Демо-плагин. Создан tests/seed_demo_data.py.
#
# Ничего не делает: история для устройств уже сгенерирована.
# На запуск бэкапа из UI возвращает [] — «изменений нет».
#
# Удалите каталог plugins/demo/ или запустите:
#     python tests/seed_demo_data.py --clean
# =========================================================

def run_backup(router):
    """Заглушка: контракт выполнен, изменений нет."""
    return []
'''


# Наборы «реалистичных» имён и хостов
DEVICE_PRESETS = [
    ("Офис Москва",      "10.10.10.1"),
    ("Склад",            "10.10.20.1"),
    ("Тестовый стенд",   "10.10.30.1"),
    ("Резервный роутер", "10.10.40.1"),
    ("Филиал СПб",       "10.10.50.1"),
    ("Филиал Казань",    "10.10.60.1"),
    ("Гостевой WiFi",    "10.10.70.1"),
    ("Магистраль",       "10.10.80.1"),
]


# =========================================================
# ПРОВЕРКИ
# =========================================================

def _ensure_dulwich():
    try:
        import dulwich  # noqa
    except ImportError:
        print(
            "Ошибка: dulwich не установлен.\n"
            "Установите: pip install dulwich",
            file=sys.stderr,
        )
        sys.exit(1)


# =========================================================
# CLEAN
# =========================================================

def clean():
    """Удаляет всё, что создал скрипт."""
    removed = []

    if PLUGIN_DIR.exists():
        shutil.rmtree(PLUGIN_DIR)
        removed.append(str(PLUGIN_DIR.relative_to(PROJECT_ROOT)))

    if BACKUP_ROOT.exists():
        shutil.rmtree(BACKUP_ROOT)
        removed.append(str(BACKUP_ROOT.relative_to(PROJECT_ROOT)))

    from core import devices as device_store

    devices = device_store.load_devices()
    kept = [d for d in devices if d.get("type") != "demo"]
    if len(kept) != len(devices):
        device_store.save_devices(kept)
        removed.append(f"data/devices.json: устройств demo — {len(devices) - len(kept)}")

    if removed:
        print("Удалено:")
        for path in removed:
            print(f"  {path}")
    else:
        print("Нечего удалять.")


# =========================================================
# ГЕНЕРАЦИЯ КОММИТА С ПРОИЗВОЛЬНОЙ ДАТОЙ
# =========================================================

def _commit_at(gs, files, message, ts):
    """
    Создаёт коммит с заданным commit_time.

    Дублирует часть логики commit_if_changed, но без
    проверки «изменилось ли» — нам нужно гарантированно
    записать N коммитов с разными timestamp'ами в прошлом.
    """
    from dulwich.objects import Blob, Commit, Tree

    with gs._lock:
        repo = gs._open_repo()
        try:
            head_sha = gs._head_sha(repo)

            blob_ids = {}
            for name, src in files.items():
                if not src.exists():
                    continue
                data = src.read_bytes()
                blob = Blob.from_string(data)
                repo.object_store.add_object(blob)
                blob_ids[name] = blob.id

            if not blob_ids:
                return False

            tree = Tree()
            for name, blob_id in blob_ids.items():
                tree.add(
                    name.encode("utf-8"),
                    0o100644,
                    blob_id,
                )
            repo.object_store.add_object(tree)

            author = b"NetworkBackupManager <nbm@local>"

            commit = Commit()
            commit.tree = tree.id
            commit.parents = [head_sha] if head_sha else []
            commit.author = author
            commit.committer = author
            commit.commit_time = ts
            commit.author_time = ts
            commit.commit_timezone = 0
            commit.author_timezone = 0
            commit.message = message.encode("utf-8")
            commit.encoding = b"UTF-8"

            repo.object_store.add_object(commit)
            repo.refs[b"refs/heads/master"] = commit.id
            return True

        finally:
            repo.close()


# =========================================================
# ГЕНЕРАЦИЯ КОНТЕНТА
# =========================================================

def _generate_content(device_idx, commit_idx):
    """
    Имитация config.rsc от MikroTik: разные данные
    на каждом шаге, с реалистичной структурой.
    """
    return (
        f"# {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"by RouterOS 7.23.5\n"
        f"# software id = DEMO-{device_idx:02d}\n"
        f"#\n"
        f"# model = DemoDevice\n"
        f"# serial number = DEMO{device_idx:04d}{commit_idx:04d}\n"
        f"/system identity\n"
        f"set name=Demo-Device-{device_idx}-v{commit_idx:03d}\n"
        f"/ip address\n"
        + "".join(
            f"add address=10.10.{device_idx}.{j}/24 "
            f"interface=ether{j}\n"
            for j in range(1, (commit_idx % 5) + 2)
        )
        + "/system clock\n"
        f"set time-zone-name=Europe/Moscow\n"
    )


# =========================================================
# ГЕНЕРАЦИЯ ИСТОРИИ
# =========================================================

def _seed_device(
    device_idx: int,
    name: str,
    host: str,
    commits: int,
    age_days: int,
    with_notes: bool,
) -> Path:
    """Создаёт репу с историей для одного устройства."""
    from core.git_storage import GitStorage

    # backup_path относительно BASE_DIR:
    # "backups/demo/device_1"
    rel_path = f"backups/demo/device_{device_idx}"
    abs_path = PROJECT_ROOT / rel_path

    if abs_path.exists():
        shutil.rmtree(abs_path)
    abs_path.mkdir(parents=True)

    gs = GitStorage(abs_path)
    file_path = abs_path / "config.rsc"

    # Растягиваем коммиты по указанному периоду в прошлом.
    # Самый старый: now - age_days, самый свежий: сейчас.
    now_ts = int(time.time())
    seconds_span = age_days * 86400
    step = seconds_span / max(1, commits - 1)

    for i in range(commits):
        # i = 0 (самый старый) ... commits-1 (свежий)
        commit_ts = int(now_ts - seconds_span + i * step)

        content = _generate_content(device_idx, i + 1)
        file_path.write_bytes(content.encode("utf-8"))

        human_date = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(commit_ts)
        )
        message = f"Backup {human_date}"

        _commit_at(
            gs,
            files={"config.rsc": file_path},
            message=message,
            ts=commit_ts,
        )

    # Заметки на 2–3 «важных» версиях в середине истории
    if with_notes and commits >= 20:
        history = gs.get_history()

        note_positions = [
            (5, "Pre-update to 7.16"),
            (commits // 2, "После планового изменения VLAN"),
            (commits - 3, "Проверено, всё ок"),
        ]

        for idx, text in note_positions:
            if 0 <= idx < len(history):
                gs.set_note(history[idx]["sha"], text)

    return abs_path


# =========================================================
# ЗАПИСЬ УСТРОЙСТВ (data/devices.json)
# =========================================================

def _write_routers(devices):
    """
    devices — список dict с полями name, host, backup_path.
    """
    routers = []
    for d in devices:
        routers.append({
            "name": d["name"],
            "host": d["host"],
            "ip": d["host"],
            "port": 22,
            "username": "admin",
            "ssh_pass": "",
            "backup_path": d["backup_path"],
            "max_backups": 5,
            "storage": {
                "git": {
                    "enabled": True,
                    "mode": "changes_only",
                },
            },
        })

    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)

    (PLUGIN_DIR / "plugin.py").write_text(
        PLUGIN_PY,
        encoding="utf-8",
    )

    from core import devices as device_store

    others = [d for d in device_store.load_devices() if d.get("type") != "demo"]
    demo = [{"id": "", "type": "demo", **r} for r in routers]
    device_store.ensure_ids(others + demo)
    device_store.save_devices(others + demo)


# =========================================================
# MAIN
# =========================================================

def seed(
    devices_count: int,
    commits: int,
    age_days: int,
    with_notes: bool,
):
    if devices_count > len(DEVICE_PRESETS):
        print(
            f"Максимум {len(DEVICE_PRESETS)} устройств. "
            f"Указано {devices_count}.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Каталог проекта: {PROJECT_ROOT}")
    print(f"Устройств:       {devices_count}")
    print(f"Коммитов:        {commits} на устройство")
    print(f"Разброс:         {age_days} дней в прошлое")
    print(f"Заметки:         {'да' if with_notes else 'нет'}")
    print()

    if PLUGIN_DIR.exists():
        shutil.rmtree(PLUGIN_DIR)
    if BACKUP_ROOT.exists():
        shutil.rmtree(BACKUP_ROOT)

    devices = []

    for i in range(devices_count):
        idx = i + 1
        name, host = DEVICE_PRESETS[i]

        print(f"[{idx}/{devices_count}] {name} ({host})...",
              end=" ", flush=True)

        _seed_device(
            device_idx=idx,
            name=name,
            host=host,
            commits=commits,
            age_days=age_days,
            with_notes=with_notes,
        )

        devices.append({
            "name": name,
            "host": host,
            "backup_path": f"backups/demo/device_{idx}",
        })

        print("готово")

    _write_routers(devices)

    print()
    print("=" * 60)
    print("Готово. Создано:")
    print(f"  plugins/demo/plugin.py")
    print(f"  data/devices.json ({devices_count} устройств demo)")
    print(f"  backups/demo/device_1..{devices_count}/")

    print()
    print("ЧТО ДЕЛАТЬ ДАЛЬШЕ:")
    print("  1. Перезапустите сервер (плагины грузятся при старте).")
    print("  2. Откройте / — в списке появятся устройства 'demo'.")
    print("  3. Откройте /git/demo/0 — история версий.")
    print("  4. Откройте /retention — настройки очистки.")
    print()
    print("Удалить всё созданное:")
    print("  python tests/seed_demo_data.py --clean")
    print("=" * 60)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Генератор демо-данных для проверки UI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  python tests/seed_demo_data.py\n"
            "  python tests/seed_demo_data.py --devices 5 --commits 100\n"
            "  python tests/seed_demo_data.py --age-days 800\n"
            "  python tests/seed_demo_data.py --clean\n"
        ),
    )

    parser.add_argument(
        "--devices",
        type=int,
        default=3,
        help="Количество устройств (1–8, по умолчанию 3)",
    )
    parser.add_argument(
        "--commits",
        type=int,
        default=50,
        help="Коммитов на устройство (по умолчанию 50)",
    )
    parser.add_argument(
        "--age-days",
        type=int,
        default=400,
        help=(
            "Растянуть историю на N дней в прошлое "
            "(по умолчанию 400 — старше дефолтного "
            "retention.max_age_days=365)"
        ),
    )
    parser.add_argument(
        "--no-notes",
        action="store_true",
        help="Не добавлять заметки к версиям",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Удалить всё созданное и выйти",
    )

    args = parser.parse_args()

    _ensure_dulwich()

    if args.clean:
        clean()
        return 0

    if args.devices < 1 or args.commits < 1 or args.age_days < 1:
        print(
            "Ошибка: --devices, --commits, --age-days "
            "должны быть ≥ 1.",
            file=sys.stderr,
        )
        return 2

    try:
        seed(
            devices_count=args.devices,
            commits=args.commits,
            age_days=args.age_days,
            with_notes=not args.no_notes,
        )
        return 0
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130
    except Exception as exc:
        print(f"\nОшибка: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())