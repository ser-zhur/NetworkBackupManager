# =========================================================
# tests/test_static_css.py
# =========================================================
#
# Стили — готовый web/static/css/tailwind.css (собирается
# tools/build_css.py), а не Tailwind Play CDN в браузере:
# иначе при первом открытии страница какое-то время была
# без стилей.
#
# =========================================================

import importlib.util
import re

import pytest

from core.context import BASE_DIR

CSS = BASE_DIR / "web" / "static" / "css" / "tailwind.css"


def _build_css_module():
    path = BASE_DIR / "tools" / "build_css.py"
    spec = importlib.util.spec_from_file_location("nbm_build_css", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_css_is_built_and_fresh():
    """
    Классы в шаблонах поменялись, а CSS не пересобран —
    новые классы остались бы без стилей.
    """
    assert CSS.exists(), "нет tailwind.css — запустите: python tools/build_css.py"
    build = _build_css_module()
    assert build.read_fingerprint() == build.tokens_fingerprint(), (
        "Шаблоны изменились, а CSS не пересобран. "
        "Запустите: python tools/build_css.py"
    )


def test_css_contains_key_rules():
    css = CSS.read_text(encoding="utf-8")
    for needle in (
        "tailwindcss v3",                          # preflight на месте
        r".max-w-\[1400px\]",                      # произвольные значения
        r"lg\:grid-cols-\[340px_minmax\(0\2c 1fr\)\]",
        ".peer:checked ~ .peer-checked\\:bg-brand-600",
        r".dark\:bg-slate-950",
        ".bg-brand-600",                           # свои цвета из конфига
    ):
        assert needle in css, needle


def test_templates_do_not_use_play_cdn():
    for path in (BASE_DIR / "web" / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert '<script src="/static/js/tailwind.js"' not in text, path.name
        assert "tailwind.config" not in text, path.name


def test_pages_link_prebuilt_css():
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    import web.server as s

    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    try:
        c = TestClient(s.app)
        for url in ("/", "/settings?tab=general", "/add", "/logs/main"):
            html = c.get(url).text
            m = re.search(r'<link rel="stylesheet" href="(/static/css/tailwind\.css\?v=\d+)">', html)
            assert m, url
            r = c.get(m.group(1))
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/css")
    finally:
        s.app.dependency_overrides.clear()
