# Pinned cleanup functions from b39f647 session-start.py; uses schema3_models.
import sys
import time
from pathlib import Path
from schema3_models import read_state_json, LOAD_FUTURE_SCHEMA
from state_discovery import is_active_state_file, is_loop_state_file
MAX_AGE_SECONDS = 7 * 86400
STALE_INACTIVE_SECONDS = 86400

def cleanup_stale_files(directory: Path) -> None:
    """Remove stale state files, preserving active sessions.

    - Inactive state files older than 1 day: delete
    - Active state files older than MAX_AGE_DAYS (7): force-deactivate and delete
    - Non-state files (context-snapshot): delete if older than MAX_AGE_DAYS
    """
    if not directory.is_dir():
        return

    # Patterns for non-state crew files. EVERY glob here MUST be anchored to a
    # crew-specific literal or crew-prefixed pattern — this cleanup runs over BOTH
    # the project `.crew/` AND the SHARED `~/.claude` root (a dir owned by ALL
    # Claude Code tools), so a bare-suffix glob (`*.restored.md`, `.*.tmp`) would
    # delete a FOREIGN tool's / a user's files. No bare-suffix globs.
    #
    # The four `*-state.json.corrupt` / `*-state-*.json.corrupt` globs sweep the
    # set-aside artifacts the Stop hook / crew-state CLI create when a state file
    # can't be parsed (they end in `.corrupt`, so the `*.json` loop above never
    # sees them) — otherwise `build-state.json.corrupt` accumulates forever. They
    # are SCOPED to the EXACT backup names crew creates — the two forms are the
    # legacy `build-state.json` and the session-scoped `build-state-<id>.json`
    # (note the HYPHEN before the id) — NOT a loose `build-state*.json.corrupt`
    # that would also match a foreign `build-stateEVIL.json.corrupt`.
    #
    # The context-snapshot globs are EXACT literals only: `context-snapshot.md`
    # (what save-context.md writes) and `context-snapshot.restored.md` (the rename
    # restore-context.md leaves behind — otherwise it accumulates forever). NOT
    # `context-snapshot*.md` (would match a user's `context-snapshot-notes.md`)
    # nor a bare `*.restored.md` (would match ANY tool's restored file). The
    # legacy `context-snapshot-*.json` glob is kept (crew-prefixed, `.json`-tailed;
    # it never matched the real `.md` artifacts but harmlessly sweeps any old JSON
    # snapshot).
    #
    # The temp globs match ONLY what `atomic_write_json` (models.py) produces:
    # `tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp")` names each temp
    # `.<state-filename>.<rand>.tmp` — already crew-anchored by the state filename,
    # so approach (a) needs no generator change. We glob EXACTLY those anchored
    # names (the two state kinds × legacy/`-<id>` forms), never a bare `.*.tmp`
    # that would delete any hidden temp from any tool. These sweep atomic-write
    # temps orphaned by a crash between mkstemp and os.replace (rare).
    #
    # The `.lock` globs match the sibling lock files `models.state_lock` creates
    # next to each state file (`<state-filename>.lock`), already crew-anchored by the
    # state filename, and swept only past MAX_AGE_DAYS. A lock in use is kept young
    # (the acquire touches its mtime), so this only reaps locks whose loop is long
    # gone: deleting one mid-use would split that loop's writers across two inodes.
    # Same exact + `-<id>` anchoring as the rest, never a bare `*.lock`.
    non_state_patterns = [
        "context-snapshot-*.json",
        "context-snapshot.md",
        "context-snapshot.restored.md",
        ".build-state.json.*.tmp",
        ".build-state-*.json.*.tmp",
        ".measure-twice-state.json.*.tmp",
        ".measure-twice-state-*.json.*.tmp",
        "build-state.json.corrupt",
        "build-state-*.json.corrupt",
        "measure-twice-state.json.corrupt",
        "measure-twice-state-*.json.corrupt",
        "build-state.json.lock",
        "build-state-*.json.lock",
        "measure-twice-state.json.lock",
        "measure-twice-state-*.json.lock",
    ]

    now = time.time()

    # Clean up loop state files (both legacy and session-scoped)
    for json_file in directory.glob("*.json"):
        try:
            if is_loop_state_file(json_file.name):
                age = now - json_file.stat().st_mtime
                _, status = read_state_json(json_file)
                if status == LOAD_FUTURE_SCHEMA:
                    # Refuse to touch a newer-schema file, the same contract every
                    # other mutating path honors: deleting is the most destructive
                    # touch there is, and a downgrade must never destroy a live
                    # loop it cannot even read. Note it only when it WOULD have
                    # been swept, so a fresh newer-format loop stays quiet.
                    limit = (MAX_AGE_SECONDS if is_active_state_file(json_file)
                             else STALE_INACTIVE_SECONDS)
                    if age > limit:
                        print(
                            f"[crew] keeping {json_file.name}: written by a newer "
                            f"crew version (state schema ahead of this install); "
                            f"not sweeping it.",
                            file=sys.stderr,
                        )
                elif is_active_state_file(json_file):
                    # Active but very old (>7 days) — force-deactivate and delete
                    if age > MAX_AGE_SECONDS:
                        json_file.unlink()
                else:
                    # Inactive (incl. corrupt/unreadable): delete if older than 1 day
                    if age > STALE_INACTIVE_SECONDS:
                        json_file.unlink()
        except (OSError, AttributeError, KeyError, ValueError):
            pass  # Skip files that can't be processed

    # Clean up non-state files
    for pattern in non_state_patterns:
        for json_file in directory.glob(pattern):
            try:
                age = now - json_file.stat().st_mtime
                if age > MAX_AGE_SECONDS:
                    json_file.unlink()
            except (OSError, AttributeError, KeyError, ValueError):
                pass
