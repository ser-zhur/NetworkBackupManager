# =========================================================
# docker/healthcheck.py — HEALTHCHECK контейнера
# =========================================================
#
# GET /api/health по HTTPS, затем по HTTP: программа может
# работать с сертификатом (NBM_TLS_CERT) или без. Сертификат
# не проверяется — запрос идёт на 127.0.0.1, а сертификат
# выписан на имя сервера.
#
# Код выхода: 0 — 200 OK, 1 — нет ответа или ошибка (503).
#
# =========================================================

import os
import ssl
import sys
import urllib.error
import urllib.request

port = os.environ.get("NBM_PORT", "8080")
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

for scheme in ("https", "http"):
    try:
        urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/api/health", timeout=4, context=ctx)
        sys.exit(0)
    except urllib.error.HTTPError:
        sys.exit(1)                     # ответил, но с ошибкой (503)
    except Exception:
        continue
sys.exit(1)
