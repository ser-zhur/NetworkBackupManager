# =========================================================
# tools/build_zabbix_template.py — шаблон Zabbix для /api/status
# =========================================================
#
#     python tools/build_zabbix_template.py
#
# Пишет web/static/monitoring/zbx_nbm_template.json (формат
# экспорта Zabbix 6.0 — его импортируют 6.0 и все новее).
# Шаблон собирается здесь, а не правится руками: uuid
# элементов стабильны (выводятся из ключей), а тест
# tests/test_zabbix_template.py проверяет, что файл свежий и
# согласованный.
#
# Как устроен шаблон:
#   • nbm.status.get — HTTP-запрос к {$NBM.URL}/api/status с
#     токеном {$NBM.TOKEN}; остальное — зависимые элементы
#     (JSONPath), второй запрос не нужен;
#   • nbm.health.get — /api/health без токена: «программа
#     недоступна» срабатывает, даже если токен неверный;
#   • обнаружение устройств по $.devices — по элементу и
#     триггеру на каждое устройство.
#
# =========================================================

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "web" / "static" / "monitoring" / "zbx_nbm_template.json"

TEMPLATE = "Network Backup Manager by HTTP"
GROUP = "Templates/Applications"
STATUS = "nbm.status.get"
HEALTH = "nbm.health.get"


def uuid(name: str) -> str:
    """Стабильный uuid в формате v4 (его проверяет импорт Zabbix)."""
    h = bytearray(hashlib.md5(f"nbm:{name}".encode()).digest())
    h[6] = (h[6] & 0x0F) | 0x40
    h[8] = (h[8] & 0x3F) | 0x80
    return h.hex()


def ref(key: str) -> str:
    return f"/{TEMPLATE}/{key}"


def jsonpath(path: str, on_error: str | None = None, value: str = "") -> dict:
    step = {"type": "JSONPATH", "parameters": [path]}
    if on_error:
        step["error_handler"] = on_error
        if value:
            step["error_handler_params"] = value
    return step


BOOL = {"type": "BOOL_TO_DECIMAL", "parameters": [""]}


def heartbeat(seconds: str = "1h") -> dict:
    return {"type": "DISCARD_UNCHANGED_HEARTBEAT", "parameters": [seconds]}


def dependent(key, name, path, value_type="UNSIGNED", master=STATUS, steps=(),
              units="", valuemap=None, history="31d", triggers=(), tags=(),
              description="", on_error=None, error_value=""):
    item = {
        "uuid": uuid(key),
        "name": name,
        "type": "DEPENDENT",
        "key": key,
        "delay": "0",
        "history": history,
        "value_type": value_type,
    }
    if value_type in ("TEXT", "CHAR"):
        item["trends"] = "0"
    if units:
        item["units"] = units
    if description:
        item["description"] = description
    if valuemap:
        item["valuemap"] = {"name": valuemap}
    item["preprocessing"] = [jsonpath(path, on_error, error_value), *steps]
    item["master_item"] = {"key": master}
    if tags:
        item["tags"] = [{"tag": t, "value": v} for t, v in tags]
    if triggers:
        item["triggers"] = list(triggers)
    return item


def trigger(name, expression, priority, description="", opdata="", key_for_uuid=None,
            manual_close=False):
    t = {
        "uuid": uuid("trigger:" + (key_for_uuid or name)),
        "expression": expression,
        "name": name,
    }
    if opdata:
        t["opdata"] = opdata
    t["priority"] = priority
    if description:
        t["description"] = description
    if manual_close:
        t["manual_close"] = "YES"
    return t


def http_item(key, name, path, headers=(), status_codes="200"):
    item = {
        "uuid": uuid(key),
        "name": name,
        "type": "HTTP_AGENT",
        "key": key,
        "delay": "{$NBM.INTERVAL}",
        "history": "0",
        "trends": "0",
        "value_type": "TEXT",
        "timeout": "15s",
        "url": "{$NBM.URL}" + path,
        "status_codes": status_codes,
        "tags": [{"tag": "component", "value": "raw"}],
    }
    if headers:
        item["headers"] = [{"name": n, "value": v} for n, v in headers]
    return item


