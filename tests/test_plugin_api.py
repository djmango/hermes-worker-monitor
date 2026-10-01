import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import plugin_api

NOW = 1_700_000_000
CANARY = "canary-value-must-not-appear"

TASKS_SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  title TEXT,
  status TEXT,
  assignee TEXT,
  started_at INTEGER,
  completed_at INTEGER,
  block_kind TEXT
)
"""


def _board(path: Path, rows, *, old_schema=False) -> None:
    """Write a stand-in kanban.db. ``old_schema`` drops the two later columns."""
    with sqlite3.connect(path) as connection:
        if old_schema:
            connection.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, started_at INTEGER)"
            )
        else:
            connection.executescript(TASKS_SCHEMA)
        connection.executemany(
            "INSERT INTO tasks (id, title, status, assignee, started_at) VALUES (?, ?, ?, ?, ?)"
            if old_schema else
            "INSERT INTO tasks (id, title, status, assignee, started_at, completed_at, block_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [tuple(row) for row in rows] if old_schema else [tuple(row) + (None,) * (7 - len(row)) for row in rows],
        )


def test_route_is_relative_and_registered():
    paths = {route.path for route in plugin_api.router.routes}
    assert paths == {"/summary"}


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


def test_summary_endpoint_exact_shape_and_fresh_bypasses_cache():
    calls = []
    payload = {
        "generated_at": NOW,
        "board": {"path": "/kanban"},
        "groups": {"blocked": 1, "waiting": 0, "running": 2, "queued": 3,
                   "scheduled": 0, "review": 0, "done_today": 4},
        "workers": {"total": 2, "active": 2, "stalled": 0, "looping": 0, "state": "active"},
    }

    def fake_build():
        calls.append(1)
        return payload

    with patch.object(plugin_api, "_build_summary_payload", fake_build):
        plugin_api._summary_cache.clear()
        first = asyncio.run(plugin_api.get_summary(fresh=0))
        cached = asyncio.run(plugin_api.get_summary(fresh=0))
        refreshed = asyncio.run(plugin_api.get_summary(fresh=1))

    assert first == cached == refreshed == payload
    assert len(calls) == 2


def test_worker_tool_events_expose_only_name_hash_and_timestamp():
    raw = [{"function": {"name": "terminal", "arguments": {"command": CANARY, "token": CANARY}}}]

    events = plugin_api._extract_calls(raw, 123.0)

    assert len(events) == 1
    assert set(events[0]) == {"tool_name", "arguments_hash", "timestamp"}
    assert events[0]["tool_name"] == "terminal"
    assert len(events[0]["arguments_hash"]) == 16
    assert CANARY not in json.dumps(events)


def test_default_profile_reads_the_root_state_db_and_named_profile_does_not():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "state.db").write_bytes(b"")
        (root / "profiles" / "alpha").mkdir(parents=True)
        (root / "profiles" / "alpha" / "state.db").write_bytes(b"")
        (root / "profiles" / "nodb").mkdir(parents=True)

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            default_paths = plugin_api._profile_databases("default")
            named_paths = plugin_api._profile_databases("alpha")
            missing_paths = plugin_api._profile_databases("nodb")
            unsafe_paths = plugin_api._profile_databases("../evil")

    assert default_paths == [root.resolve() / "state.db"]
    assert named_paths == [root.resolve() / "profiles" / "alpha" / "state.db"]
    # A named profile never falls back to another profile's store.
    assert missing_paths == []
    assert unsafe_paths == []


def test_named_profile_prefers_its_own_store_over_the_root_one():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "state.db").write_bytes(b"")
        (root / "profiles" / "default").mkdir(parents=True)
        (root / "profiles" / "default" / "state.db").write_bytes(b"")

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            paths = plugin_api._profile_databases("default")

    assert paths == [
        root.resolve() / "profiles" / "default" / "state.db",
        root.resolve() / "state.db",
    ]


def test_group_queries_read_the_live_board_shape():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        day = plugin_api._local_day_start()
        _board(root / "kanban.db", [
            ("t_run", "Run", "running", "default", NOW),
            ("t_todo", "Todo", "todo", "default", NOW),
            ("t_ready", "Ready", "ready", "default", NOW),
            ("t_blocked", "Blocked", "blocked", "default", NOW, None, "capability"),
            ("t_waiting", "Waiting", "blocked", "default", NOW, None, "dependency"),
            ("t_done_today", "Done", "done", "default", NOW, day + 1, None),
            ("t_done_older", "Older", "done", "default", NOW, day - 1, None),
        ])

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            counts = plugin_api._load_status_counts()
            kinds = plugin_api._load_blocked_kinds()
            done_today = plugin_api._load_done_today()

    assert counts == {"running": 1, "todo": 1, "ready": 1, "blocked": 2, "done": 2}
    assert kinds == {"capability": 1, "dependency": 1}
    assert done_today == 1
    assert plugin_api.normalize_groups(counts, kinds, done_today=done_today) == {
        "blocked": 1, "waiting": 1, "running": 1, "queued": 2,
        "scheduled": 0, "review": 0, "done_today": 1,
    }


def test_older_board_without_the_later_columns_still_reports():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _board(root / "kanban.db", [("t_run", "Run", "running", "default", NOW)], old_schema=True)

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            kinds = plugin_api._load_blocked_kinds()
            done_today = plugin_api._load_done_today()
            counts = plugin_api._load_status_counts()

    assert kinds == {}
    assert done_today == 0
    assert counts == {"running": 1}


def test_build_payload_combines_groups_and_worker_states():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _board(root / "kanban.db", [
            ("t_run", "Run", "running", "default", NOW),
            ("t_blocked", "Blocked", "blocked", "default", NOW, None, "needs_input"),
        ])

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True), \
                patch.object(plugin_api, "_load_tool_events", lambda task: []):
            payload = plugin_api._build_summary_payload()

    assert payload["groups"] == {"blocked": 1, "waiting": 0, "running": 1, "queued": 0,
                                 "scheduled": 0, "review": 0, "done_today": 0}
    assert payload["workers"] == {"total": 1, "active": 1, "stalled": 0, "looping": 0, "state": "active"}
    assert payload["board"] == {"path": "/kanban"}


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


def test_no_em_dashes_in_shipped_sources():
    """House rule for this fork: no em dashes in code, docs, or UI copy."""
    root = Path(__file__).resolve().parent.parent
    shipped = [
        root / "plugin.yaml",
        root / "README.md",
        root / "CHANGELOG.md",
        root / "dashboard" / "plugin_api.py",
        root / "dashboard" / "workers.py",
        root / "dashboard" / "manifest.json",
        root / "desktop" / "plugin.js",
        root / "tests" / "test_plugin_api.py",
        root / "tests" / "test_workers.py",
    ]
    for path in shipped:
        assert "\u2014" not in path.read_text(encoding="utf-8"), f"em dash found in {path.name}"


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
