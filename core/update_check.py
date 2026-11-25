# =========================================================
# core/update_check.py — проверка новой версии
# =========================================================
#
# Раз в сутки (и по кнопке) — запрос HEAD к
#   https://github.com/<владелец>/<репо>/releases/latest
# без перехода по перенаправлению: номер последней версии
# берётся из заголовка Location (…/releases/tag/vX.Y.Z).
# Тела ответа нет; программа не передаёт ни свою версию, ни
# данные об устройствах — GitHub видит только IP-адрес.
#
# Только когда вышла новая версия — один раз скачивается
# CHANGELOG.md этой версии, чтобы узнать, есть ли среди
# пропущенных версий исправления безопасности.
#
# Включено по умолчанию. Выключить: Настройки → Общие или
# NBM_UPDATE_CHECK=0 (переменная важнее настройки).
# Прокси — стандартные HTTPS_PROXY / NO_PROXY.
#
# Ничего не скачивается и не устанавливается: только
# сообщение в шапке, уведомление и поле в /api/status.
#
# =========================================================

from __future__ import annotations

import logging
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from core import context, events, settings
from core.version import SOURCE_URL, __version__

logger = logging.getLogger(__name__)

INTERVAL = timedelta(hours=24)
START_DELAY = timedelta(minutes=2)     # первая проверка — не сразу при запуске
TIMEOUT = 15
MAX_CHANGELOG_BYTES = 512 * 1024
# Без номера версии и сведений о системе
USER_AGENT = "NetworkBackupManager"

_REPO = SOURCE_URL.rstrip("/").removeprefix("https://github.com/")
LATEST_URL = f"https://github.com/{_REPO}/releases/latest"
CHANGELOG_URL = f"https://raw.githubusercontent.com/{_REPO}/v{{version}}/CHANGELOG.md"
RELEASE_URL = f"https://github.com/{_REPO}/releases/tag/v{{version}}"

_TAG_RE = re.compile(r"/releases/tag/v(\d+\.\d+\.\d+)/?$")
_SECTION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.M)

_lock = threading.Lock()
_started_at = datetime.now()


class UpdateCheckError(Exception):
    pass


# =========================================================
# Настройка и состояние
# =========================================================

def _state_file():
    return context.DATA_DIR / "update_check.json"


def env_override() -> bool | None:
    """NBM_UPDATE_CHECK: True/False — задано переменной, None — нет."""
    return settings.env_bool("NBM_UPDATE_CHECK")


def is_enabled() -> bool:
    forced = env_override()
    if forced is not None:
        return forced
    return bool(context.load_json(context.CONFIG_FILE, {}).get("update_check", True))


def load_state() -> dict:
    state = context.load_json(_state_file(), {})
    return state if isinstance(state, dict) else {}


def _save_state(state: dict) -> None:
    try:
        context.save_json(_state_file(), state)
    except OSError as exc:
        logger.warning("update_check: не удалось сохранить состояние: %s", exc)


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (text or "").strip())
    return tuple(int(p) for p in m.groups()) if m else None


def _newer(version: str | None) -> bool:
    latest, current = parse_version(version or ""), parse_version(__version__)
    return bool(latest and current and latest > current)


def available() -> dict | None:
    """
    Новая версия для шапки и /api/status:
    {"version", "security", "url", "checked"} или None.
    """
    if not is_enabled():
        return None
    state = load_state()
    latest = state.get("latest")
    if not _newer(latest):
        return None
    return {
        "version": latest,
        "security": bool(state.get("security")),
        "url": RELEASE_URL.format(version=latest),
        "checked": state.get("checked"),
    }


