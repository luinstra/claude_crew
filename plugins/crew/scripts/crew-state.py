#!/usr/bin/env python3
"""Crew state management CLI for build and measure-twice persistence."""

import argparse
import contextlib
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Import from models.py (same directory)
from models import (
    BuildState,
    MeasureTwiceState,
    atomic_write_json,
    SCHEMA_VERSION,
    LOAD_CORRUPT,
    LOAD_FUTURE_SCHEMA,
)
from state_discovery import find_session_state_file

LOOP_ALIASES = {
    "build": "bl", "bl": "bl",
    "measure-twice": "mt", "mt": "mt",
}

LOOP_CLASSES = {
    "bl": BuildState,
    "mt": MeasureTwiceState,
}

LOOP_PREFIXES = {
    "bl": "build-state",
    "mt": "measure-twice-state",
}


def _refuse_future_schema(path: Path, status: str) -> None:
    """Phase 5 refuse-to-touch contract, mirrored from the Stop hook: a state
    file written by a NEWER crew version is NEVER overwritten by a mutating CLI
    command. Exits nonzero with a loud one-line diagnostic; bytes untouched."""
    if status == LOAD_FUTURE_SCHEMA:
        print(
            f"Error: refusing to modify {path.name} — it was written by a newer "
            f"crew version (state schema ahead of this install). Upgrade crew or "
            f"remove the file manually.",
            file=sys.stderr,
        )
        sys.exit(2)


def _refuse_corrupt(path: Path, status: str) -> None:
    """Mirror the hook's corrupt handling for mutate-in-place commands
    (set/increment/deactivate): never silently overwrite an unparseable file as
    if it were clean — emit a loud diagnostic and exit nonzero."""
    if status == LOAD_CORRUPT:
        print(
            f"Error: {path.name} is corrupt (unparseable JSON) — refusing to "
            f"overwrite it. Remove it or re-init the loop with /crew:build or "
            f"/crew:measure-twice.",
            file=sys.stderr,
        )
        sys.exit(2)


def _load_for_mutation(cls, path: Path):
    """Load state for a mutate-in-place CLI command, honoring Phase 5's
    refuse-to-touch contract (same as the Stop hook): a newer-schema file is
    NEVER overwritten; a corrupt file is not silently clobbered. Returns the
    loaded (OK/MISSING) state."""
    state, status = cls.load_with_status(path)
    _refuse_future_schema(path, status)
    _refuse_corrupt(path, status)
    return state


def get_loop_filename(canonical: str, session_id: str = "") -> str:
    """Get the state filename for a loop, optionally scoped to a session."""
    base = {"bl": "build-state", "mt": "measure-twice-state"}[canonical]
    if session_id:
        return f"{base}-{session_id}.json"
    return f"{base}.json"


def get_project_dir() -> Path:
    """Get project directory from CLAUDE_PROJECT_DIR or cwd."""
    dir_str = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    return Path(dir_str)


def resolve_session_id(args) -> str:
    """Resolve session ID from CLI arg, env var, or empty string (legacy).

    Resolution order:
    1. --session-id CLI argument (highest priority)
    2. CLAUDE_SESSION_ID environment variable
    3. Empty string — legacy unsuffixed filenames (lowest priority)
    """
    cli_id = getattr(args, "session_id", None)
    if cli_id:
        return cli_id
    env_id = os.environ.get("CLAUDE_SESSION_ID", "")
    return env_id


def get_state_path(loop: str, session_id: str = "") -> Path:
    """Get path to state file for the given loop, optionally session-scoped."""
    canonical = LOOP_ALIASES.get(loop)
    if not canonical:
        print(f"Error: Unknown loop '{loop}'", file=sys.stderr)
        sys.exit(1)
    project_dir = get_project_dir()
    crew_dir = project_dir / ".crew"
    # L3: this is a READ-path helper — it must NOT create `.crew/`. Directory
    # creation lives solely in the write path (atomic_write_json's own mkdir).
    return crew_dir / get_loop_filename(canonical, session_id)


def coerce_value(value: str, field_type: type):
    """Coerce string value to appropriate type."""
    if field_type == bool:
        return value.lower() in ("true", "1", "yes")
    if field_type == int:
        return int(value)
    return value


