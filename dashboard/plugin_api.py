"""Hermes Worker Monitor dashboard API.

One route, one payload: the Kanban group counts and the health of the running
workers. The route opens the local Kanban and worker session databases
read-only and returns counts only. This fork reads no credentials, makes no
network calls, and writes nothing.
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

from fastapi import APIRouter, Query

try:
    from .workers import build_summary_response, hash_arguments, normalize_groups
except ImportError:  # Loaded by file path by the Hermes plugin loader: no package, no sys.path entry.

    def _load_sibling(name: str) -> Any:
        spec = _ilu.spec_from_file_location(f"hermes_worker_monitor_{name}", Path(__file__).with_name(f"{name}.py"))
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    _workers = _load_sibling("workers")
    build_summary_response = _workers.build_summary_response
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
