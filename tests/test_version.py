# =========================================================
# tests/test_version.py — единый номер версии
# =========================================================

import re
import subprocess
import sys
from pathlib import Path

import pytest

from core import version

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__)
    assert ".".join(map(str, version.version_tuple())) == version.__version__


def test_version_info_render_matches_version():
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import version_info
    finally:
        sys.path.pop(0)

    text = version_info.render()
    major, minor, patch = version.version_tuple()
    assert f"filevers=({major}, {minor}, {patch}, 0)" in text
    assert f"StringStruct('ProductVersion', '{version.__version__}')" in text


def test_version_info_parses_with_pyinstaller(tmp_path):
    vi = pytest.importorskip("PyInstaller.utils.win32.versioninfo")

    out = tmp_path / "version_info.txt"
    r = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "version_info.py"), str(out)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == version.__version__

    info = vi.load_version_info_from_text_file(str(out))
    assert info.ffi.fileVersionMS == (version.version_tuple()[0] << 16) | version.version_tuple()[1]


def test_main_version_flag():
    r = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--version"],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=60,
        # В канал Windows пишет в кодировке ANSI (cp1251); в окне
        # консоли — Юникодом. Для проверки текста — UTF-8.
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert r.returncode == 0, r.stderr
    first, *rest = r.stdout.strip().splitlines()
    assert first.endswith(version.__version__)      # install.sh: head -1
    assert any(version.LICENSE_ID in line for line in rest)


def test_main_version_flag_without_cyrillic_encoding():
    # Английская Windows (cp1252) или LANG=C: русский текст не
    # представим — вместо падения UnicodeEncodeError знаки «?»
    r = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--version"],
        capture_output=True, cwd=ROOT, timeout=60,
        env={**__import__("os").environ, "PYTHONIOENCODING": "cp1252"},
    )
    assert r.returncode == 0, r.stderr.decode("cp1252", "replace")
    first = r.stdout.decode("cp1252").strip().splitlines()[0]
    assert first.endswith(version.__version__)
    assert b"UnicodeEncodeError" not in r.stderr


def test_header_shows_version():
    from web import server as s

    html = s.templates.get_template("_header.html").render()
    assert f"v{version.__version__}" in html
    assert s.app.version == version.__version__


def test_changelog_has_current_version():
    """Версию без записи в CHANGELOG.md не выпускаем (release.yml)."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import release_notes
    finally:
        sys.path.pop(0)

    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    notes = release_notes.section(version.__version__, text)
    assert notes and "###" in notes
    assert "## [" not in notes                       # только свой раздел
    assert release_notes.section("0.0.1", text) is None
