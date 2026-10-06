"""Hermes Worker Monitor dashboard API.

Two read-only routes. ``/summary`` is the one payload the footer strip reads:
the Kanban group counts and the health of the running workers. ``/cards``
returns the cards behind ONE group, for the popover a count opens. Both open the
local Kanban and worker session databases read-only. This fork reads no
credentials, makes no network calls, and writes nothing.

``/cards`` is the only route that returns card identifiers, titles, and
assignees, and only for the group that was asked for. It is deliberately
uncached: it answers a hover, and a stale list under a live count would read as
a fault.
"""

from __future__ import annotations

import asyncio
import importlib.util as _ilu
import inspect
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query

try:
    from .workers import (
        DEPENDENCY_KINDS,
        GROUP_ORDER,
        QUEUED_STATUSES,
        build_summary_response,
        card_group,
        card_state,
        hash_arguments,
        normalize_groups,
    )
except ImportError:  # Loaded by file path by the Hermes plugin loader: no package, no sys.path entry.

    def _load_sibling(name: str) -> Any:
        spec = _ilu.spec_from_file_location(f"hermes_worker_monitor_{name}", Path(__file__).with_name(f"{name}.py"))
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    _workers = _load_sibling("workers")
    DEPENDENCY_KINDS = _workers.DEPENDENCY_KINDS
    GROUP_ORDER = _workers.GROUP_ORDER
    QUEUED_STATUSES = _workers.QUEUED_STATUSES
    build_summary_response = _workers.build_summary_response
    card_group = _workers.card_group
    card_state = _workers.card_state
    hash_arguments = _workers.hash_arguments
    normalize_groups = _workers.normalize_groups


router = APIRouter()
CACHE_TTL_S = 300

# Assignee values that mean "the default profile", whose sessions live in the
# root store rather than under ``profiles/``.
_DEFAULT_PROFILE_NAMES = {"", "default"}


class TTLCache:
    def __init__(self, ttl: int = CACHE_TTL_S) -> None:
        self.ttl = ttl
        self._value: dict[str, Any] | None = None
        self._stored_at = 0.0

    def clear(self) -> None:
        self._value = None
        self._stored_at = 0.0

    def get(self, *, now: float) -> dict[str, Any] | None:
        if self._value is None or now - self._stored_at >= self.ttl:
            return None
        return self._value

    def set(self, value: dict[str, Any], *, now: float) -> dict[str, Any]:
        self._value = value
        self._stored_at = now
        return value


_summary_cache = TTLCache()
_summary_lock = asyncio.Lock()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    return sqlite3.connect(f"file:{quote(str(resolved), safe='/')}?mode=ro", uri=True)


def _kanban_database() -> Path:
    return _hermes_home() / "kanban.db"


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".hermes"
    # A named profile points HERMES_HOME at ``<root>/profiles/<name>`` while
    # kanban and the profile roster remain rooted at ``<root>``.
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _task_columns(connection: sqlite3.Connection) -> set[str]:
    """Column names of ``tasks``. Older boards have fewer than this one."""
    return {str(row[1]) for row in connection.execute("PRAGMA table_info(tasks)").fetchall()}


def _load_status_counts() -> dict[str, int]:
    with closing(_readonly_connection(_kanban_database())) as connection:
        rows = connection.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
    return {str(status): int(total) for status, total in rows}


def _load_blocked_kinds() -> dict[str, int]:
    """Blocked cards by reason. An older board without the column has none."""
    with closing(_readonly_connection(_kanban_database())) as connection:
        if "block_kind" not in _task_columns(connection):
            return {}
        rows = connection.execute(
            "SELECT COALESCE(block_kind, ''), COUNT(*) FROM tasks WHERE status = ? GROUP BY 1",
            ("blocked",),
        ).fetchall()
    return {str(kind): int(total) for kind, total in rows}


def _local_day_start(now: float | None = None) -> int:
    """Midnight, local time, as a Unix timestamp."""
    moment = time.localtime(time.time() if now is None else now)
    return int(time.mktime((moment.tm_year, moment.tm_mon, moment.tm_mday, 0, 0, 0,
                            moment.tm_wday, moment.tm_yday, moment.tm_isdst)))


def _load_done_today(now: float | None = None) -> int:
    with closing(_readonly_connection(_kanban_database())) as connection:
        if "completed_at" not in _task_columns(connection):
            return 0
        row = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE completed_at IS NOT NULL AND completed_at >= ?",
            (_local_day_start(now),),
        ).fetchone()
    return int(row[0]) if row else 0


