# Hermes Worker Monitor

Kanban board groups and worker health in the Hermes Desktop footer. Worker-only fork of [hermes-monitor](https://github.com/mr-3mm3/hermes-monitor) (MIT).

One compact strip on the right side of the Desktop footer:

```text
[1 blocked]  [10 running]  21 queued   2 scheduled   26 done today
```

- Cards that stopped and need a person come first, in red.
- The running count carries a state dot: green for active, amber when a card stalls, red when a worker loops. The dot color follows the worst running card, so a stall or a loop is visible without opening anything.
- Hover any count for the breakdown, for example `10 running: 8 active, 2 stalled. Click for the board`.
- Click any count to open the Kanban board page inside the app. There is no menu to open and no second screen to read.
- A group with no cards is left out, so the strip stays short. A quiet board shows the queue, or `idle`.

## What this fork removes, and why

Upstream ships a second footer item for provider quota usage. That item is the only reason upstream needs anything outside your own machine, so this fork drops it.

Removed:

- The Quotas footer item and its menu, including Claude and Codex usage windows and the DeepSeek balance.
- The provider fan-out in the backend: every credential pool is no longer loaded, so no provider token is read.
- The `DEEPSEEK_API_KEY` lookup from the environment, the Hermes root `.env`, and each profile `.env`.
- The only outbound HTTP call in the repository (`https://api.deepseek.com/user/balance`).
- The private-function clone that worked around a Hermes internal signature, so nothing here is bound to an internal API.
- The Anthropic OAuth refresh path. Upstream calls the core credential pool `select()` when a token is near expiry, which can rewrite `auth.json`. This fork never touches it.

What is left:

- One route, `GET /summary`, on the local backend.
- Read-only SQLite access to `kanban.db`, plus each profile's `state.db` for worker activity.
- Counts only. The payload carries group counts and the count of active, stalled, and looping workers. No card identifier, title, assignee, or tool argument leaves the backend.
- Worker activity metadata inside the backend: tool name, a 16-character SHA-256 hash of the arguments, and a timestamp, used for stall and loop detection.

Counted, against upstream v0.2.2:

| | Upstream | This fork |
|---|---|---|
| Product lines (backend + desktop) | 1,488 | 742 |
| Backend (`dashboard/`) | 682 | 386 |
| Desktop (`desktop/plugin.js`) | 796 | 356 |
| Tests | 838 | 359 |

## Trust surface

- No credential is read, from any source.
- No network call is made. Zero egress.
- No file is written.
- No install-time code: no `preinstall`, no `postinstall`, no setup script, and no third-party dependency. The Python half imports only the standard library and the `fastapi` that Hermes already provides.

## Requirements

- Hermes 0.21 or later
- Hermes Desktop

## Install in Hermes

```sh
hermes plugins install djmango/hermes-worker-monitor
```

Enable its backend for the active Hermes home:

```sh
hermes plugins enable hermes-worker-monitor
```

Quit Hermes Desktop completely, then reopen it. In the app, open Capabilities -> Plugins and enable Hermes Worker Monitor. The plugin ships `defaultEnabled: false`, so it stays off until you switch it on.

### From a local checkout

Symlink the package into the Hermes plugin directory:

```sh
ln -s "$PWD" ~/.hermes/plugins/hermes-worker-monitor
hermes plugins enable hermes-worker-monitor
```

Add `hermes-worker-monitor` to `plugins.enabled` if the enable command is unavailable. Restart the gateway so the backend route is loaded, then quit and reopen Hermes Desktop so the footer item appears.

## Worker health states

The backend reads running Kanban cards and the local activity of their workers:

- Active: recent worker activity.
- Stalled: no activity for more than five minutes.
- Loop: the same tool with the same arguments appears at least four times in a row in the latest events.

Loop has priority over stalled, and stalled has priority over active when several workers run. The footer dot takes the worst of the three, and the tooltip prints the split.

Activity comes from the worker's own session store. A named profile keeps its sessions in `profiles/<name>/state.db`, and the default profile keeps them in the root `state.db`, so the reader tries the named store first and then the root store for a default-profile card. Both are opened read-only.

## Privacy

The backend opens the local databases in read-only mode and returns counts only. Message content, card titles, assignees, and tool arguments never leave the backend, and arguments are represented by hashes for the stall and loop test.

## Development

Requirements: Python 3.11+ with `pytest`, and Node.js for the syntax check. The tests are pure logic with fixtures and make no real network calls.

```sh
PYTHONPATH=. python3 -m pytest tests/ -q
node --check desktop/plugin.js
```

The backend lives in `dashboard/` (`plugin_api.py` wires the route and `workers.py` holds the pure transformations) and the Desktop half is the single module `desktop/plugin.js`.

## Upstream and attribution

The original plugin, its design, and the worker-state logic are the work of [mr-3mm3](https://github.com/mr-3mm3). This fork changes packaging, naming, and scope only, and keeps the MIT license and the upstream copyright notice.

If you want provider quota tracking as well, install upstream `hermes-monitor`, or `ai-usage-tracker` for quotas alone.

## License

MIT. See [LICENSE](LICENSE).
