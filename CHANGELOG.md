# Changelog

## 0.2.2

- Hovering a count now opens a popover with the cards behind that number instead of a text tooltip. Each row carries the card's dot, title, id, and age, and the rows are the app's own `PanelListRow` with `PanelPill` and `PanelSectionLabel`, so the footer and the app's Kanban list cannot drift apart.
- The `Click for the board` sentence is gone, and the tooltip with it. A count that needed a whole sentence to explain itself was not carrying its weight.
- The popover header shows the group's real total, and a capped list says how many more are on the board. A long group never reads as the whole board.
- The `running` rows carry their own worker state, so a stalled or looping worker is visible inside a healthy group instead of one flat color.
- New backend route `GET /cards?group=<name>&limit=<n>`. It reuses the same classification as the counts (`card_group` in `workers.py`), so a number and the cards under it cannot disagree. A test asserts, for every group, that the list length equals the count beside it.
- Only the fields a row draws come back: id, title, status, assignee, priority, blocked reason, and the created, started, and completed timestamps. The card body and the tool arguments stay in the database, and titles are capped at 200 characters.
- `/cards` is uncached on purpose: it answers a hover, and a list stale enough to disagree with the live count beside it reads as a fault.
- A backend that predates the route answers 404, and the popover says so in one line instead of showing an empty list.
- Fixed before the release: the strip rendered its count chips by calling the component as a plain function, so React threw (minified error `#300`) the moment the group set changed and the footer item disappeared. The chips are rendered through `jsx(...)` now, and a test fails if a hook-owning component is called that way again.
- Tests: 31.

## 0.2.1

- The desktop half is on by default (`defaultEnabled: true`). The strip is the point of the plugin and it reads counts only, so it no longer needs a switch flip before it appears. It can still be turned off in Capabilities, Plugins.
- README install steps updated: reopen the app and the strip is there.

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
