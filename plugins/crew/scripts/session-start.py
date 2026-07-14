#!/usr/bin/env python3
"""
Claude Crew Session Start Hook
Restores persistent mode states and injects plugin integration guidance.
"""

# --- Version guard: stdlib-only, 3.9-parseable, BEFORE any project import.
# Python 3.9 is UNSUPPORTED for hook functionality (repo requires 3.10+), but
# must be GRACEFUL: allow + loud diagnostic, never a silent crash. This
# pre-import fallback runs stdlib-only (it CANNOT build a SessionStartResult,
# models isn't imported yet), so it emits its diagnostic on the `systemMessage`
# channel (the allow-side carrier) + stderr. This is the
# intentional split from the post-import 3.10+ crash handler at the bottom of
# the module, which CAN build a SessionStartResult and therefore emits via
# SessionStart's documented hookSpecificOutput.additionalContext channel.
import json
import sys

if sys.version_info < (3, 10):
    _DIAG = (
        "[crew] SessionStart hook disabled: Python %d.%d is unsupported (crew "
        "hooks require Python 3.10+). State restore and cleanup skipped — fix "
        "the hook interpreter (e.g. install python3 >= 3.10 on PATH)."
        % sys.version_info[:2]
    )
    print(json.dumps({"systemMessage": _DIAG}))
    print(_DIAG, file=sys.stderr)
    sys.exit(0)

import os
import re
import shutil
import time
from pathlib import Path

from models import (
    SessionStartInput,
    SessionStartResult,
    read_hook_input,
    elapsed_minutes,
    get_file_age_days,
    count_incomplete_todos,
    is_verbose,
    read_state_json,
    DEFAULT_DEADLINE_MINUTES,
    DEFAULT_MAX_STOP_FIRES,
    LOAD_OK,
)
from state_discovery import is_loop_state_file, is_active_state_file


MAX_AGE_DAYS = 7
MAX_AGE_SECONDS = MAX_AGE_DAYS * 86400
STALE_INACTIVE_DAYS = 1
STALE_INACTIVE_SECONDS = STALE_INACTIVE_DAYS * 86400

# bound the stack-detection walk so it can't blow the 10s SessionStart hook
# budget on a big tree. Canonical artifact/vendor dirs are pruned (real Kotlin
# sources never live under them); the entry cap is a hard ceiling on files
# visited. Both are module-level so tests can dial the cap down.
STACK_PRUNE_DIRS = {".git", "node_modules", "build", ".venv", "venv", ".gradle", "dist", "target"}
STACK_WALK_MAX_ENTRIES = 20_000


def cleanup_stale_files(directory: Path) -> None:
    """Remove stale state files, preserving active sessions.

    - Inactive state files older than 1 day: delete
    - Active state files older than MAX_AGE_DAYS (7): force-deactivate and delete
    - Non-state files (context-snapshot): delete if older than MAX_AGE_DAYS
    """
    if not directory.is_dir():
        return

    # Patterns for non-state crew files. EVERY glob here MUST be anchored to a
    # crew-specific literal or crew-prefixed pattern — this cleanup runs over BOTH
    # the project `.crew/` AND the SHARED `~/.claude` root (a dir owned by ALL
    # Claude Code tools), so a bare-suffix glob (`*.restored.md`, `.*.tmp`) would
    # delete a FOREIGN tool's / a user's files. No bare-suffix globs.
    #
    # The four `*-state.json.corrupt` / `*-state-*.json.corrupt` globs sweep the
    # set-aside artifacts the Stop hook / crew-state CLI create when a state file
    # can't be parsed (they end in `.corrupt`, so the `*.json` loop above never
    # sees them) — otherwise `build-state.json.corrupt` accumulates forever. They
    # are SCOPED to the EXACT backup names crew creates — the two forms are the
    # legacy `build-state.json` and the session-scoped `build-state-<id>.json`
    # (note the HYPHEN before the id) — NOT a loose `build-state*.json.corrupt`
    # that would also match a foreign `build-stateEVIL.json.corrupt`.
    #
    # The context-snapshot globs are EXACT literals only: `context-snapshot.md`
    # (what save-context.md writes) and `context-snapshot.restored.md` (the rename
    # restore-context.md leaves behind — otherwise it accumulates forever). NOT
    # `context-snapshot*.md` (would match a user's `context-snapshot-notes.md`)
    # nor a bare `*.restored.md` (would match ANY tool's restored file). The
    # legacy `context-snapshot-*.json` glob is kept (crew-prefixed, `.json`-tailed;
    # it never matched the real `.md` artifacts but harmlessly sweeps any old JSON
    # snapshot).
    #
    # The temp globs match ONLY what `atomic_write_json` (models.py) produces:
    # `tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp")` names each temp
    # `.<state-filename>.<rand>.tmp` — already crew-anchored by the state filename,
    # so approach (a) needs no generator change. We glob EXACTLY those anchored
    # names (the two state kinds × legacy/`-<id>` forms), never a bare `.*.tmp`
    # that would delete any hidden temp from any tool. These sweep atomic-write
    # temps orphaned by a crash between mkstemp and os.replace (rare).
    non_state_patterns = [
        "context-snapshot-*.json",
        "context-snapshot.md",
        "context-snapshot.restored.md",
        ".build-state.json.*.tmp",
        ".build-state-*.json.*.tmp",
        ".measure-twice-state.json.*.tmp",
        ".measure-twice-state-*.json.*.tmp",
        "build-state.json.corrupt",
        "build-state-*.json.corrupt",
        "measure-twice-state.json.corrupt",
        "measure-twice-state-*.json.corrupt",
    ]

    now = time.time()

    # Clean up loop state files (both legacy and session-scoped)
    for json_file in directory.glob("*.json"):
        try:
            if is_loop_state_file(json_file.name):
                age = now - json_file.stat().st_mtime
                if is_active_state_file(json_file):
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


