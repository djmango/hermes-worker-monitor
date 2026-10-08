import asyncio
import inspect
import json
import os
import re
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
    assert paths == {"/summary", "/cards", "/card"}


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


def test_only_one_panel_of_each_kind_can_be_open():
    """One shared hover, for the chip and for the card row under it.

    Each chip used to own its open flag, so hovering across the footer left a
    row of panels up. Then a panel took the slot back whenever the pointer
    crossed it, so the group the pointer had already left stayed on screen. Both
    the chip and the card row report into one shared hover now.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "const $hover = atom(" in source
    assert "useValue($hover)" in source
    assert "useState" not in source
    assert "data-hwm-slot" in source
    # The card row is a hover layer of its own, keyed by the card.
    assert "data-hwm-card" in source
    assert "onPointerMove: event => {" in source
    assert "onPointerEnter: markInside" in source
    assert source.count("slot: '") == source.count("jsx(CountWithCards, {")


def test_the_hover_grace_is_long_enough_to_reach_a_panel():
    """The pointer has to be able to travel onto the panel it just opened.

    A short countdown loses that race, which is what made the panel feel hard to
    catch. A test fails if the grace is cut back to a knife's edge.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    match = re.search(r"const HOVER_CLOSE_MS = (\d[\d_]*)", source)
    assert match, "no hover grace in the desktop half"
    assert int(match.group(1).replace("_", "")) >= 400


def test_a_chip_is_an_icon_and_a_count_whose_slot_the_backend_answers():
    """The word lives in the panel header, and every slot is a real group.

    The desktop half asked for the group `done` while the backend only knows
    `done_today`, so that one panel answered 400 and showed no cards. The slot
    and the group it asks for are the backend's own names.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    # No chip renders its label as text: the count is the only text on it. The
    # label text carried its own tone prop, which is gone with it.
    assert "toneLabel" not in source
    assert "jsx('span', { style: { color: QUIET_COLOR }, children: label })" not in source
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
    for component in ("CardPanel", "CardPreview", "CardRow", "CountWithCards", "PopoverBody", "Section"):
        call = f"{component}({{"
        definition = f"function {component}({{"
        assert source.count(call) == source.count(definition), f"{component} is called as a plain function"
        assert f"jsx({component}," in source, f"{component} is never rendered"


def test_the_card_overlay_is_the_apps_own_centered_dialog():
    """The card floats in the middle of the screen, not off the row it came from.

    A panel hanging off a row is a small moving target that can land off screen
    or behind the list, and reaching it with a pointer is a race. The overlay is
    the app's own dialog shell instead: one fixed surface in the middle, drawn
    from the app's own parts.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "jsx(Dialog, {" in source
    for part in ("DialogContent", "DialogTitle", "DialogDescription", "DialogFooter"):
        assert part in source, part
    # Only the group list is a popover, and there is one card overlay for the
    # whole strip rather than one per row.
    assert source.count("jsxs(Popover, {") == 1
    assert source.count("jsx(CardPanel,") == 1
    # A hover must not take the caret out of the composer.
    assert source.count("onOpenAutoFocus: event => event.preventDefault()") >= 2
    # The app's dim is kept, but it must not take the pointer, and the list
    # rides above both the dim and the card so the scrim never darkens it and the
    # card never covers a row the pointer wants to click.
    assert "modal: false" in source
    assert "const OVERLAY_DIM = 'pointer-events-none'" in source
    assert "blurBackdrop: false" not in source
    assert "bg-transparent" not in source
    assert "zIndex: 125" in source


def test_every_card_in_a_list_is_warmed_before_it_is_hovered():
    """A hover must paint from cache, not open with a loading line.

    The list is on screen when the pointer arrives, so the fetch hides behind
    reading the list. The warmer and the overlay share one hook, which means one
    cache key: a hover cannot miss what was already fetched.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "function CardPrefetch({ ctx, id })" in source
    assert "jsx(CardPrefetch, { ctx, id: card.id" in source
    assert source.count("useCardDetail(") == 3


def test_a_card_opens_after_a_rest_on_a_row_not_at_once():
    """Sweeping the list must not flash a full screen overlay at every row."""
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "scheduleCard(slot, card.id)" in source
    assert "onPointerLeave: cancelScheduledCard" in source
    match = re.search(r"const CARD_OPEN_DWELL_MS = (\d[\d_]*)", source)
    assert match, "no dwell before a card opens"
    assert int(match.group(1).replace("_", "")) >= 80


def test_no_jsx_call_passes_children_as_the_key_argument():
    """`jsx(type, props, children)` is a trap: the third slot is the element KEY.

    React's automatic runtime takes children inside props, so a third argument is
    read as the key and the children are dropped without a word. A card panel
    built that way opens as an empty box, which is exactly what it looks like:
    nothing. Seen live.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    for number, line in enumerate(source.splitlines(), start=1):
        assert not re.search(r"jsx[sx]?\(.*\}, \[", line), f"children passed as the key argument on line {number}"
    assert "children:" in source


