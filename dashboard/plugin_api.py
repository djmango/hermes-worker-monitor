"""Hermes Worker Monitor dashboard API.

Worker health only. The route opens the local Kanban and worker session
databases read-only and returns activity metadata. This fork reads no
credentials, makes no network calls, and writes nothing.
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
    from .workers import build_workers_response, hash_arguments
except ImportError:  # Loaded by file path by the Hermes plugin loader: no package, no sys.path entry.

    def _load_sibling(name: str) -> Any:
        spec = _ilu.spec_from_file_location(f"hermes_worker_monitor_{name}", Path(__file__).with_name(f"{name}.py"))
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    _workers = _load_sibling("workers")
    build_workers_response, hash_arguments = _workers.build_workers_response, _workers.hash_arguments


router = APIRouter()
CACHE_TTL_S = 300


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


_workers_cache = TTLCache()
_workers_lock = asyncio.Lock()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve(strict=True)
    return sqlite3.connect(f"file:{quote(str(resolved), safe='/')}?mode=ro", uri=True)


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".hermes"
    # A named profile points HERMES_HOME at ``<root>/profiles/<name>`` while
    # kanban and the profile roster remain rooted at ``<root>``.
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _load_running_tasks() -> list[dict[str, Any]]:
    database = _hermes_home() / "kanban.db"
    with closing(_readonly_connection(database)) as connection:
        rows = connection.execute(
            "SELECT id, title, assignee, started_at FROM tasks WHERE status = ? ORDER BY started_at, id",
            ("running",),
        ).fetchall()
    return [{"id": row[0], "title": row[1], "assignee": row[2], "started_at": row[3], "kanban_url": None} for row in rows]


def _safe_profile_database(assignee: str) -> Path | None:
    if not assignee or assignee in {".", ".."} or Path(assignee).name != assignee:
        return None
    profiles = (_hermes_home() / "profiles").resolve()
    candidate = (profiles / assignee / "state.db").resolve()
    return candidate if candidate.is_relative_to(profiles) and candidate.is_file() else None


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


def _load_tool_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    database = _safe_profile_database(str(task.get("assignee") or ""))
    if database is None:
        return []
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


def _build_workers_payload() -> dict[str, Any]:
    now = int(time.time())
    tasks = _load_running_tasks()
    activity = {str(task["id"]): _load_tool_events(task) for task in tasks}
    return build_workers_response(tasks, activity, now=now)


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


@router.get("/workers")
async def get_workers(fresh: int = Query(default=0, ge=0, le=1)) -> dict[str, Any]:
    return await _cached(_workers_cache, _workers_lock, fresh, lambda: asyncio.to_thread(_build_workers_payload))