def slugify(text: str, max_length: int = 50) -> str:
    """Convert text to a filename-safe slug."""
    if text is None:
        return "plan"
    # Lowercase, replace spaces with hyphens, keep only alphanumeric and hyphens
    slug = text.lower()
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    slug = re.sub(r'-+', '-', slug)  # Collapse multiple hyphens
    slug = slug.strip('-')
    return slug[:max_length] if slug else "plan"


def _compact_show(state, canonical: str) -> str:
    """Format state as a compact single-line summary."""
    active_str = "true" if state.active else "false"
    iteration = getattr(state, "iteration", 1)
    max_iter = getattr(state, "max_iterations", 10)

    if canonical == "bl":
        raw_task = getattr(state, "prompt", "")
    else:
        raw_task = getattr(state, "task_description", "")

    # Normalize newlines and truncate task to 80 chars
    task = raw_task.replace("\n", " ")
    if len(task) > 80:
        task = task[:77] + "..."

    # Escape double quotes in task for safe inline display
    task_escaped = task.replace('"', '\\"')

    line = f'loop={canonical} active={active_str} iter={iteration}/{max_iter} task="{task_escaped}"'

    if canonical == "mt":
        plan = getattr(state, "plan_file", "")
        line += f' plan={plan}'

    return line


def cmd_show(args):
    """Show current state of a loop."""
    session_id = resolve_session_id(args)
    path = get_state_path(args.loop, session_id)
    canonical = LOOP_ALIASES[args.loop]
    cls = LOOP_CLASSES[canonical]
    state = cls.load(path)

    verbose = getattr(args, "verbose", False)
    if verbose:
        print(json.dumps(asdict(state), indent=2))
    else:
        print(_compact_show(state, canonical))


def cmd_is_active(args):
    """Check if a loop is active. Exit 0 if active, exit 1 if not."""
    session_id = resolve_session_id(args)
    path = get_state_path(args.loop, session_id)
    cls = LOOP_CLASSES[LOOP_ALIASES[args.loop]]
    state = cls.load(path)
    sys.exit(0 if state.active else 1)


def cmd_check_conflicts(args):
    """Check if THIS session already has an active loop. Exit 1 with message if conflict.

    Session-scoped: only checks if the current session has an active loop.
    Different sessions' loops are NOT conflicts.
    """
    session_id = resolve_session_id(args)
    conflict = check_for_conflicts(session_id)
    if conflict:
        print(conflict, file=sys.stderr)
        sys.exit(1)
    # No conflicts - silent success


def cmd_set(args):
    """Set a single field on a loop state."""
    session_id = resolve_session_id(args)
    path = get_state_path(args.loop, session_id)
    cls = LOOP_CLASSES[LOOP_ALIASES[args.loop]]
    state = _load_for_mutation(cls, path)

    if not hasattr(state, args.field):
        print(f"Error: {cls.__name__} has no field '{args.field}'", file=sys.stderr)
        sys.exit(1)

    # Get field type from dataclass
    field_type = type(getattr(state, args.field))
    coerced = coerce_value(args.value, field_type)
    setattr(state, args.field, coerced)
    state.save(path)


def check_for_conflicts(session_id: str = ""):
    """Check if this session already has an active loop. Returns error message or None.

    Session-scoped: checks for *-state-{session_id}.json files across BOTH loop types.
    A different session's active loop is NOT a conflict.
    """
    project_dir = get_project_dir()
    crew_dir = project_dir / ".crew"

    # A newer-schema file is treated as a conflict (refuse-to-touch): we cannot
    # parse it as our schema, so init must NOT clobber it — surface it as a
    # blocking condition instead of silently overwriting (Phase 5 contract).
    bl_path = crew_dir / get_loop_filename("bl", session_id)
    bl_state, bl_status = BuildState.load_with_status(bl_path)
    if bl_status == LOAD_FUTURE_SCHEMA:
        return (
            f"ERROR: {bl_path.name} was written by a newer crew version (state "
            f"schema ahead of this install) — refusing to touch. Upgrade crew or "
            f"remove the file manually."
        )
    if bl_state.active:
        return "ERROR: build loop is already active. Run /crew:cancel-build first or let it complete."

    mt_path = crew_dir / get_loop_filename("mt", session_id)
    mt_state, mt_status = MeasureTwiceState.load_with_status(mt_path)
    if mt_status == LOAD_FUTURE_SCHEMA:
        return (
            f"ERROR: {mt_path.name} was written by a newer crew version (state "
            f"schema ahead of this install) — refusing to touch. Upgrade crew or "
            f"remove the file manually."
        )
    if mt_state.active:
        return "ERROR: measure-twice loop is already active. Run /crew:cancel-measure-twice first or let it complete."

    return None


