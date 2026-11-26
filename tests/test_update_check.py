# =========================================================
# tests/test_update_check.py — проверка новой версии
# =========================================================
#
# Сеть подменяется: _head_location и _get_text. Проверяется,
# что запросов ровно столько, сколько нужно (HEAD — каждый
# раз, CHANGELOG — один раз на новую версию), разбор ответа,
# поиск исправлений безопасности, одно событие на версию,
# включение/выключение, шапка, /api/status и настройки.
#
# =========================================================

from datetime import datetime, timedelta

import pytest

from core import context, events, monitoring, update_check
from core.version import __version__

CUR = update_check.parse_version(__version__)
NEXT = f"{CUR[0]}.{CUR[1]}.{CUR[2] + 1}"
NEXT2 = f"{CUR[0]}.{CUR[1] + 1}.0"
LOCATION = "https://github.com/ser-zhur/NetworkBackupManager/releases/tag/v{}"


def changelog(*sections):
    """sections: (версия, есть ли «Безопасность»), от новой к старой."""
    parts = ["# История изменений\n"]
    for version, security in sections:
        parts.append(f"## [{version}] — 2026-12-01\n\n### Новое\n\n- что-то\n")
        if security:
            parts.append("\n### Безопасность\n\n- исправлено\n")
    parts.append(f"## [{__version__}] — 2026-11-25\n\n### Безопасность\n\n- старое\n")
    return "\n".join(parts)


