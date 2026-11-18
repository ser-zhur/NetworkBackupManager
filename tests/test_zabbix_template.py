# =========================================================
# tests/test_zabbix_template.py — шаблон Zabbix согласован
# с генератором и с ответом /api/status
# =========================================================

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_zabbix_template as zbx  # noqa: E402

from core import context, device_status, events, monitoring  # noqa: E402


@pytest.fixture(scope="module")
def tpl():
    return json.loads(zbx.OUT.read_text(encoding="utf-8"))["zabbix_export"]["templates"][0]


def test_file_is_fresh():
    assert zbx.OUT.read_text(encoding="utf-8") == zbx.render(), (
        "Шаблон устарел. Запустите: python tools/build_zabbix_template.py"
    )


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_uuids_unique_v4(tpl):
    export = json.loads(zbx.OUT.read_text(encoding="utf-8"))
    uuids = [n["uuid"] for n in _walk(export) if "uuid" in n]
    assert len(uuids) == len(set(uuids))
    v4 = re.compile(r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$")
    assert all(v4.match(u) for u in uuids)


def _keys(tpl):
    items = {i["key"] for i in tpl["items"]}
    protos = {p["key"] for r in tpl["discovery_rules"] for p in r["item_prototypes"]}
    return items, protos


def test_references_resolve(tpl):
    items, protos = _keys(tpl)
    masters = [n["master_item"]["key"] for n in _walk(tpl) if "master_item" in n]
    assert masters and all(m in items for m in masters)

    exprs = [n["expression"] for n in _walk(tpl) if "expression" in n]
    assert exprs
    for expr in exprs:
        refs = re.findall(
            r"/" + re.escape(zbx.TEMPLATE) + r"/([^,)\[]+(?:\[[^\]]*\])?)[,)]", expr,
        )
        assert refs, expr
        for key in refs:
            assert key in items | protos, (key, expr)


def _sample_status(tmp_path, monkeypatch):
    from core.plugin_manager import PluginManager

    monkeypatch.setattr(context, "DATA_DIR", tmp_path)
    monkeypatch.setattr(context, "STATE_FILE", tmp_path / "state.json")
    events.reset()
    events.emit(events.RUN_FINISHED, "x", total=1, success=1, failed=0,
                started="2026-01-01 00:00:00", finished="2026-01-01 00:01:00")
    pm = PluginManager()
    pm._plugins["fake"] = {"func": None, "routers": [
        {"id": "00000000000a", "name": "R", "host": "10.0.0.1"},
    ]}
    entry = device_status.record_attempt(
        {"last_run": device_status.now_str()}, False, "timed out",
    )
    context.STATE_FILE.write_text(json.dumps({"00000000000a": entry}), encoding="utf-8")

    class Sched:
        config = {"enabled": True}

        def is_alive(self):
            return True

        def is_job_running(self):
            return False

        def next_run(self, now=None):
            return now

    try:
        return monitoring.status(pm, Sched())
    finally:
        events.reset()


def _resolve(data, path):
    """Простые пути вида $.a.b — как их поймёт JSONPath Zabbix."""
    assert path.startswith("$")
    node = data
    for part in path[2:].split(".") if len(path) > 1 else []:
        assert isinstance(node, dict) and part in node, (path, part)
        node = node[part]
    return node


def test_jsonpaths_match_api(tpl, tmp_path, monkeypatch):
    status = _sample_status(tmp_path, monkeypatch)
    health = monitoring.health(None)[1]
    device = status["devices"][0]

    for item in tpl["items"]:
        steps = item.get("preprocessing") or []
        if not steps or steps[0]["type"] != "JSONPATH":
            continue
        source = health if item["master_item"]["key"] == zbx.HEALTH else status
        _resolve(source, steps[0]["parameters"][0])

    rule = tpl["discovery_rules"][0]
    assert isinstance(_resolve(status, rule["preprocessing"][0]["parameters"][0]), list)
    for m in rule["lld_macro_paths"]:
        _resolve(device, m["path"])
    for p in rule["item_prototypes"]:
        field = re.search(r"\]\.(\w+)\.first\(\)$", p["preprocessing"][0]["parameters"][0])
        assert field and field.group(1) in device, p["key"]


def test_trigger_status_values_exist(tpl):
    rule = tpl["discovery_rules"][0]
    for t in rule["trigger_prototypes"]:
        for value in re.findall(r'="(\w+)"', t["expression"]):
            assert value in device_status.STATUSES, value
