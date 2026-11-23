# =========================================================
# tools/third_party_licenses.py — THIRD_PARTY_LICENSES.txt
# =========================================================
#
# Список сторонних компонентов, которые распространяются
# вместе с программой (установщик Windows, Docker-образ), с
# полными текстами их лицензий. Лицензии MIT, BSD, Apache и
# LGPL требуют прикладывать эти тексты к копиям программы.
#
# Python-пакеты берутся из установленного окружения: всё,
# от чего зависит requirements.txt (рекурсивно). Остальное —
# из tools/licenses/.
#
#   python tools/third_party_licenses.py [файл] [--target windows|docker]
#
# Без файла — вывод в stdout.
#
# =========================================================

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSES = Path(__file__).resolve().parent / "licenses"

LICENSE_FILE_RE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)

# Пакеты под двумя лицензиями — какую из них выбираем
CHOSEN = {
    "dulwich": "Apache-2.0 (выбрана из Apache-2.0 OR GPL-2.0-or-later)",
    "cryptography": "Apache-2.0 OR BSD-3-Clause",
}

# LGPL: что сказать о замене библиотеки
LGPL_NOTE = (
    "Библиотеки под LGPL поставляются отдельными модулями Python "
    "(в установщике Windows — в папке _internal). Их можно заменить "
    "своей версией. Исходный код — на странице проекта каждой библиотеки "
    "(ссылки ниже) или в PyPI."
)


def _requirement_names(lines) -> list[str]:
    names = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if ";" in line and "extra ==" in line.split(";", 1)[1]:
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if m:
            names.append(m.group(0))
    return names


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_distributions() -> list[md.Distribution]:
    """requirements.txt и всё, от чего он зависит."""
    todo = _requirement_names((ROOT / "requirements.txt").read_text("utf-8").splitlines())
    seen: dict[str, md.Distribution] = {}
    while todo:
        name = todo.pop()
        key = _norm(name)
        if key in seen:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue        # зависимость для другой ОС
        seen[key] = dist
        todo.extend(_requirement_names(dist.requires or []))
    return sorted(seen.values(), key=lambda d: _norm(d.metadata["Name"]))


def _license_id(dist: md.Distribution) -> str:
    name = _norm(dist.metadata["Name"])
    if name in CHOSEN:
        return CHOSEN[name]
    lic = dist.metadata.get("License-Expression")
    if lic:
        return lic
    classifiers = [
        c.split("::")[-1].strip()
        for c in dist.metadata.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    return (dist.metadata.get("License") or "см. текст ниже").splitlines()[0][:80]


def _home_page(dist: md.Distribution) -> str:
    for entry in dist.metadata.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("source", "source code", "repository", "homepage", "home"):
            return url.strip()
    return dist.metadata.get("Home-page") or f"https://pypi.org/project/{dist.metadata['Name']}/"


def _license_texts(dist: md.Distribution) -> list[tuple[str, str]]:
    texts = []
    for f in dist.files or []:
        if LICENSE_FILE_RE.search(str(f).replace("\\", "/")):
            try:
                data = Path(f.locate()).read_bytes()
                texts.append((Path(str(f)).name, data.decode("utf-8", errors="replace")))
            except OSError:
                pass
    return texts


def _section(title: str, meta: list[str], texts: list[tuple[str, str]]) -> str:
    out = ["=" * 72, title, "=" * 72, *meta, ""]
    for name, text in texts:
        out += [f"----- {name} -----", text.strip(), ""]
    return "\n".join(out) + "\n"


def build(target: str = "") -> str:
    from core.version import APP_NAME, COPYRIGHT, LICENSE_ID, SOURCE_URL  # noqa: E402

    parts = [
        f"{APP_NAME} — сторонние компоненты\n"
        f"{COPYRIGHT}. Сама программа распространяется по лицензии "
        f"{LICENSE_ID} (файл LICENSE), исходный код: {SOURCE_URL}\n\n"
        "Ниже — компоненты других авторов, которые поставляются вместе "
        "с программой, и тексты их лицензий.\n\n" + LGPL_NOTE + "\n\n"
    ]

    dists = runtime_distributions()
    width = max(len(d.metadata["Name"]) for d in dists)
    parts.append("Python-пакеты:\n" + "\n".join(
        f"  {d.metadata['Name']:<{width}}  {d.version:<12} {_license_id(d)}" for d in dists
    ) + "\n\n")

    if target in ("", "windows"):
        parts.append(_section(
            f"Python {sys.version.split()[0]}",
            ["Лицензия: PSF-2.0 (Python Software Foundation License)",
             "https://www.python.org/"],
            [("LICENSE.txt", p.read_text("utf-8", errors="replace"))
             for p in [Path(sys.base_prefix) / "LICENSE.txt"] if p.exists()],
        ))
        parts.append(_section(
            "NSSM — Non-Sucking Service Manager (только установщик Windows)",
            ["Лицензия: общественное достояние (public domain)", "https://nssm.cc/"],
            [],
        ))
    if target in ("", "docker"):
        parts.append(_section(
            "Пакеты Debian (только Docker-образ)",
            ["Образ собран на python:slim (Debian). Лицензии пакетов и ссылки "
             "на их исходный код — в /usr/share/doc/<пакет>/copyright внутри "
             "образа; исходники — https://sources.debian.org/"],
            [],
        ))

    parts.append(_section(
        "rclone", ["Лицензия: MIT", "https://github.com/rclone/rclone"],
        [("COPYING", (LICENSES / "rclone.txt").read_text("utf-8"))],
    ))
    parts.append(_section(
        "Tailwind CSS 3.4 (web/static: собранные стили и сборщик)",
        ["Лицензия: MIT", "https://github.com/tailwindlabs/tailwindcss"],
        [("LICENSE", (LICENSES / "tailwindcss.txt").read_text("utf-8"))],
    ))

    for d in dists:
        parts.append(_section(
            f"{d.metadata['Name']} {d.version}",
            [f"Лицензия: {_license_id(d)}", _home_page(d)],
            _license_texts(d),
        ))
    return "".join(parts)


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(ROOT))
    target = ""
    if "--target" in argv:
        i = argv.index("--target")
        target = argv[i + 1]
        del argv[i:i + 2]
    text = build(target)
    if argv:
        Path(argv[0]).write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
