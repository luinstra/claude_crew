"""Shared helpers for discovering crew state files across legacy and session-scoped layouts."""

# Keeps `Path | None` annotations lazy so stock macOS /usr/bin/python3
# (3.9) can IMPORT this module without a TypeError at def time; the hook
# entry points' version guards then fail open loudly instead of crashing.
from __future__ import annotations

from pathlib import Path
import json
import os
import sys


_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """One-time stderr note keyed by ``key`` (never stdout: doctor/scaffold-config
    emit machine JSON there)."""
    if key in _warned:
        return
    _warned.add(key)
    print(f"crew: {message}", file=sys.stderr)


def _should_reanchor(explicit: str | None, cwd: Path) -> bool:
    """Return whether the fallback cwd should be treated as artifact drift."""
    return not explicit and cwd.name == ".crew"


def cwd_reanchored() -> bool:
    """Return whether ``crew_base()`` would re-anchor the current cwd right now."""
    return _should_reanchor(
        os.environ.get("CLAUDE_PROJECT_DIR"),
        Path(os.getcwd()),
    )


def crew_base() -> Path:
    """THE one project-root resolver every `.crew` path derives from, so the
    state layer and the review engine cannot resolve `.crew` to different trees.

    Returns ``CLAUDE_PROJECT_DIR`` when set (hooks get it), else the process cwd.
    The env var is NOT set in the Bash-tool subprocess, so cwd is the real
    fallback for every crew CLI call. On that fallback ONLY, a cwd that is itself
    a terminal `.crew` artifact dir re-anchors to its parent (warn once): crew
    never roots a project inside its own `.crew`. One identical fallback for every
    caller: no hint, no override env var, because a second knob is a second way for
    the two layers to disagree.
    """
    # EMPTY string is treated as UNSET (falls to the cwd branch), preserving the
    # old `... or os.getcwd()` truthiness exactly; only a truthy value short-circuits.
    explicit = os.environ.get("CLAUDE_PROJECT_DIR")
    if explicit:
        return Path(explicit)
    cwd = Path(os.getcwd())
    # Drift signature: the Bash-tool cwd PERSISTS across calls, so an earlier
    # `cd <root>/.crew` (or the `.crew/.crew` this bug mints) leaves cwd terminal-
    # `.crew`, which would double every `.crew/...` path. Strip the WHOLE terminal
    # `.crew` run to the first non-`.crew` parent (crew never roots inside its own
    # artifact dir); the `parent == self` guard terminates at the filesystem root.
    if _should_reanchor(explicit, cwd):
        drifted = cwd
        while cwd.name == ".crew" and cwd.parent != cwd:
            cwd = cwd.parent
        _warn_once(
            str(drifted),
            f"cwd {drifted} ends in .crew and CLAUDE_PROJECT_DIR is unset or empty; using "
            f"{cwd} as the project root. cd back to the project root or set "
            f"CLAUDE_PROJECT_DIR to silence this.",
        )
    return cwd


def anchor_path(value: str) -> str:
    """Anchor a RELATIVE CLI path argument to ``crew_base()``; absolute unchanged.

    THE one resolution rule for every explicit path arg the crew CLIs take
    (``-f``/``-o``/``--full``/``--seat``/``--detection``/…), shared by cli.py and
    crew-state.py so the two layers cannot resolve the same argument against
    different trees. A relative path resolves against the project root, never the
    shell cwd: that is what lets a shipped recipe pass a plain ``.crew/...`` path
    with no ``${...}`` expansion (which would defeat permission allowlisting) and
    still land in the right tree when the shell cwd has drifted.

    Pass-throughs, exact: ``-`` (the stdout sentinel some verbs honor), empty
    string, and None (an absent optional flag; argparse never converts None
    defaults, this guard is for direct callers). ``~`` expands first, so a
    ``~/...`` arg counts as absolute. An absolute path is returned BYTE-TRUE
    (post-expansion): no Path round-trip that would collapse redundant
    slashes or strip a trailing one.
    """
    if value is None or value in ("", "-"):
        return value
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return expanded
    return str(crew_base() / Path(expanded))


def is_active_value(value: object) -> bool:
    """THE rule for "is this loop on", shared by every reader of the field.

    Truthiness, not `is True`: the Stop hook blocks on any truthy `active`, so a
    hand-edited `"true"` or `1` is a loop that is ON, and every other reader
    (discovery, the session banner, the deactivate gate) has to agree. A stricter
    rule in the gate made `deactivate --cancel` a no-op on exactly those files:
    the escape hatch went dead while the hook kept blocking.
    """
    return bool(value)


def is_loop_state_file(filename: str) -> bool:
    """Check if a filename is a loop state file (legacy or session-scoped).

    Matches:
    - build-state.json / build-state-abc123.json
    - measure-twice-state.json / measure-twice-state-xyz789.json
    """
    return (
        (filename.startswith("build-state")
         or filename.startswith("measure-twice-state"))
        and filename.endswith(".json")
    )


def find_adoptable_legacy(
    crew_dir: Path,
    loop_prefix: str,
    session_id: str,
) -> Path | None:
    """Return the legacy unsuffixed state file if THIS session may adopt it.

    Adoptable when the file exists and its recorded session_id is empty
    (written by a pre-scoping version) or equals this session's id.
    An unreadable or unparseable file is not adoptable.
    """
    legacy_path = crew_dir / f"{loop_prefix}.json"
    if not legacy_path.is_file():
        return None
    try:
        with open(legacy_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    # Valid-but-non-object JSON (e.g. a bare list) is the corrupt category,
    # not an adoptable state file; without this guard .get() would traceback.
    if not isinstance(data, dict):
        return None
    file_session = data.get("session_id", "")
    if file_session == session_id or file_session == "":
        return legacy_path
    return None


def find_session_state_file(
    crew_dir: Path,
    loop_prefix: str,
    session_id: str,
) -> Path | None:
    """Find the state file for a given loop and session.

    When session_id is non-empty: look for session-scoped file first,
    then check legacy unsuffixed file if it matches this session.
    When session_id is empty: use legacy unsuffixed file only.
    """
    if session_id:
        scoped_path = crew_dir / f"{loop_prefix}-{session_id}.json"
        if scoped_path.is_file():
            return scoped_path
        return find_adoptable_legacy(crew_dir, loop_prefix, session_id)
    else:
        legacy_path = crew_dir / f"{loop_prefix}.json"
        if legacy_path.is_file():
            return legacy_path
        return None


def is_active_state_file(path: Path) -> bool:
    """Check if a state file is active without coupling to a specific state class.

    Used by cleanup logic that doesn't care whether it's a build or measure-twice file.
    Returns False on any IO/parse error (treat unreadable files as inactive).
    """
    try:
        with open(path) as f:
            data = json.load(f)
        return is_active_value(data.get("active", False))
    except (OSError, json.JSONDecodeError):
        return False
