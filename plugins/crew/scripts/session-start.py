#!/usr/bin/env python3
"""
Claude Crew Session Start Hook
Restores persistent mode states and injects plugin integration guidance.
"""

import json
import time
from pathlib import Path

from models import (
    SessionStartInput,
    SessionStartResult,
    FeedbackLoopState,
    MeasureTwiceState,
    read_hook_input,
    get_file_age_days,
    count_incomplete_todos,
)


MAX_AGE_DAYS = 7
MAX_AGE_SECONDS = MAX_AGE_DAYS * 86400
STALE_INACTIVE_DAYS = 1
STALE_INACTIVE_SECONDS = STALE_INACTIVE_DAYS * 86400


def is_loop_state_file(filename: str) -> bool:
    """Check if a filename is a loop state file (legacy or session-scoped).

    Matches:
    - feedback-loop-state.json (legacy)
    - measure-twice-state.json (legacy)
    - feedback-loop-state-abc123.json (session-scoped)
    - measure-twice-state-xyz789.json (session-scoped)
    """
    return (
        (filename.startswith("feedback-loop-state") or filename.startswith("measure-twice-state"))
        and filename.endswith(".json")
    )


def cleanup_stale_files(directory: Path) -> None:
    """Remove stale state files, preserving active sessions.

    - Inactive state files older than 1 day: delete
    - Active state files older than MAX_AGE_DAYS (7): force-deactivate and delete
    - Non-state files (context-snapshot): delete if older than MAX_AGE_DAYS
    """
    if not directory.is_dir():
        return

    # Patterns for non-state crew files
    non_state_patterns = [
        "context-snapshot-*.json",
    ]

    now = time.time()

    # Clean up loop state files (both legacy and session-scoped)
    for json_file in directory.glob("*.json"):
        try:
            if is_loop_state_file(json_file.name):
                age = now - json_file.stat().st_mtime
                # Load state to check active flag
                state = FeedbackLoopState.load(json_file)
                if state.active:
                    # Active but very old (>7 days) — force-deactivate and delete
                    if age > MAX_AGE_SECONDS:
                        json_file.unlink()
                else:
                    # Inactive — delete if older than 1 day
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


def cleanup_stale_todos(todos_dir: Path) -> None:
    """Remove stale todo files from ~/.claude/todos/.

    - Empty todo files ([] arrays): delete immediately
    - Todo files older than MAX_AGE_DAYS with no incomplete tasks: delete
    - Todo files older than MAX_AGE_DAYS with incomplete tasks: delete (abandoned)
    """
    if not todos_dir.is_dir():
        return

    now = time.time()

    for todo_file in todos_dir.glob("*.json"):
        try:
            with open(todo_file) as f:
                todos = json.load(f)

            # Empty array — always safe to remove
            if isinstance(todos, list) and len(todos) == 0:
                todo_file.unlink()
                continue

            # Old files — remove regardless (abandoned sessions)
            age = now - todo_file.stat().st_mtime
            if age > MAX_AGE_SECONDS:
                todo_file.unlink()

        except (OSError, json.JSONDecodeError, ValueError):
            pass


def detect_project_stack(directory: Path) -> list[str]:
    """Detect project tech stack and return skill trigger hints."""
    hints = []

    # Kotlin: .kt, .kts files → triggers sk:kotlin
    kt_files = list(directory.glob("**/*.kt"))[:1]  # Just check if any exist
    kts_files = list(directory.glob("**/*.kts"))[:1]
    if kt_files or kts_files:
        hints.append("Kotlin")

    # Gradle Kotlin DSL → triggers sk:gradle
    gradle_kts = list(directory.glob("**/*.gradle.kts"))[:1]
    if gradle_kts:
        hints.append("Gradle")

    # Exposed ORM: check build files for exposed dependency → triggers sk:exposed
    build_file = directory / "build.gradle.kts"
    if build_file.is_file():
        try:
            content = build_file.read_text()
            if "exposed" in content.lower():
                hints.append("Exposed")
            if "trino" in content.lower():
                hints.append("Trino")
        except OSError:
            pass

    return hints


def build_plugin_guidance(stack_hints: list[str]) -> str:
    """Build plugin integration guidance for session context."""
    lines = []

    # Only show skills with IMPORTANT wrapper if project uses relevant stack
    if stack_hints:
        lines.extend([
            "<IMPORTANT>",
            f"## This project uses: {', '.join(stack_hints)}",
            "",
            "Invoke the corresponding skill BEFORE writing related code:",
            "",
        ])

        if "Kotlin" in stack_hints:
            lines.append("- **Kotlin** → `sk:kotlin` (data classes, coroutines, null safety, extension functions)")
        if "Kotlin" in stack_hints:  # Kotlin implies kotlin-testing
            lines.append("- **Kotlin tests** → `sk:kotlin-testing` (Kotest, MockK, TestIdProvider, DatabaseTest)")
        if "Exposed" in stack_hints:
            lines.append("- **Exposed ORM** → `sk:exposed` (repositories, transactions, table definitions)")
        if "Gradle" in stack_hints:
            lines.append("- **Gradle** → `sk:gradle` (build config, dependencies, version catalogs)")
        if "Trino" in stack_hints:
            lines.append("- **Trino** → `sk:trino` (analytics queries, query routing)")

        lines.extend([
            "- **Git** → `sk:git` (commits, rebasing, history)",
            "",
            "Use the Skill tool to invoke these BEFORE writing code.",
            "</IMPORTANT>",
        ])

    lines.extend([
        "",
        "<system-reminder>",
        "**Crew agents** for specialized work:",
        "- Analysis/debugging/planning: `advisor` agent",
        "- Implementation: `executor` agent",
        "- Documentation: `document-writer` agent",
        "- Codebase search: `file-reader` agent",
        "</system-reminder>",
    ])

    return "\n".join(lines)