# Generated debate-dir names only: run-<...> OR a YYYYMMDD-HHMMSS timestamp
# prefix. Guards cleanup against deleting arbitrary user dirs under .crew/debates/.
_DEBATE_DIR_RE = re.compile(r"^(run-|\d{8}-\d{6})")


def cleanup_stale_debate_dirs(crew_dir: Path) -> None:
    """Remove stale, INCOMPLETE debate run directories from .crew/debates/.

    Debate runs are directories (not JSON files), so they need shutil.rmtree
    rather than unlink.  Uses the INACTIVE (1-day) threshold — never the 7-day
    active threshold — so a live debate mid-write is never nuked.

    Only removes dirs matching a GENERATED debate-dir name — never an arbitrary
    hyphenated dir a user may have parked under .crew/debates/:
    - ``run-<...>``              multi-round debate runs
    - ``<YYYYMMDD-HHMMSS>[-...]`` single-round ``<timestamp>-<slug>`` dirs

    A stale generated dir is deleted ONLY when it has NO ``synthesis.md`` inside
    it — i.e. an abandoned/incomplete debate or a bare scaffold. A dir that
    DOES contain ``synthesis.md`` is a completed decision record and is KEPT
    regardless of age (never deleted by this cleanup).
    """
    debates_dir = crew_dir / "debates"
    if not debates_dir.is_dir():
        return

    now = time.time()

    for entry in debates_dir.iterdir():
        try:
            if not entry.is_dir():
                continue
            # Match ONLY our generated dir names (run-* or a YYYYMMDD-HHMMSS
            # timestamp prefix) — NOT any dir that merely contains a hyphen.
            if not _DEBATE_DIR_RE.match(entry.name):
                continue
            age = now - entry.stat().st_mtime  # OSError raised here if stat fails
        except OSError:
            continue  # Skip entries whose stat() raises (e.g. dangling symlink)
        # A completed debate (has synthesis.md) is a decision record — KEEP it
        # regardless of age. Only sweep stale, incomplete/abandoned scaffolds.
        if (entry / "synthesis.md").exists():
            continue
        if age > STALE_INACTIVE_SECONDS:
            shutil.rmtree(str(entry), ignore_errors=True)


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
    """Detect project tech stack and return skill trigger hints.

    A single BOUNDED ``os.walk`` pass (pruned artifact dirs + an entry cap +
    early-exit once all extensions are seen) replaces three unbounded recursive
    globs, so a huge tree can't blow the 10s SessionStart hook budget. The hint
    output is behavior-identical to the old glob logic.
    """
    hints = []

    # Kotlin (.kt / .kts) and Gradle Kotlin DSL (.gradle.kts, which ALSO matches
    # **/*.kts) — detected in one walk. Exposed/Trino stay a root build-file read.
    found_kt = found_kts = found_gradle_kts = False
    visited = 0
    capped = False
    for root, dirnames, filenames in os.walk(str(directory)):
        # Prune in-place so os.walk never descends into artifact/vendor dirs.
        dirnames[:] = [d for d in dirnames if d not in STACK_PRUNE_DIRS]
        for name in filenames:
            # Check the cap INSIDE the file loop so one pathological directory
            # can't be fully iterated before the ceiling fires.
            if visited >= STACK_WALK_MAX_ENTRIES:
                capped = True
                break
            visited += 1
            if name.endswith(".gradle.kts"):
                found_gradle_kts = True
                found_kts = True  # .gradle.kts also matched the old **/*.kts glob
            elif name.endswith(".kts"):
                found_kts = True
            elif name.endswith(".kt"):
                found_kt = True
            if found_kt and found_kts and found_gradle_kts:
                break
        if capped or (found_kt and found_kts and found_gradle_kts):
            break

    if found_kt or found_kts:
        hints.append("Kotlin")
    if found_gradle_kts:
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
    """Build plugin integration guidance for session context.

    Output is gated by CREW_VERBOSE (see ``is_verbose``): the default is a TERSE
    one-liner for skills + agents; the FULL bulleted block is emitted only when
    CREW_VERBOSE is set. Stack guidance (when a stack is detected) is always
    wrapped in a high-salience ``<system-reminder>`` and, for Kotlin, prepends a
    REQUIRED instruction to load the LSP before reading/editing code.
    """
    lines = []
    verbose = is_verbose()

    # Map stack hints → relevant skills (in priority order)
    relevant_skills: list[str] = []
    if "Kotlin" in stack_hints:
        relevant_skills.append("sk:kotlin")
        relevant_skills.append("sk:kotlin-testing")
    if "Exposed" in stack_hints:
        relevant_skills.append("sk:exposed")
    if "Gradle" in stack_hints:
        relevant_skills.append("sk:gradle")
    if "Trino" in stack_hints:
        relevant_skills.append("sk:trino")

    # LSP is currently configured for Kotlin only — load it whenever Kotlin is present
    needs_lsp = "Kotlin" in stack_hints

    if stack_hints:
        # Always wrap stack guidance in a system-reminder for high salience.
        # Terse vs verbose only changes how chatty the bullets are.
        lines.append("<system-reminder>")
        lines.append(f"Project stack detected: {', '.join(stack_hints)}.")
        lines.append("")

        if needs_lsp:
            lines.append(
                "**REQUIRED first action**: Load the Kotlin LSP by calling "
                "`ToolSearch(query='select:LSP')` BEFORE reading or editing any code. "
                "Prefer LSP operations (goToDefinition, findReferences, hover, documentSymbol) "
                "over Read+grep for navigating Kotlin code."
            )
            lines.append("")

        if relevant_skills:
            if verbose:
                lines.append("**Skills available for this stack** — invoke `Skill(<name>)` BEFORE writing code in that domain:")
                if "Kotlin" in stack_hints:
                    lines.append("- `sk:kotlin` — data classes, coroutines, null safety, extension functions")
                    lines.append("- `sk:kotlin-testing` — Kotest, MockK, TestIdProvider, DatabaseTest")
                if "Exposed" in stack_hints:
                    lines.append("- `sk:exposed` — repositories, transactions, table definitions")
                if "Gradle" in stack_hints:
                    lines.append("- `sk:gradle` — build config, dependencies, version catalogs")
                if "Trino" in stack_hints:
                    lines.append("- `sk:trino` — analytics queries, query routing")
                lines.append("- `sk:git` — commits, rebasing, history (always available)")
            else:
                skill_list = ", ".join(relevant_skills)
                lines.append(f"**Skills available**: {skill_list}")
                lines.append(
                    "Invoke the matching `Skill(<name>)` BEFORE writing code in that domain (lazy-load)."
                )
            lines.append(
                "When you invoke a skill, prefix your NEXT user-facing message with "
                "`[skill: <name>]` so the user can see it loaded."
            )
            lines.append("")

        # Confirmation line — gives the user in-session visibility that the hook fired
        confirm_parts: list[str] = []
        if needs_lsp:
            confirm_parts.append("Kotlin LSP available")
        if relevant_skills:
            confirm_parts.append(f"skills ready: {', '.join(relevant_skills)}")
        confirm_text = " · ".join(confirm_parts) if confirm_parts else "stack detected"

        lines.append(
            "**In your first reply this session**, include a single status line "
            f"(after any leading thought, before substantive content): `🔧 [crew] {confirm_text}`"
        )
        lines.append("</system-reminder>")

    if verbose:
        lines.extend([
            "",
            "<system-reminder>",
            "**Crew agents** for specialized work:",
            "- Analysis/debugging/planning: `advisor` agent",
            "- Implementation: `executor` agent",
            "- Documentation: `document-writer` agent",
            "- Codebase search: `reader` agent",
            "</system-reminder>",
        ])
    else:
        lines.extend([
            "",
            "Crew agents (via Task tool): advisor, executor, document-writer, reader.",
        ])

    return "\n".join(lines)