def _resolve_init_text(args, inline_value, inline_flag_name):
    """Resolve a loop's task text from either the inline flag or ``-f`` file.

    R5 + L6: ``-f`` keeps raw ``$ARGUMENTS`` OFF the shell line (the command
    writes it to a file and passes the path). File read is UTF-8; missing file or
    a clash with the inline flag exits nonzero (2) with NO state file created.
    """
    file_path = getattr(args, "task_file", None)
    if not file_path:
        return inline_value
    if inline_value:
        print(f"Error: pass either {inline_flag_name} or -f, not both", file=sys.stderr)
        sys.exit(2)
    p = Path(file_path)
    if not p.is_file():
        print(f"Error: task file not found: {file_path}", file=sys.stderr)
        sys.exit(2)
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # A non-UTF-8 file raises UnicodeDecodeError (a ValueError, NOT an
        # OSError) — catch it alongside OSError so a bad task file exits cleanly
        # instead of tracebacking (mirrors cli.py dispatch's -f handling).
        print(f"Error: cannot read task file {file_path}: {exc}", file=sys.stderr)
        sys.exit(2)


def cmd_init(args):
    """Initialize a loop with default state."""
    session_id = resolve_session_id(args)

    # Check for conflicts (session-scoped)
    conflict = check_for_conflicts(session_id)
    if conflict:
        print(conflict, file=sys.stderr)
        sys.exit(1)

    canonical = LOOP_ALIASES[args.loop]
    path = get_state_path(args.loop, session_id)

    # Phase 5 refuse-to-touch: never overwrite a newer-schema file (already
    # blocked as a conflict above), and set a corrupt one aside (mirroring the
    # Stop hook) before re-init so a fresh loop starts on clean state.
    _, target_status = LOOP_CLASSES[canonical].load_with_status(path)
    _refuse_future_schema(path, target_status)
    if target_status == LOAD_CORRUPT:
        corrupt_path = path.with_name(path.name + ".corrupt")
        with contextlib.suppress(OSError):
            path.replace(corrupt_path)
        print(
            f"Warning: {path.name} was corrupt (unparseable JSON) — set aside as "
            f"{corrupt_path.name}; continuing with fresh state.",
            file=sys.stderr,
        )

    if canonical == "bl":
        prompt = _resolve_init_text(args, args.prompt, "--prompt")
        if not prompt or not prompt.strip():
            print("Error: --prompt or -f required for build loop", file=sys.stderr)
            sys.exit(1)
        state = BuildState(
            active=True,
            prompt=prompt,
            iteration=1,
            max_iterations=args.max_iterations or 10,
            completion_promise="DONE",
            session_id=session_id,
        )
    else:  # mt
        task = _resolve_init_text(args, args.task, "--task")
        if not task or not task.strip():
            print("Error: --task or -f required for measure-twice", file=sys.stderr)
            sys.exit(1)

        # Auto-derive plan file from task if --auto-plan is set
        if args.auto_plan:
            plan_name = slugify(task)
            plan_file = f".crew/plans/{plan_name}.md"
            # Ensure plans directory exists
            plans_dir = get_project_dir() / ".crew" / "plans"
            plans_dir.mkdir(parents=True, exist_ok=True)
        elif args.plan_file:
            plan_file = args.plan_file
        else:
            print("Error: --plan-file or --auto-plan required for measure-twice", file=sys.stderr)
            sys.exit(1)

        state = MeasureTwiceState(
            active=True,
            task_description=task,
            plan_file=plan_file,
            iteration=1,
            max_iterations=args.max_iterations or 10,
            last_verdict="",
            session_id=session_id,
        )
        # Output the plan file path so caller can use it
        if args.auto_plan:
            print(plan_file)

    state.save(path)