def build_session_status(
    directory: Path,
    home: Path,
    stack_hints: list[str],
    session_id: str = "",
) -> list[str]:
    """Build session status messages.

    When session_id is provided, shows detailed status for this session's loops
    and brief mentions of other sessions' active loops.
    """
    messages: list[str] = []

    # Project stack detection
    if stack_hints:
        messages.append(f"[Project: {', '.join(stack_hints)}]")

    # Session ID injection for Claude to use in commands
    if session_id:
        messages.append(
            f"[Session ID: {session_id}]\n"
            f"Pass this session ID when invoking crew-state.py commands: --session-id {session_id}\n"
            f"Also exported as CLAUDE_SESSION_ID environment variable."
        )

    crew_dir = directory / ".crew"

    # Collect all active loop state files
    this_session_loops: list[str] = []
    other_session_loops: list[str] = []

    if crew_dir.is_dir():
        for json_file in crew_dir.glob("*.json"):
            if not is_loop_state_file(json_file.name):
                continue

            try:
                with open(json_file) as f:
                    data = json.load(f)
                if not data.get("active", False):
                    continue

                file_session = data.get("session_id", "")
                is_this_session = (
                    not session_id  # No session_id means treat all as current
                    or file_session == session_id
                    or file_session == ""  # Pre-migration files belong to current session
                )

                # Build status line
                if json_file.name.startswith("feedback-loop-state"):
                    iteration = data.get("iteration", 1)
                    max_iter = data.get("max_iterations", 20)
                    prompt = data.get("prompt", "")
                    if is_this_session:
                        this_session_loops.append(
                            f"[Feedback Loop Active - {iteration}/{max_iter}]\n"
                            f"Task: {prompt}\n"
                            f"Continue until advisor verifies completion."
                        )
                    else:
                        other_session_loops.append(
                            f"Feedback loop (session {file_session[:8]}...): {prompt[:50]}"
                        )
                elif json_file.name.startswith("measure-twice-state"):
                    iteration = data.get("iteration", 1)
                    max_iter = data.get("max_iterations", 10)
                    task = data.get("task_description", "")
                    plan = data.get("plan_file", "")
                    if is_this_session:
                        this_session_loops.append(
                            f"[Measure-Twice Loop Active - {iteration}/{max_iter}]\n"
                            f"Task: {task}\n"
                            f"Plan: {plan}\n"
                            f"Continue until advisor approves the plan."
                        )
                    else:
                        other_session_loops.append(
                            f"Measure-twice loop (session {file_session[:8]}...): {task[:50]}"
                        )
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue

    messages.extend(this_session_loops)

    if other_session_loops:
        messages.append(
            "[Other Sessions]\n" + "\n".join(f"- {l}" for l in other_session_loops)
        )

    # Check for saved context snapshot
    context_snapshot = crew_dir / "context-snapshot.md"
    if context_snapshot.is_file():
        age_days = get_file_age_days(context_snapshot)
        if age_days < 7:
            messages.append(
                f"[Context Snapshot Available]\n"
                f"`.crew/context-snapshot.md` ({age_days}d old) - read to restore context."
            )

    # Check for incomplete todos (scoped to current session)
    todos_dir = home / ".claude" / "todos"
    incomplete_count = 0
    if todos_dir.is_dir() and session_id:
        for todo_file in todos_dir.glob("*.json"):
            if todo_file.stem.startswith(session_id):
                incomplete_count += count_incomplete_todos(todo_file)

    if incomplete_count > 0:
        messages.append(f"[{incomplete_count} pending tasks from previous session]")

    return messages


def main():
    data = read_hook_input()
    hook_input = SessionStartInput.from_dict(data)
    directory = hook_input.directory_path
    session_id = hook_input.session_id
    home = Path.home()

    # Run cleanup (silent, best-effort)
    cleanup_stale_todos(home / ".claude" / "todos")
    cleanup_stale_files(home / ".claude")
    cleanup_stale_files(directory / ".crew")

    # Detect project stack
    stack_hints = detect_project_stack(directory)

    # Build plugin integration guidance
    guidance = build_plugin_guidance(stack_hints)

    # Build session status messages (session-scoped)
    status_messages = build_session_status(directory, home, stack_hints, session_id)

    # Combine guidance and status into additionalContext
    context_parts = [guidance]
    if status_messages:
        context_parts.append("\n".join(status_messages))

    full_context = "\n\n".join(context_parts)

    # Output using SessionStartResult for proper context injection
    print(SessionStartResult.with_context(full_context).to_json())


if __name__ == "__main__":
    main()
