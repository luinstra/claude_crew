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
    LoopState,
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
from multiagent import config, review_runs, targets
from state_discovery import (
    anchor_path,
    crew_base,
    find_adoptable_legacy,
    find_session_state_file,
    is_active_value,
)

LOOP_ALIASES = {
    "build": "bl", "bl": "bl",
    "measure-twice": "mt", "mt": "mt",
}

# One dataclass serves both loops (the aliases and file prefixes below are
# what still distinguish them on disk).

LOOP_PREFIXES = {
    "bl": "build-state",
    "mt": "measure-twice-state",
}

VERDICTS = ("APPROVED", "REVISE", "REJECT", "FAILED")

# Two all-failed panels in a row is systemic, not a transient outage: it ends the
# loop instead of leaving it to re-prep a panel that is not returning.
MAX_CONSECUTIVE_REVIEW_FAILURES = 2


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


class _Refusal(Exception):
    """A check inside the state lock said no.

    Raised from a mutate function, so it escapes ``update_state_json`` BEFORE the
    write: the refusal and the untouched bytes are one guarantee rather than two
    things that have to stay in agreement.
    """


def _refuse(message: str):
    raise _Refusal(message)


class _Advisory(Exception):
    """One or more completion checks tripped that a trusted human may override.

    Distinct from ``_Refusal`` (exit 2, "called wrong or out of order"): an
    advisory names a world that drifted from what the panel saw (a partial panel,
    a moved pointer, an edited target), which is a judgment call, not a
    contradiction. Raised from a mutate BEFORE the write, so exit-3 leaves the
    bytes untouched exactly as exit-2 does. Raised ONLY on the non-force path (a
    ``--force`` record never raises: it stamps ``last_verdict_overrides`` and
    writes), so it carries just the human-facing diagnostic.
    """

    def __init__(self, diagnostic: str):
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


def _mutate_or_refuse(path: Path, mutate) -> dict:
    """``_mutate_state`` where the mutate function may refuse: a refusal exits 2
    with a diagnostic naming the fix, and nothing is written."""
    try:
        return _mutate_state(LoopState, path, mutate)
    except _Refusal as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)


def _reviews_dir(session_id: str) -> Path:
    """The session's review tree, resolved through the engine's own sanitizer so
    the gate reads exactly the dir review-prep wrote.

    Roots at `get_project_dir()` (the shared `crew_base()` resolver), the SAME
    single source the review ENGINE (review-prep/collect/run) now derives from,
    so the state layer and the engine always land on one `.crew` tree even when
    cwd differs from CLAUDE_PROJECT_DIR.
    """
    base = get_project_dir() / ".crew" / "reviews"
    return review_runs.reviews_dir(session_id, base=str(base))


def _distinct_seats(value) -> list:
    """Non-empty seat names off a roster, order preserved, duplicates dropped.

    Read defensively: rosters come off disk, and a schema stamp does not prove
    the key shape that schema implies (an adopted legacy file is stamped without
    being rewritten).
    """
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(s for s in value if isinstance(s, str) and s))


def get_loop_filename(canonical: str, session_id: str = "") -> str:
    """Get the state filename for a loop, optionally scoped to a session."""
    base = {"bl": "build-state", "mt": "measure-twice-state"}[canonical]
    if session_id:
        return f"{base}-{session_id}.json"
    return f"{base}.json"


