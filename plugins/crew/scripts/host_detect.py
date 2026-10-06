"""Stdlib-only host detection for the hook entry points."""

from collections.abc import Mapping
import os
import sys


# Kept literal-equal to multiagent/channels.py for all three marker tables.
_CODEX_HOST_MARKERS: tuple[str, ...] = (
    "CODEX_THREAD_ID",
    "CODEX_SANDBOX_NETWORK_DISABLED",
)
_CURSOR_HOST_MARKERS: tuple[str, ...] = (
    "CURSOR_AGENT",
    "CURSOR_CONVERSATION_ID",
)
_CLAUDE_HOST_MARKERS: tuple[str, ...] = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    print(message, file=sys.stderr)


def _detect_host(env: Mapping[str, str]) -> str:
    """Apply the hook-side host precedence without importing the engine.

    Order: ``CREW_HOST`` override, then codex, claude, cursor. Cursor ranks last
    because its markers ride in a terminal a human can launch another harness
    from; multiagent/channels.py holds the full rule this mirrors.
    """
    known_hosts = ("claude", "codex", "cursor", "openhands")
    override = env.get("CREW_HOST", "")
    if override:
        normalized = override.casefold()
        if normalized in known_hosts:
            return normalized
        _warn_once(
            "invalid-crew-host",
            f"crew: CREW_HOST={override!r} is not a known host (claude, codex, cursor, openhands); "
            "treating the host as unknown (no native channel)",
        )
        return "unknown"

    if any(env.get(marker) for marker in _CODEX_HOST_MARKERS):
        return "codex"
    if any(env.get(marker) for marker in _CLAUDE_HOST_MARKERS):
        return "claude"
    if any(env.get(marker) for marker in _CURSOR_HOST_MARKERS):
        return "cursor"
    return "unknown"


def detect_host() -> str:
    """Return the current host, defaulting to ``unknown`` on no markers."""
    return _detect_host(os.environ)
