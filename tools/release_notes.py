# =========================================================
# tools/release_notes.py — раздел CHANGELOG.md для релиза
# =========================================================
#
#     python tools/release_notes.py            # текущая версия
#     python tools/release_notes.py 2.0.0
#
# Печатает раздел «## [версия]» без заголовка — это описание
# релиза на GitHub (.github/workflows/release.yml). Нет
# раздела — код возврата 1: версию без записи в CHANGELOG
# не выпускаем.
#
# =========================================================

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.version import __version__  # noqa: E402


def section(version: str, text: str) -> str | None:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(rf"^## \[{re.escape(version)}\]", l)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ") or re.match(r"^\[[^\]]+\]: ", lines[i])), len(lines))
    return "\n".join(lines[start + 1:end]).strip() + "\n"


def main(argv: list[str]) -> int:
    version = argv[1] if len(argv) > 1 else __version__
    notes = section(version.lstrip("v"), (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    if notes is None:
        print(f"В CHANGELOG.md нет раздела «## [{version}]»", file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
