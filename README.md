# Hermes Worker Monitor

Kanban worker health in the Hermes Desktop status bar. Worker-only fork of [hermes-monitor](https://github.com/mr-3mm3/hermes-monitor) (MIT).

One compact item on the right side of the Desktop footer:

```text
[Worker 2]
```

The number is colored by the worst worker state (loop > stalled > active), and the spinner runs while at least one worker is active. Open the item to see each running card: title, assignee, runtime, last activity, and a link to Kanban. The menu requests a fresh reading when it opens.

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

- One route, `GET /workers`, on the local backend.
- Read-only SQLite access to `kanban.db` and each profile's `state.db`.
- Activity metadata only. Tool arguments are represented by a 16-character SHA-256 hash for loop detection.

Counted, against upstream v0.2.2:

| | Upstream | This fork |
|---|---|---|
| Product lines (backend + desktop) | 1,488 | 590 |
| Backend (`dashboard/`) | 682 | 245 |
| Desktop (`desktop/plugin.js`) | 796 | 335 |
| Tests | 838 | 168 |


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

Loop has priority over stalled, and stalled has priority over active when several workers run.

## Privacy

The backend opens the local databases in read-only mode. It returns activity metadata only. Message content and tool arguments never reach the frontend, and arguments are represented by hashes.

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
