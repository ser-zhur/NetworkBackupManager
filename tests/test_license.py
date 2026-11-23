# =========================================================
# tests/test_license.py — лицензия AGPL-3.0 и сторонние компоненты
# =========================================================

import re
import struct
import sys
import zlib
from pathlib import Path

import pytest

from core import version

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import make_favicon  # noqa: E402
import third_party_licenses as tpl  # noqa: E402


def test_license_file_is_agpl3():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")
    assert "Version 3, 19 November 2007" in text
    assert "13. Remote Network Interaction" in text
    assert version.LICENSE_ID == "AGPL-3.0-only"


def test_every_dependency_has_license_text():
    text = tpl.build()
    sections = re.split(r"^={72}\n", text, flags=re.M)
    titles = {sections[i].strip(): sections[i + 1] for i in range(1, len(sections) - 1, 2)}
    for dist in tpl.runtime_distributions():
        title = f"{dist.metadata['Name']} {dist.version}"
        assert title in titles, title
        assert "----- " in titles[title], f"{title}: нет текста лицензии"
    assert "rclone" in titles and "----- COPYING -----" in titles["rclone"]
    assert "paramiko" in text and "LGPL" in text


def test_targets():
    win = tpl.build("windows")
    docker = tpl.build("docker")
    assert "NSSM" in win and "Debian" not in win
    assert "Debian" in docker and "NSSM" not in docker


def test_favicon_is_generated_by_our_script():
    """Значок — из tools/make_favicon.py, а не чужая картинка."""
    # Сравниваем пиксели, а не байты: сжатие zlib на разных
    # системах даёт разный результат для той же картинки.
    def pixels(ico: bytes) -> list[bytes]:
        count = struct.unpack_from("<H", ico, 4)[0]
        out = []
        for k in range(count):
            size, offset = struct.unpack_from("<II", ico, 6 + 16 * k + 8)
            png = ico[offset:offset + size]
            idat = png.index(b"IDAT")
            length = struct.unpack(">I", png[idat - 4:idat])[0]
            out.append(zlib.decompress(png[idat + 4:idat + 4 + length]))
        return out

    assert pixels((ROOT / "web" / "static" / "favicon.ico").read_bytes()) == pixels(make_favicon.ico())
    assert not list((ROOT / "web" / "static").glob("icons8*"))


@pytest.fixture
def client():
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield TestClient(s.app)
    s.app.dependency_overrides.clear()


def test_footer_and_license_pages(client):
    page = client.get("/").text
    assert 'href="/license"' in page and 'href="/third-party-licenses"' in page
    assert version.SOURCE_URL in page and version.COPYRIGHT in page

    r = client.get("/license")
    assert r.headers["content-type"].startswith("text/plain")
    assert version.COPYRIGHT in r.text and "GNU AFFERO GENERAL PUBLIC LICENSE" in r.text

    r = client.get("/third-party-licenses")
    assert "paramiko" in r.text and "rclone" in r.text
