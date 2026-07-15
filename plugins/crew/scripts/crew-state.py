#!/usr/bin/env python3
"""Crew state management CLI for build and measure-twice persistence."""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path

# Import from models.py (same directory)
from models import (
    BuildState,
    MeasureTwiceState,
    StateLockError,
    effective_count,
    effective_deadline,
    elapsed_minutes,
    read_state_json,
    update_state_json,
    utc_now_iso,
    DEFAULT_DEADLINE_MINUTES,
    NO_DEADLINE,
    DEFAULT_MAX_STOP_FIRES,
    FORCE_EXIT_KEY,
    MAX_DEADLINE_MINUTES,
    SCHEMA_VERSION,
    LOAD_CORRUPT,
    LOAD_FUTURE_SCHEMA,
    LOAD_OK,
)
from multiagent import config
from state_discovery import find_adoptable_legacy, find_session_state_file

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
    """Refuse-to-touch contract, mirrored from the Stop hook: a state
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
    (set/deactivate): never silently overwrite an unparseable file as if it were
    clean, emit a loud diagnostic and exit nonzero."""
    if status == LOAD_CORRUPT:
        print(
            f"Error: {path.name} is corrupt (unparseable JSON) — refusing to "
            f"overwrite it. Remove it or re-init the loop with /crew:build or "
            f"/crew:measure-twice.",
            file=sys.stderr,
        )
        sys.exit(2)


def _mutate_state(cls, path: Path, mutate) -> dict:
    """Apply ``mutate`` to the state file under the shared interprocess lock.

    The lock spans the READ and the WRITE, so a Stop-hook counter bump landing in
    that window is not lost: an atomic write alone makes the replace indivisible,
    not the read-modify-write around it. ``mutate`` edits only the keys its verb
    owns, on the dict that is on disk NOW (unknown keys and hook-owned counters go
    back verbatim, which a dataclass round trip would drop or revert).

    Honors the same refuse-to-touch contract as the Stop hook: a newer-schema file
    is NEVER overwritten and a corrupt one is never silently clobbered (both exit
    nonzero, bytes untouched). A missing file starts from the loop's default shape,
    so a first write is a complete state file. A lock that cannot be taken is a
    FAILED write, not a silent unlocked one.
    """
    try:
        data, status = update_state_json(path, mutate, default=asdict(cls()))
    except StateLockError as exc:
        print(
            f"Error: could not lock {path.name} ({exc}). Another writer is holding "
            f"it; nothing was written. Retry.",
            file=sys.stderr,
        )
        sys.exit(2)
    _refuse_future_schema(path, status)
    _refuse_corrupt(path, status)
    return data


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
    # read-path helper: must NOT create `.crew/`. Directory creation lives
    # solely in the write path (atomic_write_json's own mkdir).
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


def _budget_summary(state) -> str:
    """The loop's REAL budget: the two hook-owned bounds that can end it.

    Reported through the SAME defensive coercions the Stop hook enforces with, so
    a hand-edited out-of-policy value can never make this line disagree with the
    bound that actually ends the loop.

    There is no round counter to report: the Stop hook counts its own fires, not
    revision rounds. `elapsed` reads `?` when `started_at` is missing or
    unparseable (a legacy file the hook has not stamped yet).
    """
    minutes = elapsed_minutes(getattr(state, "started_at", ""))
    deadline = effective_deadline(
        getattr(state, "deadline_minutes", DEFAULT_DEADLINE_MINUTES),
        opted_out=getattr(state, "no_deadline", False) is True)
    limit = "none" if deadline == NO_DEADLINE else f"{deadline}min"
    elapsed = f"{minutes:.0f}" if minutes is not None else "?"
    fires = effective_count(getattr(state, "stop_fires", 0), 0)
    max_fires = effective_count(
        getattr(state, "max_stop_fires", DEFAULT_MAX_STOP_FIRES), DEFAULT_MAX_STOP_FIRES)
    return f"fires={fires}/{max_fires} elapsed={elapsed}/{limit}"


def _compact_show(state, canonical: str) -> str:
    """Format state as a compact single-line summary."""
    active_str = "true" if state.active else "false"

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

    line = (
        f'loop={canonical} active={active_str} {_budget_summary(state)} '
        f'task="{task_escaped}"'
    )

    if canonical == "mt":
        plan = getattr(state, "plan_file", "")
        line += f' plan={plan}'

    return line