def cmd_deactivate(args):
    """Deactivate a loop with timestamp and optional reason."""
    session_id = resolve_session_id(args)
    canonical = LOOP_ALIASES[args.loop]
    cls = LOOP_CLASSES[canonical]

    # BLOCKING 1 (session-id symmetry): deactivate the SAME file the Stop hook
    # would find — the session-scoped file if present, else an adoptable legacy
    # file — so a loop init'd with a given session-id is always turned off
    # against the file it wrote (never a stale legacy file while the scoped one
    # keeps blocking). Fall back to the scoped path when nothing is found.
    crew_dir = get_project_dir() / ".crew"
    path = find_session_state_file(crew_dir, LOOP_PREFIXES[canonical], session_id)
    if path is None:
        path = get_state_path(args.loop, session_id)

    # Phase 5 refuse-to-touch: honor the same contract as the hook.
    state = _load_for_mutation(cls, path)

    # Build dict with extra metadata
    data = asdict(state)
    data["schema"] = SCHEMA_VERSION
    data["active"] = False
    data["completed_at"] = datetime.now().isoformat()
    if args.reason:
        data["reason"] = args.reason

    # Write directly (bypass .save() to include extra fields), atomically + 0600.
    atomic_write_json(path, data)


def cmd_increment(args):
    """Increment a numeric field (typically iteration)."""
    if args.field != "iteration":
        print("Error: Only 'iteration' can be incremented", file=sys.stderr)
        sys.exit(1)

    session_id = resolve_session_id(args)
    path = get_state_path(args.loop, session_id)
    cls = LOOP_CLASSES[LOOP_ALIASES[args.loop]]
    state = _load_for_mutation(cls, path)

    state.iteration += 1
    state.save(path)


def main():
    parser = argparse.ArgumentParser(
        description="Manage crew loop state files",
        prog="crew-state",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # show
    p_show = subparsers.add_parser("show", help="Display current state")
    p_show.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_show.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_show.add_argument("--verbose", "-v", action="store_true", default=False, help="Output full pretty JSON instead of compact summary")
    p_show.set_defaults(func=cmd_show)

    # is-active
    p_active = subparsers.add_parser("is-active", help="Check if loop is active (exit 0=active, 1=inactive)")
    p_active.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_active.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_active.set_defaults(func=cmd_is_active)

    # check-conflicts
    p_conflicts = subparsers.add_parser("check-conflicts", help="Check if this session has any active loop (exit 1 if conflict)")
    p_conflicts.add_argument("--session-id", dest="session_id", help="Session ID to check conflicts for")
    p_conflicts.set_defaults(func=cmd_check_conflicts)

    # set
    p_set = subparsers.add_parser("set", help="Set a single field")
    p_set.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_set.add_argument("field", help="Field name to set")
    p_set.add_argument("value", help="Value to set")
    p_set.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_set.set_defaults(func=cmd_set)

    # init
    p_init = subparsers.add_parser("init", help="Initialize a loop")
    p_init.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_init.add_argument("--prompt", help="Task prompt (for build loop)")
    p_init.add_argument("--task", help="Task description (for measure-twice)")
    p_init.add_argument(
        "-f", "--prompt-file", "--task-file", dest="task_file",
        help="Read the loop's task text from a UTF-8 file (keeps raw $ARGUMENTS "
             "off the shell); mutually exclusive with --prompt/--task",
    )
    p_init.add_argument("--plan-file", help="Plan file path (for measure-twice)")
    p_init.add_argument("--auto-plan", action="store_true", help="Auto-derive plan file from task (for measure-twice)")
    p_init.add_argument("--max-iterations", type=int, help="Override max iterations")
    p_init.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_init.set_defaults(func=cmd_init)

    # deactivate
    p_deact = subparsers.add_parser("deactivate", help="Deactivate a loop")
    p_deact.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_deact.add_argument("--reason", help="Reason for deactivation")
    p_deact.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_deact.set_defaults(func=cmd_deactivate)

    # increment
    p_inc = subparsers.add_parser("increment", help="Increment a counter")
    p_inc.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_inc.add_argument("field", choices=["iteration"])
    p_inc.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_inc.set_defaults(func=cmd_increment)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
