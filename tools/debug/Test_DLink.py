# ============================================================
# Test_DLink.py
# ============================================================
# Проверка SSH-подключения к коммутаторам D-Link: с какими
# настройками каждый из них отдаёт running-config.
#
# Зачем: paramiko 5 больше не принимает ключ хоста ssh-rsa
# (подпись SHA-1), а DSA (ssh-dss) не принимает с версии 4.
# Старые прошивки D-Link предлагают только эти два ключа.
#
# Для каждого коммутатора:
#   0) что предлагает сервер (KEXINIT): обмен ключами, ключ
#      хоста, шифры, MAC — читается без входа;
#   1) paramiko как сейчас в плагине;
#   2) paramiko + ключ хоста ssh-rsa (SHA-1) только для этого
#      соединения;
#   3) системный ssh + ssh-rsa и старые алгоритмы;
#   4) системный ssh + ssh-dss (в OpenSSH 10 DSA удалён —
#      тогда вариант пропускается).
# Успех — когда пришёл блок «Current configuration … end».
# Конфигурация никуда не сохраняется, пароли не печатаются.
# Ключи хостов запоминаются только в памяти: data/known_hosts
# не меняется (но если ключ там уже есть — он проверяется).
#
# Запуск в Docker (из папки с docker-compose.yml):
#   sudo docker compose exec -T nbm python - < Test_DLink.py
#   sudo docker compose exec -T nbm python - --only 192.0.2.10 < Test_DLink.py
#
# Запуск при установке через install.sh:
#   sudo env NBM_HOME=/var/lib/nbm /opt/nbm/venv/bin/python \
#       Test_DLink.py --app /opt/nbm/app
#
# Одно устройство вручную (пароль спросит):
#   python Test_DLink.py --host 192.0.2.10 --user admin
# ============================================================

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 40
COMMANDS = ["terminal length 0", "show running-config"]
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


# ------------------------------------------------------------
# Устройства из программы
# ------------------------------------------------------------

def _find_app(arg: str | None) -> Path | None:
    candidates = [arg] if arg else []
    candidates += [os.getcwd(), "/app", "/opt/nbm/app"]
    try:
        candidates.append(str(Path(__file__).resolve().parents[2]))
    except (NameError, IndexError):
        pass  # python - < файл: __file__ нет
    for c in candidates:
        if c and (Path(c) / "core" / "devices.py").is_file():
            return Path(c)
    return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def load_dlink_devices(app: Path, type_filter: str) -> list[dict]:
    sys.path.insert(0, str(app))
    from core import context
    from core.devices import load_devices
    from core.secure_storage import get_secure_storage

    storage = get_secure_storage()
    print(f"Данные программы: {context.DATA_DIR}")
    result = []
    for d in load_devices():
        dtype = str(d.get("type", ""))
        if _norm(type_filter) not in _norm(dtype):
            continue
        dev = dict(d)
        if dev.get("ssh_pass"):
            try:
                dev["ssh_pass"] = storage.decrypt(dev["ssh_pass"])
            except Exception as e:
                dev["ssh_pass"] = None
                dev["_error"] = f"не расшифровать пароль: {e}"
        result.append(dev)
    return result


def stored_host_keys(app: Path | None):
    """Ключи хостов, которые программа уже запомнила (paramiko)."""
    if app is None:
        return None
    try:
        from core.host_keys import KNOWN_HOSTS_FILE
        import paramiko
        keys = paramiko.HostKeys()
        if KNOWN_HOSTS_FILE.exists():
            keys.load(str(KNOWN_HOSTS_FILE))
        return keys
    except Exception as e:
        print(f"(не прочитать data/known_hosts: {e})")
        return None


# ------------------------------------------------------------
# 0. Что предлагает сервер
# ------------------------------------------------------------

def probe_kexinit(host: str, port: int) -> dict:
    """Читает баннер и KEXINIT сервера, без входа."""
    names = ["kex", "hostkey", "enc_c2s", "enc_s2c", "mac_c2s",
             "mac_s2c", "comp_c2s", "comp_s2c"]
    with socket.create_connection((host, port), CONNECT_TIMEOUT) as s:
        s.settimeout(CONNECT_TIMEOUT)
        f = s.makefile("rb")
        banner = ""
        while not banner.startswith("SSH-"):
            line = f.readline(1024)
            if not line:
                raise ConnectionError("сервер закрыл соединение до баннера")
            banner = line.decode("latin-1").strip()
        s.sendall(b"SSH-2.0-NBM_probe\r\n")
        length = struct.unpack(">I", f.read(4))[0]
        if not 16 < length < 65536:
            raise ValueError(f"странная длина пакета: {length}")
        packet = f.read(length)
        payload = packet[1:length - packet[0]]
        if payload[0] != 20:
            raise ValueError(f"ожидался KEXINIT, пришло сообщение {payload[0]}")
        pos = 17  # тип + cookie
        info = {"banner": banner}
        for n in names:
            ln = struct.unpack(">I", payload[pos:pos + 4])[0]
            info[n] = payload[pos + 4:pos + 4 + ln].decode("latin-1").split(",")
            pos += 4 + ln
        return info