def loop_budget_line(data: dict) -> str:
    """The restored loop's REAL budget: the two hook-owned bounds that can end it.

    A resumed loop keeps the fires and the wall clock it accumulated, so the
    banner reports those rather than a counter frozen at init. `elapsed` is `?`
    when `started_at` is absent or unparseable (a legacy file the Stop hook has
    not stamped yet).
    """
    minutes = elapsed_minutes(data.get("started_at", ""))
    elapsed = f"{minutes:.0f}" if minutes is not None else "?"
    deadline = data.get("deadline_minutes", DEFAULT_DEADLINE_MINUTES)
    fires = data.get("stop_fires", 0)
    max_fires = data.get("max_stop_fires", DEFAULT_MAX_STOP_FIRES)
    return f"Budget: stop fires {fires}/{max_fires} · elapsed {elapsed}/{deadline} min"


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
            f"Pass this session ID when invoking crew state … commands: --session-id {session_id}\n"
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
                # Classify via the shared reader so a corrupt or NEWER-schema
                # file is never reported as "Active" (the Stop hook refuses to
                # honor those — surfacing a phantom active loop here would
                # contradict it). Only LOAD_OK files are inspected.
                data, load_status = read_state_json(json_file)
                if load_status != LOAD_OK or data is None:
                    continue
                if not data.get("active", False):
                    continue

                file_session = data.get("session_id", "")
                is_this_session = (
                    not session_id  # No session_id means treat all as current
                    or file_session == session_id
                    or file_session == ""  # Pre-migration files belong to current session
                )

                # Build status line
                if json_file.name.startswith("build-state"):
                    prompt = data.get("prompt", "")
                    if is_this_session:
                        this_session_loops.append(
                            f"[Build Loop Active]\n"
                            f"{loop_budget_line(data)}\n"
                            f"Task: {prompt}\n"
                            f"Continue until the multi-model panel approves completion."
                        )
                    else:
                        other_session_loops.append(
                            f"Build loop (session {file_session[:8]}...): {prompt[:50]}"
                        )
                elif json_file.name.startswith("measure-twice-state"):
                    task = data.get("task_description", "")
                    plan = data.get("plan_file", "")
                    if is_this_session:
                        this_session_loops.append(
                            f"[Measure-Twice Loop Active]\n"
                            f"{loop_budget_line(data)}\n"
                            f"Task: {task}\n"
                            f"Plan: {plan}\n"
                            f"Continue until the panel approves the plan."
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
    cleanup_stale_debate_dirs(directory / ".crew")

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
    try:
        main()
    except Exception as exc:  # noqa: BLE001. Hooks are fail-open;
        # make the failure LOUD (stderr + systemMessage), never silent.
        _diag = (
            "[crew] SessionStart hook crashed (%s: %s) — continuing without "
            "state restore/cleanup for this session."
            % (exc.__class__.__name__, exc)
        )
        # models is importable here (top-of-module import succeeded before
        # main() ran). SessionStart's documented context channel is
        # hookSpecificOutput.additionalContext (systemMessage is honored on the
        # Stop allow-side, not here), so emit the diagnostic the same way this
        # hook emits its normal payload, plus stderr.
        print(SessionStartResult.with_context(_diag).to_json())
        print(_diag, file=sys.stderr)
        sys.exit(0)
