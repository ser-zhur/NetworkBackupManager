# =========================================================
# tools/version_info.py — свойства EXE из core/version.py
# =========================================================
#
#     python tools/version_info.py build\version_info.txt
#
# Пишет файл VSVersionInfo для PyInstaller (--version-file):
# версия видна в «Свойства → Подробно» у EXE. Печатает номер
# версии — build_exe.bat передаёт его в Inno Setup.
#
# =========================================================

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.version import APP_NAME, __version__, version_tuple  # noqa: E402

EXE_NAME = "NetworkBackupManager.exe"


def render() -> str:
    major, minor, patch = version_tuple()
    numbers = f"({major}, {minor}, {patch}, 0)"
    strings = {
        "CompanyName": APP_NAME,
        "FileDescription": APP_NAME,
        "FileVersion": __version__,
        "InternalName": "NetworkBackupManager",
        "OriginalFilename": EXE_NAME,
        "ProductName": APP_NAME,
        "ProductVersion": __version__,
    }
    string_lines = ",\n".join(
        f"            StringStruct({k!r}, {v!r})" for k, v in strings.items()
    )
    return f"""# Сгенерировано tools/version_info.py — не редактировать
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '041904B0',
        [
{string_lines}
        ])
    ]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
"""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__ or "usage: version_info.py <out_file>", file=sys.stderr)
        return 2
    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(), encoding="utf-8")
    print(__version__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
