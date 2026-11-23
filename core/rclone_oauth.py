# =========================================================
# core/rclone_oauth.py — вход в OneDrive / Google Drive и др.
# =========================================================
#
# Хранилища с входом через браузер (OAuth). Один «поток
# входа» (OAuthFlow) проходит этапы:
#
#   1. authorizing — `rclone authorize <тип>` поднимает на
#      127.0.0.1:53682 приёмник и печатает ссылку; человек
#      открывает её в браузере НА ЭТОМ ЖЕ компьютере и входит.
#      rclone печатает токен.
#      (Если браузер на другом компьютере — токен получают
#      там командой `rclone authorize "<тип>"` и вставляют.)
#
#   2. question — мастер `rclone config` (неинтерактивный
#      режим): технические вопросы отвечаем сами (браузер? →
#      нет; токен → полученный), содержательные (например,
#      какой диск OneDrive) отдаём в веб-интерфейс.
#
#   3. done — итоговые параметры хранилища (token, drive_id,
#      drive_type, ...) готовы к сохранению.
#
# Временный конфиг мастера — во временной папке, удаляется
# сразу по завершении. Потоки живут в памяти; брошенные
# закрываются по таймауту.
#
# =========================================================

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from core import rclone as rclone_mod

logger = logging.getLogger(__name__)

FLOW_TTL_SECONDS = 30 * 60
_WIZARD_NAME = "NBMOAUTH"

# Имена параметров rclone: латиница, цифры, «_». Всё, что
# пришло из формы, передаётся rclone после «--» — как данные,
# а не как его флаги (иначе «--log-file» и т. п. из запроса
# стали бы ключами rclone).
_OPTION_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

_URL_RE = re.compile(r"(http://127\.0\.0\.1:53682/auth\?state=\S+)")
_TOKEN_RE = re.compile(r"--->\s*(\{.*?\})\s*<---", re.S)

_flows: dict[str, "OAuthFlow"] = {}
_flows_lock = threading.Lock()


