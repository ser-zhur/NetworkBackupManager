# =========================================================
# core/version.py — версия приложения (единственный источник)
# =========================================================
#
# Отсюда номер берут: интерфейс (шапка страниц), лог при
# запуске, `NetworkBackupManager.exe --version`, свойства EXE
# (tools/version_info.py → PyInstaller) и инсталлятор
# (build_exe.bat передаёт его в Inno Setup).
#
# Формат — MAJOR.MINOR.PATCH (semver):
#   MAJOR — несовместимые изменения данных или поведения,
#   MINOR — новые функции,
#   PATCH — исправления.
#
# =========================================================

__version__ = "1.1.0"

APP_NAME = "Network Backup Manager"


def version_tuple() -> tuple[int, int, int]:
    """(major, minor, patch) — для сравнения версий."""
    major, minor, patch = (int(p) for p in __version__.split("."))
    return major, minor, patch
