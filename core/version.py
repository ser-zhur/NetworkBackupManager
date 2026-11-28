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

__version__ = "2.9.5"

APP_NAME = "Network Backup Manager"

# Лицензия: AGPL-3.0 (файл LICENSE). Автор вправе также
# предоставлять программу по отдельной коммерческой лицензии.
COPYRIGHT = "© 2026 Жураченко"
LICENSE_ID = "AGPL-3.0-only"
SOURCE_URL = "https://github.com/ser-zhur/NetworkBackupManager"


def version_tuple() -> tuple[int, int, int]:
    """(major, minor, patch) — для сравнения версий."""
    major, minor, patch = (int(p) for p in __version__.split("."))
    return major, minor, patch
