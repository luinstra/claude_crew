#!/usr/bin/env python3
"""
Test script for claude-crew hooks
Run from project root: python3 plugins/crew/scripts/tests/test-hooks.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
NC = "\033[0m"  # No Color

PASS_COUNT = 0
FAIL_COUNT = 0

# This test lives in scripts/tests/; the hook source it imports/execs sits in
# scripts/ = parent.parent (PROJECT_DIR = plugins/crew = one level above that).
SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPT_DIR.parent

# The importlib-loaded hook modules (e.g. session-start.py) do a bare
# `from models import ...` with no sys.path guard of their own. Pre-move this
# test sat in scripts/, so scripts/ was sys.path[0] and that import resolved;
# now that the test lives one level deeper, put scripts/ back on the path.
sys.path.insert(0, str(SCRIPT_DIR))


def log_pass(name: str) -> None:
    global PASS_COUNT
    print(f"{GREEN}✓ PASS{NC}: {name}")
    PASS_COUNT += 1


def log_fail(name: str, expected: str, got: str) -> None:
    global FAIL_COUNT
    print(f"{RED}✗ FAIL{NC}: {name}")
    print(f"  Expected: {expected}")
    print(f"  Got: {got}")
    FAIL_COUNT += 1


def log_section(name: str) -> None:
    print()
    print(f"{YELLOW}=== {name} ==={NC}")


# One empty dir shared by every subprocess env this suite builds: HOME is ALWAYS
# neutralized to it so the scripts under test can never read from — or clean up! —
# the developer's real home (session-start.py runs stale-file cleanup over
# ~/.claude/todos and counts real pending todos into its status messages).
# Tests that need a populated HOME override env["HOME"] after building the env.
_NEUTRAL_HOME = tempfile.mkdtemp(prefix="crew-test-neutral-home-")


def _neutral_env() -> dict:
    env = dict(os.environ)
    env["HOME"] = _NEUTRAL_HOME
    return env


def run_script(script: Path, input_data: str, extra_env: dict = None) -> str:
    """Run a Python script with input and return output."""
    env = _neutral_env()
    # Strip CREW_VERBOSE by default so tests run terse unless specified
    env.pop("CREW_VERBOSE", None)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(script)],
        input=input_data,
        capture_output=True,
        text=True,
        cwd=SCRIPT_DIR,  # Run from scripts dir so imports work
        env=env,
    )
    return result.stdout.strip()


def run_script_verbose(script: Path, input_data: str) -> str:
    """Run a Python script with CREW_VERBOSE=1."""
    return run_script(script, input_data, extra_env={"CREW_VERBOSE": "1"})


def test_equals(name: str, script: Path, input_data: str, expected: str) -> None:
    """Test that script output equals expected."""
    output = run_script(script, input_data)
    if output == expected:
        log_pass(name)
    else:
        log_fail(name, expected, output)


def test_contains(name: str, script: Path, input_data: str, expected: str) -> None:
    """Test that script output contains expected substring."""
    output = run_script(script, input_data)
    if expected in output:
        log_pass(name)
    else:
        log_fail(name, f"contains '{expected}'", output)


def test_not_contains(name: str, script: Path, input_data: str, not_expected: str) -> None:
    """Test that script output does NOT contain a substring."""
    output = run_script(script, input_data)
    if not_expected not in output:
        log_pass(name)
    else:
        log_fail(name, f"does NOT contain '{not_expected}'", f"found in: {output[:200]}")


def test_contains_verbose(name: str, script: Path, input_data: str, expected: str) -> None:
    """CREW_VERBOSE variant of test_contains.

    The nudge's full recipe (and the copy-pasteable commands inside it) renders
    ONLY under CREW_VERBOSE, so every assertion about the recipe's command text
    must run verbose or it is asserting against the terse one-liner.
    """
    output = run_script_verbose(script, input_data)
    if expected in output:
        log_pass(name)
    else:
        log_fail(name, f"contains '{expected}'", output)


def test_not_contains_verbose(name: str, script: Path, input_data: str, not_expected: str) -> None:
    """CREW_VERBOSE variant of test_not_contains (see test_contains_verbose)."""
    output = run_script_verbose(script, input_data)
    if not_expected not in output:
        log_pass(name)
    else:
        log_fail(name, f"does NOT contain '{not_expected}'", f"found in: {output[:200]}")


def main():
    # =========================================================================
    # MODELS: HookResult.to_json() shape contract (direct unit assertions of
    # the three shipped shapes)
    # =========================================================================
    log_section("models.HookResult.to_json() shapes")
    from models import HookResult

    _allow_json = HookResult.allow().to_json()
    if _allow_json == "{}":
        log_pass("HookResult.allow() -> {}")
    else:
        log_fail("HookResult.allow() -> {}", "{}", _allow_json)

    _diag_json = HookResult.allow_with_diagnostic("hook degraded").to_json()
    if json.loads(_diag_json) == {"systemMessage": "hook degraded"}:
        log_pass('HookResult.allow_with_diagnostic() -> {"systemMessage": ...}')
    else:
        log_fail('HookResult.allow_with_diagnostic() -> {"systemMessage": ...}',
                 '{"systemMessage": "hook degraded"}', _diag_json)

    _block_json = HookResult.block("Must continue").to_json()
    if json.loads(_block_json) == {"decision": "block", "reason": "Must continue"}:
        log_pass('HookResult.block() -> {"decision": "block", "reason": ...}')
    else:
        log_fail('HookResult.block() -> {"decision": "block", "reason": ...}',
                 '{"decision": "block", "reason": "Must continue"}', _block_json)

    # SessionStart uses its OWN diagnostic/context channel —
    # hookSpecificOutput.additionalContext — NOT systemMessage. This is the
    # channel the 3.10+ crash handler (session-start.py bottom) emits on; the
    # 3.9 pre-import version-guard fallback emits systemMessage instead only
    # because it's stdlib-only and can't build this object.
    from models import SessionStartResult
    _ctx_json = SessionStartResult.with_context("restored context").to_json()
    if json.loads(_ctx_json) == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "restored context",
        }
    }:
        log_pass('SessionStartResult.with_context() -> {"hookSpecificOutput": {...additionalContext...}}')
    else:
        log_fail('SessionStartResult.with_context() -> {"hookSpecificOutput": {...additionalContext...}}',
                 '{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "restored context"}}',
                 _ctx_json)

    # =========================================================================
    # MODELS: StopInput parses the live Stop payload's parking signal.
    # `background_tasks` is the ONE honest signal separating "parked, waiting on
    # work I launched" from "trying to quit". The hook's allow/block decision
    # rides on it, so parse it at the unit level too, not just through the hook.
    # =========================================================================
    log_section("models.StopInput (background_tasks / stop_hook_active)")
    from models import StopInput

    _parked_in = StopInput.from_dict({
        "directory": "/tmp/crew-x",
        "session_id": "s1",
        "background_tasks": [{"id": "bash-1", "status": "running"}],
        "stop_hook_active": True,
    })
    if (_parked_in.background_tasks == [{"id": "bash-1", "status": "running"}]
            and _parked_in.stop_hook_active is True
            and _parked_in.is_parked is True):
        log_pass("StopInput.from_dict: parses background_tasks + stop_hook_active, is_parked True")
    else:
        log_fail("StopInput.from_dict: parses background_tasks + stop_hook_active, is_parked True",
                 "one task, stop_hook_active=True, is_parked=True",
                 f"tasks={_parked_in.background_tasks!r}, "
                 f"stop_hook_active={_parked_in.stop_hook_active!r}, "
                 f"is_parked={_parked_in.is_parked!r}")

    _bare_in = StopInput.from_dict({"directory": "/tmp/crew-x"})
    if (_bare_in.background_tasks == []
            and _bare_in.stop_hook_active is False
            and _bare_in.is_parked is False):
        log_pass("StopInput.from_dict: missing keys default to [] / False, is_parked False")
    else:
        log_fail("StopInput.from_dict: missing keys default to [] / False, is_parked False",
                 "tasks=[], stop_hook_active=False, is_parked=False",
                 f"tasks={_bare_in.background_tasks!r}, "
                 f"stop_hook_active={_bare_in.stop_hook_active!r}, "
                 f"is_parked={_bare_in.is_parked!r}")

    _empty_in = StopInput.from_dict({"directory": "/tmp/crew-x", "background_tasks": []})
    if _empty_in.is_parked is False:
        log_pass("StopInput.is_parked: an EMPTY background_tasks list is not parked")
    else:
        log_fail("StopInput.is_parked: an EMPTY background_tasks list is not parked",
                 "is_parked=False", f"is_parked={_empty_in.is_parked!r}")

    # A non-list from an older or future harness reads as "no signal" rather than
    # crashing the hook or (worse) reading truthy and allowing every Stop.
    for _shape, _bad in (("string", "running"), ("object", {"bash-1": "running"})):
        _bad_in = StopInput.from_dict({"directory": "/tmp/crew-x", "background_tasks": _bad})
        if _bad_in.background_tasks == [] and _bad_in.is_parked is False:
            log_pass(f"StopInput.from_dict: a malformed background_tasks ({_shape}) reads as NOT parked")
        else:
            log_fail(f"StopInput.from_dict: a malformed background_tasks ({_shape}) reads as NOT parked",
                     "tasks=[], is_parked=False",
                     f"tasks={_bad_in.background_tasks!r}, is_parked={_bad_in.is_parked!r}")

    # -------------------------------------------------------------------------
    # is_parked counts only tasks NOT known to be finished. Today's harness
    # prunes finished work from the payload, so the filter is a no-op; it exists
    # because if a harness ever stopped pruning, every Stop after one panel
    # fan-out would read as parked, a parked Stop is ALLOWED, and the
    # panel-approval gate would switch itself off with no error.
    # -------------------------------------------------------------------------
    from models import TERMINAL_TASK_STATUSES, _task_in_flight

    if TERMINAL_TASK_STATUSES == frozenset(
            {"completed", "failed", "killed", "cancelled", "done", "error"}):
        log_pass("TERMINAL_TASK_STATUSES: the six finished statuses, and only those")
    else:
        log_fail("TERMINAL_TASK_STATUSES: the six finished statuses, and only those",
                 "completed/failed/killed/cancelled/done/error",
                 repr(sorted(TERMINAL_TASK_STATUSES)))

    def _live_shell(status):
        """The live Stop payload's shell entry (probed shape)."""
        return {"id": "b06o335yc", "type": "shell", "status": status,
                "description": "run the codex seat", "command": "crew run codex"}

    for _label, _task, _want in (
        ("a running shell", _live_shell("running"), True),
        ("a running subagent",
         {"id": "a41k22xz", "type": "subagent", "status": "running",
          "description": "panel seat: opus"}, True),
        ("a completed shell", _live_shell("completed"), False),
        ("a failed shell", _live_shell("failed"), False),
        ("an UPPERCASE terminal status", _live_shell("COMPLETED"), False),
        ("a whitespace-padded terminal status", _live_shell(" completed "), False),
        # Fail safe: keep waiting on anything not KNOWN to have finished, rather
        # than waving unverified work through.
        ("an entry with no status key",
         {"id": "x1", "type": "shell", "command": "crew run codex"}, True),
        ("a non-string status", _live_shell(5), True),
        ("an unrecognized status", _live_shell("pending"), True),
        ("a non-dict entry", "not-a-dict", True),
    ):
        if _task_in_flight(_task) is _want:
            log_pass(f"_task_in_flight: {_label} -> in_flight={_want}")
        else:
            log_fail(f"_task_in_flight: {_label} -> in_flight={_want}",
                     f"in_flight={_want}", f"in_flight={_task_in_flight(_task)!r}")

    _done_in = StopInput.from_dict({
        "directory": "/tmp/crew-x",
        "background_tasks": [_live_shell("completed")],
    })
    if _done_in.is_parked is False:
        log_pass("StopInput.is_parked: a list of only FINISHED tasks is not parked")
    else:
        log_fail("StopInput.is_parked: a list of only FINISHED tasks is not parked",
                 "is_parked=False", f"is_parked={_done_in.is_parked!r}")

    _mixed_in = StopInput.from_dict({
        "directory": "/tmp/crew-x",
        "background_tasks": [_live_shell("completed"), _live_shell("running")],
    })
    if _mixed_in.is_parked is True:
        log_pass("StopInput.is_parked: ONE live task among finished ones still parks")
    else:
        log_fail("StopInput.is_parked: ONE live task among finished ones still parks",
                 "is_parked=True", f"is_parked={_mixed_in.is_parked!r}")

    _cron_in = StopInput.from_dict({
        "directory": "/tmp/crew-x",
        "background_tasks": [_live_shell("completed")],
        "session_crons": [{"id": "cron-1", "schedule": "*/5 * * * *"}],
    })
    if _cron_in.is_parked is True:
        log_pass("StopInput.is_parked: session_crons parks even with every task finished")
    else:
        log_fail("StopInput.is_parked: session_crons parks even with every task finished",
                 "is_parked=True", f"is_parked={_cron_in.is_parked!r}")

    # Global-state isolation: every subprocess env pins HOME to _NEUTRAL_HOME
    # (see _neutral_env), so the scripts under test can never read from — or
    # clean up — the developer's real ~/.claude. This replaces the old
    # rename-aside of the real ~/.claude/todos, which mutated the real home
    # and stranded it in todos.test-backup if a run crashed mid-way.
    try:
        # Create temp directory for tests
        with tempfile.TemporaryDirectory() as test_dir:
            test_path = Path(test_dir)

            # =========================================================================
            # SESSION START TESTS
            # =========================================================================
            log_section("session-start.py")
            session_start = SCRIPT_DIR / "session-start.py"

            # Test with empty directory (no state files)
            # Now outputs hookSpecificOutput with plugin integration guidance
            test_contains(
                "No state files - outputs plugin guidance",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "hookSpecificOutput",
            )

            test_contains(
                "No state files - includes crew agents guidance",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "advisor",
            )

            # Test with Kotlin file (stack detection)
            (test_path / "Test.kt").write_text("fun main() {}")

            # Terse mode (default): stack guidance wrapped in <system-reminder>
            test_contains(
                "Terse mode, Kotlin stack - stack detection line present",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "Project stack detected: Kotlin",
            )

            # Check sk: skills are referenced in terse hint
            test_contains(
                "Terse mode, Kotlin stack - sk: reference in hint",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "sk:kotlin",
            )

            # LSP load instruction must be present when Kotlin detected
            test_contains(
                "Terse mode, Kotlin stack - LSP load instruction present",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "select:LSP",
            )

            # Confirmation line instruction must be present
            test_contains(
                "Terse mode, Kotlin stack - confirmation line instruction present",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "[crew] Kotlin LSP available",
            )

            # Skill-prefix instruction must be present
            test_contains(
                "Terse mode, Kotlin stack - skill-prefix instruction present",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "[skill:",
            )

            # No legacy <IMPORTANT> tag in either mode (replaced by <system-reminder>)
            test_not_contains(
                "Terse mode, Kotlin stack - no legacy <IMPORTANT> tag",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "<IMPORTANT>",
            )

            # Stack guidance is now wrapped in <system-reminder> (was <IMPORTANT>)
            test_contains(
                "Terse mode, Kotlin stack - stack guidance wrapped in <system-reminder>",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "<system-reminder>",
            )

            # No '## This project uses:' header in terse mode
            test_not_contains(
                "Terse mode, Kotlin stack - no '## This project uses:' header",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "## This project uses:",
            )

            # Verbose mode: <system-reminder> wrap + LSP instruction + skill bullets
            output_verbose = run_script_verbose(session_start, json.dumps({"directory": str(test_path)}))
            if "<system-reminder>" in output_verbose and "select:LSP" in output_verbose:
                log_pass("Verbose mode, Kotlin stack - <system-reminder> + LSP instruction present")
            else:
                log_fail("Verbose mode, Kotlin stack - <system-reminder> + LSP instruction present",
                         "contains <system-reminder> and select:LSP", output_verbose[:400])

            # Verbose mode: skill bullets reference sk: names
            if "sk:kotlin" in output_verbose:
                log_pass("Verbose mode, Kotlin stack - sk:kotlin skill bullet present")
            else:
                log_fail("Verbose mode, Kotlin stack - sk:kotlin skill bullet present",
                         "contains sk:kotlin", output_verbose[:400])

            # Verbose mode: no legacy <IMPORTANT> wrap
            if "<IMPORTANT>" not in output_verbose:
                log_pass("Verbose mode, Kotlin stack - no legacy <IMPORTANT> tag")
            else:
                log_fail("Verbose mode, Kotlin stack - no legacy <IMPORTANT> tag",
                         "does NOT contain <IMPORTANT>", output_verbose[:200])

            # Verbose mode: no '## This project uses:' header
            if "## This project uses:" not in output_verbose:
                log_pass("Verbose mode, Kotlin stack - '## This project uses:' header removed")
            else:
                log_fail("Verbose mode, Kotlin stack - '## This project uses:' header removed",
                         "does NOT contain '## This project uses:'", output_verbose[:200])

            # Clean up kotlin file
            (test_path / "Test.kt").unlink()

            # Terse mode: one-line agent hint (no stack present)
            test_contains(
                "Terse mode - one-line agent hint present",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "advisor, executor, document-writer, reader",
            )

            # Terse mode with no stack: no <system-reminder> block at all
            test_not_contains(
                "Terse mode (no stack) - no <system-reminder> tag",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "<system-reminder>",
            )

            # Verbose mode: full <system-reminder> agent block
            output_v = run_script_verbose(session_start, json.dumps({"directory": str(test_path)}))
            if "<system-reminder>" in output_v:
                log_pass("Verbose mode (no stack) - <system-reminder> agent block present")
            else:
                log_fail("Verbose mode (no stack) - <system-reminder> agent block present",
                         "contains <system-reminder>", output_v[:200])

            # Test with context snapshot
            crew_dir = test_path / ".crew"
            crew_dir.mkdir(parents=True, exist_ok=True)
            (crew_dir / "context-snapshot.md").write_text("# Test snapshot")

            test_contains(
                "Detects context snapshot",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "Context Snapshot Available",
            )

            # Test with build loop state
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Test task"}'
            )

            test_contains(
                "Detects active build loop",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "Build Loop Active",
            )

            # Build loop restore banner: panel wording present in both modes
            test_contains(
                "SessionStart (terse) - panel completion nudge present",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "Continue until the multi-model panel approves completion",
            )
            output_bl_v = run_script_verbose(session_start, json.dumps({"directory": str(test_path)}))
            if "Continue until the multi-model panel approves completion" in output_bl_v:
                log_pass("SessionStart (verbose) - panel completion nudge present")
            else:
                log_fail("SessionStart (verbose) - panel completion nudge present",
                         "contains 'Continue until the multi-model panel approves completion'", output_bl_v[:300])

            # Clean up build loop state for next test
            (crew_dir / "build-state.json").unlink()

            # Test with measure-twice loop state
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design auth system", "plan_file": ".crew/plans/auth-system.md"}'
            )

            test_contains(
                "Detects active measure-twice loop",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "Measure-Twice Loop Active",
            )

            # Measure-twice restore banner: panel wording present
            test_contains(
                "SessionStart (terse) - 'Continue until the panel approves the plan' nudge present",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "Continue until the panel approves the plan",
            )

            # Clean up measure-twice state for next test
            (crew_dir / "measure-twice-state.json").unlink()

            # =========================================================================
            # PERSISTENT MODE TESTS
            # =========================================================================
            log_section("persistent-mode.py")
            persistent_mode = SCRIPT_DIR / "persistent-mode.py"

            # Clean test directory
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # Test with no state - should allow stop
            # Allow shape is the benign {}; asserts nothing about the
            # ambiguous "continue" field.
            test_equals(
                "No state - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{}',
            )

            # Test with active build loop
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "completion_promise": "DONE"}'
            )

            # Block shape is load-bearing: the legacy
            # {"continue": false} was proven INERT (hard-stopped the child
            # session); {"decision": "block", "reason": ...} is the honored,
            # load-bearing schema. Assert EXACTLY the shipped shape.
            test_contains(
                "Active build loop - blocks stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '"decision": "block", "reason":',
            )

            test_contains(
                "Active build loop - names the loop in the nudge banner",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Build Loop",
            )

            # The recipe nudge prescribes the multi-model panel flow, not the
            # retired lone-advisor flow. Every recipe assertion runs VERBOSE:
            # the recipe (and the copy-pasteable commands in it) renders only
            # under CREW_VERBOSE, so a terse assertion would be vacuous.
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "completion_promise": "DONE"}'
            )
            test_contains_verbose(
                "Build loop nudge (verbose) - prescribes panel (review-prep)",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "review-prep",
            )
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "completion_promise": "DONE"}'
            )
            test_not_contains_verbose(
                "Build loop nudge (verbose) - no lone-advisor recipe",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Spawn advisor",
            )

            # The nudge's copy-pasteable command must not end in a bare
            # `--session-id` when session_id is empty, and must render the
            # flag with its value when one is present.
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "completion_promise": "DONE"}'
            )
            test_not_contains_verbose(
                "Build loop nudge (verbose) - no bare --session-id when session_id empty",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "--session-id",
            )
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "completion_promise": "DONE"}'
            )
            test_contains_verbose(
                "Build loop nudge (verbose) - renders --session-id with value",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "s9"}),
                "--session-id s9",
            )
            # A session_id carrying a space/shell metacharacter must be
            # shlex.quote'd in the copy-pasteable nudge (complements the spaced
            # plan-path test) — an unquoted value would split/execute.
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "completion_promise": "DONE"}'
            )
            test_contains_verbose(
                "Build loop nudge (verbose) - shell-quotes session_id with metacharacter",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "s x;rm"}),
                "--session-id 's x;rm'",
            )

            # Test with inactive build loop
            (crew_dir / "build-state.json").write_text(
                '{"active": false, "prompt": "Old task"}'
            )

            test_equals(
                "Inactive build loop - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{}',
            )

            # Clean up build loop state
            (crew_dir / "build-state.json").unlink()

            # Test with active measure-twice loop
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design auth", "plan_file": ".crew/plans/auth.md"}'
            )

            test_contains(
                "Active measure-twice loop - blocks stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '"decision": "block", "reason":',
            )

            test_contains(
                "Active measure-twice loop - names the loop in the nudge banner",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Measure-Twice Loop",
            )

            # The measure-twice recipe nudge prescribes the panel flow, not the
            # retired lone-advisor re-review (verbose: see the build-loop block).
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design auth", "plan_file": ".crew/plans/auth.md"}'
            )
            test_contains_verbose(
                "Measure-twice nudge (verbose) - prescribes panel (review-prep)",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "review-prep",
            )
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design auth", "plan_file": ".crew/plans/auth.md"}'
            )
            test_not_contains_verbose(
                "Measure-twice nudge (verbose) - no lone-advisor recipe",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Spawn advisor",
            )

            # A plan path with spaces (or shell metacharacters) must be
            # shell-quoted in the suggested review-prep command — an unquoted
            # path splits/executes when the orchestrator copies the command.
            (crew_dir / "measure-twice-state.json").write_text(json.dumps({
                "active": True, "task_description": "Design auth",
                "plan_file": ".crew/plans/auth plan.md",
            }))
            test_contains_verbose(
                "Measure-twice nudge (verbose) - shell-quotes spaced plan path",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "review-prep '.crew/plans/auth plan.md'",
            )

            # The measure-twice nudge renders the same shlex.quote'd session_flag
            # as the build loop — a spaced/metacharacter session_id is quoted.
            (crew_dir / "measure-twice-state.json").write_text(json.dumps({
                "active": True, "task_description": "Design auth",
                "plan_file": ".crew/plans/auth.md",
            }))
            test_contains_verbose(
                "Measure-twice nudge (verbose) - shell-quotes session_id with metacharacter",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "s x;rm"}),
                "--session-id 's x;rm'",
            )

            # Test with inactive measure-twice loop
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": false, "task_description": "Old plan"}'
            )

            test_equals(
                "Inactive measure-twice loop - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{}',
            )

            # Test priority: build loop takes precedence over measure-twice
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Build task"}'
            )
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Navel task", "plan_file": ".crew/plans/test.md"}'
            )

            test_contains(
                "Build loop takes priority over measure-twice",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Build Loop",  # Should show build loop, not measure-twice
            )

            # Clean up both state files
            (crew_dir / "build-state.json").unlink()
            (crew_dir / "measure-twice-state.json").unlink()

            # =========================================================================
            # PERSISTENT MODE TERSE/VERBOSE TESTS
            # =========================================================================
            log_section("persistent-mode.py (terse/verbose)")

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # Terse (default): NO recipe, but the two lines that must survive
            # terse mode are present: the wait guard (re-running a panel
            # mid-flight deletes the seats that already landed) and the exit
            # command (the sanctioned way out).
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Fix auth bug"}'
            )
            out_terse_bl = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "verify via the multi-model panel" not in out_terse_bl:
                log_pass("Terse nudge (bl): recipe absent")
            else:
                log_fail("Terse nudge (bl): recipe absent",
                         "does NOT contain 'verify via the multi-model panel'", out_terse_bl[:300])

            if "/crew:cancel-build" in out_terse_bl and "Just wait" in out_terse_bl:
                log_pass("Terse nudge (bl): keeps the wait guard + exit command")
            else:
                log_fail("Terse nudge (bl): keeps the wait guard + exit command",
                         "contains 'Just wait' and '/crew:cancel-build'", out_terse_bl[:300])

            # The banner is JUST the loop name: there is no round counter to
            # print (nothing writes one). The budget line carries the only two
            # bounds that can end the loop.
            if "[Build Loop]" in out_terse_bl and "stop fires: 1/150" in out_terse_bl:
                log_pass("Terse nudge (bl): '[Build Loop]' banner + elapsed/stop-fires budget line")
            else:
                log_fail("Terse nudge (bl): '[Build Loop]' banner + elapsed/stop-fires budget line",
                         "contains '[Build Loop]' and 'stop fires: 1/150'", out_terse_bl[:300])

            # No surface may print a round counter: a counter with no writer is a
            # frozen lie, which is why the field is gone rather than displayed.
            if "Round" not in out_terse_bl:
                log_pass("Terse nudge (bl): no round counter in the banner")
            else:
                log_fail("Terse nudge (bl): no round counter in the banner",
                         "no 'Round' in the nudge", out_terse_bl[:300])

            # A second terse fire bumps stop_fires.
            out_terse_bl2 = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if ("[Build Loop]" in out_terse_bl2 and "stop fires: 2/150" in out_terse_bl2
                    and "Fix auth bug" in out_terse_bl2):
                log_pass("Terse nudge (bl): second fire bumps stop_fires")
            else:
                log_fail("Terse nudge (bl): second fire bumps stop_fires",
                         "contains '[Build Loop]', 'stop fires: 2/150' and task", out_terse_bl2[:300])

            # Verbose: the full recipe renders on ANY fire (there is no
            # first-fire special case anymore).
            out_verbose_bl = run_script_verbose(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "verify via the multi-model panel" in out_verbose_bl:
                log_pass("Verbose nudge (bl): full recipe present on a later fire")
            else:
                log_fail("Verbose nudge (bl): full recipe present on a later fire",
                         "contains 'verify via the multi-model panel'", out_verbose_bl[:300])

            # Fires well under the cap keep nudging: only the hook-owned
            # stop_fires/deadline bounds force-exit (covered in the
            # termination-bounds section below).
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Fix auth bug", "stop_fires": 20}'
            )
            out_under_cap = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "Safety Limit Reached" not in out_under_cap and "stop fires: 21/150" in out_under_cap:
                log_pass("Fires under the stop_fires cap keep nudging")
            else:
                log_fail("Fires under the stop_fires cap keep nudging",
                         "no 'Safety Limit Reached', 'stop fires: 21/150'", out_under_cap[:300])

            # Prompt truncation: 500-char prompt → exactly 120 chars in reason
            long_prompt = "A" * 500
            (crew_dir / "build-state.json").write_text(
                json.dumps({"active": True, "prompt": long_prompt})
            )
            out_trunc = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            reason = json.loads(out_trunc).get("reason", "")
            task_line = [l for l in reason.split("\n") if l.startswith("Task:")]
            if task_line:
                task_value = task_line[0][len("Task: "):]
                if len(task_value) == 120 and task_value.endswith("..."):
                    log_pass("Terse mode + 500-char prompt: task truncated to 120 chars with ellipsis")
                else:
                    log_fail("Terse mode + 500-char prompt: task truncated to 120 chars with ellipsis",
                             "120 chars ending with '...'", f"len={len(task_value)}, ends={task_value[-5:]!r}")
            else:
                log_fail("Terse mode + 500-char prompt: task line present in reason",
                         "Task: ... line", reason[:200])

            # Also verify truncation in verbose mode
            out_trunc_v = run_script_verbose(persistent_mode, json.dumps({"directory": str(test_path)}))
            reason_v = json.loads(out_trunc_v).get("reason", "")
            task_line_v = [l for l in reason_v.split("\n") if l.startswith("Task:")]
            if task_line_v:
                task_value_v = task_line_v[0][len("Task: "):]
                if len(task_value_v) == 120 and task_value_v.endswith("..."):
                    log_pass("Verbose mode + 500-char prompt: task truncated to 120 chars with ellipsis")
                else:
                    log_fail("Verbose mode + 500-char prompt: task truncated to 120 chars with ellipsis",
                             "120 chars ending with '...'", f"len={len(task_value_v)}, ends={task_value_v[-5:]!r}")
            else:
                log_fail("Verbose mode + 500-char prompt: task line present in reason",
                         "Task: ... line", reason_v[:200])

            (crew_dir / "build-state.json").unlink()

            # Measure-twice terse/verbose (same contract as the build loop).
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design profiles", "plan_file": ".crew/plans/profiles.md"}'
            )
            out_terse_mt = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "When APPROVED" not in out_terse_mt:
                log_pass("Terse nudge (mt): recipe absent")
            else:
                log_fail("Terse nudge (mt): recipe absent",
                         "does NOT contain 'When APPROVED'", out_terse_mt[:300])

            if "/crew:cancel-measure-twice" in out_terse_mt and "Just wait" in out_terse_mt:
                log_pass("Terse nudge (mt): keeps the wait guard + exit command")
            else:
                log_fail("Terse nudge (mt): keeps the wait guard + exit command",
                         "contains 'Just wait' and '/crew:cancel-measure-twice'", out_terse_mt[:300])

            if ("[Measure-Twice Loop]" in out_terse_mt and "stop fires: 1/150" in out_terse_mt
                    and "Round" not in out_terse_mt
                    and "Design profiles" in out_terse_mt
                    and ".crew/plans/profiles.md" in out_terse_mt):
                log_pass("Terse nudge (mt): banner (no round counter) + budget line + task/plan")
            else:
                log_fail("Terse nudge (mt): banner (no round counter) + budget line + task/plan",
                         "'[Measure-Twice Loop]', no 'Round', stop fires: 1/150, task and plan present",
                         out_terse_mt[:300])

            out_terse_mt2 = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "[Measure-Twice Loop]" in out_terse_mt2 and "stop fires: 2/150" in out_terse_mt2:
                log_pass("Terse nudge (mt): second fire bumps stop_fires")
            else:
                log_fail("Terse nudge (mt): second fire bumps stop_fires",
                         "contains '[Measure-Twice Loop]' and 'stop fires: 2/150'", out_terse_mt2[:300])

            out_mt_v = run_script_verbose(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "When APPROVED" in out_mt_v:
                log_pass("Verbose nudge (mt): full recipe present")
            else:
                log_fail("Verbose nudge (mt): full recipe present",
                         "contains 'When APPROVED'", out_mt_v[:300])

            (crew_dir / "measure-twice-state.json").unlink()

            # =========================================================================
            # CREW STATE CLI TESTS
            # =========================================================================
            log_section("crew-state.py")
            crew_state = SCRIPT_DIR / "crew-state.py"

            def run_crew_state(args: list[str], cwd: Path) -> tuple[str, str, int]:
                """Run crew-state.py with arguments."""
                result = subprocess.run(
                    [sys.executable, str(crew_state)] + args,
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env={**_neutral_env(), "CLAUDE_PROJECT_DIR": str(cwd)},
                )
                return result.stdout.strip(), result.stderr.strip(), result.returncode

            # Clean up any existing state
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # Test init bl
            stdout, stderr, code = run_crew_state(["init", "bl", "--prompt", "Test task"], test_path)
            if code == 0 and (crew_dir / "build-state.json").exists():
                log_pass("init bl - creates state file")
            else:
                log_fail("init bl - creates state file", "exit 0 and file exists", f"exit {code}, stderr: {stderr}")

            # Test show bl (compact default). The compact line reports the REAL
            # budget (the two bounds that can end the loop), never a round
            # counter: nothing writes one, so it could only report a frozen lie.
            stdout, stderr, code = run_crew_state(["show", "bl"], test_path)
            if (code == 0 and stdout.startswith("loop=bl") and "active=true" in stdout
                    and "fires=0/150" in stdout and "elapsed=" in stdout
                    and "/120min" in stdout and 'task=' in stdout
                    and "iter=" not in stdout):
                log_pass("show bl - compact summary reports fires/elapsed budget, no round counter")
            else:
                log_fail("show bl - compact summary reports fires/elapsed budget, no round counter",
                         "loop=bl active=true fires=0/150 elapsed=N/120min task=…, no iter=", stdout)

            # Compact show: exactly one line
            if code == 0 and len(stdout.strip().splitlines()) == 1:
                log_pass("show bl - output is exactly one line")
            else:
                log_fail("show bl - output is exactly one line", "1 line", f"{len(stdout.strip().splitlines())} lines: {stdout[:200]}")

            # show bl --verbose: JSON output
            stdout_v, _, code_v = run_crew_state(["show", "bl", "--verbose"], test_path)
            if code_v == 0 and '"active": true' in stdout_v and '"prompt": "Test task"' in stdout_v:
                log_pass("show bl --verbose - pretty JSON with expected fields")
            else:
                log_fail("show bl --verbose - pretty JSON with expected fields",
                         "JSON with active and prompt", stdout_v)

            # show bl -v: same as --verbose
            stdout_sv, _, code_sv = run_crew_state(["show", "bl", "-v"], test_path)
            if code_sv == 0 and '"active": true' in stdout_sv:
                log_pass("show bl -v - short flag works same as --verbose")
            else:
                log_fail("show bl -v - short flag works same as --verbose",
                         "JSON with active: true", stdout_sv)

            # Test set bl on an allowlisted field (use --verbose for JSON assertion)
            stdout, stderr, code = run_crew_state(
                ["set", "bl", "completion_promise", "SHIPPED"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl", "--verbose"], test_path)
            if code == 0 and '"completion_promise": "SHIPPED"' in stdout2:
                log_pass("set bl - changes an allowlisted field value")
            else:
                log_fail("set bl - changes an allowlisted field value",
                         'completion_promise: "SHIPPED"', f"exit {code}, {stdout2}")

            # The `increment` verb is GONE, and so is the counter it bumped:
            # argparse must reject the subcommand outright rather than the loop
            # finding a counter it can rewrite.
            before_inc = (crew_dir / "build-state.json").read_bytes()
            stdout, stderr, code = run_crew_state(["increment", "bl", "iteration"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl", "--verbose"], test_path)
            if (code != 0 and "invalid choice" in stderr
                    and (crew_dir / "build-state.json").read_bytes() == before_inc
                    and "iteration" not in stdout2):
                log_pass("increment verb removed - rejected, state byte-unchanged, no iteration field")
            else:
                log_fail("increment verb removed - rejected, state byte-unchanged, no iteration field",
                         "nonzero + 'invalid choice', bytes identical, no iteration in state",
                         f"exit {code}, stderr={stderr[:120]!r}, state={stdout2[:200]}")

            # Test deactivate bl
            stdout, stderr, code = run_crew_state(["deactivate", "bl", "--reason", "Test done"], test_path)
            with open(crew_dir / "build-state.json") as f:
                data = json.load(f)
            if code == 0 and data.get("active") == False and "completed_at" in data and data.get("reason") == "Test done":
                log_pass("deactivate bl - sets active=false and adds metadata")
            else:
                log_fail("deactivate bl - sets active=false and adds metadata", "active=false, completed_at, reason", json.dumps(data))

            # Test init mt (use --verbose for JSON assertion)
            stdout, stderr, code = run_crew_state(["init", "mt", "--task", "Build auth", "--plan-file", ".crew/plans/auth.md"], test_path)
            stdout2, _, _ = run_crew_state(["show", "mt", "--verbose"], test_path)
            if code == 0 and '"task_description": "Build auth"' in stdout2:
                log_pass("init mt - creates measure-twice state")
            else:
                log_fail("init mt - creates measure-twice state", "task_description field", stdout2)

            # Test error: missing --prompt for bl (clean up first to avoid conflict error)
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()
            stdout, stderr, code = run_crew_state(["init", "bl"], test_path)
            if code == 1 and "--prompt or -f required" in stderr:
                log_pass("init bl without --prompt - exits with error")
            else:
                log_fail("init bl without --prompt - exits with error", "exit 1 with error message", f"exit {code}, stderr: {stderr}")

            # --deadline-minutes is the ONLY cost ceiling and `init` is invoked BY
            # the agent, so an out-of-policy bound is rejected at parse time with
            # NO state file created. A negative value used to survive (truthy),
            # land as a negative, and read as "no deadline" in the Stop hook.
            for loop_arg, text_flag, text in (("bl", "--prompt", "bounded"),
                                              ("mt", "--task", "bounded")):
                for bad in ("-1", "0", "241"):
                    for f in crew_dir.glob("*-state*.json"):
                        f.unlink()
                    extra = [] if loop_arg == "bl" else ["--plan-file", ".crew/plans/b.md"]
                    _, stderr, code = run_crew_state(
                        ["init", loop_arg, text_flag, text, "--deadline-minutes", bad] + extra,
                        test_path)
                    created = list(crew_dir.glob("*-state*.json"))
                    if code == 2 and not created and "deadline-minutes" in stderr:
                        log_pass(f"init {loop_arg} --deadline-minutes {bad} - rejected (exit 2), no state file")
                    else:
                        log_fail(f"init {loop_arg} --deadline-minutes {bad} - rejected (exit 2), no state file",
                                 "exit 2, no state file, stderr names the flag",
                                 f"exit {code}, files={[p.name for p in created]}, stderr={stderr[:120]!r}")

                # An in-policy value still writes.
                for f in crew_dir.glob("*-state*.json"):
                    f.unlink()
                extra = [] if loop_arg == "bl" else ["--plan-file", ".crew/plans/b.md"]
                _, stderr, code = run_crew_state(
                    ["init", loop_arg, text_flag, text, "--deadline-minutes", "240"] + extra,
                    test_path)
                stdout_dl, _, _ = run_crew_state(["show", loop_arg, "--verbose"], test_path)
                if code == 0 and '"deadline_minutes": 240' in stdout_dl:
                    log_pass(f"init {loop_arg} --deadline-minutes 240 - accepted at the policy maximum")
                else:
                    log_fail(f"init {loop_arg} --deadline-minutes 240 - accepted at the policy maximum",
                             "exit 0, deadline_minutes=240",
                             f"exit {code}, stderr={stderr[:120]!r}, state={stdout_dl[:160]}")

            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # Test error: invalid field name
            stdout, stderr, code = run_crew_state(["set", "bl", "nonexistent_field", "value"], test_path)
            if code == 1 and "has no field" in stderr:
                log_pass("set invalid field - exits with error")
            else:
                log_fail("set invalid field - exits with error", "exit 1 with error message", f"exit {code}, stderr: {stderr}")

            # Test show on missing file returns default state (compact: active=false)
            (crew_dir / "build-state.json").unlink(missing_ok=True)
            stdout, stderr, code = run_crew_state(["show", "bl"], test_path)
            if code == 0 and "active=false" in stdout and stdout.startswith("loop=bl"):
                log_pass("show bl missing file - returns compact default state with active=false")
            else:
                log_fail("show bl missing file - returns compact default state with active=false",
                         "compact summary with active=false", stdout)

            # show bl missing file + --verbose: JSON active=false
            stdout_mv, _, code_mv = run_crew_state(["show", "bl", "--verbose"], test_path)
            if code_mv == 0 and '"active": false' in stdout_mv:
                log_pass("show bl missing file --verbose - returns default JSON state")
            else:
                log_fail("show bl missing file --verbose - returns default JSON state",
                         '"active": false in JSON', stdout_mv)

            # Test is-active when inactive (no state file)
            (crew_dir / "build-state.json").unlink(missing_ok=True)
            stdout, stderr, code = run_crew_state(["is-active", "bl"], test_path)
            if code == 1:
                log_pass("is-active bl (inactive) - exits with code 1")
            else:
                log_fail("is-active bl (inactive) - exits with code 1", "exit 1", f"exit {code}")

            # Test is-active when active (clean up first to allow init)
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()
            run_crew_state(["init", "bl", "--prompt", "Test"], test_path)
            stdout, stderr, code = run_crew_state(["is-active", "bl"], test_path)
            if code == 0:
                log_pass("is-active bl (active) - exits with code 0")
            else:
                log_fail("is-active bl (active) - exits with code 0", "exit 0", f"exit {code}")

            # Test --auto-plan for mt (clean up all state to avoid conflicts)
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()
            stdout, stderr, code = run_crew_state(["init", "mt", "--task", "Build Auth System", "--auto-plan"], test_path)
            if code == 0 and ".crew/plans/build-auth-system.md" in stdout:
                log_pass("init mt --auto-plan - derives plan filename")
            else:
                log_fail("init mt --auto-plan - derives plan filename", ".crew/plans/build-auth-system.md", stdout)

            # Verify state has the auto-derived plan file (use --verbose for JSON field check)
            stdout2, _, _ = run_crew_state(["show", "mt", "--verbose"], test_path)
            if '"plan_file": ".crew/plans/build-auth-system.md"' in stdout2:
                log_pass("init mt --auto-plan - stores derived path in state")
            else:
                log_fail("init mt --auto-plan - stores derived path in state", "plan_file with derived path", stdout2)

            # show mt compact: contains plan= field
            stdout_mt_c, _, code_mt_c = run_crew_state(["show", "mt"], test_path)
            if code_mt_c == 0 and "plan=" in stdout_mt_c and stdout_mt_c.startswith("loop=mt"):
                log_pass("show mt compact - contains plan= field")
            else:
                log_fail("show mt compact - contains plan= field",
                         "compact line with plan=", stdout_mt_c)

            # show bl with prompt > 80 chars: task field ends with ...
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()
            long_task = "B" * 100
            run_crew_state(["init", "bl", "--prompt", long_task], test_path)
            stdout_long, _, code_long = run_crew_state(["show", "bl"], test_path)
            if code_long == 0 and '...' in stdout_long:
                log_pass("show bl long prompt - task truncated with ellipsis in compact output")
            else:
                log_fail("show bl long prompt - task truncated with ellipsis in compact output",
                         "... in compact output", stdout_long)
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # Test check-conflicts with no active loops
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()
            stdout, stderr, code = run_crew_state(["check-conflicts"], test_path)
            if code == 0:
                log_pass("check-conflicts (no active) - exits with code 0")
            else:
                log_fail("check-conflicts (no active) - exits with code 0", "exit 0", f"exit {code}, stderr: {stderr}")

            # Test check-conflicts with bl active
            run_crew_state(["init", "bl", "--prompt", "Test"], test_path)
            stdout, stderr, code = run_crew_state(["check-conflicts"], test_path)
            if code == 1 and "build loop is already active" in stderr:
                log_pass("check-conflicts (bl active) - exits with error")
            else:
                log_fail("check-conflicts (bl active) - exits with error", "exit 1 with bl error", f"exit {code}, stderr: {stderr}")

            # Test check-conflicts with mt active
            (crew_dir / "build-state.json").unlink(missing_ok=True)
            run_crew_state(["init", "mt", "--task", "Test", "--auto-plan"], test_path)
            stdout, stderr, code = run_crew_state(["check-conflicts"], test_path)
            if code == 1 and "measure-twice loop is already active" in stderr:
                log_pass("check-conflicts (mt active) - exits with error")
            else:
                log_fail("check-conflicts (mt active) - exits with error", "exit 1 with mt error", f"exit {code}, stderr: {stderr}")

            # Test init rejects when another loop is active
            stdout, stderr, code = run_crew_state(["init", "bl", "--prompt", "Should fail"], test_path)
            if code == 1 and "measure-twice loop is already active" in stderr:
                log_pass("init bl (mt active) - rejects with conflict error")
            else:
                log_fail("init bl (mt active) - rejects with conflict error", "exit 1 with conflict", f"exit {code}, stderr: {stderr}")

            # Clean up state files for subsequent tests
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # =========================================================================
            # SESSION-SCOPED CREW STATE TESTS
            # =========================================================================
            log_section("crew-state.py (session-scoped)")

            # Two sessions can each init a build loop
            stdout, stderr, code = run_crew_state(
                ["init", "bl", "--prompt", "Task for s1", "--session-id", "s1"], test_path)
            if code == 0 and (crew_dir / "build-state-s1.json").exists():
                log_pass("session init bl s1 - creates session-scoped file")
            else:
                log_fail("session init bl s1 - creates session-scoped file",
                         "exit 0 and file exists", f"exit {code}, stderr: {stderr}")

            stdout, stderr, code = run_crew_state(
                ["init", "bl", "--prompt", "Task for s2", "--session-id", "s2"], test_path)
            if code == 0 and (crew_dir / "build-state-s2.json").exists():
                log_pass("session init bl s2 - creates second session file")
            else:
                log_fail("session init bl s2 - creates second session file",
                         "exit 0 and file exists", f"exit {code}, stderr: {stderr}")

            # check-conflicts --session-id s1 fails (s1 has active bl)
            stdout, stderr, code = run_crew_state(
                ["check-conflicts", "--session-id", "s1"], test_path)
            if code == 1 and "build loop is already active" in stderr:
                log_pass("check-conflicts s1 - detects own active loop")
            else:
                log_fail("check-conflicts s1 - detects own active loop",
                         "exit 1 with bl error", f"exit {code}, stderr: {stderr}")

            # check-conflicts --session-id s3 succeeds (s3 has no loops)
            stdout, stderr, code = run_crew_state(
                ["check-conflicts", "--session-id", "s3"], test_path)
            if code == 0:
                log_pass("check-conflicts s3 - no conflict for different session")
            else:
                log_fail("check-conflicts s3 - no conflict for different session",
                         "exit 0", f"exit {code}, stderr: {stderr}")

            # show bl --session-id s1 shows s1's state (--verbose for JSON assertion)
            stdout, stderr, code = run_crew_state(
                ["show", "bl", "--session-id", "s1", "--verbose"], test_path)
            if code == 0 and '"prompt": "Task for s1"' in stdout:
                log_pass("show bl s1 - shows session-specific state")
            else:
                log_fail("show bl s1 - shows session-specific state",
                         "prompt: Task for s1", stdout)

            # deactivate bl --session-id s1 only deactivates s1
            stdout, stderr, code = run_crew_state(
                ["deactivate", "bl", "--reason", "done", "--session-id", "s1"], test_path)
            stdout2, _, _ = run_crew_state(
                ["show", "bl", "--session-id", "s2", "--verbose"], test_path)
            if code == 0 and '"active": true' in stdout2:
                log_pass("deactivate s1 - s2 remains active")
            else:
                log_fail("deactivate s1 - s2 remains active",
                         "s2 still active", stdout2)

            # session_id stored in state (--verbose for JSON assertion)
            stdout, stderr, code = run_crew_state(
                ["show", "bl", "--session-id", "s2", "--verbose"], test_path)
            if '"session_id": "s2"' in stdout:
                log_pass("init stores session_id in state JSON")
            else:
                log_fail("init stores session_id in state JSON",
                         'session_id: s2', stdout)

            # CLAUDE_SESSION_ID env var fallback (--verbose for JSON assertion)
            env_with_session = {**_neutral_env(), "CLAUDE_PROJECT_DIR": str(test_path), "CLAUDE_SESSION_ID": "s2"}
            result = subprocess.run(
                [sys.executable, str(crew_state), "show", "bl", "--verbose"],
                capture_output=True, text=True, cwd=test_path, env=env_with_session)
            if result.returncode == 0 and '"prompt": "Task for s2"' in result.stdout:
                log_pass("CLAUDE_SESSION_ID env var fallback works")
            else:
                log_fail("CLAUDE_SESSION_ID env var fallback works",
                         "prompt: Task for s2", result.stdout.strip())

            # Clean up session-scoped files
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # =========================================================================
            # BLOCKING 1: init/deactivate --session-id symmetry (no orphaned loop)
            # =========================================================================
            run_crew_state(
                ["init", "bl", "--prompt", "sym task", "--session-id", "symS"], test_path)
            scoped = crew_dir / "build-state-symS.json"
            active_before = (
                json.loads(scoped.read_text()).get("active") if scoped.exists() else None
            )
            _, _, deact_code = run_crew_state(
                ["deactivate", "bl", "--reason", "done", "--session-id", "symS"], test_path)
            still_active = []
            for f in crew_dir.glob("*-state*.json"):
                try:
                    if json.loads(f.read_text()).get("active"):
                        still_active.append(f.name)
                except (OSError, json.JSONDecodeError):
                    pass
            if deact_code == 0 and active_before is True and not still_active:
                log_pass("init/deactivate --session-id symmetry - scoped loop fully cleared")
            else:
                log_fail(
                    "init/deactivate --session-id symmetry - scoped loop fully cleared",
                    "scoped file inactive, zero active files",
                    f"code={deact_code} active_before={active_before} still_active={still_active}")
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # =========================================================================
            # BLOCKING 2: mutating CLI refuses to touch a future-schema state file
            # =========================================================================
            future_bytes = (
                b'{"active": true, "prompt": "newer", '
                b'"session_id": "futS", "schema": 99}')
            fut = crew_dir / "build-state-futS.json"
            # `set` carries an ALLOWLISTED field here: a refused field would exit
            # nonzero on the allowlist check alone and pass this test without the
            # refuse-to-touch guard ever running.
            for mut in (
                ["init", "bl", "--prompt", "x", "--session-id", "futS"],
                ["set", "bl", "completion_promise", "SHIPPED", "--session-id", "futS"],
                ["deactivate", "bl", "--reason", "x", "--session-id", "futS"],
            ):
                fut.write_bytes(future_bytes)
                _, err, code = run_crew_state(mut, test_path)
                untouched = fut.read_bytes() == future_bytes
                if code != 0 and untouched:
                    log_pass(f"future-schema refuse ({mut[0]}) - nonzero exit, bytes untouched")
                else:
                    log_fail(
                        f"future-schema refuse ({mut[0]}) - nonzero exit, bytes untouched",
                        "nonzero + bytes untouched",
                        f"code={code} untouched={untouched} err={err[:120]}")
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()

            # =========================================================================
            # CLI init over a CORRUPT state file: set aside as .corrupt, start fresh
            # =========================================================================
            corrupt_target = crew_dir / "build-state-corS.json"
            corrupt_target.write_text('{"active": true, "prompt": "broken", trunc')  # invalid JSON
            _, err, code = run_crew_state(
                ["init", "bl", "--prompt", "fresh task", "--session-id", "corS"], test_path)
            aside = crew_dir / "build-state-corS.json.corrupt"
            fresh_ok = False
            if corrupt_target.exists():
                try:
                    with open(corrupt_target) as _f:
                        _d = json.load(_f)
                    fresh_ok = _d.get("active") is True and _d.get("prompt") == "fresh task"
                except (OSError, json.JSONDecodeError):
                    fresh_ok = False
            # Assert the SUCCESS-path wording specifically: "set aside as <name>"
            # plus the .corrupt filename, and NOT the "could not be set aside"
            # failure phrasing (which also contains the substring "set aside").
            err_l = err.lower()
            note_ok = ("set aside as" in err_l
                       and aside.name in err
                       and "could not" not in err_l)
            if code == 0 and aside.exists() and fresh_ok and note_ok:
                log_pass("CLI init over corrupt file - set aside as .corrupt, fresh state written")
            else:
                log_fail("CLI init over corrupt file - set aside as .corrupt, fresh state written",
                         "exit 0, .corrupt exists, fresh active state, 'set aside as <name>' success note",
                         f"code={code} aside={aside.exists()} fresh={fresh_ok} note_ok={note_ok} err={err[:120]}")
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()

            # =========================================================================
            # _resolve_loop_path: the mutating verbs with --session-id against an
            # ADOPTED LEGACY (unsuffixed) state file mutate THAT file in place (via
            # find_session_state_file), never a stray session-scoped path. This
            # covers `set` and `deactivate` (the bug _resolve_loop_path fixed) plus
            # the read verbs (show/is-active must report the adopted file too);
            # the init/deactivate session-id symmetry is covered above.
            # =========================================================================
            legacy = crew_dir / "build-state.json"
            scoped_stray = crew_dir / "build-state-legS.json"
            # session_id:"" → adoptable by any session per find_session_state_file
            legacy.write_text(
                '{"active": true, "prompt": "legacy adopt", '
                '"session_id": ""}'
            )
            _, _, set_code = run_crew_state(
                ["set", "bl", "completion_promise", "SHIPPED", "--session-id", "legS"], test_path)
            legacy_after_set = json.loads(legacy.read_text())
            set_ok = (
                set_code == 0
                and legacy_after_set.get("completion_promise") == "SHIPPED"
                and not scoped_stray.exists()
            )
            if set_ok:
                log_pass("set --session-id over adopted legacy file - mutates legacy in place, no stray scoped file")
            else:
                log_fail(
                    "set --session-id over adopted legacy file - mutates legacy in place, no stray scoped file",
                    "legacy completion_promise=SHIPPED, no build-state-legS.json",
                    f"code={set_code} legacy_promise={legacy_after_set.get('completion_promise')} "
                    f"stray={scoped_stray.exists()}")

            # show/is-active must resolve the SAME adopted file: reporting
            # inactive while Stop still adopts the legacy loop would tell the
            # user there is nothing to cancel when there is.
            show_out, _, show_code = run_crew_state(
                ["show", "bl", "--session-id", "legS"], test_path)
            if show_code == 0 and "active=true" in show_out and "legacy adopt" in show_out:
                log_pass("show --session-id over adopted legacy file - reports the adopted loop")
            else:
                log_fail("show --session-id over adopted legacy file - reports the adopted loop",
                         "active=true with legacy task text",
                         f"code={show_code} out={show_out[:120]}")

            _, _, ia_code = run_crew_state(
                ["is-active", "bl", "--session-id", "legS"], test_path)
            if ia_code == 0:
                log_pass("is-active --session-id over adopted legacy file - exits 0 (active)")
            else:
                log_fail("is-active --session-id over adopted legacy file - exits 0 (active)",
                         "exit 0", f"exit {ia_code}")

            # deactivate resolves the same adopted file (run last: it turns the
            # legacy loop off, which the read verbs above still needed active).
            _, _, deact_leg_code = run_crew_state(
                ["deactivate", "bl", "--reason", "done", "--session-id", "legS"], test_path)
            legacy_after_deact = json.loads(legacy.read_text())
            if (deact_leg_code == 0 and legacy_after_deact.get("active") is False
                    and not scoped_stray.exists()):
                log_pass("deactivate --session-id over adopted legacy file - turns legacy off, no stray scoped file")
            else:
                log_fail(
                    "deactivate --session-id over adopted legacy file - turns legacy off, no stray scoped file",
                    "legacy active=false, no build-state-legS.json",
                    f"code={deact_leg_code} legacy_active={legacy_after_deact.get('active')} "
                    f"stray={scoped_stray.exists()}")
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()

            # =========================================================================
            # check_for_conflicts resolves through find_session_state_file: init with
            # --session-id must refuse while an ADOPTABLE legacy loop is still active.
            # Otherwise a scoped file is created, every later verb prefers it, and the
            # legacy loop is orphaned permanently active. An inactive legacy file must
            # NOT block a fresh scoped init.
            # =========================================================================
            legacy.write_text(
                '{"active": true, "prompt": "legacy adopt", '
                '"session_id": ""}'
            )
            _, err, code = run_crew_state(
                ["init", "bl", "--prompt", "new scoped", "--session-id", "legS"], test_path)
            if code == 1 and "build loop is already active" in err and not scoped_stray.exists():
                log_pass("init --session-id vs active adoptable legacy - refused, no scoped file")
            else:
                log_fail("init --session-id vs active adoptable legacy - refused, no scoped file",
                         "exit 1 with bl conflict, no build-state-legS.json",
                         f"code={code} stray={scoped_stray.exists()} err={err[:120]}")

            legacy.write_text(
                '{"active": false, "prompt": "legacy done", '
                '"session_id": ""}'
            )
            _, err, code = run_crew_state(
                ["init", "bl", "--prompt", "new scoped", "--session-id", "legS"], test_path)
            if code == 0 and scoped_stray.exists():
                log_pass("init --session-id vs inactive legacy - proceeds on scoped path")
            else:
                log_fail("init --session-id vs inactive legacy - proceeds on scoped path",
                         "exit 0 and build-state-legS.json created",
                         f"code={code} scoped={scoped_stray.exists()} err={err[:120]}")
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()

            # An INACTIVE scoped leftover must not hide an ACTIVE adoptable
            # legacy file: the two candidates are checked independently, so a
            # finished scoped loop cannot shadow a live legacy one into orphanhood.
            scoped_stray.write_text(
                '{"active": false, "prompt": "finished scoped", '
                '"session_id": "legS"}'
            )
            legacy.write_text(
                '{"active": true, "prompt": "legacy adopt", '
                '"session_id": ""}'
            )
            _, err, code = run_crew_state(
                ["init", "bl", "--prompt", "new scoped", "--session-id", "legS"], test_path)
            if code == 1 and "build loop is already active" in err:
                log_pass("init --session-id vs active legacy behind stale scoped - refused")
            else:
                log_fail("init --session-id vs active legacy behind stale scoped - refused",
                         "exit 1 with bl conflict",
                         f"code={code} err={err[:120]}")

            # An active legacy file owned by a DIFFERENT session is NOT
            # adoptable and must not block this session's init.
            legacy.write_text(
                '{"active": true, "prompt": "someone else", '
                '"session_id": "otherSession"}'
            )
            _, err, code = run_crew_state(
                ["init", "bl", "--prompt", "new scoped", "--session-id", "legS"], test_path)
            scoped_after = json.loads(scoped_stray.read_text())
            if code == 0 and scoped_after.get("active") is True:
                log_pass("init --session-id vs other-session legacy - proceeds (not adoptable)")
            else:
                log_fail("init --session-id vs other-session legacy - proceeds (not adoptable)",
                         "exit 0, scoped file re-initialized active",
                         f"code={code} active={scoped_after.get('active')} err={err[:120]}")

            # A legacy file holding valid-but-non-object JSON (the corrupt
            # category) is not adoptable and must not traceback the conflict
            # check; scoped init proceeds.
            scoped_stray.unlink(missing_ok=True)
            legacy.write_text('[]')
            _, err, code = run_crew_state(
                ["init", "bl", "--prompt", "new scoped", "--session-id", "legS"], test_path)
            if code == 0 and scoped_stray.exists() and "Traceback" not in err:
                log_pass("init --session-id vs non-object legacy JSON - proceeds, no traceback")
            else:
                log_fail("init --session-id vs non-object legacy JSON - proceeds, no traceback",
                         "exit 0, scoped file created, no traceback",
                         f"code={code} scoped={scoped_stray.exists()} err={err[:120]}")
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()

            # =========================================================================
            # CLI init rejects whitespace-only task text (inline flag AND -f file)
            # =========================================================================
            # whitespace-only --prompt
            _, err, code = run_crew_state(["init", "bl", "--prompt", "   \n\t "], test_path)
            if code != 0 and not (crew_dir / "build-state.json").exists():
                log_pass("CLI init bl - whitespace-only --prompt rejected (nonzero, no state file)")
            else:
                log_fail("CLI init bl - whitespace-only --prompt rejected",
                         "nonzero, no state file", f"code={code} exists={(crew_dir / 'build-state.json').exists()}")

            # whitespace-only --task (measure-twice)
            _, err, code = run_crew_state(["init", "mt", "--task", "  \t\n", "--auto-plan"], test_path)
            if code != 0 and not (crew_dir / "measure-twice-state.json").exists():
                log_pass("CLI init mt - whitespace-only --task rejected (nonzero, no state file)")
            else:
                log_fail("CLI init mt - whitespace-only --task rejected",
                         "nonzero, no state file", f"code={code} exists={(crew_dir / 'measure-twice-state.json').exists()}")

            # whitespace-only -f file content
            ws_file = crew_dir / "ws-task.txt"
            ws_file.write_text("   \n\t  \n")
            _, err, code = run_crew_state(["init", "bl", "-f", str(ws_file)], test_path)
            if code != 0 and not (crew_dir / "build-state.json").exists():
                log_pass("CLI init bl - whitespace-only -f content rejected (nonzero, no state file)")
            else:
                log_fail("CLI init bl - whitespace-only -f content rejected",
                         "nonzero, no state file", f"code={code} exists={(crew_dir / 'build-state.json').exists()}")
            ws_file.unlink(missing_ok=True)
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()

            # =========================================================================
            # SESSION-SCOPED PERSISTENT MODE TESTS
            # =========================================================================
            log_section("persistent-mode.py (session-scoped)")

            # s1 has active bl, s2 does not
            (crew_dir / "build-state-s1.json").write_text(
                '{"active": true, "prompt": "s1 task", "session_id": "s1"}')

            # Stop hook with session_id=s1 blocks
            test_contains(
                "Stop hook s1 - blocks when s1 has active loop",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "s1"}),
                '"decision": "block", "reason":',
            )

            # Stop hook with session_id=s2 allows
            test_equals(
                "Stop hook s2 - allows when only s1 has active loop",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "s2"}),
                '{}',
            )

            # Stop hook with empty session_id only checks legacy files
            test_equals(
                "Stop hook no session - allows (no legacy file)",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{}',
            )

            # File with no session_id is claimed by session that has one
            (crew_dir / "build-state-s1.json").unlink()
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "legacy task"}')

            test_contains(
                "Stop hook session claims file (no session_id in file)",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "any-session"}),
                '"decision": "block", "reason":',
            )

            # Clean up
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # =========================================================================
            # SESSION-SCOPED SESSION START TESTS
            # =========================================================================
            log_section("session-start.py (session-scoped)")

            # Session ID injection
            test_contains(
                "SessionStart injects session ID into context",
                session_start,
                json.dumps({"directory": str(test_path), "session_id": "test-sess-123"}),
                "Session ID: test-sess-123",
            )

            # Session ID preamble present in BOTH terse and verbose
            test_contains(
                "SessionStart (terse) - 'Pass this session ID' preamble present",
                session_start,
                json.dumps({"directory": str(test_path), "session_id": "test-sess-123"}),
                "Pass this session ID",
            )
            output_sid_v = run_script_verbose(
                session_start, json.dumps({"directory": str(test_path), "session_id": "test-sess-123"}))
            if "Pass this session ID" in output_sid_v:
                log_pass("SessionStart (verbose) - 'Pass this session ID' preamble present")
            else:
                log_fail("SessionStart (verbose) - 'Pass this session ID' preamble present",
                         "contains 'Pass this session ID'", output_sid_v[:300])

            # Preamble references the CLAUDE_SESSION_ID environment variable claim
            test_contains(
                "SessionStart - references CLAUDE_SESSION_ID env var claim",
                session_start,
                json.dumps({"directory": str(test_path), "session_id": "test-sess-123"}),
                "CLAUDE_SESSION_ID environment variable",
            )

            # Session-scoped bl detected for this session
            (crew_dir / "build-state-mysess.json").write_text(
                '{"active": true, "prompt": "My task", "session_id": "mysess"}')

            test_contains(
                "SessionStart detects session-scoped bl for this session",
                session_start,
                json.dumps({"directory": str(test_path), "session_id": "mysess"}),
                "Build Loop Active",
            )

            # Other session's loop shown as "Other Sessions"
            test_contains(
                "SessionStart shows other session loops",
                session_start,
                json.dumps({"directory": str(test_path), "session_id": "different"}),
                "Other Sessions",
            )

            # Clean up
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # =========================================================================
            # ORPHAN CLEANUP TESTS
            # =========================================================================
            log_section("Orphan cleanup")

            # Create a stale inactive session-scoped state file (>1 day old)
            stale_file = crew_dir / "build-state-stale123.json"
            stale_file.write_text('{"active": false, "prompt": "old task", "session_id": "stale123"}')
            # Set mtime to 2 days ago
            import time as _time
            old_mtime = _time.time() - (2 * 86400)
            os.utime(stale_file, (old_mtime, old_mtime))

            # Run session-start which triggers cleanup
            run_script(session_start, json.dumps({"directory": str(test_path)}))

            if not stale_file.exists():
                log_pass("Cleanup removes stale inactive session-scoped state file")
            else:
                log_fail("Cleanup removes stale inactive session-scoped state file",
                         "file deleted", "file still exists")

            # Create a stale active session-scoped state file (>7 days old)
            stale_active = crew_dir / "build-state-oldactive.json"
            stale_active.write_text('{"active": true, "prompt": "ancient task", "session_id": "oldactive"}')
            very_old_mtime = _time.time() - (8 * 86400)
            os.utime(stale_active, (very_old_mtime, very_old_mtime))

            run_script(session_start, json.dumps({"directory": str(test_path)}))

            if not stale_active.exists():
                log_pass("Cleanup removes very old active session-scoped state file")
            else:
                log_fail("Cleanup removes very old active session-scoped state file",
                         "file deleted", "file still exists")

            # A NEWER-SCHEMA file is never swept, at any age: deleting is the most
            # destructive touch there is, and the refuse-to-touch contract binds
            # this path too (a downgrade must not destroy a live loop it cannot
            # read). The current-schema twin beside it still sweeps, so this pins
            # the classification, not a disabled sweep.
            future_active = crew_dir / "build-state-futureactive.json"
            future_active.write_text(json.dumps({
                "active": True, "prompt": "newer-format loop",
                "session_id": "futureactive", "schema": 99}))
            current_active = crew_dir / "build-state-currentactive.json"
            current_active.write_text(json.dumps({
                "active": True, "prompt": "this-format loop",
                "session_id": "currentactive", "schema": 2}))
            future_bytes_before = future_active.read_bytes()
            for _af in (future_active, current_active):
                os.utime(_af, (very_old_mtime, very_old_mtime))

            run_script(session_start, json.dumps({"directory": str(test_path)}))

            if future_active.exists() and future_active.read_bytes() == future_bytes_before:
                log_pass("Cleanup preserves an 8-day-old ACTIVE future-schema state file (refuse to touch)")
            else:
                log_fail("Cleanup preserves an 8-day-old ACTIVE future-schema state file (refuse to touch)",
                         "file still on disk, bytes untouched",
                         f"exists={future_active.exists()}")

            if not current_active.exists():
                log_pass("Cleanup still sweeps the 8-day-old ACTIVE current-schema twin")
            else:
                log_fail("Cleanup still sweeps the 8-day-old ACTIVE current-schema twin",
                         "file deleted", "file still exists")

            # Clean up
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # --- .corrupt sweep is SCOPED to crew's own backups (not any *.corrupt) ---
            # A crew state backup past the age threshold IS swept.
            crew_corrupt = crew_dir / "build-state.json.corrupt"
            crew_corrupt.write_text('{"active": true, "prompt": "broken", trunc')
            mt_corrupt = crew_dir / "measure-twice-state-abc.json.corrupt"
            mt_corrupt.write_text("not json at all")
            # An UNRELATED *.corrupt file the user parked here must NOT be touched.
            user_corrupt = crew_dir / "secrets.corrupt"
            user_corrupt.write_text("user data crew never created")
            # PREFIX-COLLISION: names that share the `build-state` prefix but are
            # NOT one of crew's two backup forms (legacy `build-state.json` or
            # session-scoped `build-state-<id>.json`) must survive — a loose
            # `build-state*.json.corrupt` glob would have wrongly swept these.
            prefix_evil = crew_dir / "build-stateevil.json.corrupt"
            prefix_evil.write_text("foreign file sharing the build-state prefix")
            prefix_backup = crew_dir / "build-statebackup.json.corrupt"
            prefix_backup.write_text("another non-crew prefix collision")
            old_corrupt_mtime = _time.time() - (8 * 86400)
            for _cf in (crew_corrupt, mt_corrupt, user_corrupt, prefix_evil, prefix_backup):
                os.utime(_cf, (old_corrupt_mtime, old_corrupt_mtime))

            run_script(session_start, json.dumps({"directory": str(test_path)}))

            if not crew_corrupt.exists():
                log_pass("Cleanup sweeps stale build-state.json.corrupt backup")
            else:
                log_fail("Cleanup sweeps stale build-state.json.corrupt backup", "deleted", "still exists")

            if not mt_corrupt.exists():
                log_pass("Cleanup sweeps stale measure-twice-state-<id>.json.corrupt backup")
            else:
                log_fail("Cleanup sweeps stale measure-twice-state-<id>.json.corrupt backup", "deleted", "still exists")

            if user_corrupt.exists():
                log_pass("Cleanup preserves an unrelated user *.corrupt file (not crew's backup)")
            else:
                log_fail("Cleanup preserves an unrelated user *.corrupt file", "file exists", "file deleted")

            if prefix_evil.exists():
                log_pass("Cleanup preserves build-stateevil.json.corrupt (prefix collision, not crew's -<id> form)")
            else:
                log_fail("Cleanup preserves build-stateevil.json.corrupt (prefix collision)",
                         "file exists", "file wrongly swept by loose glob")

            if prefix_backup.exists():
                log_pass("Cleanup preserves build-statebackup.json.corrupt (prefix collision, not crew's -<id> form)")
            else:
                log_fail("Cleanup preserves build-statebackup.json.corrupt (prefix collision)",
                         "file exists", "file wrongly swept by loose glob")

            # Clean up
            for f in crew_dir.glob("*.corrupt"):
                f.unlink()

            # --- context-snapshot .md + .restored.md + orphaned .tmp sweep ---
            # The real save-context artifact is `.crew/context-snapshot.md`, and
            # restore renames it to `.crew/context-snapshot.restored.md`, which
            # accumulated forever under the old `context-snapshot-*.json`-only
            # pattern. A stale restored snapshot IS now swept; an ACTIVE state
            # file in the same pass survives untouched.
            stale_restored = crew_dir / "context-snapshot.restored.md"
            stale_restored.write_text("# old restored snapshot")
            stale_snapshot = crew_dir / "context-snapshot.md"
            stale_snapshot.write_text("# old snapshot")
            orphan_tmp = crew_dir / ".build-state-x.json.abc123.tmp"
            orphan_tmp.write_text("orphaned atomic-write temp")
            # FOREIGN files past the age threshold that share a bare suffix with
            # crew's artifacts must be PRESERVED — the exact-name/anchored globs
            # must NOT match them (mirrors the .corrupt prefix-collision negatives).
            foreign_restored = crew_dir / "foo.restored.md"        # not context-snapshot.restored.md
            foreign_restored.write_text("another tool's restored file")
            foreign_snapshot = crew_dir / "context-snapshot-notes.md"  # user's notes, not the exact artifact
            foreign_snapshot.write_text("user snapshot notes")
            foreign_tmp = crew_dir / "random.tmp"                  # bare .tmp, no crew prefix
            foreign_tmp.write_text("some tool's temp")
            foreign_hidden_tmp = crew_dir / ".sometool.tmp"        # hidden bare .tmp, no crew prefix
            foreign_hidden_tmp.write_text("some tool's hidden temp")
            snap_old_mtime = _time.time() - (8 * 86400)
            for _sf in (stale_restored, stale_snapshot, orphan_tmp,
                        foreign_restored, foreign_snapshot, foreign_tmp, foreign_hidden_tmp):
                os.utime(_sf, (snap_old_mtime, snap_old_mtime))
            # A live active loop from THIS pass must not be collateral.
            live_active = crew_dir / "build-state-liveL5.json"
            live_active.write_text('{"active": true, "prompt": "current task", "session_id": "liveL5"}')

            run_script(session_start, json.dumps({"directory": str(test_path)}))

            if not stale_restored.exists():
                log_pass("Cleanup sweeps stale context-snapshot.restored.md")
            else:
                log_fail("Cleanup sweeps stale context-snapshot.restored.md", "deleted", "still exists")

            if not stale_snapshot.exists():
                log_pass("Cleanup sweeps stale context-snapshot.md")
            else:
                log_fail("Cleanup sweeps stale context-snapshot.md", "deleted", "still exists")

            if not orphan_tmp.exists():
                log_pass("Cleanup sweeps orphaned atomic-write .tmp")
            else:
                log_fail("Cleanup sweeps orphaned atomic-write .tmp", "deleted", "still exists")

            if live_active.exists():
                log_pass("Cleanup preserves an active state file during the .md sweep")
            else:
                log_fail("Cleanup preserves an active state file during the .md sweep",
                         "file exists", "file wrongly deleted")

            if foreign_restored.exists():
                log_pass("Cleanup preserves foreign foo.restored.md (not context-snapshot.restored.md)")
            else:
                log_fail("Cleanup preserves foreign foo.restored.md",
                         "file exists", "file wrongly swept by bare *.restored.md glob")

            if foreign_snapshot.exists():
                log_pass("Cleanup preserves user context-snapshot-notes.md (not the exact artifact)")
            else:
                log_fail("Cleanup preserves user context-snapshot-notes.md",
                         "file exists", "file wrongly swept by context-snapshot*.md glob")

            if foreign_tmp.exists():
                log_pass("Cleanup preserves foreign random.tmp (no crew prefix)")
            else:
                log_fail("Cleanup preserves foreign random.tmp",
                         "file exists", "file wrongly swept by bare .*.tmp glob")

            if foreign_hidden_tmp.exists():
                log_pass("Cleanup preserves foreign .sometool.tmp (no crew prefix)")
            else:
                log_fail("Cleanup preserves foreign .sometool.tmp",
                         "file exists", "file wrongly swept by bare .*.tmp glob")

            # --- the state-lock siblings sweep on the SAME anchored terms ---
            # models.state_lock leaves a `<state-file>.lock` next to each state
            # file. A lock whose loop is long gone is reaped; a foreign `.lock`
            # (this cleanup also runs over the shared ~/.claude root) and a
            # prefix-collision name are not.
            stale_lock = crew_dir / "build-state.json.lock"
            stale_lock.write_text("")
            stale_scoped_lock = crew_dir / "measure-twice-state-abc.json.lock"
            stale_scoped_lock.write_text("")
            foreign_lock = crew_dir / "some-tool.lock"
            foreign_lock.write_text("another tool's lock")
            prefix_lock = crew_dir / "build-stateevil.json.lock"
            prefix_lock.write_text("foreign file sharing the build-state prefix")
            lock_old_mtime = _time.time() - (8 * 86400)
            for _lf in (stale_lock, stale_scoped_lock, foreign_lock, prefix_lock):
                os.utime(_lf, (lock_old_mtime, lock_old_mtime))

            run_script(session_start, json.dumps({"directory": str(test_path)}))

            if not stale_lock.exists() and not stale_scoped_lock.exists():
                log_pass("Cleanup sweeps stale state-lock siblings (legacy + -<id> forms)")
            else:
                log_fail("Cleanup sweeps stale state-lock siblings (legacy + -<id> forms)",
                         "both deleted",
                         f"legacy={stale_lock.exists()} scoped={stale_scoped_lock.exists()}")

            if foreign_lock.exists() and prefix_lock.exists():
                log_pass("Cleanup preserves foreign/prefix-collision *.lock files")
            else:
                log_fail("Cleanup preserves foreign/prefix-collision *.lock files",
                         "both preserved",
                         f"foreign={foreign_lock.exists()} prefix={prefix_lock.exists()}")

            # Clean up
            for f in set(list(crew_dir.glob("*.md")) + list(crew_dir.glob("*.tmp"))
                         + list(crew_dir.glob(".*.tmp")) + list(crew_dir.glob("*.lock"))
                         + list(crew_dir.glob("*-state*.json"))):
                f.unlink()

            # =========================================================================
            # STALE TODO CLEANUP TESTS
            # =========================================================================
            log_section("Stale todo cleanup")

            # Plant fixtures in the NEUTRAL home the subprocess sees (run_script
            # pins HOME there) — never in the developer's real ~/.claude/todos.
            todos_dir = Path(_NEUTRAL_HOME) / ".claude" / "todos"
            todos_dir.mkdir(parents=True, exist_ok=True)

            # Create empty todo file — should be cleaned up immediately
            empty_todo = todos_dir / "empty-session-agent-empty.json"
            empty_todo.write_text("[]")

            # Create old todo with incomplete tasks (>7 days) — should be cleaned up
            old_todo = todos_dir / "old-abandoned-agent-old.json"
            old_todo.write_text('[{"content": "Ancient task", "status": "in_progress"}]')
            very_old = _time.time() - (8 * 86400)
            os.utime(old_todo, (very_old, very_old))

            # Create recent todo with incomplete tasks (<7 days) — should survive
            recent_todo = todos_dir / "recent-session-agent-recent.json"
            recent_todo.write_text('[{"content": "Fresh task", "status": "pending"}]')

            # Run session-start to trigger cleanup
            run_script(session_start, json.dumps({"directory": str(test_path)}))

            if not empty_todo.exists():
                log_pass("Cleanup removes empty todo file")
            else:
                log_fail("Cleanup removes empty todo file", "file deleted", "file still exists")
                empty_todo.unlink(missing_ok=True)

            if not old_todo.exists():
                log_pass("Cleanup removes abandoned todo file (>7 days)")
            else:
                log_fail("Cleanup removes abandoned todo file (>7 days)", "file deleted", "file still exists")
                old_todo.unlink(missing_ok=True)

            if recent_todo.exists():
                log_pass("Cleanup preserves recent todo file (<7 days)")
            else:
                log_fail("Cleanup preserves recent todo file (<7 days)", "file exists", "file deleted")

            # Clean up
            recent_todo.unlink(missing_ok=True)

            # =========================================================================
            # MODELS HELPER TESTS (is_verbose / truncate)
            # =========================================================================
            log_section("models.py helpers")

            import importlib.util
            models_spec = importlib.util.spec_from_file_location("crew_models", SCRIPT_DIR / "models.py")
            models_module = importlib.util.module_from_spec(models_spec)
            models_spec.loader.exec_module(models_module)
            is_verbose = models_module.is_verbose
            truncate = models_module.truncate

            # is_verbose: truthy values
            saved_verbose = os.environ.pop("CREW_VERBOSE", None)
            try:
                for val in ("1", "true", "TRUE", "yes", "Yes"):
                    os.environ["CREW_VERBOSE"] = val
                    if is_verbose():
                        log_pass(f"is_verbose() True for CREW_VERBOSE={val!r}")
                    else:
                        log_fail(f"is_verbose() True for CREW_VERBOSE={val!r}", "True", "False")

                # is_verbose: falsy values
                for val in ("", "0", "false", "no", "off"):
                    os.environ["CREW_VERBOSE"] = val
                    if not is_verbose():
                        log_pass(f"is_verbose() False for CREW_VERBOSE={val!r}")
                    else:
                        log_fail(f"is_verbose() False for CREW_VERBOSE={val!r}", "False", "True")

                # is_verbose: unset
                os.environ.pop("CREW_VERBOSE", None)
                if not is_verbose():
                    log_pass("is_verbose() False when CREW_VERBOSE unset")
                else:
                    log_fail("is_verbose() False when CREW_VERBOSE unset", "False", "True")
            finally:
                if saved_verbose is not None:
                    os.environ["CREW_VERBOSE"] = saved_verbose
                else:
                    os.environ.pop("CREW_VERBOSE", None)

            # truncate: short text unchanged
            if truncate("hello", 120) == "hello":
                log_pass("truncate() leaves short text unchanged")
            else:
                log_fail("truncate() leaves short text unchanged", "hello", truncate("hello", 120))

            # truncate: exactly max_length unchanged
            exact = "X" * 120
            if truncate(exact, 120) == exact:
                log_pass("truncate() leaves text at exactly max_length unchanged")
            else:
                log_fail("truncate() leaves text at exactly max_length unchanged", "unchanged 120 chars", f"len={len(truncate(exact, 120))}")

            # truncate: long text → exactly max_length total with ellipsis
            long = "Y" * 500
            t = truncate(long, 120)
            if len(t) == 120 and t.endswith("...") and t[:117] == "Y" * 117:
                log_pass("truncate() truncates long text to exactly max_length with ellipsis")
            else:
                log_fail("truncate() truncates long text to exactly max_length with ellipsis",
                         "120 chars ending '...'", f"len={len(t)}, ends={t[-5:]!r}")

            # truncate: default max_length is 120
            if len(truncate("Z" * 300)) == 120:
                log_pass("truncate() default max_length is 120")
            else:
                log_fail("truncate() default max_length is 120", "120", str(len(truncate("Z" * 300))))

            # =========================================================================
            # DEBATE DIR CLEANUP TESTS (session-start cleanup_stale_debate_dirs)
            # =========================================================================
            log_section("cleanup_stale_debate_dirs")

            ss_spec = importlib.util.spec_from_file_location("crew_session_start", SCRIPT_DIR / "session-start.py")
            ss_module = importlib.util.module_from_spec(ss_spec)
            ss_spec.loader.exec_module(ss_module)
            cleanup_stale_debate_dirs = ss_module.cleanup_stale_debate_dirs

            debate_crew_dir = test_path / "debate-test" / ".crew"
            debates_dir = debate_crew_dir / "debates"
            debates_dir.mkdir(parents=True, exist_ok=True)

            old_mtime = _time.time() - (2 * 86400)

            # Stale multi-round run-* dir → removed
            stale_run = debates_dir / "run-20250101-abc"
            stale_run.mkdir()
            (stale_run / "question.md").write_text("q")
            os.utime(stale_run, (old_mtime, old_mtime))

            # Stale single-round timestamp-slug dir (real cmd_debate format
            # %Y%m%d-%H%M%S, i.e. 8 digits + '-' + 6 digits + slug) → removed
            stale_single = debates_dir / "20250101-120000-fix-auth"
            stale_single.mkdir()
            (stale_single / "round-01.md").write_text("r")
            os.utime(stale_single, (old_mtime, old_mtime))

            # Fresh run-* dir (mid-write) → preserved
            fresh_run = debates_dir / "run-99999999-live"
            fresh_run.mkdir()
            (fresh_run / "question.md").write_text("q")

            # Stale but NON-generated hyphenated user dir → must be PRESERVED
            # (the cleanup matches only run-* / YYYYMMDD-HHMMSS, not any hyphen).
            user_dir = debates_dir / "my-saved-notes"
            user_dir.mkdir()
            (user_dir / "keep.md").write_text("keep")
            os.utime(user_dir, (old_mtime, old_mtime))

            # Stale generated run-* dir that DOES contain synthesis.md → a
            # completed decision record; must be PRESERVED regardless of age.
            completed_run = debates_dir / "run-20250101-done"
            completed_run.mkdir()
            (completed_run / "question.md").write_text("q")
            (completed_run / "round-01.md").write_text("r")
            (completed_run / "synthesis.md").write_text("decision")
            os.utime(completed_run, (old_mtime, old_mtime))

            cleanup_stale_debate_dirs(debate_crew_dir)

            if not stale_run.exists():
                log_pass("cleanup_stale_debate_dirs removes stale run-* dir")
            else:
                log_fail("cleanup_stale_debate_dirs removes stale run-* dir", "dir removed", "dir exists")

            if not stale_single.exists():
                log_pass("cleanup_stale_debate_dirs removes stale timestamp-slug dir")
            else:
                log_fail("cleanup_stale_debate_dirs removes stale timestamp-slug dir", "dir removed", "dir exists")

            if user_dir.exists():
                log_pass("cleanup_stale_debate_dirs preserves a non-generated hyphenated user dir")
            else:
                log_fail("cleanup_stale_debate_dirs preserves a non-generated hyphenated user dir", "dir kept", "dir removed")

            if fresh_run.exists():
                log_pass("cleanup_stale_debate_dirs preserves fresh (live) run dir")
            else:
                log_fail("cleanup_stale_debate_dirs preserves fresh (live) run dir", "dir exists", "dir removed")

            if completed_run.exists():
                log_pass("cleanup_stale_debate_dirs preserves stale dir with synthesis.md (completed decision record)")
            else:
                log_fail("cleanup_stale_debate_dirs preserves stale dir with synthesis.md (completed decision record)", "dir kept", "dir removed")

            # No debates dir → no error
            no_debates = test_path / "no-debates" / ".crew"
            no_debates.mkdir(parents=True, exist_ok=True)
            try:
                cleanup_stale_debate_dirs(no_debates)
                log_pass("cleanup_stale_debate_dirs no-op when debates dir absent")
            except Exception as e:
                log_fail("cleanup_stale_debate_dirs no-op when debates dir absent", "no exception", repr(e))

            # =========================================================================
            # SLUGIFY TESTS
            # =========================================================================
            log_section("slugify()")

            # Import slugify from crew-state.py (hyphenated filename requires importlib)
            spec = importlib.util.spec_from_file_location("crew_state", SCRIPT_DIR / "crew-state.py")
            crew_state_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(crew_state_module)
            slugify = crew_state_module.slugify

            # Test null handling
            if slugify(None) == "plan":
                log_pass("slugify(None) returns default 'plan'")
            else:
                log_fail("slugify(None) returns default 'plan'", "plan", slugify(None))

            # Test normal string
            if slugify("Build Auth System") == "build-auth-system":
                log_pass("slugify('Build Auth System') returns 'build-auth-system'")
            else:
                log_fail("slugify('Build Auth System') returns 'build-auth-system'", "build-auth-system", slugify("Build Auth System"))

            # Test empty string
            if slugify("") == "plan":
                log_pass("slugify('') returns default 'plan'")
            else:
                log_fail("slugify('') returns default 'plan'", "plan", slugify(""))

            # =========================================================================
            # STATE DISCOVERY TESTS
            # =========================================================================
            log_section("state_discovery")

            sd_spec = importlib.util.spec_from_file_location("state_discovery", SCRIPT_DIR / "state_discovery.py")
            sd_module = importlib.util.module_from_spec(sd_spec)
            sd_spec.loader.exec_module(sd_module)
            is_loop_state_file = sd_module.is_loop_state_file
            is_active_state_file = sd_module.is_active_state_file

            # is_loop_state_file — positive cases
            for fname in ["build-state.json", "build-state-abc123.json", "measure-twice-state-xyz789.json"]:
                if is_loop_state_file(fname):
                    log_pass(f"is_loop_state_file('{fname}') -> True")
                else:
                    log_fail(f"is_loop_state_file('{fname}')", "True", "False")

            # is_loop_state_file — negative cases
            for fname in ["context-snapshot.json", "build-state.txt", "random.json"]:
                if not is_loop_state_file(fname):
                    log_pass(f"is_loop_state_file('{fname}') -> False")
                else:
                    log_fail(f"is_loop_state_file('{fname}')", "False", "True")

            # is_active_state_file — set up test files
            sd_dir = test_path / "sd-test"
            sd_dir.mkdir(parents=True, exist_ok=True)

            active_file = sd_dir / "active.json"
            active_file.write_text('{"active": true}')
            if is_active_state_file(active_file):
                log_pass("is_active_state_file(active=true) -> True")
            else:
                log_fail("is_active_state_file(active=true)", "True", "False")

            inactive_file = sd_dir / "inactive.json"
            inactive_file.write_text('{"active": false}')
            if not is_active_state_file(inactive_file):
                log_pass("is_active_state_file(active=false) -> False")
            else:
                log_fail("is_active_state_file(active=false)", "False", "True")

            missing_file = sd_dir / "missing.json"
            if not is_active_state_file(missing_file):
                log_pass("is_active_state_file(missing) -> False")
            else:
                log_fail("is_active_state_file(missing)", "False", "True")

            malformed_file = sd_dir / "malformed.json"
            malformed_file.write_text("{not valid json")
            if not is_active_state_file(malformed_file):
                log_pass("is_active_state_file(malformed) -> False")
            else:
                log_fail("is_active_state_file(malformed)", "False", "True")

            # Verify: incomplete todos do NOT block stop (path #3 was removed —
            # native Claude Code todo handling covers this now). Plant the todo
            # in the NEUTRAL home so the subprocess actually sees it — the
            # assertion is meaningless if the file is invisible to the hook.
            todos_dir = Path(_NEUTRAL_HOME) / ".claude" / "todos"
            todos_dir.mkdir(parents=True, exist_ok=True)
            test_session_id = f"test-session-{os.getpid()}"
            todo_file = todos_dir / f"{test_session_id}-agent-{test_session_id}.json"
            todo_file.write_text('[{"content": "Test task", "status": "pending"}]')

            try:
                test_equals(
                    "Incomplete todos do NOT block stop (no active loop)",
                    persistent_mode,
                    json.dumps({"directory": str(test_path), "session_id": test_session_id}),
                    '{}',
                )
            finally:
                todo_file.unlink(missing_ok=True)

            # =========================================================================
            # STATE-FILE ROBUSTNESS
            # =========================================================================
            log_section("state robustness (atomic writes, corrupt/schema, adoption)")

            import stat as _stat

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # --- 0600-at-create + schema stamp (no chmod window) ---
            p5_state = crew_dir / "build-state.json"
            models_module.BuildState(active=True, prompt="p5").save(p5_state)
            mode = _stat.S_IMODE(os.stat(p5_state).st_mode)
            if mode == 0o600:
                log_pass("save() creates state file 0600 (no chmod window)")
            else:
                log_fail("save() creates state file 0600", "0o600", oct(mode))

            with open(p5_state) as f:
                _saved = json.load(f)
            # Schema 2 added the hook-owned termination fields (stop_fires /
            # started_at); a v1 file still loads, with their defaults.
            if _saved.get("schema") == 2:
                log_pass("save() stamps schema:2")
            else:
                log_fail("save() stamps schema:2", "2", str(_saved.get("schema")))

            # --- Atomicity: crash between temp-write and os.replace ---
            # A monkeypatched os.replace records the TEMP file's mode (proving
            # it is 0600 too) then raises: the original must stay intact and
            # the finally-cleanup must leave no stray temp.
            orig_content = p5_state.read_text()
            _temp_modes = []
            _saved_replace = models_module.os.replace

            def _capture_then_boom(src, dst):
                try:
                    _temp_modes.append(_stat.S_IMODE(os.stat(src).st_mode))
                except OSError:
                    _temp_modes.append(None)
                raise RuntimeError("simulated crash before replace")

            models_module.os.replace = _capture_then_boom
            try:
                try:
                    models_module.atomic_write_json(p5_state, {"active": False, "clobbered": True})
                except RuntimeError:
                    pass
            finally:
                models_module.os.replace = _saved_replace

            after_content = p5_state.read_text()
            stray = list(crew_dir.glob(".build-state.json.*.tmp"))
            if after_content == orig_content and not stray:
                log_pass("atomic write: crash before replace leaves original intact + no stray temp")
            else:
                log_fail("atomic write: crash before replace leaves original intact + no stray temp",
                         "original bytes, zero temp files",
                         f"changed={after_content != orig_content}, stray={[s.name for s in stray]}")

            if _temp_modes and _temp_modes[0] == 0o600:
                log_pass("atomic write: temp file is 0600 by construction")
            else:
                log_fail("atomic write: temp file is 0600 by construction", "0o600",
                         oct(_temp_modes[0]) if _temp_modes and _temp_modes[0] is not None else str(_temp_modes))

            # --- Concurrent writes: no torn state, unique temps ---
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            run_crew_state(["init", "bl", "--prompt", "race"], test_path)
            _race_procs = [
                subprocess.Popen(
                    [sys.executable, str(crew_state), "set", "bl", "completion_promise", _val],
                    cwd=test_path,
                    env={**_neutral_env(), "CLAUDE_PROJECT_DIR": str(test_path)},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                for _val in ("RACE_A", "RACE_B")
            ]
            for pr in _race_procs:
                pr.wait()
            try:
                with open(crew_dir / "build-state.json") as f:
                    raced = json.load(f)
                parseable = True
            except (OSError, json.JSONDecodeError):
                raced, parseable = {}, False
            race_stray = list(crew_dir.glob(".build-state.json.*.tmp"))
            # Last writer wins (either value is fine); a torn/partial file is not.
            if (parseable and raced.get("completion_promise") in ("RACE_A", "RACE_B")
                    and raced.get("prompt") == "race" and not race_stray):
                log_pass("concurrent set: final JSON parseable, valid state, no temp collision")
            else:
                log_fail("concurrent set: final JSON parseable, valid state, no temp collision",
                         "parseable JSON, completion_promise in {RACE_A,RACE_B}, prompt intact, no stray temp",
                         f"parseable={parseable}, promise={raced.get('completion_promise')}, "
                         f"prompt={raced.get('prompt')!r}, stray={[s.name for s in race_stray]}")

            # --- adoption stamp ---
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "legacy"}'
            )
            run_script(persistent_mode, json.dumps({"directory": str(test_path), "session_id": "s1"}))
            with open(crew_dir / "build-state.json") as f:
                stamped = json.load(f)
            if stamped.get("session_id") == "s1":
                log_pass("adoption stamp: legacy file gains the hook's session_id")
            else:
                log_fail("adoption stamp: legacy file gains the hook's session_id", "s1", str(stamped.get("session_id")))

            out_s2 = run_script(persistent_mode, json.dumps({"directory": str(test_path), "session_id": "s2"}))
            if out_s2 == "{}":
                log_pass("adoption stamp: a second, different session no longer co-adopts (allows)")
            else:
                log_fail("adoption stamp: a second, different session no longer co-adopts (allows)", "{}", out_s2[:200])

            # --- Corrupt ≠ missing: one-time block + .corrupt rename + next Stop allows ---
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            corrupt_file = crew_dir / "build-state.json"
            corrupt_file.write_text('{"active": true, "prompt": "broken", trunc')  # invalid JSON
            out_corrupt = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            payload_c = json.loads(out_corrupt)
            if payload_c.get("decision") == "block" and "corrupt" in payload_c.get("reason", "").lower():
                log_pass("corrupt state → one-time block with diagnostic (not silent allow)")
            else:
                log_fail("corrupt state → one-time block with diagnostic", "decision:block + corrupt reason", out_corrupt[:200])

            if (crew_dir / "build-state.json.corrupt").exists() and not corrupt_file.exists():
                log_pass("corrupt state → file set aside as .corrupt")
            else:
                log_fail("corrupt state → file set aside as .corrupt", ".corrupt exists, original gone",
                         f"corrupt_exists={(crew_dir / 'build-state.json.corrupt').exists()}, orig_exists={corrupt_file.exists()}")

            out_corrupt2 = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if out_corrupt2 == "{}":
                log_pass("corrupt state → second Stop allows (never loops forever)")
            else:
                log_fail("corrupt state → second Stop allows", "{}", out_corrupt2[:200])

            # --- Schema: absent loads as v1 ---
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "noschema"}'
            )
            out_noschema = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if json.loads(out_noschema).get("decision") == "block":
                log_pass("absent schema loads as v1 → active loop still blocks")
            else:
                log_fail("absent schema loads as v1 → active loop still blocks", "decision:block", out_noschema[:200])

            # --- Higher schema: refuse to touch (bytes untouched, NOT renamed) ---
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            future_file = crew_dir / "build-state.json"
            future_file.write_text(
                '{"active": true, "prompt": "newer", "schema": 99}'
            )
            before_bytes = future_file.read_bytes()
            out_future = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            payload_f = json.loads(out_future)
            after_bytes = future_file.read_bytes()
            renamed = (crew_dir / "build-state.json.corrupt").exists()
            if ("decision" not in payload_f
                    and "newer crew" in payload_f.get("systemMessage", "")
                    and after_bytes == before_bytes
                    and not renamed
                    and future_file.exists()):
                log_pass("higher schema (99) → refuse to touch: bytes untouched, not renamed, loud allow diagnostic")
            else:
                log_fail("higher schema (99) → refuse to touch",
                         "allow + systemMessage, bytes identical, no .corrupt rename",
                         f"decision={payload_f.get('decision')}, sysmsg={payload_f.get('systemMessage','')[:60]!r}, "
                         f"bytes_equal={after_bytes == before_bytes}, renamed={renamed}")

            # --- read path does not create .crew/ ---
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            p5_empty = test_path / "p5-empty-proj"
            p5_empty.mkdir()
            run_crew_state(["show", "bl"], p5_empty)
            if not (p5_empty / ".crew").exists():
                log_pass("crew state show in empty dir does not create .crew/")
            else:
                log_fail("crew state show in empty dir does not create .crew/", "no .crew/", ".crew/ created")

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # =========================================================================
            # TERMINATION BOUNDS: hook-owned counters, agent-unwritable
            # Every case runs against BOTH loops: the bug this suite exists for
            # (a loop resetting its own counter to escape a cap it hit by merely
            # waiting) lives in the shared Stop path, so a fix proven on one loop
            # proves nothing about the other.
            # =========================================================================
            log_section("termination bounds (bl + mt)")

            from datetime import datetime as _dt, timedelta as _td, timezone as _tz

            def _write_loop_state(loop: str, **overrides) -> Path:
                """Write an ACTIVE, unowned (adoptable) state file for a loop.

                Fields absent from the dict are the schema-1 shape: no
                `started_at`, no `stop_fires`, no `schema` key.
                """
                if loop == "bl":
                    data = {"active": True, "prompt": "bounded task",
                            "session_id": ""}
                    path = crew_dir / "build-state.json"
                else:
                    data = {"active": True, "task_description": "bounded plan",
                            "plan_file": ".crew/plans/bounded.md",
                            "session_id": ""}
                    path = crew_dir / "measure-twice-state.json"
                data.update(overrides)
                path.write_text(json.dumps(data))
                return path

            def _read_state_json(path: Path) -> dict:
                with open(path) as fh:
                    return json.load(fh)

            def _clear_state_files() -> None:
                # The build loop has priority in the Stop hook, so a leftover
                # build-state file would shadow every mt case below.
                for f in crew_dir.glob("*-state*.json*"):
                    f.unlink()

            stop_payload = json.dumps({"directory": str(test_path)})

            for loop, banner, ok_field, ok_value in (
                ("bl", "Build Loop", "completion_promise", "SHIPPED"),
                ("mt", "Measure-Twice Loop", "last_verdict", "REVISE"),
            ):
                # --- the hook writes ONLY what it observes: its own fires ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=0)
                for _ in range(3):
                    run_script(persistent_mode, stop_payload)
                after = _read_state_json(state_path)
                if after.get("stop_fires") == 3 and "iteration" not in after:
                    log_pass(f"{loop}: 3 consecutive Stop fires bump stop_fires, no round counter written")
                else:
                    log_fail(f"{loop}: 3 consecutive Stop fires bump stop_fires, no round counter written",
                             "stop_fires=3, no iteration key",
                             f"stop_fires={after.get('stop_fires')}, keys={sorted(after)}")

                # --- `set stop_fires` refused: the exact 30x self-medication that
                # made the safety limit unreachable ---
                before_bytes = state_path.read_bytes()
                _, err, code = run_crew_state(["set", loop, "stop_fires", "0"], test_path)
                if code == 2 and state_path.read_bytes() == before_bytes:
                    log_pass(f"set {loop} stop_fires - refused (exit 2), state file byte-unchanged")
                else:
                    log_fail(f"set {loop} stop_fires - refused (exit 2), state file byte-unchanged",
                             "exit 2, bytes identical",
                             f"code={code} unchanged={state_path.read_bytes() == before_bytes} err={err[:120]}")

                # --- every other hook-owned bound is refused the same way: a
                # bound the bounded thing can widen is not a bound ---
                for hook_owned, value in (("deadline_minutes", "9999"),
                                          ("max_stop_fires", "99999"),
                                          ("parked_fires", "0"),
                                          ("max_parked_fires", "99999"),
                                          ("started_at", "2999-01-01T00:00:00+00:00")):
                    before_bytes = state_path.read_bytes()
                    _, err, code = run_crew_state(["set", loop, hook_owned, value], test_path)
                    if code == 2 and state_path.read_bytes() == before_bytes:
                        log_pass(f"set {loop} {hook_owned} - refused (exit 2), state file byte-unchanged")
                    else:
                        log_fail(f"set {loop} {hook_owned} - refused (exit 2), state file byte-unchanged",
                                 "exit 2, bytes identical",
                                 f"code={code} unchanged={state_path.read_bytes() == before_bytes} err={err[:120]}")

                # --- `set active false` refused: an unaudited backdoor around the
                # panel-approval gate; `deactivate` is the sanctioned path ---
                before_bytes = state_path.read_bytes()
                _, err, code = run_crew_state(["set", loop, "active", "false"], test_path)
                if code == 2 and state_path.read_bytes() == before_bytes:
                    log_pass(f"set {loop} active false - refused (exit 2), state file byte-unchanged")
                else:
                    log_fail(f"set {loop} active false - refused (exit 2), state file byte-unchanged",
                             "exit 2, bytes identical",
                             f"code={code} unchanged={state_path.read_bytes() == before_bytes} err={err[:120]}")

                if "deactivate" in err:
                    log_pass(f"set {loop} active false - error points at `deactivate` instead")
                else:
                    log_fail(f"set {loop} active false - error points at `deactivate` instead",
                             "stderr names deactivate", err[:160])

                # --- an allowlisted field still writes ---
                _, err, code = run_crew_state(["set", loop, ok_field, ok_value], test_path)
                after = _read_state_json(state_path)
                if code == 0 and after.get(ok_field) == ok_value:
                    log_pass(f"set {loop} {ok_field} - allowlisted field still writes")
                else:
                    log_fail(f"set {loop} {ok_field} - allowlisted field still writes",
                             f"exit 0, {ok_field}={ok_value}",
                             f"code={code} value={after.get(ok_field)!r} err={err[:120]}")

                # --- stop_fires circuit breaker: force-exit, then RELEASE ---
                # The bound is checked before the bump, so with max_stop_fires=2
                # fires 1 and 2 nudge and fire 3 trips it.
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=0, max_stop_fires=2)
                fires = [json.loads(run_script(persistent_mode, stop_payload)) for _ in range(3)]
                after = _read_state_json(state_path)
                trip_reason = fires[2].get("reason", "")
                if (fires[0].get("decision") == "block"
                        and fires[1].get("decision") == "block"
                        and fires[2].get("decision") == "block"
                        and f"[{banner} - Safety Limit Reached]" in trip_reason
                        and "livelock circuit breaker" in trip_reason
                        and after.get("active") is False):
                    log_pass(f"{loop}: stop_fires cap force-exits (Safety Limit Reached, active=False)")
                else:
                    log_fail(f"{loop}: stop_fires cap force-exits (Safety Limit Reached, active=False)",
                             f"2 nudges then '[{banner} - Safety Limit Reached]' + livelock reason, active=False",
                             f"decisions={[f.get('decision') for f in fires]}, "
                             f"active={after.get('active')}, reason={trip_reason[:120]!r}")

                # A force-exit must be distinguishable from a clean `deactivate`
                # on later inspection: it stamps the same completed_at + reason.
                if (after.get("completed_at")
                        and models_module.parse_iso(after.get("completed_at")) is not None
                        and "livelock circuit breaker" in after.get("reason", "")):
                    log_pass(f"{loop}: a stop_fires force-exit stamps completed_at + reason")
                else:
                    log_fail(f"{loop}: a stop_fires force-exit stamps completed_at + reason",
                             "parseable completed_at + livelock reason on disk",
                             f"completed_at={after.get('completed_at')!r}, reason={after.get('reason')!r}")

                out_after_trip = run_script(persistent_mode, stop_payload)
                if out_after_trip == "{}":
                    log_pass(f"{loop}: the Stop after a stop_fires force-exit ALLOWS (never wedges)")
                else:
                    log_fail(f"{loop}: the Stop after a stop_fires force-exit ALLOWS (never wedges)",
                             "{}", out_after_trip[:200])

                # --- wall-clock deadline: force-exit, then RELEASE ---
                _clear_state_files()
                stale_start = (_dt.now(_tz.utc) - _td(minutes=90)).isoformat()
                state_path = _write_loop_state(
                    loop, started_at=stale_start, deadline_minutes=60, stop_fires=0)
                out_deadline = json.loads(run_script(persistent_mode, stop_payload))
                after = _read_state_json(state_path)
                dl_reason = out_deadline.get("reason", "")
                if (out_deadline.get("decision") == "block"
                        and f"[{banner} - Safety Limit Reached]" in dl_reason
                        and "deadline reached" in dl_reason
                        and after.get("active") is False):
                    log_pass(f"{loop}: wall-clock deadline force-exits (Safety Limit Reached, active=False)")
                else:
                    log_fail(f"{loop}: wall-clock deadline force-exits (Safety Limit Reached, active=False)",
                             f"'[{banner} - Safety Limit Reached]' + deadline reason, active=False",
                             f"decision={out_deadline.get('decision')}, active={after.get('active')}, "
                             f"reason={dl_reason[:120]!r}")

                if (after.get("completed_at")
                        and models_module.parse_iso(after.get("completed_at")) is not None
                        and "deadline reached" in after.get("reason", "")):
                    log_pass(f"{loop}: a deadline force-exit stamps completed_at + reason")
                else:
                    log_fail(f"{loop}: a deadline force-exit stamps completed_at + reason",
                             "parseable completed_at + deadline reason on disk",
                             f"completed_at={after.get('completed_at')!r}, reason={after.get('reason')!r}")

                out_after_deadline = run_script(persistent_mode, stop_payload)
                if out_after_deadline == "{}":
                    log_pass(f"{loop}: the Stop after a deadline force-exit ALLOWS (never wedges)")
                else:
                    log_fail(f"{loop}: the Stop after a deadline force-exit ALLOWS (never wedges)",
                             "{}", out_after_deadline[:200])

                # --- a hand-edited deadline cannot DELETE the ceiling: a
                # non-positive or non-int value on disk must read as the default
                # (DEFAULT_DEADLINE_MINUTES), not as "unbounded". The elapsed
                # clock below is set past that default so a deleted ceiling would
                # show up as a MISSING force-exit. ---
                default_deadline = models_module.DEFAULT_DEADLINE_MINUTES
                for bad_deadline in (0, -1, "soon", True, None):
                    _clear_state_files()
                    stale_start = (
                        _dt.now(_tz.utc) - _td(minutes=default_deadline + 30)).isoformat()
                    state_path = _write_loop_state(
                        loop, started_at=stale_start, deadline_minutes=bad_deadline,
                        stop_fires=0)
                    out_bad_dl = json.loads(run_script(persistent_mode, stop_payload))
                    after = _read_state_json(state_path)
                    bad_reason = out_bad_dl.get("reason", "")
                    if (out_bad_dl.get("decision") == "block"
                            and "deadline reached" in bad_reason
                            and f"limit {default_deadline}" in bad_reason
                            and after.get("active") is False):
                        log_pass(f"{loop}: deadline_minutes={bad_deadline!r} on disk falls back to the default bound")
                    else:
                        log_fail(f"{loop}: deadline_minutes={bad_deadline!r} on disk falls back to the default bound",
                                 f"block + 'deadline reached' + 'limit {default_deadline}', active=False",
                                 f"decision={out_bad_dl.get('decision')}, active={after.get('active')}, "
                                 f"reason={bad_reason[:120]!r}")

                # --- the same threat model for the FIRE counters: a non-int on
                # disk used to raise into the fail-open handler, i.e. the circuit
                # breaker silently stopped enforcing on that fire. A garbage cap
                # falls back to the module default (never to "unbounded"), and the
                # fire that reads it heals the value on disk. ---
                for bad_field, bad_value, healed in (
                    ("stop_fires", "many", 1),
                    ("max_stop_fires", None, models_module.DEFAULT_MAX_STOP_FIRES),
                    ("parked_fires", -3, 0),
                    ("max_parked_fires", "lots", models_module.DEFAULT_MAX_PARKED_FIRES),
                ):
                    _clear_state_files()
                    state_path = _write_loop_state(loop, **{bad_field: bad_value})
                    raw_bad = run_script(persistent_mode, stop_payload)
                    out_bad = json.loads(raw_bad)
                    after = _read_state_json(state_path)
                    if (out_bad.get("decision") == "block"
                            and "systemMessage" not in out_bad
                            and after.get(bad_field) == healed
                            and after.get("active") is True):
                        log_pass(f"{loop}: a non-int {bad_field} on disk still enforces (no fail-open), heals to {healed}")
                    else:
                        log_fail(f"{loop}: a non-int {bad_field} on disk still enforces (no fail-open), heals to {healed}",
                                 f"block nudge, no systemMessage, {bad_field}={healed}, still active",
                                 f"decision={out_bad.get('decision')}, {bad_field}={after.get(bad_field)!r}, "
                                 f"out={raw_bad[:160]}")

                # --- an adopted schema-1 loop is not left unbounded forever ---
                _clear_state_files()
                state_path = _write_loop_state(loop)  # no started_at, no schema key
                out_v1 = json.loads(run_script(persistent_mode, stop_payload))
                after = _read_state_json(state_path)
                stamped = after.get("started_at", "")
                if (out_v1.get("decision") == "block"
                        and stamped
                        and models_module.parse_iso(stamped) is not None
                        and after.get("schema") == 2):
                    log_pass(f"{loop}: schema-1 state with no started_at is stamped on the first Stop fire")
                else:
                    log_fail(f"{loop}: schema-1 state with no started_at is stamped on the first Stop fire",
                             "block + parseable started_at + schema:2",
                             f"decision={out_v1.get('decision')}, started_at={stamped!r}, "
                             f"schema={after.get('schema')}")

                # --- a started_at the wall clock cannot use is REPAIRED, not
                # tolerated. Unlike the counters, a bad value here does not raise
                # into the fail-open handler, it silently DELETES the deadline:
                # unparseable reads as "elapsed unknown" and a future date reads as
                # negative elapsed, and neither ever trips. So the only cost ceiling
                # in the system could be switched off with valid JSON. Each fire
                # restamps it, which bounds the loop from that fire forward. ---
                future_stamp = (_dt.now(_tz.utc) + _td(minutes=45)).isoformat()
                for bad_label, bad_start in (
                    ("unparseable", "not-a-timestamp"),
                    ("future-dated", future_stamp),
                    ("a nonsense type (int)", 1234567890),
                ):
                    _clear_state_files()
                    state_path = _write_loop_state(
                        loop, started_at=bad_start, deadline_minutes=60, stop_fires=0)
                    out_bad_start = json.loads(run_script(persistent_mode, stop_payload))
                    after = _read_state_json(state_path)
                    healed = after.get("started_at")
                    healed_dt = (models_module.parse_iso(healed)
                                 if isinstance(healed, str) else None)
                    ahead = ((healed_dt - _dt.now(_tz.utc)).total_seconds() / 60.0
                             if healed_dt else None)
                    if (out_bad_start.get("decision") == "block"
                            and "Safety Limit Reached" not in out_bad_start.get("reason", "")
                            and healed != bad_start
                            and healed_dt is not None
                            and ahead is not None
                            and ahead <= models_module.CLOCK_SKEW_TOLERANCE_MINUTES
                            and after.get("active") is True):
                        log_pass(f"{loop}: a {bad_label} started_at is repaired on disk (deadline restored)")
                    else:
                        log_fail(f"{loop}: a {bad_label} started_at is repaired on disk (deadline restored)",
                                 "nudge (not a trip), started_at restamped to a parseable non-future value",
                                 f"decision={out_bad_start.get('decision')}, "
                                 f"started_at={healed!r}, ahead={ahead}")

                    # ... and from that repaired stamp the deadline ENFORCES: age it
                    # past the bound and the next fire force-exits. Before the
                    # repair, no amount of elapsed time could trip it.
                    aged = _read_state_json(state_path)
                    aged["started_at"] = (
                        _dt.now(_tz.utc) - _td(minutes=90)).isoformat()
                    state_path.write_text(json.dumps(aged))
                    out_enforced = json.loads(run_script(persistent_mode, stop_payload))
                    after = _read_state_json(state_path)
                    if ("deadline reached" in out_enforced.get("reason", "")
                            and after.get("active") is False):
                        log_pass(f"{loop}: the deadline enforces against the repaired started_at ({bad_label})")
                    else:
                        log_fail(f"{loop}: the deadline enforces against the repaired started_at ({bad_label})",
                                 "force-exit with 'deadline reached', active=False",
                                 f"reason={out_enforced.get('reason', '')[:120]!r}, "
                                 f"active={after.get('active')}")

            _clear_state_files()

            # models.effective_started_at, direct: the same three shapes, plus the
            # pass-through a healthy stamp must get (a repair on every fire would
            # restart the wall clock every fire, i.e. no deadline at all).
            good_stamp = (_dt.now(_tz.utc) - _td(minutes=10)).isoformat()
            skewed = (_dt.now(_tz.utc) + _td(seconds=30)).isoformat()  # inside tolerance
            for label, value, expect_repair in (
                ("a healthy stamp", good_stamp, False),
                ("a slightly-ahead clock (inside tolerance)", skewed, False),
                ("an unparseable stamp", "yesterday", True),
                ("an empty stamp", "", True),
                ("a future stamp", (_dt.now(_tz.utc) + _td(hours=3)).isoformat(), True),
                ("an int", 1234567890, True),
                ("a None", None, True),
                ("a bool", True, True),
            ):
                stamp, repaired = models_module.effective_started_at(value)
                usable = models_module.elapsed_minutes(stamp)
                if (repaired is expect_repair
                        and usable is not None
                        and (repaired or stamp == value)):
                    log_pass(f"effective_started_at: {label} -> repaired={expect_repair}, elapsed usable")
                else:
                    log_fail(f"effective_started_at: {label} -> repaired={expect_repair}, elapsed usable",
                             f"repaired={expect_repair}, a stamp elapsed_minutes can read",
                             f"repaired={repaired}, stamp={stamp!r}, elapsed={usable!r}")

            # =========================================================================
            # `crew state set` is a FIELD-LEVEL update, not a whole-state rewrite
            # The allowlist stops the agent NAMING a hook-owned field; it did not
            # stop it CLOBBERING one, because `set` loaded the whole state and
            # saved the whole state back. A Stop fire landing between that load and
            # that save was silently reverted: the counter-reset vector, reopened
            # through a field the agent IS allowed to write.
            # =========================================================================
            log_section("crew state set (field-level)")

            for loop, cls_name, ok_field, ok_value in (
                ("bl", "BuildState", "completion_promise", "SHIPPED"),
                ("mt", "MeasureTwiceState", "last_verdict", "REVISE"),
            ):
                state_cls = getattr(models_module, cls_name)

                # 1. The race, pinned. The command validates its arguments against
                #    a state it has already read; the hook fires in that window.
                _clear_state_files()
                race_path = _write_loop_state(loop, stop_fires=7, parked_fires=2)
                stale_snapshot = state_cls.load(race_path)  # what `set` starts from
                hook_view = _read_state_json(race_path)
                hook_view["stop_fires"] = 99                # the Stop hook fires NOW
                race_path.write_text(json.dumps(hook_view))

                crew_state_module._apply_field(state_cls, race_path, ok_field, ok_value)
                after = _read_state_json(race_path)
                if after.get("stop_fires") == 99 and after.get(ok_field) == ok_value:
                    log_pass(f"set {loop} {ok_field} - a stop_fires bump landing mid-command SURVIVES the write")
                else:
                    log_fail(f"set {loop} {ok_field} - a stop_fires bump landing mid-command SURVIVES the write",
                             f"stop_fires=99 (the hook's value) + {ok_field}={ok_value}",
                             f"stop_fires={after.get('stop_fires')!r}, {ok_field}={after.get(ok_field)!r}")

                # The old algorithm, run on the same file, to show what it cost: a
                # whole-state save from that stale snapshot reverts the hook's bump.
                stale_snapshot.save(race_path)
                reverted = _read_state_json(race_path)
                if reverted.get("stop_fires") == 7:
                    log_pass(f"set {loop}: a whole-state save from the stale snapshot DOES revert the counter (the vector)")
                else:
                    log_fail(f"set {loop}: a whole-state save from the stale snapshot DOES revert the counter (the vector)",
                             "stop_fires back to the stale 7",
                             f"stop_fires={reverted.get('stop_fires')!r}")

                # 2. Keys this install doesn't model are preserved, not dropped (a
                #    dataclass round trip silently deleted them).
                _clear_state_files()
                keep_path = _write_loop_state(
                    loop, stop_fires=4, completed_at="2026-01-01T00:00:00+00:00",
                    reason="prior force-exit", future_field="from a newer crew")
                _, err, code = run_crew_state(["set", loop, ok_field, ok_value], test_path)
                after = _read_state_json(keep_path)
                if (code == 0 and after.get(ok_field) == ok_value
                        and after.get("stop_fires") == 4
                        and after.get("future_field") == "from a newer crew"
                        and after.get("reason") == "prior force-exit"):
                    log_pass(f"set {loop} {ok_field} - unknown + hook-owned keys on disk are preserved verbatim")
                else:
                    log_fail(f"set {loop} {ok_field} - unknown + hook-owned keys on disk are preserved verbatim",
                             "the one field written, every other key untouched",
                             f"code={code} state={after} err={err[:120]}")

            _clear_state_files()

            # =========================================================================
            # CONCURRENT WRITERS: the Stop hook and `crew state set` are two
            # processes writing one file. An atomic WRITE does not make the
            # read-modify-write SEQUENCE around it atomic: each writer replaces the
            # file with a dict it built from the state it read BEFORE the other
            # landed, so one of the two updates is silently gone. That lost update
            # is the counter-reset escape route (a hook bump rolled back by an
            # allowlisted `set`) and its mirror (an agent field reverted by a hook
            # save). Both writers now hold ONE interprocess lock for the whole
            # read-modify-write.
            # =========================================================================
            log_section("concurrent writers (interprocess lock)")

            import threading
            import time as _time_mod
            # The SAME module object crew-state.py and persistent-mode.py import
            # (`models`), NOT the importlib copy loaded above as `crew_models`:
            # patching the copy would leave the code under test unpatched.
            import models as models_live

            pm_spec = importlib.util.spec_from_file_location(
                "crew_persistent_mode", SCRIPT_DIR / "persistent-mode.py")
            pm_module = importlib.util.module_from_spec(pm_spec)
            pm_spec.loader.exec_module(pm_module)

            def _write_conc_state() -> Path:
                _clear_state_files()
                path = crew_dir / "build-state-conc.json"
                path.write_text(json.dumps({
                    "active": True, "prompt": "concurrent task", "session_id": "conc",
                    "completion_promise": "INIT", "stop_fires": 0, "parked_fires": 0,
                    "started_at": _dt.now(_tz.utc).isoformat(), "schema": 2,
                }))
                return path

            conc_payload = json.dumps({"directory": str(test_path), "session_id": "conc"})

            # --- 1. The race, made deterministic. The window between a writer's
            # read and its replace is microseconds wide, so widen it: the LOCK, not
            # the width of the window, is what has to make this safe. Both writers
            # read the same dict, then write 0.2s later; unlocked, whichever lands
            # second erases the other's field. ---
            conc_path = _write_conc_state()
            _real_atomic_write = models_live.atomic_write_json
            race_errors = []

            def _slow_atomic_write(path, data):
                _time_mod.sleep(0.2)
                _real_atomic_write(path, data)

            def _hook_writer():
                try:
                    st = models_live.BuildState.load(conc_path)
                    pm_module._normalize_bounds(st)
                    pm_module._persist_fires(conc_path, st, stop_delta=1, parked="reset")
                except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                    race_errors.append(f"hook writer: {exc!r}")

            def _cli_writer():
                try:
                    _time_mod.sleep(0.05)  # read INSIDE the hook's window
                    crew_state_module._apply_field(
                        models_live.BuildState, conc_path, "completion_promise", "SHIPPED")
                except BaseException as exc:  # noqa: BLE001 - incl. SystemExit
                    race_errors.append(f"cli writer: {exc!r}")

            models_live.atomic_write_json = _slow_atomic_write
            try:
                race_threads = [threading.Thread(target=_hook_writer),
                                threading.Thread(target=_cli_writer)]
                for _t in race_threads:
                    _t.start()
                for _t in race_threads:
                    _t.join(timeout=15)
            finally:
                models_live.atomic_write_json = _real_atomic_write

            after = _read_state_json(conc_path)
            if (not race_errors and after.get("stop_fires") == 1
                    and after.get("completion_promise") == "SHIPPED"):
                log_pass("concurrent hook bump + `set` (widened window): BOTH survive (no lost update)")
            else:
                log_fail("concurrent hook bump + `set` (widened window): BOTH survive (no lost update)",
                         "stop_fires=1 AND completion_promise=SHIPPED",
                         f"stop_fires={after.get('stop_fires')!r}, "
                         f"completion_promise={after.get('completion_promise')!r}, "
                         f"errors={race_errors}")

            # --- 2. End-to-end, no monkeypatching: real Stop-hook processes firing
            # while a real `crew state set` loop runs against the same file. ---
            conc_path = _write_conc_state()
            conc_rounds = 12
            e2e_errors = []

            def _fire_hooks():
                try:
                    for _ in range(conc_rounds):
                        run_script(persistent_mode, conc_payload)
                except BaseException as exc:  # noqa: BLE001
                    e2e_errors.append(f"hook fires: {exc!r}")

            def _run_sets():
                try:
                    for i in range(conc_rounds):
                        run_crew_state(
                            ["set", "bl", "completion_promise", f"v{i}",
                             "--session-id", "conc"], test_path)
                except BaseException as exc:  # noqa: BLE001
                    e2e_errors.append(f"sets: {exc!r}")

            e2e_threads = [threading.Thread(target=_fire_hooks),
                           threading.Thread(target=_run_sets)]
            for _t in e2e_threads:
                _t.start()
            for _t in e2e_threads:
                _t.join(timeout=120)

            after = _read_state_json(conc_path)
            if (not e2e_errors and after.get("stop_fires") == conc_rounds
                    and after.get("completion_promise") == f"v{conc_rounds - 1}"):
                log_pass(f"{conc_rounds} concurrent Stop fires + {conc_rounds} `set` calls: "
                         f"no counter bump lost, no agent field lost")
            else:
                log_fail(f"{conc_rounds} concurrent Stop fires + {conc_rounds} `set` calls: "
                         f"no counter bump lost, no agent field lost",
                         f"stop_fires={conc_rounds}, completion_promise=v{conc_rounds - 1}",
                         f"stop_fires={after.get('stop_fires')!r}, "
                         f"completion_promise={after.get('completion_promise')!r}, "
                         f"errors={e2e_errors}")

            # --- 3. A lock that cannot be taken is a FAILED write, never a silent
            # unlocked one. The hook fails OPEN (allow + loud systemMessage): it
            # cannot write, and blocking without writing would nudge forever with
            # the counters frozen, so the bound that ends the loop never arrives.
            # The CLI exits nonzero with the bytes untouched. ---
            if models_live.fcntl is None:
                print(f"{YELLOW}~ SKIP{NC}: no fcntl on this platform (lock-contention tests)")
            else:
                conc_path = _write_conc_state()
                before_bytes = conc_path.read_bytes()
                lock_fd = os.open(str(models_live.state_lock_path(conc_path)),
                                  os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    models_live.fcntl.flock(lock_fd, models_live.fcntl.LOCK_EX)

                    raw_locked = run_script(persistent_mode, conc_payload)
                    try:
                        locked_out = json.loads(raw_locked)
                    except (json.JSONDecodeError, ValueError):
                        locked_out = {}
                    if ("decision" not in locked_out
                            and "could not lock" in locked_out.get("systemMessage", "")
                            and conc_path.read_bytes() == before_bytes):
                        log_pass("Stop hook vs a held state lock: fails OPEN (allow + loud diagnostic), no write")
                    else:
                        log_fail("Stop hook vs a held state lock: fails OPEN (allow + loud diagnostic), no write",
                                 "allow JSON with a 'could not lock' systemMessage, file byte-unchanged",
                                 f"out={raw_locked[:200]!r}, "
                                 f"unchanged={conc_path.read_bytes() == before_bytes}")

                    _, err, code = run_crew_state(
                        ["set", "bl", "completion_promise", "LOCKED",
                         "--session-id", "conc"], test_path)
                    if (code == 2 and "could not lock" in err
                            and conc_path.read_bytes() == before_bytes):
                        log_pass("`crew state set` vs a held state lock: exit 2, state file byte-unchanged")
                    else:
                        log_fail("`crew state set` vs a held state lock: exit 2, state file byte-unchanged",
                                 "exit 2, stderr says it could not lock, bytes identical",
                                 f"code={code} err={err[:160]!r} "
                                 f"unchanged={conc_path.read_bytes() == before_bytes}")
                finally:
                    models_live.fcntl.flock(lock_fd, models_live.fcntl.LOCK_UN)
                    os.close(lock_fd)

            _clear_state_files()

            # =========================================================================
            # `deactivate` stamps completed_at on the SAME clock the hook's
            # force-exit does: parse_iso reads a naive stamp AS UTC, so a local
            # naive one would be off by the machine's offset on every later read.
            # =========================================================================
            run_crew_state(["init", "bl", "--prompt", "clock task"], test_path)
            run_crew_state(["deactivate", "bl", "--reason", "done"], test_path)
            stamped_at = _read_state_json(crew_dir / "build-state.json").get("completed_at", "")
            parsed_stamp = models_module.parse_iso(stamped_at)
            if parsed_stamp is not None and parsed_stamp.tzinfo is not None:
                log_pass("deactivate - completed_at is a timezone-aware UTC stamp (same clock as force-exit)")
            else:
                log_fail("deactivate - completed_at is a timezone-aware UTC stamp (same clock as force-exit)",
                         "aware ISO timestamp", f"completed_at={stamped_at!r}")
            _clear_state_files()

            # `reason` on a force-exited file is the BOUND THAT TRIPPED, and `init`
            # quotes it back when it refuses to restart the loop. Cancelling after a
            # trip must not overwrite it, or the refusal cites "User cancelled" as
            # though that were the safety limit.
            for loop, cancel_reason in (("bl", "User cancelled"),
                                        ("mt", "User cancelled")):
                _clear_state_files()
                tripped_path = _write_loop_state(loop, stop_fires=2, max_stop_fires=2)
                run_script(persistent_mode, stop_payload)  # trips the bound
                run_crew_state(["deactivate", loop, "--reason", cancel_reason], test_path)
                after = _read_state_json(tripped_path)
                if ("livelock circuit breaker" in after.get("reason", "")
                        and after.get("deactivate_reason") == cancel_reason
                        and after.get("force_exit") is True):
                    log_pass(f"deactivate {loop} after a force-exit - keeps the trip reason, records the cancel separately")
                else:
                    log_fail(f"deactivate {loop} after a force-exit - keeps the trip reason, records the cancel separately",
                             "reason still names the tripped bound, deactivate_reason holds the cancel",
                             f"reason={after.get('reason')!r}, "
                             f"deactivate_reason={after.get('deactivate_reason')!r}")

                # A later `init` must therefore cite the BOUND, not the cancellation.
                _, err, code = run_crew_state(
                    ["init", loop, "--prompt" if loop == "bl" else "--task", "next run"]
                    + ([] if loop == "bl" else ["--auto-plan"]), test_path)
                if code != 0 and "livelock circuit breaker" in err and cancel_reason not in err:
                    log_pass(f"init {loop} after cancel-over-force-exit - the refusal cites the tripped bound")
                else:
                    log_fail(f"init {loop} after cancel-over-force-exit - the refusal cites the tripped bound",
                             "nonzero, stderr quotes the livelock reason, not the cancel text",
                             f"code={code} err={err[:160]!r}")

            _clear_state_files()

            # =========================================================================
            # `init` refuses to re-init over a FORCE-EXIT (unless --force)
            # After a force-exit the loop is inactive, so nothing else blocks a
            # fresh `init` that zeroes stop_fires and restarts the wall clock: that
            # IS the way around a safety limit. It has to be a deliberate act.
            # =========================================================================
            log_section("init vs force-exit")

            for loop, text_flag, extra in (("bl", "--prompt", []),
                                           ("mt", "--task", ["--auto-plan"])):
                # Trip the real bound rather than hand-writing the marker: this
                # asserts the hook and the CLI agree on what a force-exit looks like.
                _clear_state_files()
                exited_path = _write_loop_state(loop, stop_fires=2, max_stop_fires=2)
                run_script(persistent_mode, stop_payload)
                before_bytes = exited_path.read_bytes()

                _, err, code = run_crew_state(
                    ["init", loop, text_flag, "fresh run"] + extra, test_path)
                if (code != 0 and "force-exit" in err
                        and "--force" in err
                        and exited_path.read_bytes() == before_bytes):
                    log_pass(f"init {loop} over a force-exited loop - refused, state file byte-unchanged")
                else:
                    log_fail(f"init {loop} over a force-exited loop - refused, state file byte-unchanged",
                             "nonzero, stderr names --force, bytes identical",
                             f"code={code} unchanged={exited_path.read_bytes() == before_bytes} err={err[:160]}")

                _, err, code = run_crew_state(
                    ["init", loop, text_flag, "fresh run", "--force"] + extra, test_path)
                after = _read_state_json(exited_path)
                if (code == 0 and after.get("active") is True
                        and after.get("stop_fires") == 0
                        and not after.get("force_exit")):
                    log_pass(f"init {loop} --force - starts a fresh loop over the force-exit")
                else:
                    log_fail(f"init {loop} --force - starts a fresh loop over the force-exit",
                             "exit 0, active=True, stop_fires=0, marker cleared",
                             f"code={code} state={after} err={err[:120]}")

            _clear_state_files()

            # =========================================================================
            # `[tuning].deadline_minutes`: CLI flag > per-repo config > builtin
            # =========================================================================
            log_section("init deadline (config-resolved)")

            repo_config = crew_dir / "config.toml"
            builtin_deadline = models_module.DEFAULT_DEADLINE_MINUTES
            for toml_body, flag, expected, label in (
                ("[tuning]\ndeadline_minutes = 90\n", [], 90,
                 "per-repo [tuning].deadline_minutes is the init default"),
                ("[tuning]\ndeadline_minutes = 90\n", ["--deadline-minutes", "30"], 30,
                 "an explicit --deadline-minutes beats the config"),
                ("[tuning]\ndeadline_minutes = 999\n", [], builtin_deadline,
                 "an out-of-policy config deadline is dropped for the builtin"),
                ("[tuning]\ndeadline_minutes = \"soon\"\n", [], builtin_deadline,
                 "a non-int config deadline is dropped for the builtin"),
            ):
                _clear_state_files()
                repo_config.write_text(toml_body)
                _, err, code = run_crew_state(
                    ["init", "bl", "--prompt", "deadline task"] + flag, test_path)
                after = _read_state_json(crew_dir / "build-state.json")
                if code == 0 and after.get("deadline_minutes") == expected:
                    log_pass(f"init deadline: {label} ({expected})")
                else:
                    log_fail(f"init deadline: {label} ({expected})",
                             f"deadline_minutes={expected}",
                             f"code={code} deadline={after.get('deadline_minutes')!r} err={err[:120]}")
            repo_config.unlink(missing_ok=True)

            _clear_state_files()

            # =========================================================================
            # PARKED STOPS: waiting on launched work must be FREE
            # A Stop that fires while the session still has `background_tasks` in
            # flight is a WAIT, not an attempt to quit: the harness re-invokes the
            # session when that work lands. Blocking it forced the agent to take a
            # turn with nothing to do, so it polled: the busy-wait that burned the
            # counter and flooded the context. Every case runs against BOTH loops
            # (the parked branch lives in the shared Stop path).
            # =========================================================================
            log_section("parked stops (bl + mt)")

            def _parked_payload(tasks) -> str:
                """A Stop payload carrying `tasks` as the harness's background_tasks."""
                return json.dumps({"directory": str(test_path), "background_tasks": tasks})

            # Shape of a live in-flight entry; only its NON-EMPTINESS is load-bearing.
            one_task = [{"id": "bash-1", "status": "running", "command": "crew run codex"}]

            def _shell_task(status):
                """A background_tasks shell entry in the live payload's shape."""
                return {"id": "b06o335yc", "type": "shell", "status": status,
                        "description": "run the codex seat", "command": "crew run codex"}

            def _subagent_task(status):
                """A background_tasks Task-seat entry (a panel seat on opus/sonnet)."""
                return {"id": "a41k22xz", "type": "subagent", "status": status,
                        "description": "panel seat: opus"}

            for loop, banner in (("bl", "Build Loop"), ("mt", "Measure-Twice Loop")):
                # --- parked + active: ALLOW, and waiting costs nothing ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=5)
                parked_outs = [
                    run_script(persistent_mode, _parked_payload(one_task)) for _ in range(4)
                ]
                after = _read_state_json(state_path)
                if (all(o == "{}" for o in parked_outs)
                        and after.get("stop_fires") == 5
                        and after.get("parked_fires") == 4
                        and after.get("active") is True):
                    log_pass(f"{loop}: 4 consecutive parked fires ALLOW ({{}}), stop_fires unchanged, loop stays active")
                else:
                    log_fail(f"{loop}: 4 consecutive parked fires ALLOW ({{}}), stop_fires unchanged, loop stays active",
                             "4x '{}', stop_fires=5, parked_fires=4, active=True",
                             f"outs={[o[:60] for o in parked_outs]}, "
                             f"stop_fires={after.get('stop_fires')}, "
                             f"parked_fires={after.get('parked_fires')}, active={after.get('active')}")

                # --- a scheduled wake-up is a wait too: session_crons parks the
                # Stop exactly like background_tasks does ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=5)
                out_cron = run_script(persistent_mode, json.dumps({
                    "directory": str(test_path),
                    "session_crons": [{"id": "cron-1", "schedule": "*/5 * * * *"}],
                }))
                after = _read_state_json(state_path)
                if (out_cron == "{}"
                        and after.get("stop_fires") == 5
                        and after.get("parked_fires") == 1):
                    log_pass(f"{loop}: session_crons counts as parked (ALLOW, stop_fires unchanged)")
                else:
                    log_fail(f"{loop}: session_crons counts as parked (ALLOW, stop_fires unchanged)",
                             "'{}', stop_fires=5, parked_fires=1",
                             f"out={out_cron[:80]}, stop_fires={after.get('stop_fires')}, "
                             f"parked_fires={after.get('parked_fires')}")

                # --- the escape valve: an unrelated long-lived background shell
                # parks every Stop, so past max_parked_fires a parked Stop is
                # nudged like any other and costs a stop_fire. Without this, one
                # `tail -f` silently disables the loop for the whole session. ---
                _clear_state_files()
                state_path = _write_loop_state(
                    loop, stop_fires=0, parked_fires=3, max_parked_fires=3)
                out_valve = json.loads(run_script(persistent_mode, _parked_payload(one_task)))
                after = _read_state_json(state_path)
                valve_reason = out_valve.get("reason", "")
                if (out_valve.get("decision") == "block"
                        and f"[{banner}]" in valve_reason
                        and after.get("stop_fires") == 1
                        and after.get("active") is True):
                    log_pass(f"{loop}: a parked fire past max_parked_fires nudges and bumps stop_fires")
                else:
                    log_fail(f"{loop}: a parked fire past max_parked_fires nudges and bumps stop_fires",
                             f"block '[{banner}]' nudge, stop_fires=1, still active",
                             f"decision={out_valve.get('decision')}, stop_fires={after.get('stop_fires')}, "
                             f"active={after.get('active')}, reason={valve_reason[:120]!r}")

                # --- parked_fires counts CONSECUTIVE parks: a fire with nothing
                # in flight means the session came back and worked, so it resets ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=0, parked_fires=2)
                run_script(persistent_mode, stop_payload)  # not parked
                after = _read_state_json(state_path)
                if after.get("parked_fires") == 0 and after.get("stop_fires") == 1:
                    log_pass(f"{loop}: a non-parked fire resets parked_fires")
                else:
                    log_fail(f"{loop}: a non-parked fire resets parked_fires",
                             "parked_fires=0, stop_fires=1",
                             f"parked_fires={after.get('parked_fires')}, "
                             f"stop_fires={after.get('stop_fires')}")

                # A parked fire must never carry a block decision, however phrased.
                if all('"decision": "block"' not in o for o in parked_outs):
                    log_pass(f"{loop}: a parked fire emits no block decision")
                else:
                    log_fail(f"{loop}: a parked fire emits no block decision",
                             "no 'decision': 'block' in any parked output",
                             f"outs={[o[:80] for o in parked_outs]}")

                # --- termination beats parking: a wedged background task must not
                # be able to hide behind "I'm waiting" forever ---
                _clear_state_files()
                stale_start = (_dt.now(_tz.utc) - _td(minutes=90)).isoformat()
                state_path = _write_loop_state(
                    loop, started_at=stale_start, deadline_minutes=60, stop_fires=0)
                out_parked_dl = json.loads(run_script(persistent_mode, _parked_payload(one_task)))
                after = _read_state_json(state_path)
                dl_reason = out_parked_dl.get("reason", "")
                if (out_parked_dl.get("decision") == "block"
                        and f"[{banner} - Safety Limit Reached]" in dl_reason
                        and "deadline reached" in dl_reason
                        and after.get("active") is False):
                    log_pass(f"{loop}: parked + past deadline still force-exits (termination beats parking)")
                else:
                    log_fail(f"{loop}: parked + past deadline still force-exits (termination beats parking)",
                             f"'[{banner} - Safety Limit Reached]' + deadline reason, active=False",
                             f"decision={out_parked_dl.get('decision')}, active={after.get('active')}, "
                             f"reason={dl_reason[:120]!r}")

                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=2, max_stop_fires=2)
                out_parked_cap = json.loads(run_script(persistent_mode, _parked_payload(one_task)))
                after = _read_state_json(state_path)
                cap_reason = out_parked_cap.get("reason", "")
                if (out_parked_cap.get("decision") == "block"
                        and f"[{banner} - Safety Limit Reached]" in cap_reason
                        and "livelock circuit breaker" in cap_reason
                        and after.get("active") is False):
                    log_pass(f"{loop}: parked at the stop_fires cap still force-exits")
                else:
                    log_fail(f"{loop}: parked at the stop_fires cap still force-exits",
                             f"'[{banner} - Safety Limit Reached]' + livelock reason, active=False",
                             f"decision={out_parked_cap.get('decision')}, active={after.get('active')}, "
                             f"reason={cap_reason[:120]!r}")

                # --- NOT parked: the two "no work in flight" payload shapes (key
                # absent vs an empty list) must be indistinguishable to the hook ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=0)
                out_absent = json.loads(run_script(persistent_mode, stop_payload))
                absent_fires = _read_state_json(state_path).get("stop_fires")

                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=0)
                out_empty = json.loads(run_script(persistent_mode, _parked_payload([])))
                empty_fires = _read_state_json(state_path).get("stop_fires")

                if (out_absent.get("decision") == "block" and absent_fires == 1
                        and out_empty.get("decision") == "block" and empty_fires == 1
                        and out_absent.get("reason") == out_empty.get("reason")):
                    log_pass(f"{loop}: not parked (background_tasks absent vs []) nudges identically, stop_fires increments")
                else:
                    log_fail(f"{loop}: not parked (background_tasks absent vs []) nudges identically, stop_fires increments",
                             "both block with the same reason, stop_fires=1 each",
                             f"absent=({out_absent.get('decision')}, {absent_fires}), "
                             f"empty=({out_empty.get('decision')}, {empty_fires}), "
                             f"reasons_equal={out_absent.get('reason') == out_empty.get('reason')}")

                # --- a malformed background_tasks is NO signal, not a crash and not
                # a free pass: a future or older harness must never wedge the hook.
                # Same guard on session_crons (the other park signal). ---
                for shape, bad in (("string", "bash-1 running"),
                                   ("object", {"bash-1": "running"})):
                    for signal in ("background_tasks", "session_crons"):
                        _clear_state_files()
                        state_path = _write_loop_state(loop, stop_fires=0)
                        raw_bad = run_script(persistent_mode, json.dumps(
                            {"directory": str(test_path), signal: bad}))
                        out_bad = json.loads(raw_bad)
                        bad_fires = _read_state_json(state_path).get("stop_fires")
                        if (out_bad.get("decision") == "block"
                                and "systemMessage" not in out_bad
                                and f"[{banner}]" in out_bad.get("reason", "")
                                and bad_fires == 1):
                            log_pass(f"{loop}: a malformed {signal} ({shape}) is NOT parked - nudges, no crash")
                        else:
                            log_fail(f"{loop}: a malformed {signal} ({shape}) is NOT parked - nudges, no crash",
                                     f"block with '[{banner}]' nudge, no systemMessage, stop_fires=1",
                                     f"decision={out_bad.get('decision')}, stop_fires={bad_fires}, out={raw_bad[:160]}")

                # --- only LIVE tasks park. The harness prunes finished work
                # today, so this filter changes nothing now; it exists because a
                # harness that stopped pruning would leave one panel fan-out's
                # completed seats parking every later Stop, and a parked Stop is
                # ALLOWED: the panel-approval gate would switch off silently. ---
                for live_label, live_tasks in (
                    ("a running shell", [_shell_task("running")]),
                    # A Task seat must count, or a panel waiting on opus/sonnet nudges.
                    ("a running subagent", [_subagent_task("running")]),
                    ("one finished task next to a running one",
                     [_shell_task("completed"), _subagent_task("running")]),
                ):
                    _clear_state_files()
                    state_path = _write_loop_state(loop, stop_fires=5)
                    out_live = run_script(persistent_mode, _parked_payload(live_tasks))
                    after = _read_state_json(state_path)
                    if (out_live == "{}"
                            and after.get("stop_fires") == 5
                            and after.get("parked_fires") == 1):
                        log_pass(f"{loop}: {live_label} PARKS (ALLOW, stop_fires unchanged)")
                    else:
                        log_fail(f"{loop}: {live_label} PARKS (ALLOW, stop_fires unchanged)",
                                 "'{}', stop_fires=5, parked_fires=1",
                                 f"out={out_live[:80]}, stop_fires={after.get('stop_fires')}, "
                                 f"parked_fires={after.get('parked_fires')}")

                # --- a list of only FINISHED tasks is not a wait: nudge it ---
                for done_label, done_tasks in (
                    ("a completed shell", [_shell_task("completed")]),
                    ("an all-terminal list (completed + failed)",
                     [_shell_task("completed"), _subagent_task("failed")]),
                    ("an UPPERCASE terminal status", [_shell_task("COMPLETED")]),
                    ("a whitespace-padded terminal status", [_shell_task(" completed ")]),
                ):
                    _clear_state_files()
                    state_path = _write_loop_state(loop, stop_fires=0)
                    out_done = json.loads(run_script(persistent_mode, _parked_payload(done_tasks)))
                    after = _read_state_json(state_path)
                    if (out_done.get("decision") == "block"
                            and f"[{banner}]" in out_done.get("reason", "")
                            and after.get("stop_fires") == 1
                            and after.get("parked_fires") == 0):
                        log_pass(f"{loop}: {done_label} does NOT park - nudges, stop_fires increments")
                    else:
                        log_fail(f"{loop}: {done_label} does NOT park - nudges, stop_fires increments",
                                 f"block '[{banner}]', stop_fires=1, parked_fires=0",
                                 f"decision={out_done.get('decision')}, "
                                 f"stop_fires={after.get('stop_fires')}, "
                                 f"parked_fires={after.get('parked_fires')}")

                # --- fail safe: an entry this hook cannot read as finished counts
                # as IN FLIGHT, so an unrecognized payload degrades to a needless
                # wait rather than to waving unverified work through ---
                for safe_label, safe_task in (
                    ("an entry with no status key",
                     {"id": "x1", "type": "shell", "command": "crew run codex"}),
                    ("a non-string status", _shell_task(5)),
                    ("an unrecognized status", _shell_task("pending")),
                    ("a non-dict entry", "not-a-dict"),
                ):
                    _clear_state_files()
                    state_path = _write_loop_state(loop, stop_fires=5)
                    out_safe = run_script(persistent_mode, _parked_payload([safe_task]))
                    after = _read_state_json(state_path)
                    if (out_safe == "{}"
                            and after.get("stop_fires") == 5
                            and after.get("parked_fires") == 1):
                        log_pass(f"{loop}: {safe_label} counts as in flight and PARKS (fail safe)")
                    else:
                        log_fail(f"{loop}: {safe_label} counts as in flight and PARKS (fail safe)",
                                 "'{}', stop_fires=5, parked_fires=1",
                                 f"out={out_safe[:80]}, stop_fires={after.get('stop_fires')}, "
                                 f"parked_fires={after.get('parked_fires')}")

                # --- session_crons is NOT task-filtered: a scheduled wake-up parks
                # whatever background_tasks says ---
                for cron_label, cron_tasks in (
                    ("an empty", []),
                    ("an all-terminal", [_shell_task("completed")]),
                ):
                    _clear_state_files()
                    state_path = _write_loop_state(loop, stop_fires=5)
                    out_cron_only = run_script(persistent_mode, json.dumps({
                        "directory": str(test_path),
                        "background_tasks": cron_tasks,
                        "session_crons": [{"id": "cron-1", "schedule": "*/5 * * * *"}],
                    }))
                    after = _read_state_json(state_path)
                    if (out_cron_only == "{}"
                            and after.get("stop_fires") == 5
                            and after.get("parked_fires") == 1):
                        log_pass(f"{loop}: session_crons parks with {cron_label} background_tasks")
                    else:
                        log_fail(f"{loop}: session_crons parks with {cron_label} background_tasks",
                                 "'{}', stop_fires=5, parked_fires=1",
                                 f"out={out_cron_only[:80]}, stop_fires={after.get('stop_fires')}, "
                                 f"parked_fires={after.get('parked_fires')}")

            _clear_state_files()

            # =========================================================================
            # `crew state show --verbose` prints the ON-DISK dict
            # asdict(state) drops every key written outside the dataclass
            # (completed_at, reason, force_exit), and `init` REFUSES to restart a
            # force-exited loop on the strength of force_exit: a refusal keyed on a
            # field the supported inspection command could not display was a trap.
            # =========================================================================
            log_section("show --verbose (on-disk keys)")

            for loop in ("bl", "mt"):
                for trip_label, trip_overrides, trip_reason in (
                    ("stop_fires cap", {"stop_fires": 2, "max_stop_fires": 2},
                     "livelock circuit breaker"),
                    ("deadline",
                     {"stop_fires": 0, "deadline_minutes": 60,
                      "started_at": (_dt.now(_tz.utc) - _td(minutes=90)).isoformat()},
                     "deadline reached"),
                ):
                    _clear_state_files()
                    _write_loop_state(loop, **trip_overrides)
                    run_script(persistent_mode, stop_payload)  # trips the bound

                    out_v, err_v, code_v = run_crew_state(["show", loop, "--verbose"], test_path)
                    try:
                        shown = json.loads(out_v)
                    except json.JSONDecodeError:
                        shown = {}
                    if (code_v == 0
                            and shown.get("force_exit") is True
                            and shown.get("completed_at")
                            and trip_reason in shown.get("reason", "")):
                        log_pass(f"show {loop} --verbose - a {trip_label} force-exit shows "
                                 "force_exit + completed_at + reason")
                    else:
                        log_fail(f"show {loop} --verbose - a {trip_label} force-exit shows "
                                 "force_exit + completed_at + reason",
                                 f"force_exit=true, completed_at, reason containing {trip_reason!r}",
                                 f"code={code_v} out={out_v[:200]} err={err_v[:120]}")

                    # The compact default must not inherit the raw-dict switch: it
                    # stays the one-line summary, never the on-disk JSON.
                    out_c, _, code_c = run_crew_state(["show", loop], test_path)
                    if (code_c == 0
                            and out_c.splitlines()[0].startswith(f"loop={loop} active=false")
                            and len(out_c.splitlines()) == 1
                            and "force_exit" not in out_c
                            and "completed_at" not in out_c):
                        log_pass(f"show {loop} - the compact default is unchanged by the raw-dict switch")
                    else:
                        log_fail(f"show {loop} - the compact default is unchanged by the raw-dict switch",
                                 f"one line 'loop={loop} active=false …', no on-disk-only keys",
                                 f"code={code_c} out={out_c[:200]}")

            # A clean `deactivate` writes the same two keys, and `show --verbose`
            # must display them for the same reason.
            for loop, text_flag, extra in (("bl", "--prompt", []),
                                           ("mt", "--task", ["--auto-plan"])):
                _clear_state_files()
                run_crew_state(["init", loop, text_flag, "shown task"] + extra, test_path)
                run_crew_state(["deactivate", loop, "--reason", "panel approved"], test_path)
                out_v, err_v, code_v = run_crew_state(["show", loop, "--verbose"], test_path)
                try:
                    shown = json.loads(out_v)
                except json.JSONDecodeError:
                    shown = {}
                if (code_v == 0 and shown.get("active") is False
                        and shown.get("completed_at")
                        and shown.get("reason") == "panel approved"):
                    log_pass(f"show {loop} --verbose - a deactivated loop shows completed_at + reason")
                else:
                    log_fail(f"show {loop} --verbose - a deactivated loop shows completed_at + reason",
                             "active=false, completed_at, reason='panel approved'",
                             f"code={code_v} out={out_v[:200]} err={err_v[:120]}")

            # Keys this install does not model are DISPLAYED, not silently dropped:
            # the dataclass round trip is exactly what hid them.
            for loop in ("bl", "mt"):
                _clear_state_files()
                _write_loop_state(loop, future_field="from a newer crew")
                out_v, _, code_v = run_crew_state(["show", loop, "--verbose"], test_path)
                try:
                    shown = json.loads(out_v)
                except json.JSONDecodeError:
                    shown = {}
                if code_v == 0 and shown.get("future_field") == "from a newer crew":
                    log_pass(f"show {loop} --verbose - an unknown on-disk key survives the round trip")
                else:
                    log_fail(f"show {loop} --verbose - an unknown on-disk key survives the round trip",
                             "future_field displayed", f"code={code_v} out={out_v[:200]}")

            # No state file at all: the dataclass-default fallback still emits valid
            # JSON rather than crashing the inspection command.
            for loop in ("bl", "mt"):
                _clear_state_files()
                out_v, err_v, code_v = run_crew_state(["show", loop, "--verbose"], test_path)
                try:
                    shown = json.loads(out_v)
                except json.JSONDecodeError:
                    shown = None
                if (code_v == 0 and isinstance(shown, dict)
                        and shown.get("active") is False):
                    log_pass(f"show {loop} --verbose - a MISSING state file emits valid default JSON")
                else:
                    log_fail(f"show {loop} --verbose - a MISSING state file emits valid default JSON",
                             "exit 0, parseable JSON object with active=false",
                             f"code={code_v} out={out_v[:200]} err={err_v[:120]}")

            _clear_state_files()

            # =========================================================================
            # BOUNDED STACK DETECTION
            # =========================================================================
            log_section("stack detection (pruned bounded walk)")

            detect_project_stack = ss_module.detect_project_stack

            # Prune proven: a .kt ONLY under a pruned dir is NOT detected.
            p6_prune = test_path / "p6-prune"
            (p6_prune / "node_modules" / "pkg" / "src").mkdir(parents=True)
            (p6_prune / "node_modules" / "pkg" / "src" / "Buried.kt").write_text("class X")
            if "Kotlin" not in detect_project_stack(p6_prune):
                log_pass("stack detect: .kt only under a pruned dir (node_modules) is NOT detected")
            else:
                log_fail("stack detect: .kt only under a pruned dir is NOT detected", "no Kotlin", "Kotlin detected")

            # Hit: a .kt in a real source dir IS detected.
            p6_hit = test_path / "p6-hit"
            (p6_hit / "src" / "main").mkdir(parents=True)
            (p6_hit / "src" / "main" / "App.kt").write_text("class A")
            if "Kotlin" in detect_project_stack(p6_hit):
                log_pass("stack detect: .kt in a real source dir IS detected")
            else:
                log_fail("stack detect: .kt in a real source dir IS detected", "Kotlin", "not detected")

            # Parity: a .gradle.kts yields BOTH Kotlin and Gradle (old globs did).
            p6_gradle = test_path / "p6-gradle"
            p6_gradle.mkdir()
            (p6_gradle / "settings.gradle.kts").write_text('rootProject.name = "x"')
            gh = detect_project_stack(p6_gradle)
            if "Kotlin" in gh and "Gradle" in gh:
                log_pass("stack detect: a .gradle.kts yields both Kotlin and Gradle (glob parity)")
            else:
                log_fail("stack detect: a .gradle.kts yields both Kotlin and Gradle", "Kotlin+Gradle", str(gh))

            # Wall-clock bound: a deep/wide decoy under node_modules + one real
            # .kt returns well under the hook budget because the decoy is pruned.
            p6_wall = test_path / "p6-wall"
            (p6_wall / "src").mkdir(parents=True)
            (p6_wall / "src" / "Main.kt").write_text("class M")
            _decoy = p6_wall / "node_modules"
            for _i in range(40):
                _d = _decoy / f"pkg{_i}" / "deep" / "deeper"
                _d.mkdir(parents=True)
                for _j in range(20):
                    (_d / f"f{_j}.js").write_text("x")
            _t0 = _time.perf_counter()
            wall_hints = detect_project_stack(p6_wall)
            _elapsed = _time.perf_counter() - _t0
            if "Kotlin" in wall_hints and _elapsed < 2.0:
                log_pass(f"stack detect: pruned walk returns fast on a big decoy tree ({_elapsed:.3f}s)")
            else:
                log_fail("stack detect: pruned walk returns fast on a big decoy tree",
                         "Kotlin + < 2s", f"hints={wall_hints}, elapsed={_elapsed:.3f}s")

            # Entry-cap: a low cap halts the walk before it descends to a deep
            # .kt (root's plain files exhaust the budget first) — proves the cap.
            p6_cap = test_path / "p6-cap"
            p6_cap.mkdir()
            for _j in range(10):
                (p6_cap / f"plain{_j}.txt").write_text("x")
            _sub = p6_cap / "sub"
            _sub.mkdir()
            (_sub / "Deep.kt").write_text("class D")
            _saved_cap = ss_module.STACK_WALK_MAX_ENTRIES
            ss_module.STACK_WALK_MAX_ENTRIES = 5
            try:
                capped = detect_project_stack(p6_cap)
            finally:
                ss_module.STACK_WALK_MAX_ENTRIES = _saved_cap
            if "Kotlin" not in capped:
                log_pass("stack detect: entry cap halts the walk before a deep .kt (bounded)")
            else:
                log_fail("stack detect: entry cap halts the walk before a deep .kt", "no Kotlin (cap hit)", str(capped))
            if "Kotlin" in detect_project_stack(p6_cap):
                log_pass("stack detect: same tree with default cap finds the deep .kt (cap was the cause)")
            else:
                log_fail("stack detect: same tree with default cap finds the deep .kt", "Kotlin", "not detected")

            # =========================================================================
            # LOOP INIT -f (task off the shell)
            # =========================================================================
            log_section("crew state init -f (task off the shell)")

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # bl: -f reads the file into `prompt`, flags preserved byte-for-byte.
            p8_taskfile = crew_dir / "task-bl-p8.txt"
            p8_taskfile.write_text("Fix the auth bug --panel full", encoding="utf-8")
            _, stderr, code = run_crew_state(
                ["init", "bl", "-f", str(p8_taskfile), "--session-id", "p8"], test_path)
            if code == 0:
                sj, _, _ = run_crew_state(["show", "bl", "--session-id", "p8", "--verbose"], test_path)
                if '"prompt": "Fix the auth bug --panel full"' in sj:
                    log_pass("init bl -f: reads file into prompt (flags preserved byte-for-byte)")
                else:
                    log_fail("init bl -f: reads file into prompt", "prompt from file", sj)
            else:
                log_fail("init bl -f: exit 0", "exit 0", f"exit {code}, stderr: {stderr}")

            # mt: -f reads the file into `task_description`.
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            p8_mtfile = crew_dir / "task-mt-p8.txt"
            p8_mtfile.write_text("Design the profile system", encoding="utf-8")
            _, stderr, code = run_crew_state(
                ["init", "mt", "-f", str(p8_mtfile), "--auto-plan", "--session-id", "p8m"], test_path)
            if code == 0:
                sj, _, _ = run_crew_state(["show", "mt", "--session-id", "p8m", "--verbose"], test_path)
                if '"task_description": "Design the profile system"' in sj:
                    log_pass("init mt -f: reads file into task_description")
                else:
                    log_fail("init mt -f: reads file into task_description", "task from file", sj)
            else:
                log_fail("init mt -f: exit 0", "exit 0", f"exit {code}, stderr: {stderr}")

            # -f + inline flag together → clean error (exit 2), no state file.
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            p8_conflict = crew_dir / "task-conflict.txt"
            p8_conflict.write_text("x", encoding="utf-8")
            _, stderr, code = run_crew_state(
                ["init", "bl", "--prompt", "inline", "-f", str(p8_conflict), "--session-id", "p8c"], test_path)
            if code == 2 and "not both" in stderr and not (crew_dir / "build-state-p8c.json").exists():
                log_pass("init bl -f + --prompt together: clean error (exit 2), no state file")
            else:
                log_fail("init bl -f + --prompt together: clean error", "exit 2, 'not both', no file",
                         f"exit {code}, stderr={stderr!r}, file={(crew_dir / 'build-state-p8c.json').exists()}")

            # Missing file → clean nonzero exit, no state file.
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            _, stderr, code = run_crew_state(
                ["init", "bl", "-f", str(crew_dir / "nope.txt"), "--session-id", "p8x"], test_path)
            if code != 0 and "not found" in stderr and not (crew_dir / "build-state-p8x.json").exists():
                log_pass("init bl -f missing file: clean nonzero exit, no state file created")
            else:
                log_fail("init bl -f missing file: clean nonzero exit, no state file",
                         "nonzero, 'not found', no file",
                         f"exit {code}, stderr={stderr!r}, file={(crew_dir / 'build-state-p8x.json').exists()}")

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # =========================================================================
            # PYTHON 3.9 IMPORT BOMB + LOUD FAIL-OPEN
            # =========================================================================
            log_section("hooks: version guard + fail-open")

            session_start_path = SCRIPT_DIR / "session-start.py"
            state_discovery_path = SCRIPT_DIR / "state_discovery.py"

            # Static: state_discovery carries the __future__ import (the
            # `-> Path | None` annotation is a def-time TypeError on 3.9
            # without it).
            sd_src = state_discovery_path.read_text()
            if "from __future__ import annotations" in sd_src:
                log_pass("state_discovery.py has `from __future__ import annotations`")
            else:
                log_fail("state_discovery.py has `from __future__ import annotations`",
                         "__future__ import present", "absent")

            # Static: the stdlib-only version guard appears textually BEFORE
            # the first project import in both hook entry points.
            for hook_path in (persistent_mode, session_start_path):
                src = hook_path.read_text()
                guard_idx = src.find("sys.version_info < (3, 10)")
                import_idx = src.find("from models import")
                if 0 <= guard_idx < import_idx:
                    log_pass(f"{hook_path.name}: version guard precedes project imports")
                else:
                    log_fail(f"{hook_path.name}: version guard precedes project imports",
                             "guard before `from models import`",
                             f"guard_idx={guard_idx}, import_idx={import_idx}")

            # Behavioral: an unexpected in-hook crash fails OPEN (valid allow
            # JSON on stdout) and LOUD (diagnostic on stderr + the hook's own
            # documented output channel: Stop uses
            # systemMessage; SessionStart uses its documented
            # hookSpecificOutput.additionalContext channel). Fault: a
            # non-string "directory" makes Path() raise TypeError inside
            # main().
            for hook_path in (persistent_mode, session_start_path):
                crash_env = _neutral_env()
                crash_env.pop("CLAUDE_PROJECT_DIR", None)  # force the stdin fault path
                proc = subprocess.run(
                    [sys.executable, str(hook_path)],
                    input='{"directory": 123}',
                    capture_output=True, text=True, cwd=SCRIPT_DIR, env=crash_env,
                )
                name = f"{hook_path.name}: crash fails open + loud"
                try:
                    payload = json.loads(proc.stdout.strip())
                except (json.JSONDecodeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    if hook_path.name == "session-start.py":
                        hso = payload.get("hookSpecificOutput", {})
                        diag = hso.get("additionalContext", "") if isinstance(hso, dict) else ""
                    else:
                        diag = payload.get("systemMessage", "")
                else:
                    diag = ""
                if (
                    proc.returncode == 0
                    and isinstance(payload, dict)
                    and "decision" not in payload
                    and "crashed" in diag
                    and "[crew]" in proc.stderr
                ):
                    log_pass(name)
                else:
                    log_fail(name,
                             "rc 0, allow JSON with loud diagnostic field, stderr diagnostic",
                             f"rc={proc.returncode} stdout={proc.stdout[:150]!r} stderr={proc.stderr[:150]!r}")

            # Real-interpreter test (skip if no Python 3.9 available): both
            # hook entry points are visible no-ops on 3.9 (allow + loud
            # diagnostic, exit 0), and state_discovery imports cleanly.
            py39 = None
            for candidate in ("python3.9", "/usr/bin/python3"):
                cand_path = shutil.which(candidate) if candidate == "python3.9" else candidate
                if not cand_path or not Path(cand_path).exists():
                    continue
                ver = subprocess.run([cand_path, "-c", "import sys; print(sys.version_info[:2])"],
                                     capture_output=True, text=True)
                if ver.returncode == 0 and ver.stdout.strip() == "(3, 9)":
                    py39 = cand_path
                    break
            if py39 is None:
                print(f"{YELLOW}~ SKIP{NC}: no Python 3.9 interpreter found (real-interpreter guard test)")
            else:
                imp = subprocess.run(
                    [py39, "-c", "import state_discovery"],
                    capture_output=True, text=True, cwd=SCRIPT_DIR,
                )
                if imp.returncode == 0:
                    log_pass("python3.9 imports state_discovery (annotations lazy)")
                else:
                    log_fail("python3.9 imports state_discovery",
                             "rc 0", f"rc={imp.returncode} stderr={imp.stderr[:200]!r}")

                for hook_path in (persistent_mode, session_start_path):
                    proc = subprocess.run(
                        [py39, str(hook_path)],
                        input=json.dumps({"directory": str(test_path)}),
                        capture_output=True, text=True, cwd=SCRIPT_DIR, env=_neutral_env(),
                    )
                    name = f"{hook_path.name} on 3.9: visible no-op (allow + diagnostic)"
                    try:
                        payload = json.loads(proc.stdout.strip())
                    except (json.JSONDecodeError, ValueError):
                        payload = None
                    if (
                        proc.returncode == 0
                        and isinstance(payload, dict)
                        and "decision" not in payload
                        and "unsupported" in payload.get("systemMessage", "")
                        and "[crew]" in proc.stderr
                    ):
                        log_pass(name)
                    else:
                        log_fail(name,
                                 "rc 0, allow JSON + systemMessage, stderr diagnostic",
                                 f"rc={proc.returncode} stdout={proc.stdout[:150]!r} stderr={proc.stderr[:150]!r}")

    finally:
        # Nothing to restore — isolation is the neutral HOME, not a mutation
        # of the developer's real ~/.claude.
        pass

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print()
    print("===========================================")
    print(f"Results: {GREEN}{PASS_COUNT} passed{NC}, {RED}{FAIL_COUNT} failed{NC}")
    print("===========================================")

    if FAIL_COUNT > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