def cmd_show(args):
    """Show current state of a loop."""
    session_id = resolve_session_id(args)
    path = _resolve_loop_path(args.loop, session_id)
    canonical = LOOP_ALIASES[args.loop]
    cls = LOOP_CLASSES[canonical]
    state = cls.load(path)

    verbose = getattr(args, "verbose", False)
    if verbose:
        # Print what is ON DISK, not asdict(state): the dataclass drops the keys
        # written outside it (completed_at, reason, force_exit), and `init`
        # refuses to restart a force-exited loop based on exactly those. A
        # refusal the supported inspection command cannot explain is a trap.
        data, status = read_state_json(path)
        if status == LOAD_OK and isinstance(data, dict):
            print(json.dumps(data, indent=2))
        else:
            print(json.dumps(asdict(state), indent=2))
    else:
        print(_compact_show(state, canonical))


def cmd_is_active(args):
    """Check if a loop is active. Exit 0 if active, exit 1 if not."""
    session_id = resolve_session_id(args)
    path = _resolve_loop_path(args.loop, session_id)
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


def _resolve_loop_path(loop: str, session_id: str) -> Path:
    """Resolve the state file this session's loop verbs target — the SAME file
    the Stop hook resolves (find_session_state_file, so an adopted legacy file
    is read and mutated on its real path), falling back to the fresh
    session-scoped path when nothing is found. Keeps show/is-active and
    set/deactivate in agreement about which file the loop lives in."""
    canonical = LOOP_ALIASES[loop]
    crew_dir = get_project_dir() / ".crew"
    path = find_session_state_file(crew_dir, LOOP_PREFIXES[canonical], session_id)
    if path is None:
        path = get_state_path(loop, session_id)
    return path


def _apply_field(cls, path: Path, field: str, raw_value: str) -> None:
    """Write ONE key into the state file, under the lock, reading AT WRITE TIME.

    The dict written back is the one on disk NOW, not a snapshot taken before the
    command validated its arguments: a `stop_fires` the Stop hook bumped in that
    window is written back as the HOOK left it, never as this process last saw it.
    """
    # Type comes from the DATACLASS field, never from whatever is on disk, so a
    # hand-edited wrong-typed value can't dictate how the new one is coerced.
    field_type = type(getattr(cls(), field))
    value = coerce_value(raw_value, field_type)

    def mutate(data: dict) -> dict:
        data[field] = value
        data["schema"] = SCHEMA_VERSION
        return data

    _mutate_state(cls, path, mutate)


def cmd_set(args):
    """Set a single field on a loop state: a FIELD-LEVEL update, not a whole-state
    rewrite.

    Refuses any field outside the loop's AGENT_SETTABLE allowlist. The allowlist
    is deliberately narrow: every field the Stop hook's termination predicate
    reads is off limits, so the loop's safety bounds cannot be edited by the
    thing they are meant to bound. `deactivate` remains the audited way to turn a
    loop off; this verb must never be a back door around it.

    Only the ONE requested key is written; the rest of the file goes back
    verbatim. A whole-state save would let an allowlisted write ALSO restore this
    process's stale snapshot of `stop_fires`/`parked_fires`/`started_at`, which
    reopens the counter-reset hole the allowlist exists to close (the Stop hook
    can bump a counter between this command's read and its write).
    """
    session_id = resolve_session_id(args)
    path = _resolve_loop_path(args.loop, session_id)
    cls = LOOP_CLASSES[LOOP_ALIASES[args.loop]]
    defaults = cls()

    if not hasattr(defaults, args.field):
        print(f"Error: {cls.__name__} has no field '{args.field}'", file=sys.stderr)
        sys.exit(1)

    if args.field not in cls.AGENT_SETTABLE:
        allowed = ", ".join(sorted(cls.AGENT_SETTABLE)) or "(none)"
        print(
            f"Error: '{args.field}' is not settable: it is owned by the Stop "
            f"hook, which enforces this loop's termination bounds.\n"
            f"  Settable fields: {allowed}\n"
            f"  To stop the loop:  crew state deactivate {args.loop} "
            f"--reason '<why>' --session-id <id>",
            file=sys.stderr,
        )
        sys.exit(2)

    _apply_field(cls, path, args.field, args.value)