def get_project_dir() -> Path:
    """Get project directory: the shared ``crew_base()`` resolver
    (CLAUDE_PROJECT_DIR or cwd, except a terminal `.crew` fallback cwd re-anchors
    to its parent with a one-time stderr advisory)."""
    return crew_base()


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

    raw_task = state.task

    # Normalize newlines and truncate task to 80 chars
    task = raw_task.replace("\n", " ")
    if len(task) > 80:
        task = task[:77] + "..."

    # Escape double quotes in task for safe inline display
    task_escaped = task.replace('"', '\\"')

    # phase/round are the two facts that say WHERE the loop is (which verb is
    # legal next, and how many times the panel sent it back). Read defensively:
    # a compact line is an inspection surface, so garbage on disk must render,
    # not raise.
    phase = getattr(state, "phase", "") or "drafting"
    review_round = effective_count(getattr(state, "revision_round", 0), 0)

    line = (
        f'loop={canonical} active={active_str} phase={phase} '
        f'round={review_round} {_budget_summary(state)} '
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
    state = LoopState.load(path)

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
    state = LoopState.load(path)
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
    cls = LoopState
    defaults = cls()

    if not hasattr(defaults, args.field):
        print(f"Error: {cls.__name__} has no field '{args.field}'", file=sys.stderr)
        sys.exit(1)

    if args.field not in cls.AGENT_SETTABLE:
        allowed = ", ".join(sorted(cls.AGENT_SETTABLE)) or "(none)"
        print(
            f"Error: '{args.field}' is not settable: the termination bounds are "
            f"owned by the Stop hook, and the verdict/run-identity fields are "
            f"owned by the review verbs that validate what they record.\n"
            f"  Settable fields: {allowed}\n"
            f"  To stop the loop early:  crew state deactivate {args.loop} "
            f"--cancel --reason '<why>' --session-id <id>",
            file=sys.stderr,
        )
        sys.exit(2)

    value = args.value
    if args.field == "plan_file":
        # A path field persists into loop state and is consumed AS-IS by later
        # readers (review-prep, the banner), each in its own cwd. Anchor a
        # relative value to the project root at write time so the stored path
        # resolves the same everywhere (mirrors init's --auto-plan absolute).
        value = anchor_path(value)

    _apply_field(cls, path, args.field, value)


def cmd_await(args):
    """Set (or `--clear`) the human-wait pause on a loop (`awaiting_input`).

    The orchestrator runs this before yielding to the human while the loop is
    active with nothing in flight (an ok=false executor stop, an exit-3 --force
    advisory, an AskUserQuestion). The Stop hook then treats the wait like a park:
    it ALLOWS the Stop (no `stop_fire`) under the same `max_parked_fires` cap, so
    the session actually stops and the human's next message re-invokes it, instead
    of the agent being dragged back on every turn-end (the nudge livelock).

    It auto-clears when the loop advances (`begin-review` / `record-verdict`) or
    when the cap trips; `--clear` clears it explicitly. FIELD-LEVEL, own key only,
    so it never carries back a stale counter snapshot (the counter-reset hole
    `set` closes); that is also why it is a dedicated verb, not an AGENT_SETTABLE
    field: it bounds nothing (the cap defeats a stuck one), so it does not belong
    on the allowlist the safety bounds depend on.

    On an inactive / missing / corrupt / newer-schema loop it is a NO-OP success
    (nothing written): the flag is inert unless the loop is active, and a no-op
    keeps the verb safe to call defensively at teardown and honors refuse-to-touch
    for a newer-schema file. The note names the actual load status so a live but
    unparseable (corrupt / newer-schema) file is never mislabeled "not active".
    """
    session_id = resolve_session_id(args)
    path = _resolve_loop_path(args.loop, session_id)
    state, status = LoopState.load_with_status(path)
    if not state.active:
        if status == LOAD_CORRUPT:
            why = "its state file is corrupt (unparseable)"
        elif status == LOAD_FUTURE_SCHEMA:
            why = "its state file was written by a newer crew (refusing to touch)"
        else:
            why = "it is not active"
        print(
            f"Note: {args.loop} loop: {why}; awaiting-input is a no-op "
            f"(nothing written).",
            file=sys.stderr,
        )
        return
    target = not getattr(args, "clear", False)

    def mutate(data: dict) -> dict:
        data["awaiting_input"] = target
        data["schema"] = SCHEMA_VERSION
        return data

    _mutate_state(LoopState, path, mutate)
    print(f"awaiting_input={'true' if target else 'false'} {args.loop}")


def check_for_conflicts(session_id: str = ""):
    """Check if this session already has an active loop. Returns error message or None.

    Session-scoped: checks BOTH loop types on the SAME path the Stop hook and
    mutating verbs resolve (find_session_state_file), so an adoptable legacy
    unsuffixed file counts too. A different session's active loop is NOT a conflict.
    """
    project_dir = get_project_dir()
    crew_dir = project_dir / ".crew"

    checks = (
        ("bl",
         "ERROR: build loop is already active. Run /crew:cancel-build first or let it complete."),
        ("mt",
         "ERROR: measure-twice loop is already active. Run /crew:cancel-measure-twice first or let it complete."),
    )
    for canonical, active_msg in checks:
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
            state, status = LoopState.load_with_status(path)
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


def _same_file(a: Path, b: Path) -> bool:
    """True when the two paths name the same file.

    os.path.samefile when both exist (covers symlink aliases and hard links);
    resolved-path equality when either is missing, so a comparand not on disk
    yet still guards. Never raises: an unresolvable path is just "no match".
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        try:
            return a.resolve() == b.resolve()
        except OSError:
            return False


def cmd_init(args):
    """Initialize a loop with default state."""
    # Validated BEFORE any state work: --consume names a file to delete, so
    # without -f there is nothing it could mean and silence would hide the typo.
    if getattr(args, "consume", False) and not getattr(args, "task_file", None):
        print(
            "Error: --consume requires -f (it deletes the spill file a "
            "successful init just read; there is no file to consume without -f)",
            file=sys.stderr,
        )
        sys.exit(2)

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
    _, target_status = LoopState.load_with_status(path)
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
        state = LoopState(
            active=True,
            loop=canonical,
            task=prompt,
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
            # Ensure plans directory exists, anchored to the shared resolver.
            plans_dir = get_project_dir() / ".crew" / "plans"
            plans_dir.mkdir(parents=True, exist_ok=True)
            # Store the ANCHORED absolute path, not a cwd-relative string: the
            # value persists into loop state, and later plan writes / `review-prep`
            # / the banner consume it AS-IS. A relative `.crew/plans/...` would
            # re-resolve against whatever cwd the consumer runs in, splitting from
            # the dir just created here; the absolute path resolves the same
            # everywhere it is used.
            plan_file = str(plans_dir / f"{plan_name}.md")
        elif args.plan_file:
            plan_file = args.plan_file
        else:
            print("Error: --plan-file or --auto-plan required for measure-twice", file=sys.stderr)
            sys.exit(1)

        deadline = _resolve_deadline(args)
        state = LoopState(
            active=True,
            loop=canonical,
            task=task,
            plan_file=plan_file,
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
    _mutate_state(LoopState, path, lambda _data: fresh)

    # Only after the state write landed: a failed init exits above and leaves
    # the spill for retry. A failed unlink is a warning, never a failed init
    # (the loop is live either way).
    if getattr(args, "consume", False):
        spill = Path(args.task_file)
        # -f is caller-named: if it resolves to the state file just written,
        # or to the mt plan file, the unlink would destroy live loop data.
        # Skip the delete with a warning, never fail the init.
        collision = ""
        if _same_file(spill, path):
            collision = f"the loop state file ({path})"
        elif canonical == "mt" and _same_file(spill, Path(plan_file)):
            collision = f"the plan file ({plan_file})"
        if collision:
            print(
                f"Warning: --consume skipped: -f {args.task_file} resolves to "
                f"{collision}; not deleting it.",
                file=sys.stderr,
            )
        else:
            try:
                spill.unlink()
            except OSError as exc:
                print(
                    f"Warning: init succeeded but could not delete the consumed "
                    f"task file {args.task_file}: {exc}",
                    file=sys.stderr,
                )


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
    """Turn a loop off through one of its two sanctioned exits.

    A loop ends either on a recorded completing verdict (`record-verdict` leaves
    `phase == done`) or on an explicit `--cancel`. Anything else is refused: a
    plain deactivate mid-loop is how an unreviewed finish used to pass for an
    approved one, since the state file kept no record of which it had been.
    `--cancel` is legal from ANY phase (an escape hatch that a phase check could
    veto is not an escape hatch); it is audited by the `cancelled` exit_kind it
    leaves behind.
    """
    session_id = resolve_session_id(args)

    # Session-id symmetry: deactivate the SAME file the Stop hook would find (the
    # session-scoped file if present, else an adoptable legacy file), so a loop
    # init'd with a given session-id is always turned off against the file it
    # wrote, never a stale legacy file while the scoped one keeps blocking. `set`
    # shares this resolver so all mutating verbs agree on which file they touch.
    # Fall back to the scoped path when nothing is found.
    path = _resolve_loop_path(args.loop, session_id)
    cancelling = bool(getattr(args, "cancel", False))

    def mutate(data: dict) -> dict:
        # is_active_value, never `is True`: ONE rule decides "is this loop on"
        # across the Stop hook, discovery, and this gate. A garbage truthy
        # `active` ("true", 1) blocks Stop, so it must be turned OFF here rather
        # than fall through as an already-off no-op, which is what left the
        # escape hatch dead on precisely the files that needed it.
        active = is_active_value(data.get("active", False))
        phase = data.get("phase") or "drafting"
        if active and not cancelling and phase != "done":
            _refuse(
                f"loop '{args.loop}' is in phase '{phase}', so no panel has "
                f"signed off on this work. A loop ends on a completing verdict "
                f"(`crew state record-verdict {args.loop} APPROVED --session-id "
                f"{session_id or '<id>'}`, which leaves phase=done) or on an "
                f"explicit `crew state deactivate {args.loop} --cancel`. Nothing "
                f"was written."
            )
        if active:
            data["schema"] = SCHEMA_VERSION
            data["active"] = False
            # utc_now_iso, NOT a naive local `datetime.now()`: the Stop hook's
            # force-exit stamps this same field in aware UTC, and `parse_iso` reads
            # a naive stamp AS UTC, so two clocks in one field would be off by the
            # local offset.
            data["completed_at"] = utc_now_iso()
            # exit_kind is the terminal CATEGORY, not the verdict: "approved"
            # covers every completing verdict, a REVISE --minor-only finish
            # included. `last_verdict` is what says which one it actually was.
            data["exit_kind"] = "cancelled" if cancelling else "approved"
        # An already-off loop falls through every stamp above: whatever ended it
        # (a tripped bound, a terminal verdict, an earlier deactivate) recorded how
        # and when, so turning it off again changes nothing rather than restamping
        # someone else's exit. Idempotent, not an error.
        if args.reason:
            if not active or data.get(FORCE_EXIT_KEY):
                # `reason` on a loop that is already off is the exit that ended it
                # (the bound that tripped, the panel that returned nothing), and
                # `init` quotes a tripped bound back when it refuses to restart the
                # loop. Overwriting it would make a later refusal cite "User
                # cancelled" as though THAT were the safety limit. A cancel after a
                # trip is recorded next to it, not over it.
                data["deactivate_reason"] = args.reason
            else:
                data["reason"] = args.reason
        return data

    # Refuse-to-touch + field-level edit under the lock: same contract as the hook,
    # writing back only the keys this verb owns (see _mutate_state). The gate lives
    # INSIDE the mutate so a refusal and the untouched bytes are one guarantee.
    _mutate_or_refuse(path, mutate)


def cmd_begin_review(args):
    """Freeze the identity of the panel this loop is about to launch.

    Legal from `drafting` (the first panel) and from `reviewing` too: an
    orchestrator that re-prepped after a compaction re-arms the freeze rather
    than hitting a transition error it would only route around. Strictness
    belongs at `record-verdict`, which is where a stale identity could actually
    certify the wrong bytes.

    Everything frozen comes from the run dir's own `run.json`, never from the
    pointer: seat results are stamped from that record, so the gate has to judge
    against the same authority. The pointer only names WHICH run.
    """
    session_id = resolve_session_id(args)
    path = _resolve_loop_path(args.loop, session_id)
    reviews = _reviews_dir(session_id)

    run_id = review_runs.read_pointer_run_id(reviews)
    if not run_id:
        print(
            f"Error: no usable run pointer at "
            f"{reviews / review_runs.POINTER_NAME}: there is no prepped panel to "
            f"freeze. Run `crew review-prep <target> --session-id "
            f"{session_id or '<id>'}` first.",
            file=sys.stderr,
        )
        sys.exit(2)

    run_d = reviews / run_id
    try:
        record = review_runs.read_run_json(run_d)
        review_runs.verify_run_record(
            record, expected_run_id=run_id,
            source=run_d / review_runs.RUN_JSON_NAME,
        )
    except review_runs.ReviewRunError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    target_spec = record.get("target_spec") or ""
    if not target_spec:
        # Without a replayable spec the completion gate could never re-hash the
        # target, so it would have to certify on trust. Refuse at the freeze.
        print(
            f"Error: run {run_id} carries no replayable target spec; re-prep it "
            f"(remove {run_d} and run `crew review-prep` again).",
            file=sys.stderr,
        )
        sys.exit(2)

    # The frozen roster comes from `seat_signatures`, never the
    # `subprocess_seats`/`task_seats` lists: the signature map is INSIDE the
    # hashed identity `verify_run_record` just checked, the lists are outside it
    # and untyped, so only the signatures can say who the panel was without the
    # record's own digest being able to disagree. An edited list would otherwise
    # shrink the roster (and the quorum threshold with it) on a record that still
    # verifies. `collect`'s quorum header counts from this same field.
    sigs = record.get("seat_signatures")
    expected = _distinct_seats(list(sigs)) if isinstance(sigs, dict) else []
    if not expected:
        # An empty roster freezes a panel no seat can ever satisfy: the quorum
        # check would later report the uninterpretable "0 of 0 launched seats, 1
        # needed". Refuse where the spec check does, while the fix is still one
        # re-prep away.
        print(
            f"Error: run {run_id} names no seats, so a panel over it could never "
            f"reach quorum; re-prep it (its manifest is what carries the roster): "
            f"remove {run_d} and run `crew review-prep` again.",
            file=sys.stderr,
        )
        sys.exit(2)

    def mutate(data: dict) -> dict:
        # is_active_value, never `is True`: the Stop hook reads a truthy `active`
        # (1, "true") as ON and goes on blocking, so a verb that read it as OFF
        # would strand the loop with no way to progress through review.
        if not is_active_value(data.get("active", False)):
            _refuse(
                f"loop '{args.loop}' is not active, so it has nothing to review. "
                f"Start it with `crew state init {args.loop} …`."
            )
        phase = data.get("phase") or "drafting"
        if phase not in ("drafting", "reviewing"):
            _refuse(
                f"cannot begin a review from phase '{phase}': the loop already "
                f"has a completing verdict on record. Deactivate it, or start a "
                f"fresh loop."
            )
        data["schema"] = SCHEMA_VERSION
        data["phase"] = "reviewing"
        # Tie an adopted legacy flat-state file to the owning session, but only
        # when we HAVE one: on the legacy path (no --session-id, no env) the
        # resolved id is "", and stamping that would clobber a stored owner an
        # adopted file already carries. Guarded, so a real id stamps and an empty
        # one preserves what is on disk.
        if session_id:
            data["session_id"] = session_id
        data["run_id"] = run_id
        data["target_sha256"] = record.get("target_sha256") or ""
        data["target_spec"] = target_spec
        data["target_base"] = record.get("target_base") or ""
        data["expected_seats"] = expected
        # The loop is advancing (a panel is about to run), so it is no longer
        # waiting on the human: clear the pause so a resumed loop re-arms its nudge.
        data["awaiting_input"] = False
        return data

    _mutate_or_refuse(path, mutate)
    print(f"phase=reviewing run={run_id} seats={len(expected)}")


def _usable_seats(run_d: Path, seats: list, run_id: str, target_sha256: str) -> list:
    """The manifest seats whose result in THIS run dir counts.

    The predicate is the engine's success-only tier (`seat_landed_valid` ->
    `result_valid`), the same one the digest counts with, so the header the
    orchestrator reads and the gate that decides can never disagree about what a
    usable seat is. A run dir that is not there yields zero: absence is a
    refusal, never a pass.
    """
    return [
        s for s in seats
        if review_runs.seat_landed_valid(run_d, s, run_id, target_sha256)
    ]


def cmd_record_verdict(args):
    """Record the panel's verdict, gated on the frozen run identity.

    Legal only from `reviewing`. The scan resolves EXCLUSIVELY from state
    (`.crew/reviews/<sid>/<state.run_id>`): a pointer or flat-dir fallback would
    let a re-prep, or a stray dir, decide which results a verdict is made of,
    which is the whole hole this closes.

    A verdict that COMPLETES the loop (APPROVED, or REVISE with nothing blocking)
    faces three further checks (quorum, pointer, target drift) that REVISE and
    REJECT skip, because a degraded panel may always demand more work, it just can
    never sign off. Every verdict but FAILED needs at least one usable seat: a
    panel that returned nothing found nothing, whatever it is recorded as.

    Those completion checks plus the zero-usable one are ADVISORY, not hard: for a
    single trusted user, walling off the whole loop on a mid-review comment edit
    (target drift) is over-strong. So without ``--force`` they COLLECT every
    tripped condition, print one combined diagnostic, record nothing, and exit 3
    (distinct from the exit-2 hard errors so a caller can tell "ask the human"
    from "you called it wrong"); with ``--force`` they are a warning and the
    verdict records, stamped in ``last_verdict_overrides`` for the audit trail.
    The HARD checks (loop inactive, wrong phase, no/invalid run identity, FAILED
    over a usable panel, no frozen spec) stay exit 2 and still run first: they are
    contradictions, not judgment calls.
    """
    session_id = resolve_session_id(args)
    path = _resolve_loop_path(args.loop, session_id)
    verdict = args.verdict
    minor_only = bool(getattr(args, "minor_only", False))
    if minor_only and verdict != "REVISE":
        print(
            f"Error: --minor-only qualifies a REVISE (the panel found nothing "
            f"blocking); it means nothing on {verdict}.",
            file=sys.stderr,
        )
        sys.exit(2)
    completing = verdict == "APPROVED" or (verdict == "REVISE" and minor_only)
    force = bool(getattr(args, "force", False))
    reviews = _reviews_dir(session_id)
    # Carries the (name, fix-text) pairs a --force record overrode OUT of the
    # locked mutate, so the warning prints only after the write actually lands.
    overrode: list = []

    def mutate(data: dict) -> dict:
        # is_active_value, never `is True` (see begin-review).
        if not is_active_value(data.get("active", False)):
            _refuse(
                f"loop '{args.loop}' is not active: a verdict describes a panel a "
                f"LIVE loop launched. Nothing was recorded."
            )
        phase = data.get("phase") or "drafting"
        if phase != "reviewing":
            _refuse(
                f"cannot record a verdict from phase '{phase}': this loop has no "
                f"panel in flight. Run `crew review-prep`, then `crew state "
                f"begin-review {args.loop} --session-id {session_id or '<id>'}`."
            )
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            _refuse(
                f"this loop froze no run identity, so there is nothing to judge "
                f"the results against. Run `crew state begin-review {args.loop} "
                f"--session-id {session_id or '<id>'}` after review-prep."
            )
        try:
            review_runs.validate_run_id(run_id)
        except review_runs.ReviewRunError as exc:
            _refuse(f"the frozen run identity is unusable ({exc}); re-run begin-review.")

        target_sha256 = data.get("target_sha256") or ""
        expected = _distinct_seats(data.get("expected_seats"))
        run_d = reviews / run_id
        usable = _usable_seats(run_d, expected, run_id, target_sha256)

        if verdict == "FAILED":
            if usable:
                _refuse(
                    f"FAILED says the panel returned nothing, but run {run_id} "
                    f"holds {len(usable)} usable result(s) "
                    f"({', '.join(usable)}): record REVISE/APPROVED/REJECT "
                    f"instead. Nothing was recorded."
                )
            failures = effective_count(
                data.get("consecutive_review_failures"), 0) + 1
            data["schema"] = SCHEMA_VERSION
            data["last_verdict"] = verdict
            # The override stamp pairs with last_verdict; FAILED overrides nothing.
            data.pop("last_verdict_overrides", None)
            data["consecutive_review_failures"] = failures
            data["phase"] = "drafting"
            # A recorded verdict means the loop advanced, so it is no longer
            # waiting on the human: clear the pause (re-arms the nudge).
            data["awaiting_input"] = False
            if failures >= MAX_CONSECUTIVE_REVIEW_FAILURES:
                # In THIS transaction, never a follow-up deactivate call: an
                # unlocked gap between recording the failure and turning the loop
                # off is a window where a reader sees a half-transitioned loop.
                data["active"] = False
                data["exit_kind"] = "review_failed"
                data["completed_at"] = utc_now_iso()
                data["reason"] = (
                    f"{failures} consecutive review runs produced no usable seat "
                    f"results (most recent: {run_id}); ending the loop rather "
                    f"than re-prepping a panel that is not returning."
                )
            return data

        # A completing verdict with no replayable spec is a malformed freeze, not
        # a drifted world: there is nothing to re-hash against, so this stays a
        # HARD refusal (exit 2) checked before the advisory gathering below.
        spec = data.get("target_spec") or ""
        if completing and not spec:
            _refuse(
                "this loop froze no replayable target spec, so the reviewed "
                "content cannot be proven unchanged. Re-run review-prep + "
                "begin-review + the panel."
            )

        # ADVISORY gathering: every remaining check describes a world that
        # drifted from what the panel saw, which is a human's call. Collect ALL
        # that trip (never stop at the first) so the combined diagnostic names
        # every one; without --force this raises exit 3, with --force it records
        # and stamps the list. Each entry is (short-name, existing fix text).
        advisory: list = []
        if not usable:
            # The symmetric half of FAILED's refusal: FAILED requires zero usable
            # seats, so every other verdict requires at least one. Recorded as
            # progress an all-failed panel would reset consecutive_review_failures
            # and the returning-nothing bound would never trip: a --force here is
            # a human choosing to record over a panel they read themselves.
            advisory.append((
                "zero-usable",
                f"a {verdict} verdict describes what the panel found, but 0 of "
                f"{len(expected)} launched seats returned a usable result in run "
                f"{run_id}, so the panel found nothing. Record FAILED instead "
                f"(the verdict for a panel that produced nothing)."
            ))
        if completing:
            needed = len(expected) // 2 + 1
            if len(usable) < needed:
                advisory.append((
                    "quorum-not-met",
                    f"quorum not met: {len(usable)} of {len(expected)} launched "
                    f"seats returned a usable result in run {run_id}, {needed} "
                    f"needed. Fix: re-run the missing seats against this run, or "
                    f"record REVISE/REJECT (a partial panel may demand more work, "
                    f"never sign off)."
                ))
            pointer_run_id = review_runs.read_pointer_run_id(reviews)
            if pointer_run_id != run_id:
                advisory.append((
                    "pointer-divergence",
                    f"pointer divergence: the run pointer names "
                    f"{pointer_run_id or 'nothing'} but this loop froze {run_id}, "
                    f"so the panel was re-prepped (or the pointer swept) after the "
                    f"freeze. Fix: `crew state begin-review {args.loop} "
                    f"--session-id {session_id or '<id>'}` so the frozen identity "
                    f"matches the latest prep, then run the panel over it."
                ))
            # Re-resolved with the base VERBATIM as frozen (empty for every kind
            # but branch, where the flag is what the diff was derived from):
            # substituting a default here would re-hash something the panel never
            # read.
            # This resolve shells out to git while the state lock is HELD, so a
            # concurrent Stop hook can time out on it. Accepted: that path fails
            # open loudly without writing, and it costs seconds at most. Resolving
            # outside the lock would trade it for a re-hash of bytes the gate never
            # judged, which is the drift this check exists to catch.
            # This drift re-hash resolves the target through `targets.resolve`,
            # which anchors to `crew_base()` (the project root), the SAME source
            # `_reviews_dir` roots at (see there), so the re-hash and the panel
            # read one `.crew` tree even when the process cwd differs. A target
            # that genuinely no longer resolves yields a drift advisory (clearable
            # with `--force`), never a false clean PASS.
            try:
                fresh = review_runs.sha256_text(
                    targets.resolve(spec, base=data.get("target_base") or "").content
                )
            except targets.TargetError as exc:
                advisory.append((
                    "target-no-longer-resolves",
                    f"the reviewed target {spec!r} no longer resolves ({exc}), so "
                    f"it cannot be shown to be what the panel read. Fix: re-run "
                    f"`crew review-prep` + begin-review + the panel."
                ))
            else:
                if fresh != target_sha256:
                    advisory.append((
                        "target-drift",
                        f"target drift: {spec!r} now hashes to {fresh}, but the "
                        f"panel reviewed {target_sha256 or '(nothing)'}. The thing "
                        f"on disk is not the thing that was reviewed. Fix: re-run "
                        f"`crew review-prep` + begin-review + the panel over the "
                        f"current content."
                    ))

        if advisory and not force:
            names = ", ".join(n for n, _ in advisory)
            body = "\n".join(f"  - {text}" for _, text in advisory)
            raise _Advisory(
                f"Advisory: recording {verdict} names a world changed from what "
                f"the panel reviewed ({len(advisory)} condition(s): {names}). "
                f"These are advisories, not errors: surface them to the user, and "
                f"record with --force ONLY on the user's explicit say-so "
                f"(otherwise fix and re-panel). Nothing was recorded.\n{body}"
            )

        data["schema"] = SCHEMA_VERSION
        data["last_verdict"] = verdict
        # A recorded verdict means the loop advanced (a completing verdict or a new
        # revision round), so it is no longer waiting on the human: clear the pause.
        data["awaiting_input"] = False
        # Any verdict that is not FAILED is evidence the panel CAN return.
        data["consecutive_review_failures"] = 0
        if completing:
            data["phase"] = "done"
        else:
            # The one place a revision round advances.
            data["revision_round"] = effective_count(
                data.get("revision_round"), 0) + 1
            data["phase"] = "drafting"
        if advisory:
            # --force reached here: stamp the audit trail with the same names the
            # warning prints, and carry them out for the post-write warning.
            overrode.extend(advisory)
            data["last_verdict_overrides"] = [n for n, _ in advisory]
        else:
            # Keep the stamp paired with last_verdict: a clean record clears any
            # override left by a prior round.
            data.pop("last_verdict_overrides", None)
        return data

    try:
        data = _mutate_state(LoopState, path, mutate)
    except _Advisory as exc:
        # The diagnostic already opens `Advisory:`, so the exit-3 tier the exit
        # code distinguishes is visible in the text too, never mislabeled
        # `Error:` (which would read as "you called it wrong, force it").
        print(exc.diagnostic, file=sys.stderr)
        sys.exit(3)
    except _Refusal as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    if overrode:
        names = ", ".join(n for n, _ in overrode)
        print(
            f"Warning: recorded {verdict} over {len(overrode)} advisory "
            f"condition(s) via --force ({names}); the override is stamped in "
            f"last_verdict_overrides.",
            file=sys.stderr,
        )
    print(
        f"verdict={verdict} phase={data.get('phase')} "
        f"round={data.get('revision_round', 0)} "
        f"active={'true' if data.get('active') else 'false'}"
    )


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

    # await: set/clear the human-wait pause the Stop hook honors as a bounded wait
    p_await = subparsers.add_parser(
        "await", help="Pause the loop while waiting on the human (Stop-hook wait)")
    p_await.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_await.add_argument(
        "--clear", action="store_true", default=False,
        help="Clear the pause instead of setting it")
    p_await.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_await.set_defaults(func=cmd_await)

    # init
    p_init = subparsers.add_parser("init", help="Initialize a loop")
    p_init.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_init.add_argument("--prompt", help="Task prompt (for build loop)")
    p_init.add_argument("--task", help="Task description (for measure-twice)")
    p_init.add_argument(
        "-f", "--prompt-file", "--task-file", dest="task_file", type=anchor_path,
        help="Read the loop's task text from a UTF-8 file (keeps raw $ARGUMENTS "
             "off the shell); mutually exclusive with --prompt/--task. A "
             "relative path resolves against the project root (crew_base), "
             "never the shell cwd, so recipes pass a plain .crew/... path.",
    )
    p_init.add_argument(
        "--consume", action="store_true", default=False,
        help="After a SUCCESSFUL init from -f, delete the spill file just read "
             "(folds the recipes' separate rm step into the engine). A failed "
             "init leaves the file for retry; requires -f (exit 2 without it).",
    )
    p_init.add_argument(
        "--plan-file", type=anchor_path,
        help="Plan file path (for measure-twice); a relative path resolves "
             "against the project root before it is stored",
    )
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
    p_deact.add_argument(
        "--cancel", action="store_true",
        help="End the loop WITHOUT a completing verdict (the human escape hatch; "
             "legal from any phase, recorded as exit_kind=cancelled)",
    )
    p_deact.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_deact.set_defaults(func=cmd_deactivate)

    # begin-review
    p_begin = subparsers.add_parser(
        "begin-review",
        help="Freeze the prepped panel's run identity into the loop (phase=reviewing)",
    )
    p_begin.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_begin.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_begin.set_defaults(func=cmd_begin_review)

    # record-verdict
    p_verdict = subparsers.add_parser(
        "record-verdict",
        help="Record the panel's verdict against the frozen run identity",
    )
    p_verdict.add_argument("loop", choices=list(LOOP_ALIASES.keys()))
    p_verdict.add_argument("verdict", choices=list(VERDICTS))
    p_verdict.add_argument(
        "--minor-only", dest="minor_only", action="store_true", default=False,
        help="REVISE with nothing blocking: completes the loop like APPROVED, so "
             "it faces the same quorum/pointer/drift checks",
    )
    p_verdict.add_argument(
        "--force", action="store_true", default=False,
        help="Record despite tripped ADVISORY conditions (quorum, pointer, "
             "target drift, unresolvable target, zero usable seats): they warn "
             "to stderr and stamp last_verdict_overrides instead of refusing "
             "(exit 3). The hard errors (inactive/wrong phase/no run identity/"
             "no frozen target spec) are never forced.",
    )
    p_verdict.add_argument("--session-id", dest="session_id", help="Session ID for scoped state")
    p_verdict.set_defaults(func=cmd_record_verdict)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
