#!/usr/bin/env python3
"""
Claude Crew Persistent Mode Hook
Blocks Stop while a build or measure-twice loop is active.
Generic todo continuation is handled by Claude Code natively.
"""

from pathlib import Path

from models import (
    StopInput,
    HookResult,
    BuildState,
    MeasureTwiceState,
    read_hook_input,
    is_verbose,
    truncate,
)
from state_discovery import find_session_state_file


def main():
    data = read_hook_input()
    hook_input = StopInput.from_dict(data)
    directory = hook_input.directory_path
    session_id = hook_input.session_id

    crew_dir = directory / ".crew"

    # Priority 1: Build Loop (session-scoped lookup)
    loop_file = find_session_state_file(crew_dir, "build-state", session_id)
    if loop_file:
        loop_state = BuildState.load(loop_file)

        if loop_state.active:
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
1. "${{CLAUDE_PLUGIN_ROOT}}/crew" review-prep working-tree --session-id {session_id}
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
        measure_state = MeasureTwiceState.load(measure_file)

        if measure_state.active:
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
2. "${{CLAUDE_PLUGIN_ROOT}}/crew" review-prep {measure_state.plan_file} --session-id {session_id}
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
    main()