def check_for_conflicts(session_id: str = ""):
    """Check if this session already has an active loop. Returns error message or None.

    Session-scoped: checks BOTH loop types on the SAME path the Stop hook and
    mutating verbs resolve (find_session_state_file), so an adoptable legacy
    unsuffixed file counts too. A different session's active loop is NOT a conflict.
    """
    project_dir = get_project_dir()
    crew_dir = project_dir / ".crew"

    checks = (
        ("bl", BuildState,
         "ERROR: build loop is already active. Run /crew:cancel-build first or let it complete."),
        ("mt", MeasureTwiceState,
         "ERROR: measure-twice loop is already active. Run /crew:cancel-measure-twice first or let it complete."),
    )
    for canonical, cls, active_msg in checks:
        # Check the scoped target AND any adoptable legacy file INDEPENDENTLY:
        # init must not ignore an active legacy loop that other verbs and
        # sessions would still adopt (that would orphan it as a permanently
        # active file), and find_session_state_file cannot be used here because
        # it prefers the scoped file even when it is an inactive leftover,
        # which would hide an active legacy loop behind a finished one.
        candidates = [crew_dir / get_loop_filename(canonical, session_id)]
        if session_id:
            legacy = find_adoptable_legacy(
                crew_dir, LOOP_PREFIXES[canonical], session_id)
            if legacy is not None:
                candidates.append(legacy)
        for path in candidates:
            state, status = cls.load_with_status(path)
            # A newer-schema file is treated as a conflict (refuse-to-touch): we
            # cannot parse it as our schema, so init must NOT clobber it, and we
            # surface it as a blocking condition instead of silently overwriting.
            if status == LOAD_FUTURE_SCHEMA:
                return (
                    f"ERROR: {path.name} was written by a newer crew version (state "
                    f"schema ahead of this install) — refusing to touch. Upgrade crew or "
                    f"remove the file manually."
                )
            if state.active:
                return active_msg

    return None


