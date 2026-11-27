# =========================================================
# core/tls_store.py — сертификаты HTTPS (Настройки → HTTPS)
# =========================================================
#
# Загруженные сертификаты лежат в data/tls/<id>/:
#
#     cert.pem   сертификат сервера и за ним цепочка (PEM)
#     key.pem    закрытый ключ без пароля (PKCS#8 PEM)
#
# Назначенный записывается в config.json (tls_cert/tls_key,
# пути от data/) — его подхватывает core.settings.tls_files()
# при следующем запуске веб-сервера.
#
# Принимаются PEM (сертификат, ключ и цепочка — одним файлом
# или несколькими), DER и PFX/P12. Ключ с паролем и PFX
# расшифровываются паролем из формы: веб-сервер не умеет
# спрашивать пароль ключа при запуске. Генерации нет.
#
# =========================================================

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core import context, settings

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 256 * 1024
EXPIRE_SOON_DAYS = 30

_PEM_RE = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----.+?-----END \1-----", re.S)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class CertError(Exception):
    """Понятная пользователю ошибка."""


def tls_dir() -> Path:
    return context.DATA_DIR / "tls"


# =========================================================
# Разбор загруженных файлов
# =========================================================

def _load_pem(data: bytes, name: str, password: bytes | None, certs: list, keys: list) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    blocks = list(_PEM_RE.finditer(data))
    if not blocks:
        raise CertError(f"{name}: в файле нет PEM-блоков")
    for block in blocks:
        kind = block.group(1).decode()
        raw = block.group(0)
        if kind in ("CERTIFICATE", "TRUSTED CERTIFICATE", "X509 CERTIFICATE"):
            try:
                certs.append(x509.load_pem_x509_certificate(raw.replace(b"TRUSTED CERTIFICATE", b"CERTIFICATE")))
            except ValueError as exc:
                raise CertError(f"{name}: повреждённый сертификат ({exc})") from exc
        elif kind.endswith("PRIVATE KEY"):
            keys.append(_load_key(lambda pw: serialization.load_pem_private_key(raw, pw), name, password))
        elif "REQUEST" in kind:
            raise CertError(f"{name}: это запрос на сертификат (CSR), а не сам сертификат")
        # прочие блоки (параметры DH, ключи EC PARAMETERS) — пропускаем


def _load_key(loader, name: str, password: bytes | None):
    try:
        return loader(None)
    except TypeError:
        # ключ зашифрован
        if not password:
            raise CertError(f"{name}: закрытый ключ защищён паролем — укажите пароль") from None
        try:
            return loader(password)
        except (ValueError, TypeError) as exc:
            raise CertError(f"{name}: неверный пароль ключа") from exc
    except ValueError as exc:
        raise CertError(f"{name}: не удалось прочитать закрытый ключ ({exc})") from exc


def _load_binary(data: bytes, name: str, password: bytes | None, certs: list, keys: list) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    try:
        certs.append(x509.load_der_x509_certificate(data))
        return
    except ValueError:
        pass
    try:
        keys.append(serialization.load_der_private_key(data, None))
        return
    except (ValueError, TypeError):
        pass
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, password or None)
    except ValueError as exc:
        hint = "проверьте пароль" if password else "если файл защищён паролем, укажите его"
        raise CertError(f"{name}: не удалось прочитать как PEM, DER или PFX/P12 — {hint}") from exc
    if key is not None:
        keys.append(key)
    if cert is not None:
        certs.append(cert)
    certs.extend(extra or [])