def test_the_strip_follows_the_boards_own_column_order():
    """The strip reads left to right like the board it reports on.

    The board's columns are triage, todo, scheduled, ready, running, blocked,
    review, done, so the strip runs backlog, scheduled, running, blocked (the
    dependency half included), review, then the day's finished cards. The
    backend's GROUP_ORDER is the one order, and the strip has to match it.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    slots = [name for name in re.findall(r"slot: '([a-z_]+)'", source) if name in plugin_api.GROUP_ORDER]
    assert slots == list(plugin_api.GROUP_ORDER), slots
    assert plugin_api.GROUP_ORDER[0] == "queued", "the board's first columns are the backlog"


def test_the_core_kanban_counter_is_hidden_by_one_scoped_rule():
    """The app's own counter reports the same board the strip does.

    A render style statusbar item is never listed in the bar's own show/hide menu
    (that menu lists declarative items that name themselves), and one plugin
    cannot unregister another plugin's contribution, so the duplicate goes with a
    single stylesheet rule. It has to stay scoped to the statusbar and to the one
    item carrying the project glyph, and it has to stay reversible.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "const CORE_COUNTER_STYLE_ID" in source
    assert '[data-slot="statusbar"] button:has(.codicon-project){display:none}' in source
    assert source.count("HIDE_CORE_COUNTER_CSS") == 2
    assert "hideCoreCounter()" in source
    assert "document.getElementById(CORE_COUNTER_STYLE_ID)" in source


def test_the_card_and_the_list_leave_at_once_while_the_chip_keeps_the_grace():
    """Both layers are left at once. Only the trip IN gets a grace.

    The pointer needs time to cross from a chip to the list, and from a row up to
    the card, so those trips keep the heartbeat. Leaving either layer does not: if
    a card is on show when the list goes, the card keeps the bounded trip and the
    timer ends it, so nothing lingers and nothing sticks.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "onPointerLeave: releaseCardNow," in source
    assert "onPointerLeave: releaseList," in source
    assert "function releaseList(event) {" in source
    assert "function releaseCardNow(event) {" in source
    # The chip keeps its grace: that one is the trip into the list.
    assert "onPointerLeave: releaseSlot," in source
    # A card on show holds the hover for the trip; nothing on show exits on the
    # short beat instead of waiting out a trip nobody is taking.
    list_leave = source[source.index("function releaseList(event) {") :][:900]
    assert "releaseAfter(hover.card === null ? LIST_EXIT_MS : HOVER_CLOSE_MS)" in list_leave


def test_the_card_sits_on_top_and_can_never_reach_the_list():
    """The card is the surface on top, and the two panels never overlap.

    The list is anchored above the statusbar and capped at 14rem of rows. The card
    is centred at 38 percent of the viewport and capped at 100vh minus 26rem, so
    it stays clear of that band on any screen size. That is what lets the card be
    on top while every row stays clickable: a centered dialog with no cap reached
    down into the list and swallowed the row clicks, so clicking a card did
    nothing at all.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "maxHeight: 'calc(100vh - 26rem)'" in source
    assert "top: '38%'" in source
    # `translate`, not `transform`: v4's centring uses the standalone property,
    # so a transform of the same shape adds to it and puts the card off screen.
    assert "translate: '-50% -50%'" in source
    assert "transform: 'translate(-50%, -50%)'" not in source
    assert "maxHeight: '14rem'" in source
    # The card is above the list, and the list above the dim.
    assert "zIndex: 125" in source
    # And a row click still opens the board.
    assert "onSelect: () => onOpen(card)" in source
    assert "host.navigate(BOARD_PATH)" in source
    assert "onOpen: openBoard" in source


