# =========================================================
# core/notify_channels.py — каналы доставки уведомлений
# =========================================================
#
# Канал знает только, КАК отправить готовое сообщение:
# Telegram (Bot API), почта (SMTP), webhook (POST JSON —
# подходит для Slack, Mattermost, Rocket.Chat, n8n и своих
# обработчиков). ЧТО и КОГДА отправлять — core/notifications.py.
#
# Поля настроек — StorageField (как у хранилищ): по ним
# строится форма; type="password" хранятся зашифрованными.
#
# send() бросает NotifyError с понятным текстом; секреты
# (токен бота, пароли) в текст ошибки не попадают.
#
# =========================================================

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from core.storage.base import StorageField
from core.version import APP_NAME, __version__

TIMEOUT = 20


class NotifyError(Exception):
    pass


@dataclass
class Message:
    subject: str                    # одна строка — заголовок
    text: str                       # тело (без заголовка)
    level: str = "info"             # info / warning / error
    events: list = field(default_factory=list)   # события, из которых собрано

    def full_text(self) -> str:
        return f"{self.subject}\n\n{self.text}".strip()


def _http_post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, bytes]:
    """POST JSON. (код, тело). Подменяется в тестах."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": f"NetworkBackupManager/{__version__}",
                 **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except urllib.error.URLError as exc:
        raise NotifyError(f"нет соединения: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise NotifyError(f"нет соединения: {exc}") from None


class Channel:
    type = ""
    name = ""
    icon = ""
    description = ""
    fields: list[StorageField] = []

    def send(self, settings: dict, message: Message) -> None:
        raise NotImplementedError

    def secret_fields(self) -> list[str]:
        return [f.name for f in self.fields if f.type == "password"]

    def validate(self, settings: dict) -> str:
        """'' — настройки полные, иначе что не так."""
        missing = [f.label for f in self.fields if f.required and not str(settings.get(f.name) or "").strip()]
        return ("Не заполнено: " + ", ".join(missing)) if missing else ""


# =========================================================
# Telegram
# =========================================================

class TelegramChannel(Channel):
    type = "telegram"
    name = "Telegram"
    icon = "✈"
    description = "Сообщение от бота в личный чат, группу или канал."
    fields = [
        StorageField(
            name="bot_token", label="Токен бота", type="password", required=True,
            placeholder="123456789:AA…",
            help="Создайте бота у @BotFather — он выдаст токен.",
        ),
        StorageField(
            name="chat_id", label="ID чата", required=True, placeholder="123456789 или -100…",
            help="Напишите боту (или добавьте его в группу), затем откройте "
                 "https://api.telegram.org/bot<токен>/getUpdates — там chat.id.",
        ),
    ]

    LIMIT = 4000        # у Telegram 4096 символов на сообщение

    API = "https://api.telegram.org"

    def send(self, settings: dict, message: Message) -> None:
        token = str(settings.get("bot_token") or "").strip()
        chat_id = str(settings.get("chat_id") or "").strip()
        text = message.full_text()
        if len(text) > self.LIMIT:
            text = text[: self.LIMIT - 20] + "\n…(сокращено)"

        status, body = _http_post(
            f"{self.API}/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        try:
            answer = json.loads(body or b"{}")
        except ValueError:
            answer = {}
        if status == 200 and answer.get("ok"):
            return
        detail = answer.get("description") or f"HTTP {status}"
        if status == 401 or status == 404:
            detail = "неверный токен бота"
        raise NotifyError(f"Telegram: {detail}")


# =========================================================
# Почта
# =========================================================

class EmailChannel(Channel):
    type = "email"
    name = "Почта"
    icon = "✉"
    description = "Письмо через SMTP-сервер."
    fields = [
        StorageField(name="host", label="SMTP-сервер", required=True, placeholder="smtp.example.com"),
        StorageField(name="port", label="Порт", type="number", default=587, required=True),
        StorageField(
            name="security", label="Защита", type="select", default="starttls",
            choices=[("starttls", "STARTTLS (обычно порт 587)"),
                     ("ssl", "SSL/TLS (обычно порт 465)"),
                     ("none", "Без шифрования (порт 25)")],
        ),
        StorageField(name="username", label="Логин", help="Пусто — без авторизации."),
        StorageField(name="password", label="Пароль", type="password"),
        StorageField(name="sender", label="Отправитель", required=True, placeholder="nbm@example.com"),
        StorageField(
            name="recipients", label="Получатели", required=True,
            placeholder="admin@example.com, noc@example.com",
            help="Через запятую.",
        ),
    ]

    @staticmethod
    def _recipients(settings) -> list[str]:
        raw = str(settings.get("recipients") or "")
        return [r.strip() for r in raw.replace(";", ",").split(",") if r.strip()]

    def validate(self, settings: dict) -> str:
        err = super().validate(settings)
        if err:
            return err
        try:
            port = int(settings.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not 1 <= port <= 65535:
            return "Некорректный порт"
        if not self._recipients(settings):
            return "Не указаны получатели"
        return ""

    def send(self, settings: dict, message: Message) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[NBM] {message.subject}"
        msg["From"] = str(settings.get("sender") or "").strip()
        msg["To"] = ", ".join(self._recipients(settings))
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="nbm.local")
        msg.set_content(f"{message.text}\n\n— {APP_NAME}\n")

        host = str(settings.get("host") or "").strip()
        port = int(settings.get("port") or 587)
        security = settings.get("security") or "starttls"
        user = str(settings.get("username") or "").strip()
        password = str(settings.get("password") or "")

        try:
            if security == "ssl":
                smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                                        context=ssl.create_default_context())
            else:
                smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
            with smtp:
                if security == "starttls":
                    smtp.starttls(context=ssl.create_default_context())
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError:
            raise NotifyError("Почта: сервер отклонил логин или пароль") from None
        except smtplib.SMTPRecipientsRefused:
            raise NotifyError("Почта: сервер отклонил получателей") from None
        except smtplib.SMTPException as exc:
            raise NotifyError(f"Почта: {exc}") from None
        except (OSError, ssl.SSLError) as exc:
            raise NotifyError(f"Почта: нет соединения с {host}:{port} — {exc}") from None


# =========================================================
# Webhook
# =========================================================

class WebhookChannel(Channel):
    type = "webhook"
    name = "Webhook"
    icon = "🔗"
    description = ("POST JSON на ваш адрес. Поле text — готовый текст: подходит "
                   "для входящих webhook Slack, Mattermost, Rocket.Chat.")
    fields = [
        StorageField(name="url", label="Адрес", required=True, placeholder="https://…"),
        StorageField(name="header_name", label="Заголовок авторизации",
                     placeholder="Authorization", help="Необязательно."),
        StorageField(name="header_value", label="Значение заголовка", type="password",
                     placeholder="Bearer …"),
    ]

    def validate(self, settings: dict) -> str:
        err = super().validate(settings)
        if err:
            return err
        url = str(settings.get("url") or "").strip().lower()
        if not url.startswith(("http://", "https://")):
            return "Адрес должен начинаться с http:// или https://"
        return ""

    def send(self, settings: dict, message: Message) -> None:
        headers = {}
        name = str(settings.get("header_name") or "").strip()
        if name:
            headers[name] = str(settings.get("header_value") or "")
        payload = {
            "text": message.full_text(),
            "subject": message.subject,
            "body": message.text,
            "level": message.level,
            "app": APP_NAME,
            "version": __version__,
            "events": [
                {k: e.get(k) for k in ("id", "at", "kind", "level", "message", "device_id", "data")}
                for e in message.events
            ],
        }
        status, body = _http_post(str(settings.get("url")).strip(), payload, headers)
        if not 200 <= status < 300:
            snippet = (body or b"")[:200].decode("utf-8", errors="replace").strip()
            raise NotifyError(f"Webhook: HTTP {status}" + (f" — {snippet}" if snippet else ""))


CHANNELS: dict[str, Channel] = {
    c.type: c for c in (TelegramChannel(), EmailChannel(), WebhookChannel())
}
