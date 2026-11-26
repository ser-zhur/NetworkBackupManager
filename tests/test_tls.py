# =========================================================
# tests/test_tls.py — HTTPS: сертификат, запуск, значок 🔓
# =========================================================
#
# Сертификаты выпускаются на лету через cryptography (она уже
# есть — зависимость paramiko). Проверяется разбор настроек,
# отказ запускаться с негодным сертификатом, параметры для
# uvicorn, значок «🔓 HTTP» в шапке и docker/healthcheck.py
# против настоящих HTTP- и HTTPS-серверов.
#
# =========================================================

import http.server
import os
import ssl
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import context, settings

crypto = pytest.importorskip("cryptography")
from cryptography import x509                                      # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization   # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec           # noqa: E402
from cryptography.x509.oid import NameOID                          # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def make_cert(folder: Path, days: int = 365, password: bytes | None = None, combined: bool = False):
    """Самоподписанный сертификат на localhost; (cert, key) или (combined, None)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    enc = (serialization.BestAvailableEncryption(password) if password
           else serialization.NoEncryption())
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    folder.mkdir(parents=True, exist_ok=True)
    if combined:
        (folder / "both.pem").write_bytes(cert_pem + key_pem)
        return folder / "both.pem", None
    (folder / "cert.pem").write_bytes(cert_pem)
    (folder / "key.pem").write_bytes(key_pem)
    return folder / "cert.pem", folder / "key.pem"


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    monkeypatch.setattr(context, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("NBM_TLS_CERT", raising=False)
    monkeypatch.delenv("NBM_TLS_KEY", raising=False)
    return tmp_path


# ---------------------------------------------------------
# Настройки
# ---------------------------------------------------------

def test_no_tls_by_default(data):
    assert settings.tls_files() is None


def test_relative_paths_from_data_dir(data, monkeypatch):
    cert, key = make_cert(data / "tls", days=400)
    monkeypatch.setenv("NBM_TLS_CERT", "tls/cert.pem")
    monkeypatch.setenv("NBM_TLS_KEY", "tls/key.pem")
    assert settings.tls_files() == (cert, key)
    expires = settings.cert_expires(cert)
    assert expires and 398 <= (expires - datetime.now(timezone.utc)).days <= 400


def test_config_json_and_combined_file(data):
    both, _ = make_cert(data / "tls", combined=True)
    context.save_json(context.CONFIG_FILE, {"tls_cert": str(both)})      # абсолютный путь
    assert settings.tls_files() == (both, None)


def test_env_wins_over_config(data, monkeypatch):
    cert, key = make_cert(data / "a")
    context.save_json(context.CONFIG_FILE, {"tls_cert": "missing.pem"})
    monkeypatch.setenv("NBM_TLS_CERT", str(cert))
    monkeypatch.setenv("NBM_TLS_KEY", str(key))
    assert settings.tls_files() == (cert, key)


@pytest.mark.parametrize("case, words", [
    ("key_only", "не задан NBM_TLS_CERT"),
    ("missing", "Файл не найден"),
    ("garbage", "PEM"),
    ("no_key", "NBM_TLS_KEY"),
    ("encrypted", "без пароля"),
])
def test_bad_tls_settings(data, monkeypatch, case, words):
    cert, key = make_cert(data / "tls", password=b"secret" if case == "encrypted" else None)
    if case == "key_only":
        monkeypatch.setenv("NBM_TLS_KEY", str(key))
    elif case == "missing":
        monkeypatch.setenv("NBM_TLS_CERT", "tls/nope.pem")
    elif case == "garbage":
        (data / "tls" / "cert.pem").write_text("not a certificate")
        monkeypatch.setenv("NBM_TLS_CERT", str(cert))
        monkeypatch.setenv("NBM_TLS_KEY", str(key))
    elif case == "no_key":
        monkeypatch.setenv("NBM_TLS_CERT", str(cert))       # ключ в другом файле, не указан
    else:
        # ключ с паролем: ошибка, а не вопрос пароля в консоли
        monkeypatch.setenv("NBM_TLS_CERT", str(cert))
        monkeypatch.setenv("NBM_TLS_KEY", str(key))
    with pytest.raises(settings.TlsConfigError, match=words):
        settings.tls_files()


# ---------------------------------------------------------
# Запуск веб-сервера
# ---------------------------------------------------------

@pytest.fixture
def run_calls(monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    return calls


def test_run_web_server_with_tls(data, monkeypatch, run_calls, caplog):
    import main
    cert, key = make_cert(data / "tls", days=10)
    monkeypatch.setenv("NBM_TLS_CERT", "tls/cert.pem")
    monkeypatch.setenv("NBM_TLS_KEY", "tls/key.pem")
    main.run_web_server()
    assert run_calls[0]["ssl_certfile"] == str(cert) and run_calls[0]["ssl_keyfile"] == str(key)
    assert "истекает" in caplog.text


def test_run_web_server_refuses_bad_cert(data, monkeypatch, run_calls):
    import main
    monkeypatch.setenv("NBM_TLS_CERT", "tls/nope.pem")
    with pytest.raises(SystemExit):
        main.run_web_server()
    assert run_calls == []                       # на HTTP молча не переходит


def test_run_web_server_plain_http(data, run_calls):
    import main
    main.run_web_server()
    assert "ssl_certfile" not in run_calls[0]


# ---------------------------------------------------------
# Значок «🔓 HTTP»
# ---------------------------------------------------------

def _request(url: str, client: str):
    from starlette.requests import Request
    scheme, rest = url.split("://")
    host = rest.split("/")[0]
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "scheme": scheme, "server": (host.rsplit(":", 1)[0], 8080),
        "client": (client, 50000), "headers": [(b"host", host.encode())],
    })


@pytest.mark.parametrize("url, client, insecure", [
    ("http://192.0.2.10:8080", "192.0.2.20", True),      # по сети, HTTP
    ("https://192.0.2.10:8080", "192.0.2.20", False),    # HTTPS
    ("http://localhost:8080", "172.17.0.1", False),      # Docker: с сервера через localhost
    ("http://127.0.0.1:8080", "127.0.0.1", False),
    ("http://[::1]:8080", "::1", False),
    ("http://192.0.2.10:8080", "127.0.0.1", False),      # браузер на самом сервере
    ("http://nbm.example:8080", "192.0.2.20", True),
])
def test_insecure_connection(url, client, insecure):
    s = pytest.importorskip("web.server")
    assert s.insecure_connection(_request(url, client)) is insecure


@pytest.fixture
def web():
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    yield s, TestClient
    s.app.dependency_overrides.clear()


def test_header_badge(web):
    s, TestClient = web
    # TestClient: клиент «testclient», хост «testserver» — как по сети
    page = TestClient(s.app).get("/").text
    assert 'data-insecure="1"' in page and 'href="/settings?tab=https" data-insecure' in page
    assert 'data-insecure' not in TestClient(s.app, base_url="https://testserver").get("/").text
    assert 'data-insecure' not in TestClient(s.app, base_url="http://localhost").get("/").text


# ---------------------------------------------------------
# docker/healthcheck.py
# ---------------------------------------------------------

class _Health(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status if self.path == "/api/health" else 404)
        self.end_headers()

    def log_message(self, *a):
        pass


def _serve(cert=None, key=None, status=200):
    handler = type("H", (_Health,), {"status": status})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _healthcheck(port: int) -> int:
    env = {**os.environ, "NBM_PORT": str(port)}
    return subprocess.run([sys.executable, str(ROOT / "docker" / "healthcheck.py")],
                          env=env, timeout=30).returncode


@pytest.mark.parametrize("tls, status, code", [
    (False, 200, 0), (True, 200, 0), (True, 503, 1), (False, 503, 1),
])
def test_healthcheck_script(tmp_path, tls, status, code):
    cert, key = make_cert(tmp_path) if tls else (None, None)
    srv = _serve(cert, key, status)
    try:
        assert _healthcheck(srv.server_address[1]) == code
    finally:
        srv.shutdown()


def test_healthcheck_nothing_listening():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert _healthcheck(port) == 1