def test_a_row_click_opens_that_card_on_the_board():
    """The board has no deep link for one card, so the click is made for you.

    Its drawer opens from local state inside the page, a card carries no id in the
    DOM, and the plugin SDK has no hook for it. So the row hands the card's own
    title to the navigator, which lands on the board and then clicks the card whose
    text starts with that title. A title it cannot find leaves the board as the
    destination rather than doing nothing.
    """
    source = (
        Path(__file__).resolve().parent.parent / "desktop" / "plugin.js"
    ).read_text(encoding="utf-8")
    assert "function openCardOnBoard(card) {" in source
    assert "onSelect: () => onOpen(card)" in source
    assert "openCardOnBoard(card)" in source
    assert "querySelectorAll('[draggable=\"true\"]')" in source
    assert "const CARD_OPEN_TIMEOUT_MS = 2500" in source
    # It checks its own work: the card's short id, which only its drawer shows.
    assert "includes(short)" in source
    assert "node.dataset.hwmTried !== '1'" in source
    # The chip opens the board with no card picked, and the card's own button
    # opens the card it is showing.
    assert "onClick: () => openBoard()" in source
    assert "onClick: () => onOpen(detail.card)" in source


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


# -- the whole card behind one row -------------------------------------------

DETAIL_SCHEMA = """
CREATE TABLE task_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, status TEXT,
  started_at INTEGER, ended_at INTEGER, outcome TEXT, summary TEXT, error TEXT
);
CREATE TABLE task_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT, body TEXT, created_at INTEGER
);
CREATE TABLE task_attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, filename TEXT, stored_path TEXT, created_at INTEGER
);
"""


def _detail_board(root: Path) -> None:
    with sqlite3.connect(root / "kanban.db") as connection:
        connection.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT, assignee TEXT, "
            "priority INTEGER, started_at INTEGER, completed_at INTEGER, block_kind TEXT, result TEXT)"
        )
        connection.execute(
            "INSERT INTO tasks VALUES ('t_blocked', 'Needs a person', 'the whole brief', 'blocked', "
            "'default', 5, ?, NULL, 'needs_input', NULL)",
            (NOW,),
        )
        connection.executescript(DETAIL_SCHEMA)
        connection.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, summary) "
            "VALUES ('t_blocked', 'blocked', ?, ?, 'blocked', 'stopped for input')",
            (NOW, NOW + 60),
        )
        for index in range(4):
            connection.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES ('t_blocked', 'skg', ?, ?)",
                (f"comment {index}", NOW + index),
            )
        connection.execute(
            "INSERT INTO task_attachments (task_id, filename, stored_path, created_at) "
            "VALUES ('t_blocked', 'notes.txt', '/tmp/notes.txt', ?)",
            (NOW,),
        )


def test_the_card_preview_returns_one_card_with_its_run_comments_and_attachments():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _detail_board(root)

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            payload = plugin_api._load_card_payload("t_blocked")
            assert payload is not None
            missing = plugin_api._load_card_payload("t_nope")
            injected = plugin_api._load_card_payload("t_blocked' OR 1=1 --")

    card = payload["card"]
    assert card["id"] == "t_blocked"
    assert card["title"] == "Needs a person"
    assert card["body"] == "the whole brief"
    assert card["block_kind"] == "needs_input"
    assert card["priority"] == 5
    assert payload["run"]["outcome"] == "blocked"
    assert payload["run"]["summary"] == "stopped for input"
    assert payload["comments"]["count"] == 4
    # The three newest, oldest first, so the preview reads downward in time.
    assert [item["body"] for item in payload["comments"]["recent"]] == ["comment 1", "comment 2", "comment 3"]
    assert payload["attachments"] == {"count": 1, "names": ["notes.txt"]}
    assert missing is None
    # A quoted id is data, never SQL.
    assert injected is None


def test_the_card_preview_clips_runaway_text():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _detail_board(root)
        with sqlite3.connect(root / "kanban.db") as connection:
            connection.execute("UPDATE tasks SET body = ?", ("x" * (plugin_api._BODY_CAP + 500),))

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            payload = plugin_api._load_card_payload("t_blocked")

    assert payload is not None
    assert len(payload["card"]["body"]) == plugin_api._BODY_CAP


def test_a_board_without_the_run_and_comment_tables_still_answers():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _board(root / "kanban.db", [("t_blocked", "Needs a person", "blocked", "default", NOW, None, "needs_input")])

        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            payload = plugin_api._load_card_payload("t_blocked")

    assert payload is not None
    assert payload["card"]["id"] == "t_blocked"
    assert payload["run"] is None
    assert payload["comments"]["count"] == 0
    assert payload["attachments"]["names"] == []


def test_the_card_route_refuses_a_card_that_is_not_there():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _detail_board(root)
        with patch.dict(os.environ, {"HERMES_HOME": str(root)}, clear=True):
            try:
                asyncio.run(plugin_api.get_card(id="t_nope"))
            except HTTPException as error:
                assert error.status_code == 404
            else:
                raise AssertionError("a missing card was accepted")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