def _public_bytes(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return public_key.public_bytes(serialization.Encoding.DER,
                                   serialization.PublicFormat.SubjectPublicKeyInfo)


def parse(files: list[tuple[str, bytes]], password: str = ""):
    """
    (ключ, [сертификат сервера, промежуточные…]) из загруженных
    файлов. Ключ должен быть ровно один и подходить к одному из
    сертификатов; цепочка выстраивается по издателю, лишние
    сертификаты отбрасываются.
    """
    from cryptography.hazmat.primitives.asymmetric import ec, rsa

    pw = password.encode() if password else None
    certs: list = []
    keys: list = []
    for name, data in files:
        if not data:
            continue
        if len(data) > MAX_FILE_BYTES:
            raise CertError(f"{name}: файл слишком большой для сертификата")
        if b"-----BEGIN" in data:
            _load_pem(data, name, pw, certs, keys)
        else:
            _load_binary(data, name, pw, certs, keys)

    if not certs:
        raise CertError("Сертификат не найден: загрузите файл сертификата (PEM, CRT, CER или PFX)")
    unique_keys = {_public_bytes(k.public_key()): k for k in keys}
    if not unique_keys:
        raise CertError("Нет закрытого ключа: загрузите файл ключа вместе с сертификатом "
                        "(или PFX, в котором есть ключ)")
    if len(unique_keys) > 1:
        raise CertError("Загружено несколько разных закрытых ключей — нужен один")
    key = next(iter(unique_keys.values()))
    if not isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        raise CertError("Ключ этого типа браузеры не поддерживают — нужен RSA или ECDSA")

    by_fp = {}
    for c in certs:
        by_fp.setdefault(c.fingerprint(_sha256()), c)
    certs = list(by_fp.values())

    key_pub = _public_bytes(key.public_key())
    leaf = next((c for c in certs if _public_bytes(c.public_key()) == key_pub), None)
    if leaf is None:
        raise CertError("Закрытый ключ не подходит ни к одному из загруженных сертификатов")

    chain = [leaf]
    rest = [c for c in certs if c is not leaf]
    while rest and chain[-1].issuer != chain[-1].subject:
        nxt = next((c for c in rest if c.subject == chain[-1].issuer), None)
        if nxt is None:
            break
        chain.append(nxt)
        rest.remove(nxt)
    return key, chain


def _sha256():
    from cryptography.hazmat.primitives import hashes
    return hashes.SHA256()


# =========================================================
# Сведения о сертификате
# =========================================================

def _names(cert) -> tuple[str, list[str]]:
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    cn = ""
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if attrs:
        cn = str(attrs[0].value)
    sans: list[str] = []
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        sans = [str(v) for v in ext.get_values_for_type(x509.DNSName)]
        sans += [str(v) for v in ext.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        pass
    return cn, sans


def _issuer_name(cert) -> str:
    from cryptography.x509.oid import NameOID
    for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
        attrs = cert.issuer.get_attributes_for_oid(oid)
        if attrs:
            return str(attrs[0].value)
    return cert.issuer.rfc4514_string()


def host_matches(host: str, sans: list[str]) -> bool:
    """Подходит ли имя или адрес к subjectAltName (с * в первой части)."""
    import ipaddress

    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return any(_same_ip(ip, s) for s in sans)
    except ValueError:
        pass
    for name in sans:
        name = name.lower().rstrip(".")
        if name == host:
            return True
        if name.startswith("*.") and "." in host and host.split(".", 1)[1] == name[2:]:
            return True
    return False


def _same_ip(ip, value: str) -> bool:
    import ipaddress
    try:
        return ipaddress.ip_address(value) == ip
    except ValueError:
        return False


def describe(chain: list, now: datetime | None = None) -> dict:
    """Сведения для интерфейса и предупреждения."""
    now = now or datetime.now(timezone.utc)
    leaf = chain[0]
    cn, sans = _names(leaf)
    not_before = leaf.not_valid_before_utc
    not_after = leaf.not_valid_after_utc
    days_left = (not_after - now).days
    self_signed = leaf.issuer == leaf.subject

    warnings = []
    if now > not_after:
        warnings.append(f"Срок действия истёк {not_after:%d.%m.%Y} — браузеры его не примут")
    elif days_left <= EXPIRE_SOON_DAYS:
        warnings.append(f"Срок действия заканчивается через {days_left} дн.")
    if now < not_before:
        warnings.append(f"Начнёт действовать только {not_before:%d.%m.%Y}")
    if not sans:
        warnings.append("Нет subjectAltName: современные браузеры такой сертификат не примут")
    if self_signed:
        warnings.append("Самоподписанный: браузер предупредит, пока сертификат не добавлен в доверенные")
    elif len(chain) == 1:
        warnings.append("Загружен только сам сертификат, без промежуточных: если его выпустил "
                        "промежуточный центр сертификации, загрузите и его, иначе браузер может не доверять")

    return {
        "cn": cn,
        "sans": sans,
        "issuer": "" if self_signed else _issuer_name(leaf),
        "self_signed": self_signed,
        "not_before": not_before,
        "not_after": not_after,
        "days_left": days_left,
        "expired": now > not_after,
        "chain_len": len(chain),
        "fingerprint": leaf.fingerprint(_sha256()).hex(":").upper(),
        "warnings": warnings,
    }


# =========================================================
# Хранилище data/tls/<id>/
# =========================================================

def _make_id(cn: str, sans: list[str], not_after: datetime) -> str:
    base = (cn or (sans[0] if sans else "") or "cert").lower()
    base = re.sub(r"[^a-z0-9.-]+", "-", base.replace("*", "wildcard")).strip(".-") or "cert"
    base = f"{base[:40]}-{not_after:%Y%m%d}"
    candidate, n = base, 2
    while (tls_dir() / candidate).exists():
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _paths(cert_id: str) -> tuple[Path, Path]:
    if not _ID_RE.match(cert_id or ""):
        raise CertError("Неверное имя сертификата")
    folder = tls_dir() / cert_id
    return folder / "cert.pem", folder / "key.pem"


def _read_chain(cert_path: Path) -> list:
    from cryptography import x509
    return x509.load_pem_x509_certificates(cert_path.read_bytes())


def import_files(files: list[tuple[str, bytes]], password: str = "") -> tuple[str, bool]:
    """
    Сохраняет сертификат в data/tls/<id>/. (id, новый ли): тот же
    сертификат второй раз не сохраняется — возвращается прежний.
    """
    from cryptography.hazmat.primitives import serialization

    key, chain = parse(files, password)
    fingerprint = chain[0].fingerprint(_sha256())
    for item in list_certs():
        if item["fingerprint_raw"] == fingerprint:
            return item["id"], False

    info = describe(chain)
    cert_id = _make_id(info["cn"], info["sans"], info["not_after"])
    cert_pem = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())

    tls_dir().mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".new-", dir=tls_dir()))
    try:
        (tmp / "cert.pem").write_bytes(cert_pem)
        key_file = tmp / "key.pem"
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key_pem)
        settings.check_tls_pair(tmp / "cert.pem", key_file)
        tmp.rename(tls_dir() / cert_id)
    except settings.TlsConfigError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise CertError(str(exc)) from exc
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    logger.info("🔒 Загружен сертификат %s (%s, до %s)", cert_id, info["cn"], info["not_after"].date())
    return cert_id, True


