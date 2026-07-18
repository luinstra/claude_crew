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
import time
from pathlib import Path

import artifact_prune
from models import (
    SessionStartInput,
    SessionStartResult,
    coalesce_task,
    override_completion_note,
    read_hook_input,
    effective_count,
    effective_deadline,
    NO_DEADLINE,
    elapsed_minutes,
    get_file_age_days,
    count_incomplete_todos,
    is_verbose,
    read_state_json,
    DEFAULT_DEADLINE_MINUTES,
    DEFAULT_MAX_STOP_FIRES,
    LOAD_FUTURE_SCHEMA,
    LOAD_OK,
)
from state_discovery import is_loop_state_file, is_active_state_file, is_active_value


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
    #
    # The `.lock` globs match the sibling lock files `models.state_lock` creates
    # next to each state file (`<state-filename>.lock`), already crew-anchored by the
    # state filename, and swept only past MAX_AGE_DAYS. A lock in use is kept young
    # (the acquire touches its mtime), so this only reaps locks whose loop is long
    # gone: deleting one mid-use would split that loop's writers across two inodes.
    # Same exact + `-<id>` anchoring as the rest, never a bare `*.lock`.
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
        "build-state.json.lock",
        "build-state-*.json.lock",
        "measure-twice-state.json.lock",
        "measure-twice-state-*.json.lock",
    ]

    now = time.time()

    # Clean up loop state files (both legacy and session-scoped)
    for json_file in directory.glob("*.json"):
        try:
            if is_loop_state_file(json_file.name):
                age = now - json_file.stat().st_mtime
                _, status = read_state_json(json_file)
                if status == LOAD_FUTURE_SCHEMA:
                    # Refuse to touch a newer-schema file, the same contract every
                    # other mutating path honors: deleting is the most destructive
                    # touch there is, and a downgrade must never destroy a live
                    # loop it cannot even read. Note it only when it WOULD have
                    # been swept, so a fresh newer-format loop stays quiet.
                    limit = (MAX_AGE_SECONDS if is_active_state_file(json_file)
                             else STALE_INACTIVE_SECONDS)
                    if age > limit:
                        print(
                            f"[crew] keeping {json_file.name}: written by a newer "
                            f"crew version (state schema ahead of this install); "
                            f"not sweeping it.",
                            file=sys.stderr,
                        )
                elif is_active_state_file(json_file):
                    # Active but very old (>7 days) — force-deactivate and delete
                    if age > MAX_AGE_SECONDS:
                        json_file.unlink()
                else:
                    # Inactive (incl. corrupt/unreadable): delete if older than 1 day
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


# `crew signal` mints EXACTLY `<tag>.json` with the tag in [A-Za-z0-9_-], so the
# marker name grammar is this sweep's anchor: a bare `*.json` would reap whatever
# else was parked in the dir.
_SIGNAL_MARKER_RE = re.compile(r"^[A-Za-z0-9_-]+\.json$")

REVIEWS_SUBDIR = "reviews"
SIGNALS_SUBDIR = "signals"


def _sweep_signal_markers(session_dir: Path, now: float) -> None:
    """Reap aged agent signal markers under ``<session>/signals/``.

    Nothing else prunes them (``wait`` reads and LEAVES a marker so the report
    stays readable at the path it printed), so they accumulate forever. The age
    rule needs no liveness check of its own: a wait blocks for minutes, so a
    marker this old cannot be one a live wait is about to read, and an
    orchestrator reusing a tag clears it before dispatch regardless.
    """
    # own_dir (is_dir AND not is_symlink), never a plain is_dir: this sweep
    # unlink()s under signals/ UNATTENDED every session start, so a symlinked
    # signals/ segment would let it delete normally-named JSON at the link target,
    # OUTSIDE the project. A symlinked segment means this is not the tree crew
    # created, so skip it (leak, never delete-through).
    signals_dir = session_dir / SIGNALS_SUBDIR
    if not artifact_prune.own_dir(signals_dir):
        return
    for marker in signals_dir.glob("*.json"):
        try:
            if not marker.is_file() or not _SIGNAL_MARKER_RE.match(marker.name):
                continue
            if now - marker.stat().st_mtime > MAX_AGE_SECONDS:
                marker.unlink()
        except OSError:
            continue