def _load_running_tasks() -> list[dict[str, Any]]:
    with closing(_readonly_connection(_kanban_database())) as connection:
        rows = connection.execute(
            "SELECT id, assignee, started_at FROM tasks WHERE status = ? ORDER BY started_at, id",
            ("running",),
        ).fetchall()
    return [{"id": row[0], "assignee": row[1], "started_at": row[2]} for row in rows]


# -- one group's cards --------------------------------------------------------

# What a popover row needs. A column the board does not have is skipped, so an
# older board still answers.
_CARD_FIELDS = (
    "id",
    "title",
    "status",
    "assignee",
    "priority",
    "block_kind",
    "created_at",
    "started_at",
    "completed_at",
)

# Longest title sent. The popover draws one line, and the card body never leaves
# the database.
_TITLE_CAP = 200

# Statuses behind each status-shaped group. The other two groups are shaped by
# ``block_kind`` and by the day boundary instead.
_GROUP_STATUSES: dict[str, tuple[str, ...]] = {
    "running": ("running",),
    "queued": QUEUED_STATUSES,
    "scheduled": ("scheduled",),
    "review": ("review",),
}

# Per group, which card a person wants first: the longest run, then the newest
# finish. Everything else sorts by priority, then by age.
_GROUP_ORDER_BY = {
    "running": (("started_at", "ASC"),),
    "done_today": (("completed_at", "DESC"),),
}
_DEFAULT_ORDER_BY = (("priority", "DESC"), ("created_at", "ASC"))


def _order_clause(group: str, columns: set[str]) -> str:
    """ORDER BY for one group, built only from columns the board has."""
    pairs = _GROUP_ORDER_BY.get(group) or _DEFAULT_ORDER_BY
    parts = [f"{field} {direction}" for field, direction in pairs if field in columns]
    parts.append("id ASC")
    return ", ".join(parts)


def _group_where(group: str, columns: set[str], *, day_start: int) -> tuple[str, list[Any]]:
    """SQL that narrows the scan to a group's candidates.

    ``card_group`` still decides membership after the read, so a board that
    cannot tell the two blocked groups apart (no ``block_kind`` column) answers
    with an empty list rather than a wrong one.
    """
    if group in ("blocked", "waiting"):
        if "status" not in columns:
            return ("WHERE 0", [])
        if "block_kind" not in columns:
            return ("WHERE status = ?", ["blocked"]) if group == "blocked" else ("WHERE 0", [])
        marks = ", ".join("?" for _ in DEPENDENCY_KINDS)
        within = "IN" if group == "waiting" else "NOT IN"
        return (
            f"WHERE status = ? AND COALESCE(block_kind, '') {within} ({marks})",
            ["blocked", *DEPENDENCY_KINDS],
        )
    if group == "done_today":
        if "completed_at" not in columns:
            return ("WHERE 0", [])
        return ("WHERE completed_at IS NOT NULL AND completed_at >= ?", [day_start])
    statuses = _GROUP_STATUSES.get(group, ())
    if not statuses or "status" not in columns:
        return ("WHERE 0", [])
    marks = ", ".join("?" for _ in statuses)
    return (f"WHERE status IN ({marks})", list(statuses))


def _load_group_cards(group: str, limit: int, *, day_start: int) -> list[dict[str, Any]]:
    """Up to ``limit`` cards of one group, most relevant first."""
    with closing(_readonly_connection(_kanban_database())) as connection:
        columns = _task_columns(connection)
        where, params = _group_where(group, columns, day_start=day_start)
        fields = [field for field in _CARD_FIELDS if field in columns]
        if not fields:
            return []
        rows = connection.execute(
            f"SELECT {', '.join(fields)} FROM tasks {where} ORDER BY {_order_clause(group, columns)} LIMIT ?",
            (*params, int(limit)),
        ).fetchall()

    cards: list[dict[str, Any]] = []
    for row in rows:
        card = dict(zip(fields, row))
        if card_group(
            status=str(card.get("status") or ""),
            block_kind=card.get("block_kind"),
            completed_at=card.get("completed_at"),
            day_start=day_start,
        ) != group:
            continue
        title = card.get("title")
        if isinstance(title, str) and len(title) > _TITLE_CAP:
            card["title"] = title[:_TITLE_CAP]
        cards.append(card)
    return cards