def _resolve_init_text(args, inline_value, inline_flag_name):
    """Resolve a loop's task text from either the inline flag or ``-f`` file.

    ``-f`` keeps raw ``$ARGUMENTS`` OFF the shell line (the command
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


def _find_force_exited(canonical: str, session_id: str):
    """The state file of a loop the Stop hook FORCE-EXITED, or None.

    Checks the file a fresh `init` would replace AND any adoptable legacy file
    (the same two candidates check_for_conflicts weighs), because after a
    force-exit both are `active: false` and neither blocks as a conflict.
    """
    crew_dir = get_project_dir() / ".crew"
    candidates = [crew_dir / get_loop_filename(canonical, session_id)]
    if session_id:
        legacy = find_adoptable_legacy(crew_dir, LOOP_PREFIXES[canonical], session_id)
        if legacy is not None:
            candidates.append(legacy)
    for path in candidates:
        data, status = read_state_json(path)
        if status == LOAD_OK and data.get(FORCE_EXIT_KEY):
            return path
    return None


def _resolve_deadline(args) -> int:
    """The loop's wall clock: CLI flag > per-repo > global > builtin.

    Same precedence as every other crew config key. The config value is already
    validated (0, or 1..MAX_DEADLINE_MINUTES) by the getter, and the flag by
    argparse, so nothing out of policy reaches the state file. Compared with
    `is not None`, never truthiness: 0 is the explicit no-deadline opt-out and
    must win exactly like any other explicit value.
    """
    if args.deadline_minutes is not None:
        return args.deadline_minutes
    configured = config.deadline_minutes()
    return configured if configured is not None else DEFAULT_DEADLINE_MINUTES


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

    # A force-exited loop is turned off, so nothing above blocks a fresh init that
    # zeroes stop_fires and restarts the wall clock: re-initing IS the way around
    # a safety limit. Make it a deliberate, visible act rather than the default
    # path. (Only the sanctioned CLI is gated here; the state file itself remains
    # writable by any tool, so this raises the cost, it does not seal the door.)
    if not getattr(args, "force", False):
        exited = _find_force_exited(canonical, session_id)
        if exited is not None:
            data, _ = read_state_json(exited)
            reason = (data or {}).get("reason", "a safety limit")
            print(
                f"ERROR: {exited.name} records a force-exit ({reason}). Re-initing "
                f"restarts the loop's fire counter and wall clock, which is exactly "
                f"how a loop escapes the bound that just stopped it.\n"
                f"  Report where the work stands instead. If a fresh loop really is "
                f"warranted, say so and re-run with --force.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Refuse-to-touch: never overwrite a newer-schema file (already
    # blocked as a conflict above), and set a corrupt one aside (mirroring the
    # Stop hook) before re-init so a fresh loop starts on clean state.
    _, target_status = LOOP_CLASSES[canonical].load_with_status(path)
    _refuse_future_schema(path, target_status)
    if target_status == LOAD_CORRUPT:
        corrupt_path = path.with_name(path.name + ".corrupt")
        renamed = True
        try:
            path.replace(corrupt_path)
        except OSError:
            # Rename failed — the fresh state.save() below will overwrite the
            # unreadable file in place. Don't claim we "set it aside".
            renamed = False
        if renamed:
            print(
                f"Warning: {path.name} was corrupt (unparseable JSON) — set aside "
                f"as {corrupt_path.name}; continuing with fresh state.",
                file=sys.stderr,
            )
        else:
            print(
                f"Warning: {path.name} was corrupt (unparseable JSON) and could "
                f"not be set aside — starting fresh over the unreadable file.",
                file=sys.stderr,
            )

    if canonical == "bl":
        prompt = _resolve_init_text(args, args.prompt, "--prompt")
        if not prompt or not prompt.strip():
            print("Error: --prompt or -f required for build loop", file=sys.stderr)
            sys.exit(1)
        deadline = _resolve_deadline(args)
        state = BuildState(
            active=True,
            prompt=prompt,
            completion_promise="DONE",
            session_id=session_id,
            started_at=utc_now_iso(),
            deadline_minutes=deadline,
            no_deadline=deadline == NO_DEADLINE,
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

        deadline = _resolve_deadline(args)
        state = MeasureTwiceState(
            active=True,
            task_description=task,
            plan_file=plan_file,
            last_verdict="",
            session_id=session_id,
            started_at=utc_now_iso(),
            deadline_minutes=deadline,
            no_deadline=deadline == NO_DEADLINE,
        )
        # Output the plan file path so caller can use it
        if args.auto_plan:
            print(plan_file)

    fresh = asdict(state)
    fresh["schema"] = SCHEMA_VERSION

    # A REPLACE, not an edit: a fresh loop zeroes the fire counters, restarts the
    # wall clock, and drops the previous loop's deactivation keys (completed_at /
    # reason / force_exit), which would otherwise leave the new loop looking
    # force-exited. It still goes through the lock so it cannot interleave with a
    # Stop-hook write and leave a half-old, half-new file behind.
    _mutate_state(LOOP_CLASSES[canonical], path, lambda _data: fresh)


def deadline_minutes_arg(value: str) -> int:
    """argparse type for --deadline-minutes, validated BEFORE any state is written.

    The wall clock is a cost ceiling and `init` is invoked BY THE AGENT (the
    loop commands template this call), so the flag is policed on both ends and
    the no-deadline opt-out is NOT accepted here: a bound the bounded thing can
    delete is not a bound. The opt-out lives only in the operator's channel,
    `[tuning].deadline_minutes = 0` in a crew config file. Out-of-range is an
    ERROR (argparse exits 2 with no state file created), never a silent clamp.
    """
    try:
        minutes = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if minutes <= 0 or minutes > MAX_DEADLINE_MINUTES:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_DEADLINE_MINUTES} minutes, got "
            f"{minutes} (no-deadline is config-only: [tuning].deadline_minutes = 0)"
        )
    return minutes


def cmd_deactivate(args):
    """Deactivate a loop with timestamp and optional reason."""
    session_id = resolve_session_id(args)
    canonical = LOOP_ALIASES[args.loop]
    cls = LOOP_CLASSES[canonical]

    # Session-id symmetry: deactivate the SAME file the Stop hook would find (the
    # session-scoped file if present, else an adoptable legacy file), so a loop
    # init'd with a given session-id is always turned off against the file it
    # wrote, never a stale legacy file while the scoped one keeps blocking. `set`
    # shares this resolver so all mutating verbs agree on which file they touch.
    # Fall back to the scoped path when nothing is found.
    path = _resolve_loop_path(args.loop, session_id)

    def mutate(data: dict) -> dict:
        data["schema"] = SCHEMA_VERSION
        data["active"] = False
        # utc_now_iso, NOT a naive local `datetime.now()`: the Stop hook's
        # force-exit stamps this same field in aware UTC, and `parse_iso` reads a
        # naive stamp AS UTC, so two clocks in one field would be off by the local
        # offset.
        data["completed_at"] = utc_now_iso()
        if args.reason:
            if data.get(FORCE_EXIT_KEY):
                # `reason` on a force-exited file is the bound that tripped, and
                # `init` quotes it back when it refuses to restart the loop.
                # Overwriting it would make a later refusal cite "User cancelled"
                # as though THAT were the safety limit. Cancelling after a trip is
                # recorded next to it, not over it.
                data["deactivate_reason"] = args.reason
            else:
                data["reason"] = args.reason
        return data

    # Refuse-to-touch + field-level edit under the lock: same contract as the hook,
    # writing back only the keys this verb owns (see _mutate_state).
    _mutate_state(cls, path, mutate)


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
    p_init.add_argument(
        "--deadline-minutes", dest="deadline_minutes", type=deadline_minutes_arg,
        help=f"Absolute wall-clock bound on the loop, 1-{MAX_DEADLINE_MINUTES} "
             f"minutes (default: [tuning].deadline_minutes, else "
             f"{DEFAULT_DEADLINE_MINUTES}; the no-deadline opt-out is "
             f"config-only)",
    )
    p_init.add_argument(
        "--force", action="store_true", default=False,
        help="Start a fresh loop even when the previous one was force-exited by a "
             "safety limit (otherwise refused)",
    )
    p_init.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_init.set_defaults(func=cmd_init)

    # deactivate
    p_deact = subparsers.add_parser("deactivate", help="Deactivate a loop")
    p_deact.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_deact.add_argument("--reason", help="Reason for deactivation")
    p_deact.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_deact.set_defaults(func=cmd_deactivate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
