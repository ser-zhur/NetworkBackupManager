# =========================================================
# tests/gen_history.py
# =========================================================
#
# Генератор тестовой git-истории.
#
# Запускается вручную из корня проекта:
#
#     python tests/gen_history.py /tmp/nbm-test 100
#
# Создаёт репу в /tmp/nbm-test с 100 коммитами и
# двумя заметками на «важных» версиях.
#
# Использование:
#     python tests/gen_history.py <каталог> <кол-во> [--fresh]
#
#     --fresh   удалить каталог, если он существует
#
# =========================================================

import argparse
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def generate(
    target: Path,
    n: int = 100,
    fresh: bool = False,
) -> None:
    """Создаёт git-репу с n коммитами."""

    from core.git_storage import GitStorage

    if target.exists():
        if fresh:
            shutil.rmtree(target)
            print(f"Удалён старый каталог: {target}")
        else:
            print(
                f"Каталог уже существует: {target}\n"
                f"Передайте --fresh, чтобы удалить его."
            )
            return

    target.mkdir(parents=True)

    gs = GitStorage(target)
    file_path = target / "config.rsc"

    for i in range(1, n + 1):
        # Содержимое растёт линейно — каждая версия отличается
        content = (
            f"# version {i:04d}\n"
            f"# software id = TEST-{i:04d}\n"
            f"/system identity\n"
            f"set name=Router-{i:03d}\n"
            + "".join(
                f"add address=192.168.{i}.{j} list=TEST\n"
                for j in range(1, i % 5 + 1)
            )
        )
        file_path.write_bytes(content.encode("utf-8"))

        ok, created, msg = gs.commit_if_changed(
            files={"config.rsc": file_path},
            text_files=["config.rsc"],
            message=f"Backup #{i:03d}",
            normalize=lambda name, data: data,
        )

        if not ok:
            print(f"Коммит #{i}: ошибка — {msg}")
            return

        if created and i % 20 == 0:
            print(f"  создано {i} коммитов")

    # Заметки на «важных» версиях
    history = gs.get_history()

    if len(history) >= 60:
        gs.set_note(history[10]["sha"], "Pre-update 7.16")
        gs.set_note(history[50]["sha"], "После аварии, откатились")

    print()
    print(f"✅ Готово: {target}")
    print(f"   Коммитов: {len(gs.get_history())}")
    print(f"   Файл:     {file_path}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Генератор тестовой git-истории",
    )
    parser.add_argument(
        "target",
        type=Path,
        help="Каталог для репы (например, /tmp/nbm-test)",
    )
    parser.add_argument(
        "n",
        type=int,
        nargs="?",
        default=100,
        help="Количество коммитов (по умолчанию 100)",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Удалить каталог перед генерацией",
    )

    args = parser.parse_args()

    try:
        generate(args.target, args.n, args.fresh)
        return 0
    except KeyboardInterrupt:
        print("\nПрервано.")
        return 130
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())