# ------------------------------------------------------------
# Чтение конфигурации из интерактивной сессии
# ------------------------------------------------------------

def check_config(raw: str) -> str:
    text = ANSI_RE.sub("", raw).replace("\r", "")
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if l.strip().startswith("Current configuration")), None)
    if start is None:
        tail = " | ".join(l.strip() for l in lines[-5:] if l.strip())
        raise RuntimeError(
            f"нет 'Current configuration' в выводе ({len(text)} симв.): {tail[:200]}"
        )
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip() == "end"), None)
    if end is None:
        raise RuntimeError("конфигурация обрезана: нет строки 'end'")
    return f"конфигурация {end - start + 1} строк"


def paramiko_shell(client) -> str:
    shell = client.invoke_shell()
    time.sleep(1)
    while shell.recv_ready():
        shell.recv(65535)
    for cmd in COMMANDS:
        shell.send(cmd + "\n")
        time.sleep(1)
    buf = ""
    deadline = time.time() + READ_TIMEOUT
    while time.time() < deadline:
        if shell.recv_ready():
            buf += shell.recv(65535).decode("utf-8", errors="ignore")
            if re.search(r"\bend\s*\n", buf):
                time.sleep(0.5)
                while shell.recv_ready():
                    buf += shell.recv(65535).decode("utf-8", errors="ignore")
                break
        else:
            time.sleep(0.3)
    shell.close()
    return check_config(buf)


# ------------------------------------------------------------
# 1–2. paramiko
# ------------------------------------------------------------

def legacy_transport_class():
    """Transport, который дополнительно принимает ключ хоста ssh-rsa."""
    import paramiko
    from cryptography.hazmat.primitives import hashes

    class LegacyRSAKey(paramiko.RSAKey):
        HASHES = {**paramiko.RSAKey.HASHES, "ssh-rsa": hashes.SHA1}

    class LegacyTransport(paramiko.Transport):
        _key_info = {**paramiko.Transport._key_info, "ssh-rsa": LegacyRSAKey}
        _preferred_keys = tuple(paramiko.Transport._preferred_keys) + (
            () if "ssh-rsa" in paramiko.Transport._preferred_keys
            else ("ssh-rsa",)
        )

    return LegacyTransport


def try_paramiko(dev: dict, known, legacy: bool) -> str:
    import paramiko
    client = paramiko.SSHClient()
    if known is not None:
        # Только в памяти: data/known_hosts не меняется.
        for name, entry in known.items():
            for ktype, key in entry.items():
                client.get_host_keys().add(name, ktype, key)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = {}
    if legacy:
        kwargs["transport_factory"] = legacy_transport_class()
    try:
        client.connect(
            hostname=dev["host"], port=dev["port"],
            username=dev["user"], password=dev["password"],
            timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT,
            auth_timeout=CONNECT_TIMEOUT,
            allow_agent=False, look_for_keys=False, **kwargs,
        )
        t = client.get_transport()
        key = t.get_remote_server_key()
        detail = (f"ключ={key.get_name()} {key.fingerprint}, "
                  f"шифр={t.local_cipher}, mac={t.local_mac}")
        return paramiko_shell(client) + "; " + detail
    finally:
        client.close()


# ------------------------------------------------------------
# 3–4. системный ssh
# ------------------------------------------------------------

def ssh_supported(ssh: str, what: str) -> set[str]:
    try:
        out = subprocess.run([ssh, "-Q", what], capture_output=True,
                             text=True, timeout=10).stdout
        return set(out.split())
    except Exception:
        return set()


def make_askpass(tmp: Path, ssh: str) -> Path:
    # Windows OpenSSH (System32) запускает .cmd; ssh из Git для
    # Windows и Linux — сценарий sh.
    if os.name == "nt" and "system32" in ssh.lower():
        p = tmp / "askpass.cmd"
        p.write_text(
            f'@"{sys.executable}" -I -c "import os;'
            f"print(os.environ['NBM_TEST_PW'])\"\r\n",
            encoding="ascii",
        )
    else:
        p = tmp / "askpass"
        p.write_text("#!/bin/sh\nprintf '%s\\n' \"$NBM_TEST_PW\"\n")
        p.chmod(stat.S_IRWXU)
    return p


