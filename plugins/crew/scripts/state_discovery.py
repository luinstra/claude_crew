"""Shared helpers for discovering crew state files across legacy and session-scoped layouts."""

# Keeps `Path | None` annotations lazy so stock macOS /usr/bin/python3
# (3.9) can IMPORT this module without a TypeError at def time; the hook
# entry points' version guards then fail open loudly instead of crashing.
from __future__ import annotations

from pathlib import Path
import json
import os


def crew_base() -> Path:
    """THE one project-root resolver every `.crew` path derives from, so the
    state layer and the review engine cannot resolve `.crew` to different trees.

    Returns ``CLAUDE_PROJECT_DIR`` (the truth: the harness always sets it), else
    the process cwd (a hook's cwd IS the project root). One identical fallback for
    every caller: no hint, no override env var, because a second knob is a second
    way for the two layers to disagree.
    """
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


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
