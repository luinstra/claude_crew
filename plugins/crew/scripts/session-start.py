#!/usr/bin/env python3
"""
Claude Crew Session Start Hook
Restores persistent mode states and injects plugin integration guidance.
"""

import time
from pathlib import Path

from models import (
    SessionStartInput,
    SessionStartResult,
    FeedbackLoopState,
    MeasureTwiceState,
    SessionBreadcrumb,
    read_hook_input,
    get_file_age_days,
    count_incomplete_todos,
)


MAX_AGE_DAYS = 7
MAX_AGE_SECONDS = MAX_AGE_DAYS * 86400


def cleanup_stale_files(directory: Path) -> None:
    """Remove stale state files older than MAX_AGE_DAYS, preserving active sessions."""
    if not directory.is_dir():
        return

    # Only clean up crew state files, not Claude Code's internal files
    state_file_patterns = [
        "feedback-loop-state.json",
        "measure-twice-state.json",
        "session-breadcrumb.json",
        "context-snapshot-*.json",
    ]

    now = time.time()
    for pattern in state_file_patterns:
        for json_file in directory.glob(pattern):
            try:
                age = now - json_file.stat().st_mtime
                if age > MAX_AGE_SECONDS:
                    # Don't delete files with active sessions
                    state = FeedbackLoopState.load(json_file)
                    if not state.active:
                        json_file.unlink()
            except (OSError, AttributeError, KeyError, ValueError):
                pass  # Skip files that can't be loaded as state


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
) -> list[str]:
    """Build session status messages."""
    messages: list[str] = []

    # Project stack detection
    if stack_hints:
        messages.append(f"[Project: {', '.join(stack_hints)}]")

    # Check for feedback loop state
    loop_state = FeedbackLoopState.load(directory / ".crew" / "feedback-loop-state.json")
    if loop_state.active:
        messages.append(
            f"[Feedback Loop Active - {loop_state.iteration}/{loop_state.max_iterations}]\n"
            f"Task: {loop_state.prompt}\n"
            f"Continue until advisor verifies completion."
        )

    # Check for measure-twice loop state
    measure_state = MeasureTwiceState.load(directory / ".crew" / "measure-twice-state.json")
    if measure_state.active:
        messages.append(
            f"[Measure-Twice Loop Active - {measure_state.iteration}/{measure_state.max_iterations}]\n"
            f"Task: {measure_state.task_description}\n"
            f"Plan: {measure_state.plan_file}\n"
            f"Continue until advisor approves the plan."
        )

    # Check for saved context snapshot
    context_snapshot = directory / ".crew" / "context-snapshot.md"
    if context_snapshot.is_file():
        age_days = get_file_age_days(context_snapshot)
        if age_days < 7:
            messages.append(
                f"[Context Snapshot Available]\n"
                f"`.crew/context-snapshot.md` ({age_days}d old) - read to restore context."
            )

    # Check for session breadcrumb
    breadcrumb = SessionBreadcrumb.load(directory / ".crew" / "session-breadcrumb.json")
    if breadcrumb:
        messages.append(f"[Previous Session]\nLast exit: {breadcrumb.last_session}")

    # Check for incomplete todos
    todos_dir = home / ".claude" / "todos"
    incomplete_count = 0
    if todos_dir.is_dir():
        for todo_file in todos_dir.glob("*.json"):
            incomplete_count += count_incomplete_todos(todo_file)

    if incomplete_count > 0:
        messages.append(f"[{incomplete_count} pending tasks from previous session]")

    return messages


def main():
    data = read_hook_input()
    hook_input = SessionStartInput.from_dict(data)
    directory = hook_input.directory_path
    home = Path.home()

    # Run cleanup (silent, best-effort)
    cleanup_stale_files(home / ".claude" / "todos")
    cleanup_stale_files(home / ".claude")
    cleanup_stale_files(directory / ".crew")

    # Detect project stack
    stack_hints = detect_project_stack(directory)

    # Build plugin integration guidance
    guidance = build_plugin_guidance(stack_hints)

    # Build session status messages
    status_messages = build_session_status(directory, home, stack_hints)

    # Combine guidance and status into additionalContext
    context_parts = [guidance]
    if status_messages:
        context_parts.append("\n".join(status_messages))

    full_context = "\n\n".join(context_parts)

    # Output using SessionStartResult for proper context injection
    print(SessionStartResult.with_context(full_context).to_json())


if __name__ == "__main__":
    main()
