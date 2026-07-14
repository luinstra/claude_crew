#!/usr/bin/env python3
"""
Claude Crew Persistent Mode Hook
Blocks Stop while a build or measure-twice loop is active.
Generic todo continuation is handled by Claude Code natively.
"""

# --- Version guard: stdlib-only, 3.9-parseable, BEFORE any project import.
# Python 3.9 is UNSUPPORTED for hook functionality (repo requires 3.10+), but
# must be GRACEFUL: allow + loud diagnostic, never a silent crash and never
# functional persistence. Diagnostic channels: stderr + systemMessage (allow-side).
import json
import sys

if sys.version_info < (3, 10):
    _DIAG = (
        "[crew] Stop hook disabled: Python %d.%d is unsupported (crew hooks "
        "require Python 3.10+). Persistence loops will NOT block — fix the "
        "hook interpreter (e.g. install python3 >= 3.10 on PATH)."
        % sys.version_info[:2]
    )
    print(json.dumps({"systemMessage": _DIAG}))
    print(_DIAG, file=sys.stderr)
    sys.exit(0)

import shlex
from pathlib import Path

from models import (
    StopInput,
    HookResult,
    BuildState,
    MeasureTwiceState,
    read_hook_input,
    is_verbose,
    truncate,
    utc_now_iso,
    elapsed_minutes,
    LOAD_CORRUPT,
    LOAD_FUTURE_SCHEMA,
)
from state_discovery import find_session_state_file


def _corrupt_block_message(loop_file: Path, disposition: str) -> str:
    """One-time diagnostic for an unparseable state file (never-choke: the file
    is disposed of so the NEXT Stop allows). ``disposition`` describes what
    actually happened to the file so the message never lies:
    - ``"set_aside"`` — renamed to ``<name>.corrupt`` (the normal path).
    - ``"deleted"``   — rename failed, so it was deleted (unlink fallback).
    - ``"stuck"``     — both rename AND delete failed; the file is untouched.
    """
    if disposition == "set_aside":
        fate = (
            f"It has been set aside as `{loop_file.name}.corrupt`."
        )
    elif disposition == "deleted":
        fate = (
            "It could not be renamed, so the unreadable state file was deleted."
        )
    else:  # "stuck"
        fate = (
            "It could not be set aside or deleted (check permissions) — the "
            "unreadable state file is still in place."
        )
    return (
        f"[Crew - State File Corrupt]\n\n"
        f"The loop state file `{loop_file.name}` could not be parsed (corrupt "
        f"JSON) — any active loop state is lost. {fate} If a loop was active, "
        f"re-init it with `/crew:build` or `/crew:measure-twice`."
    )


def _future_schema_diagnostic(loop_file: Path) -> str:
    """Loud allow-side diagnostic for a newer-crew state file (refuse to
    touch: no rename, no rewrite — a downgrade must not strand a live loop)."""
    return (
        f"[crew] `{loop_file.name}` was written by a newer crew version "
        f"(state schema ahead of this install) — leaving it untouched. Upgrade "
        f"crew or deactivate the loop manually. Allowing stop."
    )


def _handle_load_status(loop_file: Path, status: str):
    """Handle non-OK load statuses. Returns True if the hook emitted output
    and should return; False to continue with the loaded (OK) state."""
    if status == LOAD_CORRUPT:
        # Set the corrupt file aside so the next Stop allows (one-time block).
        corrupt_path = loop_file.with_name(loop_file.name + ".corrupt")
        try:
            loop_file.replace(corrupt_path)
            disposition = "set_aside"
        except OSError:
            # Rename failed — never wedge the user in a permanent block loop.
            # Try to delete the corrupt file so the next Stop allows; if THAT
            # also fails, allow this Stop (fail-open) with the diagnostic on the
            # allow side rather than blocking forever with no path to recovery.
            try:
                loop_file.unlink()
                disposition = "deleted"
            except OSError:
                print(HookResult.allow_with_diagnostic(_corrupt_block_message(loop_file, "stuck")).to_json())
                return True
        print(HookResult.block(_corrupt_block_message(loop_file, disposition)).to_json())
        return True
    if status == LOAD_FUTURE_SCHEMA:
        # Refuse to touch: bytes untouched, loud diagnostic, allow stop.
        print(HookResult.allow_with_diagnostic(_future_schema_diagnostic(loop_file)).to_json())
        return True
    return False