def sweep_signal_markers_all(crew_dir: Path | None = None) -> None:
    """Reap aged agent signal markers across every session under ``.crew/reviews/``.

    Signal markers are ephemeral bookkeeping (the same sanctioned auto-clean class
    as state files): nothing else prunes them, so ``session-start`` still reaps the
    aged ones. This function is signal-sweeping ONLY. It deletes NO review-run dir:
    that destructive venue moved to the attended ``crew swab`` command, and
    ``report_stale_artifacts`` merely reports the orphans it would remove.

    The `.crew` root defaults to CWD-RELATIVE (`Path(".crew")`), matching `crew
    swab` and the engine's review/signal WRITERS (which resolve `_REVIEWS_BASE =
    ".crew/reviews"` cwd-relative and never consult CLAUDE_PROJECT_DIR). A
    CLAUDE_PROJECT_DIR root would make this sweep look where no markers were
    written when cwd != CLAUDE_PROJECT_DIR. An explicit `crew_dir` is honored (the
    tests pass one).
    """
    if crew_dir is None:
        crew_dir = Path(".crew")
    # own_dir at EVERY directory segment, `.crew` root FIRST (then reviews root,
    # each session dir, and the signals dir inside _sweep_signal_markers): a
    # symlinked segment would redirect this unattended unlink() sweep to delete
    # files outside the project. is_dir() follows symlinks and would delete-through;
    # own_dir refuses the link. Guarding reviews_root alone leaves the one-level-up
    # gap: a symlinked `.crew` still resolves to a real reviews/ at the link target,
    # so reviews_root would pass and aged external JSON would be unlinked.
    if not artifact_prune.own_dir(crew_dir):
        return
    reviews_root = crew_dir / REVIEWS_SUBDIR
    if not artifact_prune.own_dir(reviews_root):
        return

    now = time.time()
    for session_dir in reviews_root.iterdir():
        if not artifact_prune.own_dir(session_dir):
            continue
        _sweep_signal_markers(session_dir, now)


