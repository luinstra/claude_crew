#!/usr/bin/env python3
"""Crew state management CLI for feedback-loop and measure-twice persistence."""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# Import from models.py (same directory)
from models import FeedbackLoopState, MeasureTwiceState

LOOP_ALIASES = {
    "feedback-loop": "fl", "fl": "fl",
    "measure-twice": "mt", "mt": "mt",
}

LOOP_CLASSES = {
    "fl": FeedbackLoopState,
    "mt": MeasureTwiceState,
}


def get_loop_filename(canonical: str, session_id: str = "") -> str:
    """Get the state filename for a loop, optionally scoped to a session."""
    base = {"fl": "feedback-loop-state", "mt": "measure-twice-state"}[canonical]
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
    crew_dir.mkdir(parents=True, exist_ok=True)
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


def cmd_show(args):
    """Show current state of a loop."""
    session_id = resolve_session_id(args)
    path = get_state_path(args.loop, session_id)
    cls = LOOP_CLASSES[LOOP_ALIASES[args.loop]]
    state = cls.load(path)
    print(json.dumps(asdict(state), indent=2))


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
    state = cls.load(path)

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

    fl_path = crew_dir / get_loop_filename("fl", session_id)
    fl_state = FeedbackLoopState.load(fl_path)
    if fl_state.active:
        return "ERROR: feedback-loop is already active. Run /crew:cancel-feedback-loop first or let it complete."

    mt_path = crew_dir / get_loop_filename("mt", session_id)
    mt_state = MeasureTwiceState.load(mt_path)
    if mt_state.active:
        return "ERROR: measure-twice loop is already active. Run /crew:cancel-measure-twice first or let it complete."

    return None


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

    if canonical == "fl":
        if not args.prompt:
            print("Error: --prompt required for feedback-loop", file=sys.stderr)
            sys.exit(1)
        state = FeedbackLoopState(
            active=True,
            prompt=args.prompt,
            iteration=1,
            max_iterations=args.max_iterations or 20,
            completion_promise="DONE",
            session_id=session_id,
        )
    else:  # mt
        if not args.task:
            print("Error: --task required for measure-twice", file=sys.stderr)
            sys.exit(1)

        # Auto-derive plan file from task if --auto-plan is set
        if args.auto_plan:
            plan_name = slugify(args.task)
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
            task_description=args.task,
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
    path = get_state_path(args.loop, session_id)
    cls = LOOP_CLASSES[LOOP_ALIASES[args.loop]]
    state = cls.load(path)

    # Build dict with extra metadata
    data = asdict(state)
    data["active"] = False
    data["completed_at"] = datetime.now().isoformat()
    if args.reason:
        data["reason"] = args.reason

    # Write directly (bypass .save() to include extra fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def cmd_increment(args):
    """Increment a numeric field (typically iteration)."""
    if args.field != "iteration":
        print("Error: Only 'iteration' can be incremented", file=sys.stderr)
        sys.exit(1)

    session_id = resolve_session_id(args)
    path = get_state_path(args.loop, session_id)
    cls = LOOP_CLASSES[LOOP_ALIASES[args.loop]]
    state = cls.load(path)

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
    p_init.add_argument("--prompt", help="Task prompt (for feedback-loop)")
    p_init.add_argument("--task", help="Task description (for measure-twice)")
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
