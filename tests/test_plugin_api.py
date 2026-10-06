import asyncio
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

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


def test_routes_are_relative_and_registered():
    paths = {route.path for route in plugin_api.router.routes}
    assert paths == {"/summary", "/cards"}


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


def test_only_one_count_popover_can_be_open():
    """One shared open slot, and the chip under the pointer always wins it.

    Each chip used to own its open flag, so hovering across the footer left a
    row of panels up. Then the panel took the slot back whenever the pointer
    crossed it, so the group the pointer had already left stayed on screen.
    The slot is shared, the strip decides it from the pointer, and a panel only
    holds its own.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "const $openSlot = atom(" in source
    assert "useValue($openSlot) === slot" in source
    assert "useState" not in source
    assert "data-hwm-slot" in source
    assert "onPointerMove: event => {" in source
    assert "onPointerEnter: markInside" in source
    assert source.count("slot: '") == source.count("jsx(CountWithCards, {")


def test_a_chip_is_an_icon_and_a_count_whose_slot_the_backend_answers():
    """The word lives in the panel header, and every slot is a real group.

    The desktop half asked for the group `done` while the backend only knows
    `done_today`, so that one panel answered 400 and showed no cards. The slot
    and the group it asks for are the backend's own names.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    # No chip renders its label as text; the count is the only text on it.
    assert "children: label" not in source
    for group in plugin_api.GROUP_ORDER:
        assert f"slot: '{group}'," in source, group
        assert f"group: '{group}'," in source, group
    assert "done_today: { dot: GREEN" in source
    # The old names must not come back.
    assert "slot: 'done'," not in source
    assert "group: 'done'," not in source


def test_the_desktop_half_renders_its_components_instead_of_calling_them():
    """A component that owns hooks must be RENDERED, not called as a function.

    The strip draws a variable number of count chips, so calling one as a plain
    function hands React a different hook count between renders: it throws
    (minified error #300) and the whole footer item vanishes. Seen live.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    for component in ("CardRow", "CountWithCards", "PopoverBody"):
        call = f"{component}({{"
        definition = f"function {component}({{"
        assert source.count(call) == source.count(definition), f"{component} is called as a plain function"
        assert f"jsx({component}," in source, f"{component} is never rendered"


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


# -- the card list behind one count ------------------------------------------


def _board_with_every_group() -> list[tuple]:
    day = plugin_api._local_day_start()
    return [
        ("t_blocked_a", "Needs a person", "blocked", "default", NOW, None, "needs_input"),
        ("t_blocked_b", "Another stop", "blocked", "default", NOW, None, "capability"),
        ("t_waiting", "Waiting on its parent", "blocked", "default", NOW, None, "dependency"),
        ("t_run", "Running worker", "running", "default", NOW),
        ("t_queued", "Ready to pick up", "todo", "default", NOW),
        ("t_scheduled", "Scheduled for later", "scheduled", "default", NOW),
        ("t_review", "Awaiting review", "review", "default", NOW),
        ("t_done_today", "Finished today", "done", "default", NOW, day + 1, None),
        ("t_done_older", "Finished last week", "done", "default", NOW, day - 1, None),
    ]


def test_the_card_list_under_a_count_always_matches_that_count():
    """The popover list and the strip count come from ONE classification."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _board(root / "kanban.db", _board_with_every_group())

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            totals = plugin_api.normalize_groups(
                plugin_api._load_status_counts(),
                plugin_api._load_blocked_kinds(),
                done_today=plugin_api._load_done_today(),
            )
            payloads = {group: plugin_api._build_cards_payload(group, 25) for group in plugin_api.GROUP_ORDER}

    for group, payload in payloads.items():
        assert payload["total"] == totals[group], group
        assert payload["shown"] == len(payload["cards"]) == totals[group], group
        assert payload["group"] == group

    assert [card["id"] for card in payloads["blocked"]["cards"]] == ["t_blocked_a", "t_blocked_b"]
    assert [card["id"] for card in payloads["waiting"]["cards"]] == ["t_waiting"]
    assert [card["id"] for card in payloads["running"]["cards"]] == ["t_run"]
    assert [card["id"] for card in payloads["queued"]["cards"]] == ["t_queued"]
    assert [card["id"] for card in payloads["scheduled"]["cards"]] == ["t_scheduled"]
    assert [card["id"] for card in payloads["review"]["cards"]] == ["t_review"]
    assert [card["id"] for card in payloads["done_today"]["cards"]] == ["t_done_today"]


def test_a_capped_card_list_still_reports_the_real_total():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _board(root / "kanban.db", [
            ("t_blocked_a", "One", "blocked", "default", NOW, None, "needs_input"),
            ("t_blocked_b", "Two", "blocked", "default", NOW, None, "capability"),
            ("t_blocked_c", "Three", "blocked", "default", NOW, None, "needs_input"),
        ])

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            payload = plugin_api._build_cards_payload("blocked", 2)

    assert payload["total"] == 3
    assert payload["shown"] == 2
    assert payload["limit"] == 2


def test_the_card_list_carries_no_card_body_or_arguments():
    """Only the fields a row draws leave the database."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _board(root / "kanban.db", [("t_blocked", "Needs a person", "blocked", "default", NOW, None, "needs_input")])

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            payload = plugin_api._build_cards_payload("blocked", 25)

    card = payload["cards"][0]
    assert set(card) <= set(plugin_api._CARD_FIELDS)
    assert "body" not in card
    assert card["title"] == "Needs a person"
    assert card["block_kind"] == "needs_input"


def test_a_board_without_block_kind_answers_blocked_and_no_waiting():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _board(root / "kanban.db", [("t_blocked", "Needs a person", "blocked", "default", NOW)], old_schema=True)

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            blocked = plugin_api._build_cards_payload("blocked", 25)
            waiting = plugin_api._build_cards_payload("waiting", 25)
            done = plugin_api._build_cards_payload("done_today", 25)

    assert [card["id"] for card in blocked["cards"]] == ["t_blocked"]
    assert waiting["cards"] == []
    # No completed_at column on the older board: an empty window, not a guess.
    assert done["cards"] == []


def test_a_running_card_carries_its_own_worker_state():
    """The running rows color themselves the way the chip does."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _board(root / "kanban.db", [("t_run", "Running worker", "running", "default", NOW)])

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True), \
                patch.object(plugin_api, "_load_tool_events", lambda task: []):
            payload = plugin_api._build_cards_payload("running", 25)

    assert payload["cards"][0]["worker_state"] == "active"


def test_an_unknown_group_is_refused():
    try:
        asyncio.run(plugin_api.get_cards(group="something-new"))
    except HTTPException as error:
        assert error.status_code == 400
    else:
        raise AssertionError("an unknown group was accepted")


def test_the_cards_route_bounds_its_page_by_default():
    parameter = inspect.signature(plugin_api.get_cards).parameters["limit"]
    assert parameter.default.default == 25
    assert any(getattr(constraint, "le", None) == 50 for constraint in parameter.default.metadata)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