def _rel(path: Path) -> str:
    return path.relative_to(context.DATA_DIR).as_posix()


def configured_id() -> str:
    """id назначенного в config.json сертификата из data/tls/ или ""."""
    cert_raw, _ = settings.tls_config_paths()
    m = re.fullmatch(r"tls/([^/]+)/cert\.pem", cert_raw.replace("\\", "/"))
    return m.group(1) if m else ""


def list_certs() -> list[dict]:
    """Загруженные сертификаты, новые (по сроку) первыми."""
    root = tls_dir()
    if not root.is_dir():
        return []
    active = configured_id()
    items = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith(".") or not _ID_RE.match(folder.name):
            continue
        cert_path, key_path = folder / "cert.pem", folder / "key.pem"
        item = {"id": folder.name, "active": folder.name == active, "error": ""}
        try:
            chain = _read_chain(cert_path)
            item.update(describe(chain))
            item["fingerprint_raw"] = chain[0].fingerprint(_sha256())
            if not key_path.is_file():
                item["error"] = "нет файла key.pem"
        except Exception as exc:
            item.update({"cn": folder.name, "sans": [], "warnings": [], "fingerprint_raw": b"",
                         "not_after": None, "days_left": None, "expired": False})
            item["error"] = f"не читается cert.pem: {exc}"
        items.append(item)
    items.sort(key=lambda i: (not i["active"], -(i["not_after"].timestamp() if i["not_after"] else 0)))
    return items


def assign(cert_id: str) -> None:
    """Назначить: проверить файлы и записать в config.json."""
    cert_path, key_path = _paths(cert_id)
    try:
        settings.check_tls_pair(cert_path, key_path)
    except settings.TlsConfigError as exc:
        raise CertError(str(exc)) from exc
    with context.CONFIG_LOCK:
        config = context.load_json(context.CONFIG_FILE, {})
        config["tls_cert"] = _rel(cert_path)
        config["tls_key"] = _rel(key_path)
        context.save_json(context.CONFIG_FILE, config)
    logger.info("🔒 Назначен сертификат HTTPS %s", cert_id)


def unassign() -> None:
    """Выключить HTTPS: убрать tls_cert/tls_key из config.json."""
    with context.CONFIG_LOCK:
        config = context.load_json(context.CONFIG_FILE, {})
        config.pop("tls_cert", None)
        config.pop("tls_key", None)
        context.save_json(context.CONFIG_FILE, config)
    logger.info("🔒 HTTPS выключен в настройках")


def delete(cert_id: str) -> None:
    cert_path, _ = _paths(cert_id)
    if cert_id == configured_id():
        raise CertError("Этот сертификат назначен — сначала назначьте другой или выключите HTTPS")
    folder = cert_path.parent
    if not folder.is_dir():
        raise CertError("Сертификат не найден")
    shutil.rmtree(folder)
    logger.info("🔒 Удалён сертификат %s", cert_id)


def running_matches_config() -> bool:
    """С тем ли сертификатом (или без) работает веб-сервер, что в настройках."""
    try:
        files = settings.tls_files()
    except settings.TlsConfigError:
        return False
    want = str(files[0]) if files else ""
    return os.path.normcase(want) == os.path.normcase(settings.RUNNING_CERT)
