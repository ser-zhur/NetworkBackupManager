# =========================================================
# tests/test_web_files.py — обновление web/ рядом с EXE
# =========================================================

import sys

from core import context


def _bundle(tmp_path):
    src = tmp_path / "bundle" / "web"
    (src / "templates").mkdir(parents=True)
    (src / "static" / "css").mkdir(parents=True)
    (src / "templates" / "index.html").write_text("new index", encoding="utf-8")
    (src / "templates" / "added.html").write_text("added", encoding="utf-8")
    (src / "static" / "css" / "tailwind.css").write_text("new css", encoding="utf-8")
    return src


def test_sync_tree_copies_missing_and_changed(tmp_path):
    src = _bundle(tmp_path) / "templates"
    dst = tmp_path / "app" / "templates"
    dst.mkdir(parents=True)
    (dst / "index.html").write_text("old index", encoding="utf-8")
    (dst / "extra.html").write_text("keep", encoding="utf-8")

    assert context._sync_tree(src, dst) == 2
    assert (dst / "index.html").read_text(encoding="utf-8") == "new index"
    assert (dst / "added.html").read_text(encoding="utf-8") == "added"
    # Лишние файлы не трогаем
    assert (dst / "extra.html").read_text(encoding="utf-8") == "keep"

    # Повторно — ничего не копируется
    assert context._sync_tree(src, dst) == 0


def test_sync_tree_same_size_different_content(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.html").write_text("AAAA", encoding="utf-8")
    (dst / "a.html").write_text("BBBB", encoding="utf-8")

    assert context._sync_tree(src, dst) == 1
    assert (dst / "a.html").read_text(encoding="utf-8") == "AAAA"


def test_sync_tree_missing_source(tmp_path):
    assert context._sync_tree(tmp_path / "nope", tmp_path / "dst") == 0


def test_ensure_web_files_frozen_updates(tmp_path, monkeypatch):
    src = _bundle(tmp_path)
    web = tmp_path / "app" / "web"
    (web / "templates").mkdir(parents=True)
    (web / "templates" / "index.html").write_text("old index", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(src.parent), raising=False)
    monkeypatch.setattr(context, "WEB_DIR", web)

    context.ensure_web_files()

    assert (web / "templates" / "index.html").read_text(encoding="utf-8") == "new index"
    assert (web / "templates" / "added.html").exists()
    assert (web / "static" / "css" / "tailwind.css").read_text(encoding="utf-8") == "new css"


def test_ensure_web_files_source_mode_noop(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    web = tmp_path / "web"
    monkeypatch.setattr(context, "WEB_DIR", web)

    context.ensure_web_files()

    assert not web.exists()