# =========================================================
# Сеть
# =========================================================

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _head_location(url: str) -> str:
    """HEAD без перехода по перенаправлению → Location. Подменяется в тестах."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raise UpdateCheckError(f"GitHub ответил {resp.status} без перенаправления")
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.headers.get("Location") or ""
        raise UpdateCheckError(f"GitHub ответил {exc.code}") from None
    except urllib.error.URLError as exc:
        raise UpdateCheckError(f"нет связи с github.com: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise UpdateCheckError(f"нет связи с github.com: {exc}") from None


def _get_text(url: str) -> str:
    """GET текста (не больше MAX_CHANGELOG_BYTES). Подменяется в тестах."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(MAX_CHANGELOG_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise UpdateCheckError(f"CHANGELOG.md: GitHub ответил {exc.code}") from None
    except urllib.error.URLError as exc:
        raise UpdateCheckError(f"CHANGELOG.md: нет связи — {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise UpdateCheckError(f"CHANGELOG.md: нет связи — {exc}") from None


def has_security_fixes(changelog: str, current: str, latest: str) -> bool:
    """Есть ли «### Безопасность» в версиях новее current, до latest включительно."""
    cur, top = parse_version(current), parse_version(latest)
    matches = list(_SECTION_RE.finditer(changelog))
    for i, m in enumerate(matches):
        version = parse_version(m.group(1))
        if not version or not (cur < version <= top):
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog)
        if re.search(r"^### Безопасность", changelog[m.end():end], re.M):
            return True
    return False


# =========================================================
# Проверка
# =========================================================

def check_now() -> dict:
    """
    Проверить сейчас. → состояние. Ошибка сети не
    исключение: записывается в state["error"].
    """
    with _lock:
        state = load_state()
        state["checked"] = datetime.now().isoformat(timespec="seconds")
        try:
            location = _head_location(LATEST_URL)
            m = _TAG_RE.search(location)
            if not m:
                raise UpdateCheckError(f"не удалось узнать номер версии из ответа GitHub ({location[:120]!r})")
            latest = m.group(1)

            if _newer(latest) and (latest != state.get("latest") or state.get("security_of") != __version__):
                # Новая версия (или программу обновили до промежуточной) —
                # один раз узнать, есть ли исправления безопасности.
                try:
                    changelog = _get_text(CHANGELOG_URL.format(version=latest))
                    state["security"] = has_security_fixes(changelog, __version__, latest)
                    state["security_of"] = __version__
                except UpdateCheckError as exc:
                    logger.info("Проверка обновлений: %s", exc)
                    state["security"] = False
                    state.pop("security_of", None)

            state["latest"] = latest
            state["error"] = ""
        except UpdateCheckError as exc:
            state["error"] = str(exc)
            logger.info("Проверка обновлений не удалась: %s", exc)
        _save_state(state)

    _notify(state)
    return state


def _notify(state: dict) -> None:
    """Событие о новой версии — один раз на версию."""
    latest = state.get("latest")
    if not _newer(latest) or state.get("notified") == latest:
        return
    security = bool(state.get("security"))
    message = f"Доступна новая версия {latest} (установлена {__version__})"
    if security:
        message += " — с исправлениями безопасности"
    message += f": {RELEASE_URL.format(version=latest)}"
    logger.info("⬆ %s", message)
    events.emit(
        events.UPDATE_AVAILABLE, message,
        level="warning" if security else "info",
        version=latest, security=security, url=RELEASE_URL.format(version=latest),
    )
    with _lock:
        fresh = load_state()
        fresh["notified"] = latest
        _save_state(fresh)


def next_check(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    checked = load_state().get("checked")
    try:
        last = datetime.fromisoformat(checked) if checked else None
    except ValueError:
        last = None
    if last is None or last > now:          # не проверяли или часы перевели назад
        return _started_at + START_DELAY
    return last + INTERVAL


def tick(now: datetime | None = None) -> bool:
    """Планировщик, каждые ~30 с. Пора — проверка в отдельном потоке."""
    if not is_enabled():
        return False
    now = now or datetime.now()
    if now < next_check(now) or _lock.locked():
        return False
    threading.Thread(target=check_now, daemon=True, name="UpdateCheck").start()
    return True
