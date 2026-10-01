# Changelog

## 0.2.0

- The footer item now shows the board instead of a running-card count. One strip with `blocked`, `waiting`, `running`, `queued`, `scheduled`, `review`, and `done today`; a group with no cards is left out.
- Cards that stopped and need a person are first and red. Cards blocked on another card read as `waiting`, since the board clears those on its own.
- Hover any count for the breakdown (`10 running: 8 active, 2 stalled. Click for the board`). The tooltip is the app's own tooltip component.
- Click any count to open the Kanban board page in the app through `host.navigate('/kanban')`. The dropdown menu, the per-card rows, and the external link are gone, so the desktop half is smaller and there is no second screen to read.
- The running label never changes width. Only the state dot changes color, so the strip does not jitter as workers stall or loop.
- The route is now `GET /summary`, and the payload carries counts only: the group counts plus the number of active, stalled, and looping workers. No card identifier, title, assignee, or tool argument leaves the backend.
- The backend reads the board with one connection per query and tolerates an older board: a `tasks` table without `block_kind` or `completed_at` reports zero rather than failing.
- Removed every em dash from the code, the docs, and the UI copy. A test fails if one comes back.
- Tests: 20.

## 0.1.1

- Activity lookup no longer assumes every assignee has a profile directory. The default profile keeps its sessions in the root `state.db`, so reading only `profiles/<assignee>/state.db` left every default-profile card with no activity signal, and stall and loop detection never fired. The reader now tries the named profile store first, then the root store for the default assignee. A named profile never falls back to another store.
- Two tests cover the fallback, the preference order, and the rejection of unsafe assignee names.

## 0.1.0

- Fork of hermes-monitor v0.2.2 (MIT, mr-3mm3), worker health only.
- Renamed to hermes-worker-monitor, with its own plugin.yaml, manifest, and desktop plugin id.
- Removed the Quotas footer item, the provider fan-out, the DeepSeek key lookup and balance call, the Anthropic OAuth refresh path, and the internal-function clone.
- The backend now has one route, `GET /workers`, and the module reads no credentials and makes no network calls.
- Trimmed the test suite to the worker tests, plus a guard test that fails if a credential or network reference returns to the backend module.