@pytest.fixture
def net(tmp_path, monkeypatch):
    """Чистая папка данных, проверка включена, сеть — заглушка."""
    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    monkeypatch.setattr(context, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("NBM_UPDATE_CHECK", raising=False)

    calls = {"head": [], "get": [], "latest": NEXT, "changelog": changelog((NEXT, False)), "fail": None}

    def head(url):
        calls["head"].append(url)
        if calls["fail"]:
            raise update_check.UpdateCheckError(calls["fail"])
        return LOCATION.format(calls["latest"])

    def get(url):
        calls["get"].append(url)
        return calls["changelog"]

    monkeypatch.setattr(update_check, "_head_location", head)
    monkeypatch.setattr(update_check, "_get_text", get)
    emitted = []
    monkeypatch.setattr(events, "emit", lambda kind, message, **kw: emitted.append((kind, message, kw)))
    calls["events"] = emitted
    return calls


def test_parse_and_security_sections():
    assert update_check.parse_version("v2.5.0") == (2, 5, 0)
    assert update_check.parse_version("2.5") is None
    # Версии далеко впереди текущей: changelog() дописывает раздел
    # __version__ с «Безопасность», номера не должны совпасть
    text = changelog(("99.2.0", False), ("99.1.1", True), ("99.1.0", False))
    # 99.1.1 с «Безопасность» — среди пропущенных
    assert update_check.has_security_fixes(text, "99.0.0", "99.2.0")
    # уже на 99.1.1 — безопасность в старом, новых нет
    assert not update_check.has_security_fixes(text, "99.1.1", "99.2.0")
    # секция текущей версии (тоже с «Безопасность») не считается
    assert not update_check.has_security_fixes(text, "99.1.1", "99.1.1")


def test_new_version_found_once(net):
    state = update_check.check_now()
    assert state["latest"] == NEXT and state["error"] == "" and state["security"] is False
    assert net["head"] == [update_check.LATEST_URL]
    assert net["get"] == [update_check.CHANGELOG_URL.format(version=NEXT)]
    assert len(net["events"]) == 1
    kind, message, kw = net["events"][0]
    assert kind == events.UPDATE_AVAILABLE and NEXT in message and kw["level"] == "info"

    # Следующие проверки: только HEAD, CHANGELOG и событие — не повторяются
    update_check.check_now()
    update_check.check_now()
    assert len(net["head"]) == 3 and len(net["get"]) == 1 and len(net["events"]) == 1

    avail = update_check.available()
    assert avail["version"] == NEXT and avail["security"] is False
    assert avail["url"].endswith(f"/releases/tag/v{NEXT}")


def test_security_release_and_next_version(net):
    net["changelog"] = changelog((NEXT, True))
    update_check.check_now()
    assert update_check.available()["security"] is True
    assert net["events"][0][2]["level"] == "warning"
    assert "безопасности" in net["events"][0][1]

    # Вышла ещё одна версия — снова CHANGELOG и новое событие;
    # исправление в промежуточной версии всё ещё учитывается
    net["latest"] = NEXT2
    net["changelog"] = changelog((NEXT2, False), (NEXT, True))
    update_check.check_now()
    assert len(net["get"]) == 2 and len(net["events"]) == 2
    assert update_check.available() == {
        "version": NEXT2, "security": True,
        "url": update_check.RELEASE_URL.format(version=NEXT2),
        "checked": update_check.load_state()["checked"],
    }


def test_up_to_date_and_errors(net):
    net["latest"] = __version__
    state = update_check.check_now()
    assert state["latest"] == __version__ and net["get"] == [] and net["events"] == []
    assert update_check.available() is None

    net["fail"] = "нет связи с github.com: timed out"
    state = update_check.check_now()
    assert state["error"] == "нет связи с github.com: timed out"
    assert state["latest"] == __version__                 # прежний результат не теряется


def test_unexpected_location(net, monkeypatch):
    monkeypatch.setattr(update_check, "_head_location", lambda url: "https://github.com/login")
    state = update_check.check_now()
    assert "номер версии" in state["error"] and "latest" not in state


def test_changelog_failure_still_reports(net, monkeypatch):
    def broken(url):
        raise update_check.UpdateCheckError("CHANGELOG.md: GitHub ответил 404")
    monkeypatch.setattr(update_check, "_get_text", broken)
    update_check.check_now()
    assert update_check.available()["security"] is False
    assert len(net["events"]) == 1
    # в следующий раз попробует узнать про безопасность снова
    monkeypatch.setattr(update_check, "_get_text", lambda url: changelog((NEXT, True)))
    update_check.check_now()
    assert update_check.available()["security"] is True
    assert len(net["events"]) == 1                       # событие уже было


def test_enabled_setting_and_env(net, monkeypatch):
    assert update_check.is_enabled()                     # по умолчанию включено
    context.save_json(context.CONFIG_FILE, {"update_check": False})
    assert not update_check.is_enabled()
    monkeypatch.setenv("NBM_UPDATE_CHECK", "1")
    assert update_check.is_enabled() and update_check.env_override() is True
    monkeypatch.setenv("NBM_UPDATE_CHECK", "0")
    context.save_json(context.CONFIG_FILE, {"update_check": True})
    assert not update_check.is_enabled()

    # выключено — нет ни значка, ни запросов
    update_check.check_now()
    assert update_check.available() is None
    assert update_check.tick(datetime.now() + timedelta(days=2)) is False


def test_tick_schedule(net, monkeypatch):
    started = datetime(2026, 12, 1, 10, 0)
    monkeypatch.setattr(update_check, "_started_at", started)
    started_threads = []

    class FakeThread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            started_threads.append(self.target)

    monkeypatch.setattr(update_check.threading, "Thread", FakeThread)
    # не сразу после запуска
    assert update_check.tick(started + timedelta(minutes=1)) is False
    assert update_check.tick(started + timedelta(minutes=3)) is True
    # после проверки — через сутки
    context.save_json(update_check._state_file(), {"checked": (started + timedelta(minutes=3)).isoformat()})
    assert update_check.tick(started + timedelta(hours=23)) is False
    assert update_check.tick(started + timedelta(hours=24, minutes=4)) is True
    assert started_threads == [update_check.check_now, update_check.check_now]


def test_monitoring_status(net):
    net["changelog"] = changelog((NEXT, True))
    update_check.check_now()
    st = monitoring.status()
    assert st["app"]["update"] == {"available": True, "version": NEXT, "security": True}
    assert any(NEXT in w and "безопасности" in w for w in st["warnings"])


def test_notification_rule():
    from core import notifications
    event = {"kind": events.UPDATE_AVAILABLE, "message": "Доступна новая версия", "level": "info"}
    assert notifications.DEFAULT_RULES["update"] is True
    assert notifications.wants(notifications.DEFAULT_RULES, event)
    assert not notifications.wants({**notifications.DEFAULT_RULES, "update": False}, event)
    assert notifications.normalize_rules({"update": True})["update"] is True
    msg = notifications.build_message([event], notifications.DEFAULT_RULES)
    assert msg.subject.startswith("⬆️ Доступна новая версия")


# ---------------------------------------------------------
# Веб-интерфейс: значок в шапке, раздел настроек, кнопка
# ---------------------------------------------------------

@pytest.fixture
def web(net, monkeypatch):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient

    monkeypatch.setattr(s, "CONFIG_FILE", context.CONFIG_FILE)   # сервер импортирует путь напрямую

    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient(s.app)
    s.app.dependency_overrides.clear()


def test_header_badge_only_when_newer(web, net):
    _, client = web
    page = client.get("/settings?tab=general").text
    assert "data-update=" not in page                   # ещё не проверялось
    assert "Ещё не проверялось" in page

    net["changelog"] = changelog((NEXT, True))
    r = client.post("/settings/update-check/run", headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/settings?tab=general").text
    assert f'data-update="{NEXT}"' in page and "исправлениями безопасности" in page

    net["latest"] = __version__
    client.post("/settings/update-check/run", headers={"Origin": "http://testserver"})
    page = client.get("/settings?tab=general").text
    assert "data-update=" not in page and "установлена последняя версия" in page


def test_general_settings_toggle(web, monkeypatch):
    _, client = web
    form = {"host": "127.0.0.1", "port": "8080", "max_parallel_backups": "5",
            "backup_timeout_seconds": "300", "tag_rows": "2"}
    client.post("/settings/general/save", data=form, headers={"Origin": "http://testserver"})
    assert context.load_json(context.CONFIG_FILE, {})["update_check"] is False
    client.post("/settings/general/save", data={**form, "update_check": "true"},
                headers={"Origin": "http://testserver"})
    assert context.load_json(context.CONFIG_FILE, {})["update_check"] is True

    # NBM_UPDATE_CHECK задан — настройка не трогается, флажок неактивен
    monkeypatch.setenv("NBM_UPDATE_CHECK", "0")
    client.post("/settings/general/save", data=form, headers={"Origin": "http://testserver"})
    assert context.load_json(context.CONFIG_FILE, {})["update_check"] is True
    page = client.get("/settings?tab=general").text
    assert "NBM_UPDATE_CHECK=0" in page and "Проверить сейчас" not in page