def _termination_reason(state):
    """Why this loop must stop NOW, or None to keep going.

    Both bounds are hook-owned and unwritable by the agent (see
    ``AGENT_SETTABLE``). Neither is a stall timer: a single panel seat can take
    minutes, and any late-landing seat would reset a stall clock forever.
    """
    if state.stop_fires >= state.max_stop_fires:
        return (
            f"livelock circuit breaker: {state.stop_fires} Stop fires without "
            f"the loop finishing (limit {state.max_stop_fires})"
        )
    minutes = elapsed_minutes(state.started_at)
    if minutes is not None and state.deadline_minutes > 0 and minutes >= state.deadline_minutes:
        return (
            f"deadline reached: {minutes:.0f} minutes elapsed "
            f"(limit {state.deadline_minutes})"
        )
    return None


def _elapsed_note(state) -> str:
    """One-line budget line for the nudge, so a runaway loop is VISIBLE."""
    minutes = elapsed_minutes(state.started_at)
    clock = f"{minutes:.0f}/{state.deadline_minutes} min" if minutes is not None else "unknown"
    return f"Elapsed: {clock} · stop fires: {state.stop_fires}/{state.max_stop_fires}"


def _handle_loop(loop_file, state, parked, banner, task_text, detail_lines, recipe, cancel_cmd):
    """Shared Stop handling for both loops. Emits output; returns True if handled.

    The hook is a READER of loop progress: it NEVER writes `iteration`, which
    counts revision rounds. It writes only the fields it owns: `stop_fires`,
    the `started_at` stamp, and `active=False` when a bound trips. Conflating the
    two is what made `iteration` mean "Stop fires" and drove a live loop to reset
    its own counter to escape a limit it kept hitting for merely waiting.
    """
    reason = _termination_reason(state)
    if reason:
        state.active = False
        state.save(loop_file)
        print(HookResult.block(
            f"""[{banner} - Safety Limit Reached]

Loop deactivated: {reason}.

Do NOT re-init the loop. Report where the work actually stands: what completed,
what did not, and what you were waiting on."""
        ).to_json())
        return True

    if parked:
        # Work the session launched is still in flight, so this Stop is a WAIT,
        # not an attempt to quit. ALLOW it: the harness re-invokes the session
        # when that work completes, and the loop picks up from there. Blocking
        # here is what manufactured the busy-wait: the agent was forced to take
        # a turn with nothing to do, so it polled, which burned the counter and
        # flooded the context until compaction wiped its memory of the panel.
        # The wall-clock deadline above still bounds a wait that never ends.
        state.save(loop_file)
        print(HookResult.allow().to_json())
        return True

    state.stop_fires += 1
    state.save(loop_file)

    # The full recipe is CREW_VERBOSE-only. It used to also print on the first
    # fire, but the first fire is not special: the loop parks and re-fires many
    # times per round, so that only bought a wall of text at the least useful
    # moment. The one line that must survive terse mode is the wait guard:
    # re-running a panel mid-flight deletes the seats that already landed.
    if is_verbose():
        body = recipe
    else:
        body = (
            f"Waiting on seats? Just wait. Do NOT re-run the panel or clear "
            f"landed seat files. Exit: `{cancel_cmd}`"
        )

    detail = "\n".join(detail_lines)
    print(HookResult.block(
        f"""[{banner} - Round {state.iteration}/{state.max_iterations}]
{_elapsed_note(state)}

Task: {task_text}
{detail}
{body}"""
    ).to_json())
    return True


