#!/usr/bin/env python3
"""
Claude Crew Persistent Mode Hook
Blocks Stop while a build or measure-twice loop is active.
Generic todo continuation is handled by Claude Code natively.
"""

# --- C4 version guard: stdlib-only, 3.9-parseable, BEFORE any project import.
# Python 3.9 is UNSUPPORTED for hook functionality (repo requires 3.10+), but
# must be GRACEFUL: allow + loud diagnostic, never a silent crash and never
# functional persistence. Diagnostic channels are the smoke-VERIFIED ones from
# the 2026-07-06 C2 record: stderr + systemMessage (allow-side).
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
    LOAD_CORRUPT,
    LOAD_FUTURE_SCHEMA,
)
from state_discovery import find_session_state_file


def _corrupt_block_message(loop_file: Path) -> str:
    """One-time block diagnostic for an unparseable state file (never-choke:
    the file is set aside as .corrupt so the NEXT Stop allows)."""
    return (
        f"[Crew - State File Corrupt]\n\n"
        f"The loop state file `{loop_file.name}` could not be parsed (corrupt "
        f"JSON) — any active loop state is lost. It has been set aside as "
        f"`{loop_file.name}.corrupt`. If a loop was active, re-init it with "
        f"`/crew:build` or `/crew:measure-twice`."
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
        except OSError:
            # Rename failed — never wedge the user in a permanent block loop.
            # Try to delete the corrupt file so the next Stop allows; if THAT
            # also fails, allow this Stop (fail-open) with the diagnostic on the
            # allow side rather than blocking forever with no path to recovery.
            try:
                loop_file.unlink()
            except OSError:
                print(HookResult.allow_with_diagnostic(_corrupt_block_message(loop_file)).to_json())
                return True
        print(HookResult.block(_corrupt_block_message(loop_file)).to_json())
        return True
    if status == LOAD_FUTURE_SCHEMA:
        # Refuse to touch: bytes untouched, loud diagnostic, allow stop.
        print(HookResult.allow_with_diagnostic(_future_schema_diagnostic(loop_file)).to_json())
        return True
    return False


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
            # L1 adoption stamp: claim an unowned legacy file for this session
            # before the iteration save (the fire already saves — zero extra
            # writes). Closes the co-adoption window at first touch.
            if not loop_state.session_id and session_id:
                loop_state.session_id = session_id
            if loop_state.iteration <= loop_state.max_iterations:
                # iteration field = "next nudge to display"; init sets it to 1,
                # we display it as-is then increment for the next Stop fire.
                task_text = truncate(loop_state.prompt, 120)
                if is_verbose() or loop_state.iteration == 1:
                    # Full recipe on verbose mode or first Stop fire
                    message = f"""[Build Loop - Iteration {loop_state.iteration}/{loop_state.max_iterations}]

Task: {task_text}

Continue working. When complete, verify via the multi-model panel
(/crew:build Step 2):
1. "${{CLAUDE_PLUGIN_ROOT}}/crew" review-prep working-tree{session_flag}
2. Fan out ALL resolved seats, wait for every seat, collect, synthesize
3. If APPROVED, deactivate the loop and summarize
4. If REVISE with [BLOCKING] issues, fix and re-verify

To exit early: `/crew:cancel-build`
"""
                else:
                    # Terse mode for second Stop fire onwards
                    message = f"""[Build Loop - Iteration {loop_state.iteration}/{loop_state.max_iterations}]
Task: {task_text}
"""
                loop_state.iteration += 1
                loop_state.save(loop_file)
                print(HookResult.block(message).to_json())
                return

            # Max iterations reached - force exit
            loop_state.active = False
            loop_state.save(loop_file)
            message = f"""[Build Loop - Safety Limit Reached]

Maximum iterations ({loop_state.max_iterations}) reached. Loop deactivated.

Review what was accomplished and whether the task is complete.
"""
            print(HookResult.block(message).to_json())
            return

    # Priority 2: Measure-Twice Loop (session-scoped lookup)
    measure_file = find_session_state_file(crew_dir, "measure-twice-state", session_id)
    if measure_file:
        measure_state, status = MeasureTwiceState.load_with_status(measure_file)
        if _handle_load_status(measure_file, status):
            return

        if measure_state.active:
            # L1 adoption stamp (see build loop above).
            if not measure_state.session_id and session_id:
                measure_state.session_id = session_id
            if measure_state.iteration <= measure_state.max_iterations:
                # iteration field = "next nudge to display"; init sets it to 1,
                # we display it as-is then increment for the next Stop fire.
                task_text = truncate(measure_state.task_description, 120)
                if is_verbose() or measure_state.iteration == 1:
                    # Full recipe on verbose mode or first Stop fire
                    message = f"""[Measure-Twice Loop - Iteration {measure_state.iteration}/{measure_state.max_iterations}]

Task: {task_text}
Plan: {measure_state.plan_file}

Continue refining the plan. Verify via the multi-model panel
(/crew:measure-twice Phase 3):
1. If you just received panel feedback, revise the plan to address [BLOCKING] issues
2. "${{CLAUDE_PLUGIN_ROOT}}/crew" review-prep {shlex.quote(measure_state.plan_file)}{session_flag}
3. Fan out ALL resolved seats, wait for every seat, collect, synthesize
4. When APPROVED (or only [MINOR] issues), deactivate the loop and present the final plan

To exit early: `/crew:cancel-measure-twice`
"""
                else:
                    # Terse mode for second Stop fire onwards
                    message = f"""[Measure-Twice Loop - Iteration {measure_state.iteration}/{measure_state.max_iterations}]
Task: {task_text}
Plan: {measure_state.plan_file}
"""
                measure_state.iteration += 1
                measure_state.save(measure_file)
                print(HookResult.block(message).to_json())
                return

            # Max iterations reached - force exit
            measure_state.active = False
            measure_state.save(measure_file)
            message = f"""[Measure-Twice Loop - Safety Limit Reached]

Maximum iterations ({measure_state.max_iterations}) reached. Loop deactivated.

Present the current plan to the user and explain where it stands.
"""
            print(HookResult.block(message).to_json())
            return

    # No active loop — allow stop
    print(HookResult.allow().to_json())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — Stop is architecturally fail-open;
        # make the failure LOUD (smoke-verified channels), never silent.
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