def build() -> dict:
    sev_map = "NBM severity"

    items = [
        http_item(STATUS, "NBM: Получить состояние", "/api/status",
                  headers=[("Authorization", "Bearer {$NBM.TOKEN}")]),
        http_item(HEALTH, "NBM: Проверка доступности", "/api/health",
                  status_codes="200,503"),

        # Без «отбрасывать неизменённое»: на этом элементе nodata().
        dependent("nbm.health", "NBM: Доступность", "$.status", "CHAR", master=HEALTH,
                  history="7d", tags=[("component", "health")],
                  triggers=[
                      trigger("NBM: программа недоступна",
                              f"nodata({ref('nbm.health')},{{$NBM.NODATA}})=1", "HIGH",
                              "Нет ответа от /api/health дольше {$NBM.NODATA}: программа "
                              "остановлена, недоступна по сети или неверен {$NBM.URL}."),
                      trigger("NBM: проверка доступности не пройдена",
                              f"last({ref('nbm.health')})<>\"ok\"", "HIGH",
                              "/api/health вернул ошибку: планировщик остановлен или нет "
                              "записи в папку данных. Подробности — в журнале программы."),
                  ]),

        dependent("nbm.version", "NBM: Версия", "$.app.version", "CHAR",
                  steps=[heartbeat("1d")], history="90d", tags=[("component", "application")]),
        dependent("nbm.uptime", "NBM: Время работы", "$.app.uptime_s", units="s",
                  tags=[("component", "application")],
                  triggers=[trigger("NBM: программа перезапущена",
                                    f"last({ref('nbm.uptime')})<600", "INFO",
                                    manual_close=True)]),
        dependent("nbm.severity", "NBM: Общее состояние", "$.severity", valuemap=sev_map,
                  tags=[("component", "health")]),
        dependent("nbm.problems", "NBM: Проблемы", "$.problems", "TEXT", history="7d",
                  steps=[{"type": "JAVASCRIPT",
                          "parameters": ["return JSON.parse(value).join('\\n');"]}],
                  tags=[("component", "health")]),

        dependent("nbm.scheduler.enabled", "NBM: Автобэкап включён", "$.scheduler.enabled",
                  steps=[BOOL, heartbeat()], tags=[("component", "scheduler")]),
        dependent("nbm.scheduler.alive", "NBM: Планировщик работает", "$.scheduler.alive",
                  steps=[BOOL], tags=[("component", "scheduler")],
                  triggers=[trigger("NBM: планировщик не работает",
                                    f"last({ref('nbm.scheduler.alive')})=0", "HIGH",
                                    "Поток планировщика остановлен — плановые бэкапы не "
                                    "запускаются. Перезапустите программу.")]),
        dependent("nbm.scheduler.overdue", "NBM: Плановый бэкап просрочен",
                  "$.scheduler.overdue", steps=[BOOL], tags=[("component", "scheduler")],
                  triggers=[trigger("NBM: плановый бэкап не начался вовремя",
                                    f"last({ref('nbm.scheduler.overdue')})=1", "AVERAGE",
                                    "Срок планового бэкапа прошёл больше часа назад, а "
                                    "прогон не начался.")]),
        dependent("nbm.scheduler.next_run", "NBM: Следующий плановый бэкап",
                  "$.scheduler.next_run", "CHAR", steps=[heartbeat()],
                  on_error="CUSTOM_VALUE", error_value="—",
                  tags=[("component", "scheduler")]),

        dependent("nbm.lastrun.finished", "NBM: Последний прогон — завершён",
                  "$.last_run.finished", "CHAR", steps=[heartbeat()],
                  on_error="DISCARD_VALUE", tags=[("component", "run")]),
        dependent("nbm.lastrun.failed", "NBM: Последний прогон — ошибок",
                  "$.last_run.failed", on_error="DISCARD_VALUE", tags=[("component", "run")],
                  triggers=[trigger("NBM: последний прогон завершился с ошибками",
                                    f"last({ref('nbm.lastrun.failed')})>0", "WARNING",
                                    opdata="Ошибок: {ITEM.LASTVALUE1}")]),
    ]

    for code, label in (("total", "всего"), ("ok", "актуально"), ("warn", "давно"),
                        ("fail", "с ошибкой"), ("none", "без бэкапов"),
                        ("disabled", "отключено")):
        items.append(dependent(f"nbm.devices.{code}", f"NBM: Устройств — {label}",
                               f"$.counts.{code}", tags=[("component", "devices")]))

    # ---- Обнаружение устройств ----
    def dev(path):
        return f"$.devices[?(@.id=='{{#ID}}')].{path}.first()"

    def proto(key, name, path, value_type="UNSIGNED", **kw):
        p = dependent(key, name, dev(path), value_type,
                      tags=[("component", "device"), ("device", "{#NAME}")], **kw)
        p["uuid"] = uuid("proto:" + key)
        return p

    sev_key = "nbm.device.severity[{#ID}]"
    status_key = "nbm.device.status[{#ID}]"
    err_key = "nbm.device.error[{#ID}]"
    age_key = "nbm.device.age[{#ID}]"
    label = "{#NAME} ({#HOST})"

    discovery = {
        "uuid": uuid("nbm.devices.discovery"),
        "name": "NBM: Обнаружение устройств",
        "type": "DEPENDENT",
        "key": "nbm.devices.discovery",
        "delay": "0",
        "lifetime": "7d",
        "item_prototypes": [
            proto(status_key, f"NBM {label}: Статус", "status", "CHAR",
                  steps=[heartbeat()]),
            proto(sev_key, f"NBM {label}: Состояние", "severity", valuemap=sev_map),
            proto("nbm.device.failures[{#ID}]", f"NBM {label}: Неудач подряд", "failures"),
            proto(age_key, f"NBM {label}: Дней с успешного бэкапа", "success_age_days",
                  units="d", on_error="DISCARD_VALUE"),
            proto(err_key, f"NBM {label}: Ошибка", "error", "TEXT", history="7d",
                  steps=[heartbeat()]),
            proto("nbm.device.last_success[{#ID}]", f"NBM {label}: Последний успешный бэкап",
                  "last_success", "CHAR", steps=[heartbeat()], on_error="DISCARD_VALUE"),
        ],
        "trigger_prototypes": [
            # «and length(...)>=0» / «and last(...)>=0» — всегда истинно;
            # нужно, чтобы показать значение второго элемента в opdata.
            trigger(f"NBM: {label}: бэкап с ошибкой",
                    f"last({ref(status_key)})=\"fail\" and length(last({ref(err_key)}))>=0",
                    "AVERAGE",
                    "Последняя попытка бэкапа или выгрузки в хранилище не удалась, либо "
                    "успешного бэкапа не было больше 30 дней.",
                    opdata="{ITEM.LASTVALUE2}", key_for_uuid="proto:device-fail"),
            trigger(f"NBM: {label}: давно не было успешного бэкапа",
                    f"last({ref(status_key)})=\"warn\" and last({ref(age_key)})>=0",
                    "WARNING",
                    "Последний успешный бэкап — больше 14 дней назад.",
                    opdata="Дней: {ITEM.LASTVALUE2}", key_for_uuid="proto:device-warn"),
            trigger(f"NBM: {label}: бэкапов ещё не было",
                    f"last({ref(status_key)})=\"none\"", "INFO",
                    "Устройство добавлено, но успешного бэкапа ещё не было.",
                    key_for_uuid="proto:device-none"),
        ],
        "master_item": {"key": STATUS},
        "lld_macro_paths": [
            {"lld_macro": "{#ID}", "path": "$.id"},
            {"lld_macro": "{#NAME}", "path": "$.name"},
            {"lld_macro": "{#HOST}", "path": "$.host"},
            {"lld_macro": "{#TYPE}", "path": "$.type"},
        ],
        "preprocessing": [jsonpath("$.devices"), heartbeat("1h")],
    }

    return {
        "zabbix_export": {
            "version": "6.0",
            "groups": [{"uuid": uuid("group:" + GROUP), "name": GROUP}],
            "templates": [{
                "uuid": uuid("template:" + TEMPLATE),
                "template": TEMPLATE,
                "name": TEMPLATE,
                "description": (
                    "Network Backup Manager: состояние бэкапов сетевых "
                    "устройств через HTTP API (/api/status, /api/health).\n\n"
                    "Макросы узла: {$NBM.URL} — адрес программы без / в конце "
                    "(например http://192.0.2.10:8080), {$NBM.TOKEN} — токен мониторинга "
                    "(Настройки → Мониторинг)."
                ),
                "groups": [{"name": GROUP}],
                "items": items,
                "discovery_rules": [discovery],
                "macros": [
                    {"macro": "{$NBM.URL}", "value": "http://127.0.0.1:8080",
                     "description": "Адрес Network Backup Manager без / в конце"},
                    {"macro": "{$NBM.TOKEN}", "type": "SECRET_TEXT",
                     "description": "Токен мониторинга (Настройки → Мониторинг)"},
                    {"macro": "{$NBM.INTERVAL}", "value": "5m",
                     "description": "Период опроса"},
                    {"macro": "{$NBM.NODATA}", "value": "15m",
                     "description": "Нет ответа дольше — «программа недоступна»"},
                ],
                "valuemaps": [{
                    "uuid": uuid("valuemap:" + sev_map),
                    "name": sev_map,
                    "mappings": [
                        {"value": "0", "newvalue": "ОК"},
                        {"value": "1", "newvalue": "Внимание"},
                        {"value": "2", "newvalue": "Проблема"},
                    ],
                }],
            }],
        },
    }


def render() -> str:
    return json.dumps(build(), ensure_ascii=False, indent=4) + "\n"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"OK: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