def report_stale_artifacts(crew_dir: Path | None = None) -> str:
    """RETURN a one-line COUNT-ONLY notice about stale review-run + debate orphans, or "".

    Destructive rmtree of the user's disk from the unattended session-start hook
    is the wrong venue: an unattended sweep has no human reading the list before
    it deletes. This is the report-only replacement: it reuses the ONE shared
    enumerator every safety rule already lives in
    (`artifact_prune.collect_prunable`, the same list `crew swab` acts on) and
    returns a single terse line pointing the human at `crew swab`, which OWNS the
    deletion. Returning (not printing) lets `main()` ride the notice on
    the SessionStart `additionalContext` channel, the one carrier a successful
    hook actually delivers to the user/Claude, rather than stderr (which no one
    reliably sees). Empty string when there are no candidates.

    It enumerates with `with_sizes=False`, so the notice carries a COUNT but NO
    byte total: exact per-dir sizing is an unbounded recursive walk that belongs to
    the ATTENDED `crew swab` (where the human waits), not to every session start,
    where a big stale tree could slow the hook and suppress restored context. The
    count stays ACCURATE (every validation runs; only the sizing walk is skipped).

    The `.crew` root defaults to CWD-RELATIVE (`Path(".crew")`), matching `crew
    swab` and the engine writers (cwd-relative `_REVIEWS_BASE`, never
    CLAUDE_PROJECT_DIR): the whole point of this report is to recommend `crew
    swab`, so the two MUST enumerate the SAME tree, or a report naming artifacts
    swab won't touch is worse than none. An explicit `crew_dir` is honored (the
    tests pass one).
    """
    if crew_dir is None:
        crew_dir = Path(".crew")
    items = artifact_prune.collect_prunable(crew_dir, time.time(), with_sizes=False)
    if not items:
        return ""
    return (
        f"[crew] {len(items)} stale crew artifact(s) under .crew/; "
        f"run `crew swab` to review or remove them."
    )


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
    not stamped yet). The bounds run through the SAME coercions the Stop hook
    enforces with, so the banner can never advertise a bound the hook won't honor.
    """
    minutes = elapsed_minutes(data.get("started_at", ""))
    elapsed = f"{minutes:.0f}" if minutes is not None else "?"
    deadline = effective_deadline(
        data.get("deadline_minutes", DEFAULT_DEADLINE_MINUTES),
        opted_out=data.get("no_deadline") is True)
    limit = "none" if deadline == NO_DEADLINE else str(deadline)
    fires = effective_count(data.get("stop_fires", 0), 0)
    max_fires = effective_count(
        data.get("max_stop_fires", DEFAULT_MAX_STOP_FIRES), DEFAULT_MAX_STOP_FIRES)
    return f"Budget: stop fires {fires}/{max_fires} · elapsed {elapsed}/{limit} min"


def loop_next_step(data: dict, ongoing: str, loop: str) -> str:
    """What the restored loop's NEXT move is, which depends on its phase.

    From `done` a completing verdict (APPROVED, or REVISE --minor-only) is
    already on record and both review verbs refuse, so the only move left is
    `deactivate`: telling that loop to keep working toward panel approval strands
    it, since the Stop hook goes on blocking while every verb it was pointed at
    says no. Phase is read defensively (a garbage value falls back to the ongoing
    guidance, never raises).

    `loop` is the caller's alias (`bl`/`mt`), which `deactivate` requires: a bare
    `crew state deactivate` just errors on the missing positional, so the one step
    the guidance names has to be the runnable command.
    """
    if (data.get("phase") or "drafting") == "done":
        note = override_completion_note(data.get("last_verdict_overrides"))
        if note:
            # A forced completion is NOT a clean sign-off: say what it was
            # recorded over, so the banner never launders a --force into approval.
            return (
                f"This completion was {note}: the verdict is recorded and the "
                f"only step left is `crew state deactivate {loop}`, then summarize."
            )
        return (
            "The panel signed off on this (APPROVED, or REVISE with nothing "
            "blocking): the verdict is recorded and the only step left is "
            f"`crew state deactivate {loop}`, then summarize."
        )
    return ongoing


def orphan_loop_line(label: str, loop: str, file_session: str, task: str, age_days: int) -> str:
    """One entry per ACTIVE loop owned by a DIFFERENT session, with the exact
    command that ends it.

    A resumed session gets a NEW id, so a loop started before the resume is
    invisible to this session's banner and unreachable by a bare `deactivate`,
    while its Stop hook goes on blocking the session that owns it. Adopting it
    here would be a guess (that session may be genuinely alive in another
    terminal), so this only REPORTS and hands over the command: the human
    decides. `--cancel` is the sanctioned way to end a loop with no verdict, and
    the line carries the two things the reader cannot derive, the loop positional
    (a bare `deactivate` just errors on the missing argument) and the OLD
    session's full id (the banner's 8-char prefix elsewhere is display-only).
    """
    return (
        f"- {label} (session {file_session}, {age_days}d old): {task}\n"
        f'  End it if it is dead: "${{CLAUDE_PLUGIN_ROOT}}/crew" state deactivate '
        f'{loop} --session-id {file_session} --cancel --reason "orphaned session"'
    )


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
                if not is_active_value(data.get("active", False)):
                    continue

                file_session = data.get("session_id", "")
                is_this_session = (
                    not session_id  # No session_id means treat all as current
                    or file_session == session_id
                    or file_session == ""  # Pre-migration files belong to current session
                )

                # Build status line
                if json_file.name.startswith("build-state"):
                    prompt = coalesce_task(data)
                    if is_this_session:
                        this_session_loops.append(
                            f"[Build Loop Active]\n"
                            f"{loop_budget_line(data)}\n"
                            f"Task: {prompt}\n"
                            + loop_next_step(
                                data,
                                "Continue until the multi-model panel approves completion.",
                                "bl")
                        )
                    else:
                        other_session_loops.append(
                            orphan_loop_line(
                                "Build loop", "bl", file_session, prompt[:60],
                                get_file_age_days(json_file))
                        )
                elif json_file.name.startswith("measure-twice-state"):
                    task = coalesce_task(data)
                    plan = data.get("plan_file", "")
                    if is_this_session:
                        this_session_loops.append(
                            f"[Measure-Twice Loop Active]\n"
                            f"{loop_budget_line(data)}\n"
                            f"Task: {task}\n"
                            f"Plan: {plan}\n"
                            + loop_next_step(
                                data, "Continue until the panel approves the plan.", "mt")
                        )
                    else:
                        other_session_loops.append(
                            orphan_loop_line(
                                "Measure-twice loop", "mt", file_session, task[:60],
                                get_file_age_days(json_file))
                        )
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue

    messages.extend(this_session_loops)

    if other_session_loops:
        messages.append(
            "[Other Sessions]\n"
            "Active loops this session does not own. Nothing was adopted or "
            "cancelled: they may be live in another terminal, or orphaned by a "
            "/resume (which hands the session a new id, leaving the old id's "
            "loop blocking on its own Stop hook). Ask the user before running "
            "any line below.\n"
            + "\n".join(other_session_loops)
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
    # State-file/todo/snapshot sweeps stay on the CLAUDE_PROJECT_DIR root (state
    # files really are resolved via CLAUDE_PROJECT_DIR by crew-state/models). The
    # review-run/debate REPORT and the signal-marker sweep resolve `.crew`
    # cwd-relative instead, to match `crew swab` and the cwd-relative engine
    # writers (default arg = Path(".crew")); pinning them to CLAUDE_PROJECT_DIR
    # would diverge from the command the report points the human at.
    sweep_signal_markers_all()
    swab_notice = report_stale_artifacts()

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
    # The swab notice rides the same additionalContext channel as the rest of the
    # session context (a successful hook's only reliably-delivered carrier).
    if swab_notice:
        context_parts.append(swab_notice)

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
