"""Shared helpers for discovering crew state files across legacy and session-scoped layouts."""

# C4: keeps `Path | None` annotations lazy so stock macOS /usr/bin/python3
# (3.9) can IMPORT this module without a TypeError at def time — the hook
# entry points' version guards then fail open loudly instead of crashing.
from __future__ import annotations

from pathlib import Path
import json


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
        legacy_path = crew_dir / f"{loop_prefix}.json"
        if legacy_path.is_file():
            try:
                with open(legacy_path) as f:
                    data = json.load(f)
                file_session = data.get("session_id", "")
                if file_session == session_id or file_session == "":
                    return legacy_path
            except (OSError, json.JSONDecodeError):
                pass
        return None
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
        return bool(data.get("active", False))
    except (OSError, json.JSONDecodeError):
        return False
