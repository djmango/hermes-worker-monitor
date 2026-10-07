# Hermes Worker Monitor

Kanban board groups and worker health in the Hermes Desktop footer. Worker-only fork of [hermes-monitor](https://github.com/mr-3mm3/hermes-monitor) (MIT).

One compact strip on the right side of the Desktop footer:

```text
[1 blocked]  [10 running]  21 queued   2 scheduled   26 done today
```

- Cards that stopped and need a person come first, in red.
- The running count carries a state dot: green for active, amber when a card stalls, red when a worker loops. The dot color follows the worst running card, so a stall or a loop is visible without opening anything.
- Hover any count for the cards behind it: a list of that group's cards, each row with the card's dot, title, id, and age. The rows are the app's own list row from the Desktop plugin SDK, so the footer and the Kanban page cannot drift apart.
- Hover a card in that list for the whole card in a centered overlay over the app, the way a fuzzy finder floats in the middle of the screen: the title and its pills, the fields, the description as markdown, the newest run, the recent comments, and the attachments. It is the same set the board's own drawer shows, built from the app's own dialog shell, panel parts and chat markdown renderer. Read-only: the actions stay one click away in the drawer, because a desktop plugin cannot reach the Kanban plugin's own API.
- Nothing needs a precise pointer. The card opens after a short rest on a row, so sweeping the list does not flash overlays, and both layers stay up while the pointer is on any of the strip, the list, or the overlay. A pointer crossing the gap to the centered overlay is mid journey, not gone, and the grace is 1000ms. Cards are fetched while the list is on screen, so the overlay paints from cache instead of opening with a loading line.
- The app behind the card is dimmed and blurred the way the app dims behind any dialog, and the list rides above that dim so it never darkens the list the pointer is walking back to. The dim does not take the pointer, so the list stays live.
- Click any count, or any card in the list, to open the Kanban board page inside the app. There is no menu to open and no second screen to read.
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

- Three routes on the local backend: `GET /summary` for the strip, `GET /cards` for the list a count opens, and `GET /card` for the card a hovered row opens.
- Read-only SQLite access to `kanban.db`, plus each profile's `state.db` for worker activity.
- `GET /summary` is counts only. The payload carries group counts and the count of active, stalled, and looping workers. No card identifier, title, assignee, or tool argument is in it.
- `GET /cards` returns the cards of the one group that was asked for, and only the fields a row draws: id, title, status, assignee, priority, blocked reason, and the created, started, and completed timestamps. Titles are capped at 200 characters.
- `GET /card` returns the one card that was asked for, the way the drawer shows it: its fields, its description (capped at 6000 characters), its result, its last failure, its newest run, its three newest comments (each capped at 500 characters), and the names of up to ten attachments. Nothing else on the board is read for it.
- Worker activity metadata inside the backend: tool name, a 16-character SHA-256 hash of the arguments, and a timestamp, used for stall and loop detection.

Counted, against upstream v0.2.2:

| | Upstream | This fork |
|---|---|---|
| Product lines (backend + desktop) | 1,488 | 1,759 |
| Backend (`dashboard/`) | 682 | 726 |
| Desktop (`desktop/plugin.js`) | 796 | 1,033 |
| Tests | 838 | 787 |

The comparison is against the fork at 0.2.1. The hover preview and `GET /card` are additions upstream has no counterpart for, so this fork is no longer the smaller of the two.

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

Quit Hermes Desktop completely, then reopen it. The footer strip appears on its own: the desktop half ships `defaultEnabled: true`, so there is no switch to flip. To hide it, open Capabilities -> Plugins and switch Hermes Worker Monitor off. Turning it back on restores it.

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

Loop has priority over stalled, and stalled has priority over active when several workers run. The footer dot takes the worst of the three. Each row in the hover list for `running` carries its own state color, so one stalled worker is visible inside a healthy group.

Activity comes from the worker's own session store. A named profile keeps its sessions in `profiles/<name>/state.db`, and the default profile keeps them in the root `state.db`, so the reader tries the named store first and then the root store for a default-profile card. Both are opened read-only.

## Privacy

The backend opens the local databases in read-only mode and writes nothing. `GET /summary` returns counts only. `GET /cards` returns the cards of the one group that was asked for, and `GET /card` returns the one card a row was hovered on, description included. Both card routes answer only to the app that asked, over the same authenticated channel as every other route, and they are the only ones that carry card text. Message content and tool arguments never leave the backend, and arguments are represented by hashes for the stall and loop test.

## Development

Requirements: Python 3.11+ with `pytest`, and Node.js for the syntax check. The tests are pure logic with fixtures and make no real network calls.

```sh
PYTHONPATH=. python3 -m pytest tests/ -q
# or, without pytest:
PYTHONPATH=. python3 -m unittest discover -s tests -t tests
node --check desktop/plugin.js
```

The backend lives in `dashboard/` (`plugin_api.py` wires the route and `workers.py` holds the pure transformations) and the Desktop half is the single module `desktop/plugin.js`.

## Upstream and attribution

The original plugin, its design, and the worker-state logic are the work of [mr-3mm3](https://github.com/mr-3mm3). This fork changes packaging, naming, and scope only, and keeps the MIT license and the upstream copyright notice.

If you want provider quota tracking as well, install upstream `hermes-monitor`, or `ai-usage-tracker` for quotas alone.

## License

MIT. See [LICENSE](LICENSE).