class OAuthFlow:
    def __init__(self, type_name: str, options: dict):
        self.id = uuid.uuid4().hex
        self.type_name = type_name
        # Параметры из формы (client_id и т.п.) — в открытом виде
        self.options = {k: v for k, v in (options or {}).items() if v not in ("", None)}
        self.created = time.time()

        self.state = "starting"       # authorizing | question | done | error
        self.url = ""
        self.message = ""
        self.question: dict | None = None
        self.result: dict = {}

        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._tmpdir: Path | None = None
        self._rclone_state = ""

    # -----------------------------------------------------
    # Этап 1: вход через браузер
    # -----------------------------------------------------

    def start_authorize(self) -> None:
        binary = rclone_mod.resolve_binary()
        if binary is None:
            self._fail("rclone не найден")
            return

        args = [binary, "authorize", "--auth-no-open-browser", "--", self.type_name]
        if self.options.get("client_id") and self.options.get("client_secret"):
            args += [str(self.options["client_id"]), str(self.options["client_secret"])]

        # Приёмник на 127.0.0.1:53682 один — прочие входы отменяем
        cancel_authorizing(except_id=self.id)

        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=rclone_mod._isolated_env(),
            )
        except Exception as exc:
            self._fail(f"не удалось запустить rclone authorize: {exc}")
            return

        self.state = "authorizing"
        self.message = "Откройте ссылку и войдите в учётную запись"
        threading.Thread(target=self._read_authorize, daemon=True,
                         name=f"oauth-{self.id[:6]}").start()

    def _read_authorize(self) -> None:
        output = []
        proc = self._proc
        for line in proc.stdout:
            output.append(line)
            if not self.url:
                m = _URL_RE.search(line)
                if m:
                    self.url = m.group(1)
        proc.wait()

        text = "".join(output)
        m = _TOKEN_RE.search(text)
        with self._lock:
            if self.state != "authorizing":
                return        # отменён
            if not m:
                tail = " | ".join(l.strip() for l in output[-3:] if l.strip())
                self._fail("вход не завершён" + (f": {tail}" if tail else ""))
                return
        self.start_with_token(m.group(1))

    # -----------------------------------------------------
    # Этап 2: мастер rclone config
    # -----------------------------------------------------

    def start_with_token(self, token: str) -> None:
        token = (token or "").strip()
        try:
            data = json.loads(token)
            if not isinstance(data, dict) or "access_token" not in data:
                raise ValueError
        except ValueError:
            self._fail(
                "это не токен rclone — нужен JSON вида "
                '{"access_token": ..., "refresh_token": ...}'
            )
            return

        self._token = json.dumps(data, separators=(",", ":"))
        binary = rclone_mod.resolve_binary()
        if binary is None:
            self._fail("rclone не найден")
            return

        self._tmpdir = Path(tempfile.mkdtemp(prefix=_TMP_PREFIX))
        conf = self._tmpdir / "rclone.conf"

        args = [binary, "config", "create", "--non-interactive", "--config", str(conf),
                "--", _WIZARD_NAME, self.type_name]
        for k, v in self.options.items():
            if not _OPTION_NAME_RE.match(str(k)):
                self._fail(f"Недопустимое имя параметра: {k!r}")
                return
            if isinstance(v, bool):
                v = "true" if v else "false"
            args += [str(k), str(v)]
        self._step(args)

    def answer(self, result) -> None:
        with self._lock:
            if self.state != "question":
                return
        if isinstance(result, bool):
            result = "true" if result else "false"
        self._continue(str(result))

    def _continue(self, result: str) -> None:
        binary = rclone_mod.resolve_binary()
        conf = self._tmpdir / "rclone.conf"
        self._step([
            binary, "config", "update", "--continue",
            # «--ключ=значение»: значение не станет флагом rclone
            f"--state={self._rclone_state}", f"--result={result}",
            "--non-interactive", "--config", str(conf),
            "--", _WIZARD_NAME,
        ])

    def _step(self, args: list[str]) -> None:
        try:
            r = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120, env=rclone_mod._isolated_env(),
            )
        except Exception as exc:
            self._fail(f"rclone config: {exc}")
            return

        try:
            data = json.loads(r.stdout or "")
        except ValueError:
            tail = " | ".join(
                l.strip() for l in (r.stderr or r.stdout or "").splitlines()[-2:] if l.strip()
            )
            self._fail(f"rclone config: {tail or 'неожиданный ответ'}")
            return

        self._rclone_state = data.get("State") or ""
        option = data.get("Option")
        error = data.get("Error") or ""

        if not self._rclone_state:
            self._finish()
            return

        name = (option or {}).get("Name")
        # Технические вопросы — отвечаем сами
        if name == "config_is_local" and not error:
            self._continue("false")
            return
        if name == "config_token" and not error:
            self._continue(self._token)
            return

        with self._lock:
            self.state = "question"
            self.message = error
            self.question = _question_view(option or {}, error)

    def _finish(self) -> None:
        conf = self._tmpdir / "rclone.conf" if self._tmpdir else None
        section = rclone_mod.read_remote_section(conf, _WIZARD_NAME) if conf else {}
        section.pop("type", None)
        with self._lock:
            if not section.get("token"):
                self._fail("rclone не сохранил токен")
                return
            self.result = section
            self.state = "done"
            self.question = None
            self.message = "Вход выполнен"
        self._cleanup()

    # -----------------------------------------------------
    # Служебное
    # -----------------------------------------------------

    def _fail(self, message: str) -> None:
        with self._lock:
            self.state = "error"
            self.message = message
            self.question = None
        logger.warning("oauth[%s]: %s", self.type_name, message)
        self._cleanup()

    def cancel(self) -> None:
        with self._lock:
            if self.state in ("done", "error"):
                return
            self.state = "error"
            self.message = "Вход отменён"
        self._cleanup()

    def _cleanup(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def summary(self) -> str:
        """Что получено — без секретов."""
        keys = [k for k in ("drive_type", "drive_id", "team_drive", "root_folder_id")
                if self.result.get(k)]
        return ", ".join(f"{k}={self.result[k]}" for k in keys)

    def view(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "state": self.state,
                "url": self.url,
                "message": self.message,
                "question": self.question,
                "summary": self.summary() if self.state == "done" else "",
            }


def _question_view(option: dict, error: str) -> dict:
    help_text = (option.get("Help") or "").strip()
    examples = [
        {"value": str(e.get("Value", "")), "help": (e.get("Help") or "").strip()}
        for e in (option.get("Examples") or [])
    ]
    return {
        "name": option.get("Name") or "",
        "help": help_text,
        "type": option.get("Type") or "string",
        "examples": examples,
        "exclusive": bool(option.get("Exclusive")),
        "default": option.get("DefaultStr") or "",
        "required": bool(option.get("Required")),
        "error": error,
    }


# =========================================================
# Реестр потоков
# =========================================================

_TMP_PREFIX = "nbm_oauth_"


def _gc() -> None:
    now = time.time()
    with _flows_lock:
        old = [fid for fid, f in _flows.items() if now - f.created > FLOW_TTL_SECONDS]
        for fid in old:
            _flows.pop(fid).cancel()
        in_use = {f._tmpdir for f in _flows.values() if f._tmpdir is not None}
    _sweep_stale_tmpdirs(now, in_use)


def _sweep_stale_tmpdirs(now: float, in_use: set) -> None:
    """
    Временные конфиги мастера (в них токен), брошенные
    прошлым запуском программы — удаляем по истечении TTL.
    """
    try:
        candidates = list(Path(tempfile.gettempdir()).glob(_TMP_PREFIX + "*"))
    except OSError:
        return
    for path in candidates:
        if path in in_use:
            continue
        try:
            if now - path.stat().st_mtime > FLOW_TTL_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def start(type_name: str, options: dict, token: str = "") -> OAuthFlow:
    """
    Новый поток входа. token — вставленный вручную (тогда
    сразу мастер); иначе — вход через браузер.
    """
    _gc()
    flow = OAuthFlow(type_name, options)
    with _flows_lock:
        _flows[flow.id] = flow
    if token:
        flow.start_with_token(token)
    else:
        flow.start_authorize()
        # Дождаться ссылки (обычно < 1 с)
        for _ in range(50):
            if flow.url or flow.state != "authorizing":
                break
            time.sleep(0.1)
    return flow


def get(flow_id: str) -> OAuthFlow | None:
    _gc()
    with _flows_lock:
        return _flows.get(flow_id or "")


def cancel_authorizing(except_id: str = "") -> None:
    with _flows_lock:
        flows = [f for fid, f in _flows.items() if fid != except_id]
    for f in flows:
        if f.state == "authorizing":
            f.cancel()
