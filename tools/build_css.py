# =========================================================
# tools/build_css.py — сборка web/static/css/tailwind.css
# =========================================================
#
# Зачем: раньше стили генерировал Tailwind Play CDN
# (web/static/js/tailwind.js) прямо в браузере, ПОСЛЕ
# загрузки страницы. При первом открытии (холодный кэш,
# 400 КБ JS) страница заметное время была без стилей.
# Готовый CSS-файл браузер применяет до первой отрисовки.
#
# Как: собираем все «похожие на классы» слова из шаблонов и
# Python-кода, открываем их в headless Chrome вместе с тем
# же tailwind.js и конфигом — Play CDN генерирует CSS —
# забираем сгенерированный <style>. Без npm и интернета.
#
# Запуск (после правок классов в шаблонах):
#
#     python tools/build_css.py
#
# Лишние слова не страшны: Tailwind игнорирует то, что не
# является его классом.
#
# =========================================================

from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAILWIND_JS = ROOT / "web" / "static" / "js" / "tailwind.js"
OUTPUT = ROOT / "web" / "static" / "css" / "tailwind.css"

# Где искать классы: шаблоны + Python, который их генерирует
SOURCES = [
    *sorted((ROOT / "web" / "templates").rglob("*.html")),
    ROOT / "web" / "server.py",
]

# Тот же конфиг, что был в _base_head.html
TAILWIND_CONFIG = """
tailwind.config = {
    darkMode: 'class',
    theme: {
        extend: {
            colors: {
                brand: {
                    50:'#eff6ff',100:'#dbeafe',200:'#bfdbfe',300:'#93c5fd',
                    400:'#60a5fa',500:'#3b82f6',600:'#2563eb',700:'#1d4ed8',
                    800:'#1e40af',900:'#1e3a8a',950:'#172554',
                },
            },
        },
    },
};
"""

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "chrome", "google-chrome", "chromium", "msedge",
]

# Кандидаты в классы: латиница, цифры и символы вариантов /
# произвольных значений Tailwind (hover:, w-[1400px], bg-x/60,
# after:content-[''], grid-cols-[340px_minmax(0,1fr)]).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-:/.\[\]%#!'(),+*]+")


def collect_tokens() -> set[str]:
    tokens: set[str] = set()
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        for raw in _TOKEN_RE.findall(text):
            for t in (raw, raw.strip("'"), raw.strip("'").rstrip(".:")):
                if t and any(c.isalpha() for c in t) and len(t) <= 80:
                    tokens.add(t)
    return tokens


def tokens_fingerprint(tokens: set[str] | None = None) -> str:
    """
    Отпечаток набора слов из шаблонов. Пишется в заголовок
    tailwind.css; тест сверяет его с текущими шаблонами —
    если классы поменялись, а CSS не пересобран, тест падает.
    """
    if tokens is None:
        tokens = collect_tokens()
    data = "\n".join(sorted(tokens)).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def read_fingerprint(path: Path = OUTPUT) -> str | None:
    """Отпечаток из заголовка собранного CSS."""
    try:
        head = path.read_text(encoding="utf-8")[:300]
    except OSError:
        return None
    m = re.search(r"tokens:([0-9a-f]{16})", head)
    return m.group(1) if m else None


def find_browser() -> str:
    for cand in CHROME_CANDIDATES:
        if Path(cand).exists():
            return cand
        found = shutil.which(cand)
        if found:
            return found
    sys.exit("Не найден Chrome/Edge для сборки CSS.")


def build() -> tuple[str, set[str]]:
    tokens = collect_tokens()
    class_attr = html.escape(" ".join(sorted(tokens)), quote=True)

    with tempfile.TemporaryDirectory(prefix="nbm_css_") as tmp:
        tmp = Path(tmp)
        page = tmp / "probe.html"
        page.write_text(
            "<!doctype html><html class=\"dark\"><head>"
            f"<script src=\"{TAILWIND_JS.as_uri()}\"></script>"
            f"<script>{TAILWIND_CONFIG}</script>"
            "</head><body>"
            f"<div class=\"{class_attr}\"></div>"
            "</body></html>",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                find_browser(),
                "--headless=new", "--disable-gpu", "--no-first-run",
                "--allow-file-access-from-files",
                f"--user-data-dir={tmp / 'profile'}",
                "--virtual-time-budget=10000",
                "--dump-dom", page.as_uri(),
            ],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )

    dom = result.stdout
    styles = re.findall(r"<style[^>]*>(.*?)</style>", dom, flags=re.S)
    css = next((s for s in styles if "tailwindcss" in s), None)
    if not css:
        sys.exit(
            "Tailwind не сгенерировал стили (нет <style> в DOM).\n"
            + (result.stderr or "")[-2000:]
        )

    # dump-dom экранирует только текстовые узлы; в <style> HTML-
    # сущностей нет, но на всякий случай раскодируем &gt; и т.п.
    return html.unescape(css), tokens


def main() -> None:
    css, tokens = build()
    n_tokens = len(tokens)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "/* Сгенерировано tools/build_css.py из web/templates и "
        "web/server.py. Не править вручную. "
        f"tokens:{tokens_fingerprint(tokens)} */\n"
    )
    OUTPUT.write_text(header + css, encoding="utf-8", newline="\n")
    print(
        f"OK: {OUTPUT.relative_to(ROOT)} — {len(css) // 1024} КБ, "
        f"слов-кандидатов: {n_tokens}"
    )


if __name__ == "__main__":
    main()
