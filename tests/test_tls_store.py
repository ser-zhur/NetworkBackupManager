# =========================================================
# tests/test_tls_store.py — сертификаты из интерфейса
# =========================================================
#
# Цепочка «корневой → промежуточный → сервер» выпускается на
# лету (cryptography). Проверяются форматы загрузки (PEM
# раздельно и одним файлом, DER, PFX, ключ с паролем),
# ошибки, назначение в config.json, удаление, NBM_TLS_CERT=off
# и вкладка Настройки → HTTPS с перезапуском.
#
# =========================================================

import os
from datetime import datetime, timedelta, timezone

import pytest

from core import context, monitoring, settings, tls_store

pytest.importorskip("cryptography")
from cryptography import x509                                          # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization       # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, ed25519      # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs12        # noqa: E402
from cryptography.x509.oid import NameOID                              # noqa: E402

PEM = serialization.Encoding.PEM
DER = serialization.Encoding.DER


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject, key, issuer_name, issuer_key, *, ca=False, sans=("nbm.example",), days=365):
    now = datetime.now(timezone.utc)
    b = (x509.CertificateBuilder()
         .subject_name(_name(subject)).issuer_name(_name(issuer_name))
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if sans:
        import ipaddress
        names = []
        for s in sans:
            try:
                names.append(x509.IPAddress(ipaddress.ip_address(s)))
            except ValueError:
                names.append(x509.DNSName(s))
        b = b.add_extension(x509.SubjectAlternativeName(names), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def pki():
    root_key = ec.generate_private_key(ec.SECP256R1())
    root = _cert("Test Root", root_key, "Test Root", root_key, ca=True, sans=())
    inter_key = ec.generate_private_key(ec.SECP256R1())
    inter = _cert("Test Intermediate", inter_key, "Test Root", root_key, ca=True, sans=())
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = _cert("nbm.example", key, "Test Intermediate", inter_key,
                 sans=("nbm.example", "*.lan.example", "192.0.2.10"))
    return {"root": root, "inter": inter, "leaf": leaf, "key": key}


def key_pem(key, password=None):
    enc = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return key.private_bytes(PEM, serialization.PrivateFormat.PKCS8, enc)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    monkeypatch.setattr(context, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("NBM_TLS_CERT", raising=False)
    monkeypatch.delenv("NBM_TLS_KEY", raising=False)
    monkeypatch.setattr(settings, "RUNNING_CERT", "")
    return tmp_path


# ---------------------------------------------------------
# Форматы
# ---------------------------------------------------------

def test_separate_pem_files(data, pki):
    cert_id, new = tls_store.import_files([
        ("server.crt", pki["leaf"].public_bytes(PEM)),
        ("server.key", key_pem(pki["key"])),
        ("chain.pem", pki["inter"].public_bytes(PEM) + pki["root"].public_bytes(PEM)),
    ])
    assert new and cert_id.startswith("nbm.example-")
    [item] = tls_store.list_certs()
    assert item["id"] == cert_id and item["cn"] == "nbm.example" and not item["active"]
    assert item["sans"] == ["nbm.example", "*.lan.example", "192.0.2.10"]
    assert item["issuer"] == "Test Intermediate" and item["chain_len"] == 3
    assert item["warnings"] == [] and item["error"] == ""
    # в файле: сервер, затем промежуточный и корневой
    chain = x509.load_pem_x509_certificates((data / "tls" / cert_id / "cert.pem").read_bytes())
    assert [c.subject for c in chain] == [pki["leaf"].subject, pki["inter"].subject, pki["root"].subject]
    if os.name == "posix":
        assert (data / "tls" / cert_id / "key.pem").stat().st_mode & 0o777 == 0o600

    # тот же сертификат второй раз не сохраняется
    assert tls_store.import_files([("all.pem", pki["leaf"].public_bytes(PEM) + key_pem(pki["key"]))]) == (cert_id, False)
    assert len(tls_store.list_certs()) == 1


def test_one_file_any_order_and_unrelated_cert(data, pki):
    other_key = ec.generate_private_key(ec.SECP256R1())
    unrelated = _cert("other", other_key, "other", other_key)
    blob = (pki["inter"].public_bytes(PEM) + key_pem(pki["key"])
            + unrelated.public_bytes(PEM) + pki["leaf"].public_bytes(PEM))
    key, chain = tls_store.parse([("bundle.pem", blob)])
    assert [c.subject for c in chain] == [pki["leaf"].subject, pki["inter"].subject]


def test_pfx_with_password(data, pki):
    pfx = pkcs12.serialize_key_and_certificates(
        b"nbm", pki["key"], pki["leaf"], [pki["inter"]],
        serialization.BestAvailableEncryption(b"s3cret"))
    with pytest.raises(tls_store.CertError, match="укажите его"):
        tls_store.parse([("cert.pfx", pfx)])
    with pytest.raises(tls_store.CertError, match="проверьте пароль"):
        tls_store.parse([("cert.pfx", pfx)], "wrong")
    cert_id, _ = tls_store.import_files([("cert.pfx", pfx)], "s3cret")
    # ключ сохранён без пароля — веб-сервер его прочитает
    settings.check_tls_pair(data / "tls" / cert_id / "cert.pem", data / "tls" / cert_id / "key.pem")


def test_encrypted_pem_key(data, pki):
    files = [("c.pem", pki["leaf"].public_bytes(PEM)), ("k.pem", key_pem(pki["key"], b"pw"))]
    with pytest.raises(tls_store.CertError, match="защищён паролем"):
        tls_store.parse(files)
    with pytest.raises(tls_store.CertError, match="неверный пароль"):
        tls_store.parse(files, "nope")
    cert_id, _ = tls_store.import_files(files, "pw")
    assert b"ENCRYPTED" not in (data / "tls" / cert_id / "key.pem").read_bytes()


def test_der_files(data, pki):
    key_der = pki["key"].private_bytes(DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    key, chain = tls_store.parse([("c.cer", pki["leaf"].public_bytes(DER)), ("k.der", key_der)])
    assert chain[0] == pki["leaf"]


@pytest.mark.parametrize("case, words", [
    ("no_key", "Нет закрытого ключа"),
    ("wrong_key", "не подходит"),
    ("two_keys", "несколько"),
    ("csr", "CSR"),
    ("garbage", "PEM, DER или PFX"),
    ("only_key", "Сертификат не найден"),
    ("ed25519", "RSA или ECDSA"),
])
def test_bad_uploads(data, pki, case, words):
    leaf = ("c.pem", pki["leaf"].public_bytes(PEM))
    other = ec.generate_private_key(ec.SECP256R1())
    files = {
        "no_key": [leaf],
        "wrong_key": [leaf, ("k.pem", key_pem(other))],
        "two_keys": [leaf, ("k.pem", key_pem(pki["key"]) + key_pem(other))],
        "csr": [("req.csr", x509.CertificateSigningRequestBuilder().subject_name(_name("x"))
                 .sign(other, hashes.SHA256()).public_bytes(PEM))],
        "garbage": [("c.cer", b"\x00\x01 not a cert")],
        "only_key": [("k.pem", key_pem(pki["key"]))],
    }.get(case)
    if case == "ed25519":
        ed = ed25519.Ed25519PrivateKey.generate()
        cert = (x509.CertificateBuilder().subject_name(_name("ed")).issuer_name(_name("ed"))
                .public_key(ed.public_key()).serial_number(1)
                .not_valid_before(datetime.now(timezone.utc))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .sign(ed, None))
        files = [("c.pem", cert.public_bytes(PEM) + key_pem(ed))]
    with pytest.raises(tls_store.CertError, match=words):
        tls_store.import_files(files)
    assert tls_store.list_certs() == []


def test_warnings(pki):
    key = ec.generate_private_key(ec.SECP256R1())
    self_signed = _cert("nbm", key, "nbm", key, sans=(), days=10)
    info = tls_store.describe([self_signed])
    text = " ".join(info["warnings"])
    assert "через" in text and "subjectAltName" in text and "Самоподписанный" in text
    assert "промежуточных" in tls_store.describe([pki["leaf"]])["warnings"][0]
    later = datetime.now(timezone.utc) + timedelta(days=400)
    assert "истёк" in tls_store.describe([pki["leaf"]], now=later)["warnings"][0]


@pytest.mark.parametrize("host, ok", [
    ("nbm.example", True), ("NBM.example", True), ("a.lan.example", True),
    ("b.a.lan.example", False), ("lan.example", False), ("192.0.2.10", True),
    ("192.0.2.11", False), ("other.example", False),
])
def test_host_matches(host, ok):
    assert tls_store.host_matches(host, ["nbm.example", "*.lan.example", "192.0.2.10"]) is ok


# ---------------------------------------------------------
# Назначение, удаление, NBM_TLS_CERT
# ---------------------------------------------------------

def _upload(pki):
    return tls_store.import_files([("all.pem", pki["leaf"].public_bytes(PEM) + key_pem(pki["key"]))])[0]


def test_assign_delete_unassign(data, pki):
    cert_id = _upload(pki)
    tls_store.assign(cert_id)
    assert context.load_json(context.CONFIG_FILE, {}) == {
        "tls_cert": f"tls/{cert_id}/cert.pem", "tls_key": f"tls/{cert_id}/key.pem"}
    assert settings.tls_files() == (data / "tls" / cert_id / "cert.pem", data / "tls" / cert_id / "key.pem")
    assert tls_store.configured_id() == cert_id and tls_store.list_certs()[0]["active"]
    assert not tls_store.running_matches_config()                 # нужен перезапуск

    with pytest.raises(tls_store.CertError, match="назначен"):
        tls_store.delete(cert_id)
    with pytest.raises(tls_store.CertError, match="Неверное имя"):
        tls_store.delete("../config.json")
    with pytest.raises(tls_store.CertError, match="Неверное имя"):
        tls_store.assign("..")

    settings.RUNNING_CERT = str(data / "tls" / cert_id / "cert.pem")
    assert tls_store.running_matches_config()

    tls_store.unassign()
    assert settings.tls_files() is None and context.load_json(context.CONFIG_FILE, {}) == {}
    tls_store.delete(cert_id)
    assert tls_store.list_certs() == [] and not (data / "tls" / cert_id).exists()


def test_assign_refuses_broken_files(data, pki):
    cert_id = _upload(pki)
    (data / "tls" / cert_id / "key.pem").write_text("broken")
    with pytest.raises(tls_store.CertError):
        tls_store.assign(cert_id)
    assert context.load_json(context.CONFIG_FILE, {}) == {}
    assert "error" in tls_store.list_certs()[0]


def test_env_off_and_env_path(data, pki, monkeypatch):
    cert_id = _upload(pki)
    tls_store.assign(cert_id)
    monkeypatch.setenv("NBM_TLS_CERT", "off")
    assert settings.tls_files() is None and settings.tls_env_override() == "off"
    monkeypatch.setenv("NBM_TLS_CERT", "tls/missing.pem")
    with pytest.raises(settings.TlsConfigError):
        settings.tls_files()
    monkeypatch.delenv("NBM_TLS_CERT")
    monkeypatch.setenv("NBM_TLS_KEY", "k.pem")
    with pytest.raises(settings.TlsConfigError, match="NBM_TLS_KEY"):
        settings.tls_files()


def test_monitoring_warns_on_expiring_cert(data, monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    path = data / "c.pem"
    path.write_bytes(_cert("nbm", key, "nbm", key, days=5).public_bytes(PEM))
    monkeypatch.setattr(settings, "RUNNING_CERT", str(path))
    assert any("Сертификат HTTPS истекает" in w for w in monitoring.status()["warnings"])


# ---------------------------------------------------------
# Веб: Настройки → HTTPS
# ---------------------------------------------------------

@pytest.fixture
def web(data, monkeypatch):
    s = pytest.importorskip("web.server")
    pytest.importorskip("httpx2", reason="нужен httpx2 для TestClient")
    from fastapi.testclient import TestClient
    restarts = []
    monkeypatch.setattr(s, "_restart_program",
                        lambda reason, hint="": restarts.append(hint) or (True, "Перезапуск. " + hint))
    s.app.dependency_overrides[s.require_auth] = lambda: "test"
    client = TestClient(s.app, base_url="https://nbm.example:8443", headers={"Origin": "https://nbm.example:8443"})
    yield client, restarts
    s.app.dependency_overrides.clear()


def test_web_upload_assign_restart(web, pki, data):
    client, restarts = web
    page = client.get("/settings?tab=https").text
    assert "Сертификатов ещё нет" in page and 'data-running="http"' in page
    assert "data-restart-needed" not in page and "data-upload-insecure" not in page

    r = client.post("/settings/https/upload", files={
        "cert": ("server.crt", pki["leaf"].public_bytes(PEM)),
        "key": ("server.key", key_pem(pki["key"], b"pw")),
        "chain": ("chain.pem", pki["inter"].public_bytes(PEM)),
    }, data={"password": "pw"})
    assert r.status_code == 200 and "Сертификат загружен" in r.text
    cert_id = tls_store.list_certs()[0]["id"]
    assert f'data-cert="{cert_id}"' in r.text and "data-host-mismatch" not in r.text

    r = client.post("/settings/https/assign", data={"id": cert_id})
    assert "Сертификат назначен" in r.text and "data-restart-needed" in r.text and "data-active" in r.text

    r = client.post("/settings/https/restart")
    assert restarts and "https://nbm.example:8443" in restarts[0]
    assert 'href="https://nbm.example:8443/settings?tab=https"' in r.text

    r = client.post("/settings/https/delete", data={"id": cert_id})
    assert "назначен" in r.text and tls_store.list_certs()
    client.post("/settings/https/disable")
    r = client.post("/settings/https/delete", data={"id": cert_id})
    assert "Сертификат удалён" in r.text and not tls_store.list_certs()


def test_web_upload_error_and_insecure_hint(web, pki, data):
    s = pytest.importorskip("web.server")
    from fastapi.testclient import TestClient
    client = TestClient(s.app, base_url="http://192.0.2.10:8080")   # HTTP по сети
    r = client.post("/settings/https/upload", headers={"Origin": "http://192.0.2.10:8080"},
                    files={"cert": ("c.pem", pki["leaf"].public_bytes(PEM))})
    assert "Нет закрытого ключа" in r.text and "data-upload-insecure" in r.text


def test_web_env_locks_assign_and_broken_config_blocks_restart(web, pki, data, monkeypatch):
    client, restarts = web
    cert_id = _upload(pki)
    monkeypatch.setenv("NBM_TLS_CERT", "off")
    r = client.post("/settings/https/assign", data={"id": cert_id})
    assert "NBM_TLS_CERT" in r.text and "data-env-locked" in r.text
    assert context.load_json(context.CONFIG_FILE, {}) == {}
    monkeypatch.delenv("NBM_TLS_CERT")

    context.save_json(context.CONFIG_FILE, {"tls_cert": "tls/missing/cert.pem"})
    r = client.post("/settings/https/restart")
    assert "Перезапуск отменён" in r.text and restarts == []