def main():
    data = read_hook_input()
    hook_input = StopInput.from_dict(data)
    directory = hook_input.directory_path
    session_id = hook_input.session_id

    crew_dir = directory / ".crew"

    # Rendered inside copy-pasteable shell commands: only emit the flag when
    # there is a value (no trailing bare `--session-id`), and shell-quote it.
    session_flag = f" --session-id {shlex.quote(session_id)}" if session_id else ""

    # Priority 1: Build Loop (session-scoped lookup)
    loop_file = find_session_state_file(crew_dir, "build-state", session_id)
    if loop_file:
        loop_state, status = BuildState.load_with_status(loop_file)
        if _handle_load_status(loop_file, status):
            return

        if loop_state.active:
            # adoption stamp: claim an unowned legacy file for this session
            # before the save (the fire already saves, zero extra writes).
            # Closes the co-adoption window at first touch.
            if not loop_state.session_id and session_id:
                loop_state.session_id = session_id
            # A schema-1 file predates the wall clock; start it now rather than
            # leaving the loop unbounded.
            if not loop_state.started_at:
                loop_state.started_at = utc_now_iso()
            _handle_loop(
                loop_file,
                loop_state,
                hook_input.is_parked,
                banner="Build Loop",
                task_text=truncate(loop_state.prompt, 120),
                detail_lines=[],
                recipe=f"""
Continue working. When complete, verify via the multi-model panel
(/crew:build Step 2):
1. "${{CLAUDE_PLUGIN_ROOT}}/crew" review-prep working-tree{session_flag}
2. Fan out ALL resolved seats, wait for every seat, collect, synthesize
3. If APPROVED, deactivate the loop and summarize
4. If REVISE with [BLOCKING] issues, fix and re-verify

If you are WAITING on seats that are still running, just wait. Do NOT re-run
the panel and do NOT clear seat files that already landed.

To exit early: `/crew:cancel-build`""",
                cancel_cmd="/crew:cancel-build",
            )
            return

    # Priority 2: Measure-Twice Loop (session-scoped lookup)
    measure_file = find_session_state_file(crew_dir, "measure-twice-state", session_id)
    if measure_file:
        measure_state, status = MeasureTwiceState.load_with_status(measure_file)
        if _handle_load_status(measure_file, status):
            return

        if measure_state.active:
            # adoption stamp (see build loop above).
            if not measure_state.session_id and session_id:
                measure_state.session_id = session_id
            if not measure_state.started_at:
                measure_state.started_at = utc_now_iso()
            _handle_loop(
                measure_file,
                measure_state,
                hook_input.is_parked,
                banner="Measure-Twice Loop",
                task_text=truncate(measure_state.task_description, 120),
                detail_lines=[f"Plan: {measure_state.plan_file}"],
                recipe=f"""
Continue refining the plan. Verify via the multi-model panel
(/crew:measure-twice Phase 3):
1. If you just received panel feedback, revise the plan to address [BLOCKING] issues
2. "${{CLAUDE_PLUGIN_ROOT}}/crew" review-prep {shlex.quote(measure_state.plan_file)}{session_flag}
3. Fan out ALL resolved seats, wait for every seat, collect, synthesize
4. When APPROVED (or only [MINOR] issues), deactivate the loop and present the final plan

If you are WAITING on seats that are still running, just wait. Do NOT re-run
the panel and do NOT clear seat files that already landed.

To exit early: `/crew:cancel-measure-twice`""",
                cancel_cmd="/crew:cancel-measure-twice",
            )
            return

    # No active loop — allow stop
    print(HookResult.allow().to_json())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001. Stop is architecturally fail-open;
        # make the failure LOUD (stderr + systemMessage), never silent.
        _diag = (
            "[crew] Stop hook crashed (%s: %s) — allowing stop. An active "
            "persistence loop may not be enforced this fire."
            % (exc.__class__.__name__, exc)
        )
        # models is importable here (top-of-module import succeeded before
        # main() ran), so use the shared allow-with-diagnostic shape.
        print(HookResult.allow_with_diagnostic(_diag).to_json())
        print(_diag, file=sys.stderr)
        sys.exit(0)
