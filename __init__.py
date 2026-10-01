"""Hermes Worker Monitor has no agent-side capabilities.

The plugin ships a Desktop status-bar UI (``desktop/plugin.js``) and one
read-only dashboard route (``dashboard/plugin_api.py``). This module exists so
the agent plugin loader can import the package without registering anything.
"""


def register(ctx) -> None:  # noqa: ARG001 - intentionally registers nothing
    return None
