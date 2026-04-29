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
            if loop_state.iteration < loop_state.max_iterations:
                loop_state.iteration += 1
                loop_state.save(loop_file)

                message = f"""[Build Loop - Iteration {loop_state.iteration}/{loop_state.max_iterations}]

Task: {loop_state.prompt}

Continue working. When complete:
1. Spawn advisor to verify and review your work
2. If APPROVED, deactivate the loop and summarize what was accomplished
3. If REVISE with [BLOCKING] issues, fix them and re-verify

To exit early: `/crew:cancel-build`
"""
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
            if measure_state.iteration < measure_state.max_iterations:
                measure_state.iteration += 1
                measure_state.save(measure_file)

                message = f"""[Measure-Twice Loop - Iteration {measure_state.iteration}/{measure_state.max_iterations}]

Task: {measure_state.task_description}
Plan: {measure_state.plan_file}

Continue refining the plan:
1. If you just received advisor feedback, revise the plan to address [BLOCKING] issues
2. Spawn advisor to re-review using Task(subagent_type="crew:advisor", prompt="...")
3. When APPROVED (or only [MINOR] issues), deactivate the loop and present the final plan

To exit early: `/crew:cancel-measure-twice`
"""
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
