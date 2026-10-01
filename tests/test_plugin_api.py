import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import plugin_api


NOW = 1_700_000_000
CANARY = "canary-value-must-not-appear"


def test_routes_are_relative_and_registered():
    paths = {route.path for route in plugin_api.router.routes}
    assert paths == {"/workers"}


def test_sqlite_connections_are_enforced_read_only():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "state.db"
        with sqlite3.connect(path) as writable:
            writable.execute("CREATE TABLE sample (value TEXT)")
        with plugin_api._readonly_connection(path) as readonly:
            try:
                readonly.execute("INSERT INTO sample VALUES ('forbidden')")
            except sqlite3.OperationalError as error:
                assert "readonly" in str(error).lower()
            else:
                raise AssertionError("read-only connection unexpectedly accepted a write")


def test_workers_endpoint_exact_shape_and_fresh_bypasses_cache():
    calls = []

    def fake_build():
        calls.append(1)
        return {"generated_at": NOW, "count": 0, "workers": []}

    with patch.object(plugin_api, "_build_workers_payload", fake_build):
        plugin_api._workers_cache.clear()
        first = asyncio.run(plugin_api.get_workers(fresh=0))
        cached = asyncio.run(plugin_api.get_workers(fresh=0))
        refreshed = asyncio.run(plugin_api.get_workers(fresh=1))

    assert first == cached == refreshed == {"generated_at": NOW, "count": 0, "workers": []}
    assert len(calls) == 2


def test_worker_tool_events_expose_only_name_hash_and_timestamp():
    raw = [{"function": {"name": "terminal", "arguments": {"command": CANARY, "token": CANARY}}}]

    events = plugin_api._extract_calls(raw, 123.0)

    assert len(events) == 1
    assert set(events[0]) == {"tool_name", "arguments_hash", "timestamp"}
    assert events[0]["tool_name"] == "terminal"
    assert len(events[0]["arguments_hash"]) == 16
    assert CANARY not in json.dumps(events)


def test_module_reads_no_credentials_and_makes_no_network_calls():
    """The fork's whole point: the module surface must stay credential-free."""
    source = Path(plugin_api.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "urllib.request",
        "credential_pool",
        "account_usage",
        "DEEPSEEK",
        "API_KEY",
        "FunctionType",
        "quotas",
        "requests",
        "socket",
    ):
        assert forbidden not in source, f"unexpected reference to {forbidden!r} in plugin_api.py"


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
