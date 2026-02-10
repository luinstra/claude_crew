#!/usr/bin/env python3
"""
Claude Crew Persistent Mode Hook
Handles feedback-loop persistence and todo continuation.
Prevents stopping when work remains incomplete.
"""

import json
from pathlib import Path

from models import (
    StopInput,
    HookResult,
    FeedbackLoopState,
    MeasureTwiceState,
    SessionBreadcrumb,
    read_hook_input,
    count_incomplete_todos,
)


def count_all_incomplete_todos(directory: Path) -> int:
    """Count all incomplete todos across global and project locations."""
    count = 0
    home = Path.home()

    # Global todos
    todos_dir = home / ".claude" / "todos"
    if todos_dir.is_dir():
        for todo_file in todos_dir.glob("*.json"):
            count += count_incomplete_todos(todo_file)

    # Project todos
    for todo_path in [
        directory / ".crew" / "todos.json",
        directory / ".claude" / "todos.json",
    ]:
        count += count_incomplete_todos(todo_path)

    return count


def find_session_state_file(
    crew_dir: Path,
    loop_prefix: str,
    session_id: str,
) -> Path | None:
    """Find the state file for a given loop and session.

    When session_id is non-empty: look for session-scoped file first,
    then check legacy unsuffixed file if it matches this session.
    When session_id is empty: use legacy unsuffixed file only.

    Returns the path to the state file or None if not found.
    """
    if session_id:
        # Primary: session-scoped file
        scoped_path = crew_dir / f"{loop_prefix}-{session_id}.json"
        if scoped_path.is_file():
            return scoped_path

        # Fallback: legacy unsuffixed file if it belongs to this session
        legacy_path = crew_dir / f"{loop_prefix}.json"
        if legacy_path.is_file():
            try:
                with open(legacy_path) as f:
                    data = json.load(f)
                file_session = data.get("session_id", "")
                # Owned by this session, or pre-migration (no session_id field)
                if file_session == session_id or file_session == "":
                    return legacy_path
            except (OSError, json.JSONDecodeError):
                pass

        return None
    else:
        # No session_id: legacy behavior — check unsuffixed file only
        legacy_path = crew_dir / f"{loop_prefix}.json"
        if legacy_path.is_file():
            return legacy_path
        return None


def main():
    data = read_hook_input()
    hook_input = StopInput.from_dict(data)
    directory = hook_input.directory_path
    session_id = hook_input.session_id

    crew_dir = directory / ".crew"

    # Priority 1: Feedback Loop (session-scoped lookup)
    loop_file = find_session_state_file(crew_dir, "feedback-loop-state", session_id)
    if loop_file:
        loop_state = FeedbackLoopState.load(loop_file)

        if loop_state.active:
            if loop_state.iteration < loop_state.max_iterations:
                loop_state.iteration += 1
                loop_state.save(loop_file)

                message = f"""[Feedback Loop - Iteration {loop_state.iteration}/{loop_state.max_iterations}]

Task: {loop_state.prompt}

Continue working. When complete:
1. Spawn advisor to verify your work
2. If approved, deactivate the loop and summarize what was accomplished

To exit early: `/crew:cancel-feedback-loop`
"""
                print(HookResult.block(message).to_json())
                return

            # Max iterations reached - force exit
            loop_state.active = False
            loop_state.save(loop_file)
            message = f"""[Feedback Loop - Safety Limit Reached]

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

    # Priority 3: Todo Continuation (baseline persistence)
    incomplete_count = count_all_incomplete_todos(directory)
    if incomplete_count > 0:
        message = f"""[{incomplete_count} tasks remaining]

Continue working on incomplete tasks.
"""
        print(HookResult.block(message).to_json())
        return

    # Auto-save session breadcrumb on clean exit
    try:
        breadcrumb = SessionBreadcrumb.create_now(directory, hook_input.session_id)
        breadcrumb.save(directory / ".crew" / "session-breadcrumb.json")
    except OSError:
        pass  # Best effort

    # No blocking needed
    print(HookResult.allow().to_json())


if __name__ == "__main__":
    main()