def _build_cards_payload(group: str, limit: int) -> dict[str, Any]:
    """One group's cards, plus that group's real total from the same fold the
    footer strip counts with, so a capped list never reads as the whole board."""
    now = int(time.time())
    cards = _load_group_cards(group, limit, day_start=_local_day_start(now))
    if group == "running":
        # The chip colors itself by the worst live worker, so each row carries
        # its own state instead of one flat color for the group.
        for card in cards:
            card["worker_state"] = card_state(_load_tool_events(card), now=now)
    totals = normalize_groups(_load_status_counts(), _load_blocked_kinds(), done_today=_load_done_today(now))
    return {
        "generated_at": now,
        "group": group,
        "limit": int(limit),
        "shown": len(cards),
        "total": int(totals.get(group) or 0),
        "cards": cards,
    }


def _profile_databases(assignee: str) -> list[Path]:
    """Candidate read-only stores for *assignee*, most specific first.

    A named profile keeps its sessions in ``profiles/<name>/state.db``. The
    default profile has no profile directory at all: its sessions live in the
    root ``<hermes home>/state.db``. Reading only the named path would leave
    every card of the default profile with no activity signal.
    """
    if not assignee or assignee in {".", ".."} or Path(assignee).name != assignee:
        return []
    root = _hermes_home()
    candidates: list[Path] = []
    profiles = (root / "profiles").resolve()
    named = (profiles / assignee / "state.db").resolve()
    if named.is_relative_to(profiles) and named.is_file():
        candidates.append(named)
    if assignee in _DEFAULT_PROFILE_NAMES:
        root_database = (root / "state.db").resolve()
        if root_database.is_relative_to(root.resolve()) and root_database.is_file():
            candidates.append(root_database)
    return candidates


def _extract_calls(tool_calls: Any, timestamp: float) -> list[dict[str, Any]]:
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except (TypeError, ValueError):
            return []
    if not isinstance(tool_calls, list):
        return []
    events = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else call
        name = function.get("name") or call.get("tool_name")
        if not name:
            continue
        events.append({"tool_name": str(name), "arguments_hash": hash_arguments(function.get("arguments", {})), "timestamp": timestamp})
    return events


def _events_from(database: Path, task: dict[str, Any]) -> list[dict[str, Any]]:
    started = float(task.get("started_at") or 0)
    with closing(_readonly_connection(database)) as connection:
        session = connection.execute(
            "SELECT id FROM sessions WHERE source = ? AND started_at >= ? ORDER BY ABS(started_at - ?) LIMIT 1",
            ("kanban", started - 60, started),
        ).fetchone()
        if session is None:
            return []
        rows = connection.execute(
            "SELECT tool_calls, timestamp FROM messages WHERE session_id = ? AND tool_calls IS NOT NULL "
            "AND active = 1 AND compacted = 0 ORDER BY timestamp DESC, id DESC LIMIT 200",
            (session[0],),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for tool_calls, timestamp in reversed(rows):
        events.extend(_extract_calls(tool_calls, float(timestamp)))
    return events


def _load_tool_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    for database in _profile_databases(str(task.get("assignee") or "")):
        events = _events_from(database, task)
        if events:
            return events
    return []


def _build_summary_payload() -> dict[str, Any]:
    now = int(time.time())
    tasks = _load_running_tasks()
    activity = {str(task["id"]): _load_tool_events(task) for task in tasks}
    groups = normalize_groups(_load_status_counts(), _load_blocked_kinds(), done_today=_load_done_today(now))
    return build_summary_response(tasks, activity, groups, now=now)


async def _cached(cache: TTLCache, lock: asyncio.Lock, fresh: int, builder: Callable[[], Any]) -> dict[str, Any]:
    now = time.monotonic()
    if not fresh:
        cached = cache.get(now=now)
        if cached is not None:
            return cached
    async with lock:
        now = time.monotonic()
        if not fresh:
            cached = cache.get(now=now)
            if cached is not None:
                return cached
        value = builder()
        if inspect.isawaitable(value):
            value = await value
        return cache.set(value, now=time.monotonic())


@router.get("/summary")
async def get_summary(fresh: int = Query(default=0, ge=0, le=1)) -> dict[str, Any]:
    return await _cached(_summary_cache, _summary_lock, fresh, lambda: asyncio.to_thread(_build_summary_payload))


@router.get("/cards")
async def get_cards(
    group: str = Query(..., description="One of: " + ", ".join(GROUP_ORDER)),
    limit: int = Query(default=25, ge=1, le=50),
) -> dict[str, Any]:
    """The cards behind ONE group, for the popover the desktop half opens.

    Uncached on purpose: it answers a hover, and a list stale enough to disagree
    with the live count beside it reads as a fault.
    """
    if group not in GROUP_ORDER:
        raise HTTPException(status_code=400, detail=f"Unknown group: {group}")
    return await asyncio.to_thread(_build_cards_payload, group, limit)