def try_openssh(dev: dict, ssh: str, askpass: Path, tmp: Path,
                hostkey: str, verbose: bool) -> str:
    keys = ssh_supported(ssh, "key")
    if hostkey not in keys:
        raise SkipTest(f"этот ssh не знает {hostkey} ({ssh_version(ssh)})")
    kex = ssh_supported(ssh, "kex")
    ciphers = ssh_supported(ssh, "cipher")
    macs = ssh_supported(ssh, "mac")
    legacy_kex = [k for k in ("diffie-hellman-group-exchange-sha1",
                              "diffie-hellman-group14-sha1",
                              "diffie-hellman-group1-sha1") if k in kex]
    legacy_ciphers = [c for c in ("aes128-cbc", "aes192-cbc", "aes256-cbc",
                                  "3des-cbc") if c in ciphers]
    legacy_macs = [m for m in ("hmac-sha1", "hmac-md5") if m in macs]

    # Пустые файлы вместо /dev/null: ssh из Git для Windows не
    # понимает NUL, а Windows OpenSSH — /dev/null.
    null = tmp / "empty"
    null.touch()
    cmd = [
        ssh, "-F", str(null), "-tt",
        "-p", str(dev["port"]),
        "-oConnectTimeout=%d" % CONNECT_TIMEOUT,
        "-oStrictHostKeyChecking=accept-new",
        f"-oUserKnownHostsFile={tmp / 'known_hosts'}",
        f"-oGlobalKnownHostsFile={null}",
        "-oPubkeyAuthentication=no",
        "-oPreferredAuthentications=password,keyboard-interactive",
        "-oNumberOfPasswordPrompts=1",
        f"-oHostKeyAlgorithms=+{hostkey}",
    ]
    if legacy_kex:
        cmd.append("-oKexAlgorithms=+" + ",".join(legacy_kex))
    if legacy_ciphers:
        cmd.append("-oCiphers=+" + ",".join(legacy_ciphers))
    if legacy_macs:
        cmd.append("-oMACs=+" + ",".join(legacy_macs))
    cmd += ["-vv"] if verbose else ["-oLogLevel=ERROR"]
    cmd.append(f"{dev['user']}@{dev['host']}")

    env = dict(os.environ, NBM_TEST_PW=dev["password"],
               SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force",
               DISPLAY=os.environ.get("DISPLAY", ":0"))
    stdin = "\n".join(COMMANDS + ["logout"]) + "\n"
    try:
        r = subprocess.run(cmd, input=stdin.encode(), capture_output=True,
                           env=env, timeout=CONNECT_TIMEOUT + READ_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", errors="ignore")
        try:
            return check_config(out) + " (сессия не закрылась сама)"
        except RuntimeError:
            raise RuntimeError("таймаут") from None
    out = r.stdout.decode("utf-8", errors="ignore")
    err = r.stderr.decode("utf-8", errors="ignore").strip()
    try:
        result = check_config(out)
    except RuntimeError as e:
        lines = [l for l in err.splitlines() if l.strip()]
        if verbose:
            lines = [l for l in lines if not l.startswith("debug")] + [
                l for l in lines if "kex: " in l or "host key" in l.lower()]
        raise RuntimeError(
            (" | ".join(lines[-4:]) or str(e)) + f" (код {r.returncode})"
        ) from None
    kh = (tmp / "known_hosts")
    fp = ""
    if kh.exists():
        fp = subprocess.run(["ssh-keygen", "-l", "-f", str(kh)],
                            capture_output=True, text=True).stdout.strip()
        kh.unlink()
    return result + (f"; ключ {fp}" if fp else "")


def ssh_version(ssh: str) -> str:
    try:
        r = subprocess.run([ssh, "-V"], capture_output=True, text=True,
                           timeout=10)
        return (r.stderr or r.stdout).strip()
    except Exception:
        return "?"


class SkipTest(Exception):
    pass


# ------------------------------------------------------------

def run_device(dev: dict, known, ssh: str | None, askpass, tmp,
               verbose: bool) -> dict:
    title = f"{dev['name']} ({dev['host']}:{dev['port']}, {dev['user']})"
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)
    results = {}

    if dev.get("_error"):
        print(f"  ⚠ {dev['_error']}")
        return {"_title": title, "_error": dev["_error"]}

    try:
        info = probe_kexinit(dev["host"], dev["port"])
        print(f"  Баннер:      {info['banner']}")
        print(f"  Обмен ключ.: {', '.join(info['kex'])}")
        print(f"  Ключ хоста:  {', '.join(info['hostkey'])}")
        print(f"  Шифры:       {', '.join(info['enc_c2s'])}")
        print(f"  MAC:         {', '.join(info['mac_c2s'])}")
        results["_hostkey"] = ",".join(info["hostkey"])
    except Exception as e:
        print(f"  ❌ Не прочитать предложение сервера: {e}")
        return {"_title": title, "_error": str(e)}

    attempts = [
        ("paramiko", lambda: try_paramiko(dev, known, legacy=False)),
        ("paramiko+ssh-rsa", lambda: try_paramiko(dev, known, legacy=True)),
    ]
    if ssh:
        attempts += [
            ("ssh+ssh-rsa", lambda: try_openssh(dev, ssh, askpass, tmp,
                                                "ssh-rsa", verbose)),
            ("ssh+ssh-dss", lambda: try_openssh(dev, ssh, askpass, tmp,
                                                "ssh-dss", verbose)),
        ]

    for name, fn in attempts:
        started = time.monotonic()
        try:
            detail = fn()
            mark, status = "✅", "OK"
        except SkipTest as e:
            detail, mark, status = str(e), "➖", "пропущен"
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            mark, status = "❌", "ошибка"
        took = time.monotonic() - started
        print(f"  {mark} {name:<18} {took:5.1f} с  {detail}")
        results[name] = status
    results["_title"] = title
    return results


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Проверка SSH-подключения к D-Link с разными настройками")
    ap.add_argument("--app", help="папка программы (где core/)")
    ap.add_argument("--type", default="dlink",
                    help="часть типа устройства (по умолчанию dlink)")
    ap.add_argument("--only", help="имя или адрес устройства (часть)")
    ap.add_argument("--host", help="проверить один адрес вручную")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--ssh", default=shutil.which("ssh"),
                    help="путь к системному ssh")
    ap.add_argument("--verbose", action="store_true",
                    help="подробный журнал paramiko и ssh")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.CRITICAL,
        format="    %(name)s: %(message)s", stream=sys.stdout,
    )

    try:
        import paramiko
        print(f"Python {sys.version.split()[0]}, paramiko {paramiko.__version__}")
    except ImportError:
        print("Нет paramiko — запускайте Python программы")
        return 2
    print(f"Системный ssh: {ssh_version(args.ssh) if args.ssh else 'не найден'}")

    app = None
    if args.host:
        devices = [{
            "name": args.host, "host": args.host, "port": args.port,
            "user": args.user,
            "password": getpass.getpass(f"Пароль {args.user}@{args.host}: "),
        }]
    else:
        app = _find_app(args.app)
        if app is None:
            print("Не найдена папка программы — укажите --app или --host")
            return 2
        raw = load_dlink_devices(app, args.type)
        devices = []
        for d in raw:
            host = d.get("host") or d.get("ip")
            name = d.get("name") or host
            if args.only and args.only not in f"{name} {host}":
                continue
            dev = {
                "name": name, "host": host,
                "port": int(d.get("port") or 22),
                "user": d.get("username") or d.get("user") or "admin",
                "password": d.get("ssh_pass") or "",
            }
            if d.get("_error"):
                dev["_error"] = d["_error"]
            elif not dev["password"]:
                dev["_error"] = "не задан пароль"
            if d.get("disabled"):
                dev["name"] += " [отключено]"
            devices.append(dev)
        print(f"Устройств D-Link: {len(devices)}")
        if not devices:
            from core.devices import load_devices
            types = sorted({str(d.get("type")) for d in load_devices()})
            print(f"Типы устройств в программе: {', '.join(types)}")
            return 1

    known = stored_host_keys(app)
    tmp = Path(tempfile.mkdtemp(prefix="nbm-dlink-"))
    try:
        askpass = make_askpass(tmp, args.ssh) if args.ssh else None
        summary = [run_device(d, known, args.ssh, askpass, tmp, args.verbose)
                   for d in devices]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    methods = ["paramiko", "paramiko+ssh-rsa", "ssh+ssh-rsa", "ssh+ssh-dss"]
    print("\n" + "=" * 70 + "\nИТОГ\n" + "=" * 70)
    for r in summary:
        if r.get("_error"):
            print(f"{r['_title']}: ⚠ {r['_error']}")
            continue
        cells = []
        for m in methods:
            if m in r:
                mark = {"OK": "✅", "пропущен": "➖"}.get(r[m], "❌")
                cells.append(f"{mark} {m}")
        print(f"{r['_title']}\n    ключи: {r.get('_hostkey')}\n    "
              + "   ".join(cells))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    sys.exit(main())
