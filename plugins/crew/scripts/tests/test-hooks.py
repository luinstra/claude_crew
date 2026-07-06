#!/usr/bin/env python3
"""
Test script for claude-crew hooks
Run from project root: python3 plugins/crew/scripts/tests/test-hooks.py
"""

import json
import os
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


def main():
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
                '{"active": true, "prompt": "Test task", "iteration": 2, "max_iterations": 10}'
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
                '{"active": true, "task_description": "Design auth system", "plan_file": ".crew/plans/auth-system.md", "iteration": 3, "max_iterations": 10}'
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
                f.unlink()

            # Test with no state - should allow stop
            # Allow shape is the benign {} — asserts nothing about the
            # ambiguous "continue" field (2026-07-06 C2 smoke).
            test_equals(
                "No state - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{}',
            )

            # Test with active build loop
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "iteration": 1, "max_iterations": 10, "completion_promise": "DONE"}'
            )

            # Block shape pinned by the 2026-07-06 C2 live smoke: the legacy
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
                "Active build loop - shows iteration",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Build Loop",
            )

            # Phase 3 (C3): the first-fire nudge prescribes the multi-model
            # panel flow, not the retired lone-advisor flow. Reset to
            # iteration 1 so the full recipe (not the terse variant) renders.
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "iteration": 1, "max_iterations": 10, "completion_promise": "DONE"}'
            )
            test_contains(
                "Build loop nudge - prescribes panel (review-prep)",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "review-prep",
            )
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "iteration": 1, "max_iterations": 10, "completion_promise": "DONE"}'
            )
            test_not_contains(
                "Build loop nudge - no lone-advisor recipe",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Spawn advisor",
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
                '{"active": true, "task_description": "Design auth", "plan_file": ".crew/plans/auth.md", "iteration": 1, "max_iterations": 10}'
            )

            test_contains(
                "Active measure-twice loop - blocks stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '"decision": "block", "reason":',
            )

            test_contains(
                "Active measure-twice loop - shows iteration",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Measure-Twice Loop",
            )

            # Phase 3 (C3): measure-twice first-fire nudge prescribes the
            # panel flow, not the retired lone-advisor re-review.
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design auth", "plan_file": ".crew/plans/auth.md", "iteration": 1, "max_iterations": 10}'
            )
            test_contains(
                "Measure-twice nudge - prescribes panel (review-prep)",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "review-prep",
            )
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design auth", "plan_file": ".crew/plans/auth.md", "iteration": 1, "max_iterations": 10}'
            )
            test_not_contains(
                "Measure-twice nudge - no lone-advisor recipe",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Spawn advisor",
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
                '{"active": true, "prompt": "Build task", "iteration": 1, "max_iterations": 10}'
            )
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Navel task", "plan_file": ".crew/plans/test.md", "iteration": 1, "max_iterations": 10}'
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
                f.unlink()

            # iteration=1 (first Stop fire): display reads "1/N", full recipe present
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Fix auth bug", "iteration": 1, "max_iterations": 10}'
            )
            out_terse_iter1 = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "verify via the multi-model panel" in out_terse_iter1:
                log_pass("Terse mode + iteration 1 (bl): full recipe present")
            else:
                log_fail("Terse mode + iteration 1 (bl): full recipe present",
                         "contains 'verify via the multi-model panel'", out_terse_iter1[:300])

            # First Stop fire displays "Iteration 1/10" (display-then-increment)
            if "Iteration 1/10" in out_terse_iter1:
                log_pass("First Stop fire (bl) displays 'Iteration 1/10'")
            else:
                log_fail("First Stop fire (bl) displays 'Iteration 1/10'",
                         "contains 'Iteration 1/10'", out_terse_iter1[:300])

            # State now has iteration=2. Terse second fire: recipe absent
            out_terse_iter2 = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "verify via the multi-model panel" not in out_terse_iter2:
                log_pass("Terse mode + iteration 2 (bl): recipe absent")
            else:
                log_fail("Terse mode + iteration 2 (bl): recipe absent",
                         "does NOT contain 'verify via the multi-model panel'", out_terse_iter2[:300])

            if "cancel-build" not in out_terse_iter2:
                log_pass("Terse mode + iteration 2 (bl): cancel-build absent")
            else:
                log_fail("Terse mode + iteration 2 (bl): cancel-build absent",
                         "does NOT contain 'cancel-build'", out_terse_iter2[:300])

            # Second Stop fire displays "Iteration 2/10"
            if "Iteration 2/10" in out_terse_iter2 and "Fix auth bug" in out_terse_iter2:
                log_pass("Second Stop fire (bl) displays 'Iteration 2/10' with task")
            else:
                log_fail("Second Stop fire (bl) displays 'Iteration 2/10' with task",
                         "contains 'Iteration 2/10' and task", out_terse_iter2[:300])

            # Verbose mode (state now iteration=3): full recipe present despite iteration>1
            out_verbose_iter3 = run_script_verbose(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "verify via the multi-model panel" in out_verbose_iter3:
                log_pass("Verbose mode + iteration 3 (bl): full recipe present")
            else:
                log_fail("Verbose mode + iteration 3 (bl): full recipe present",
                         "contains 'verify via the multi-model panel'", out_verbose_iter3[:300])

            # Safety cap: exactly max_iterations nudges. State is at iteration=4 now.
            # Run 7 more terse nudges → display 4/10..10/10, leaving iteration=11.
            for _ in range(7):
                run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            out_safety = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "Safety Limit Reached" in out_safety:
                log_pass("Safety cap fires after exactly max_iterations nudges (10/10 displayed)")
            else:
                log_fail("Safety cap fires after exactly max_iterations nudges",
                         "contains 'Safety Limit Reached'", out_safety[:300])

            # Prompt truncation: 500-char prompt → exactly 120 chars in reason
            long_prompt = "A" * 500
            (crew_dir / "build-state.json").write_text(
                json.dumps({"active": True, "prompt": long_prompt, "iteration": 5, "max_iterations": 10})
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

            # Measure-twice terse/verbose
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design profiles", "plan_file": ".crew/plans/profiles.md", "iteration": 1, "max_iterations": 10}'
            )
            out_mt_iter1 = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "When APPROVED" in out_mt_iter1 and "Iteration 1/10" in out_mt_iter1:
                log_pass("Terse mode + iteration 1 (mt): full recipe + 'Iteration 1/10'")
            else:
                log_fail("Terse mode + iteration 1 (mt): full recipe + 'Iteration 1/10'",
                         "contains 'When APPROVED' and 'Iteration 1/10'", out_mt_iter1[:300])

            out_mt_iter2 = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if ("When APPROVED" not in out_mt_iter2 and "cancel-measure-twice" not in out_mt_iter2
                    and "Design profiles" in out_mt_iter2 and ".crew/plans/profiles.md" in out_mt_iter2):
                log_pass("Terse mode + iteration 2 (mt): recipe absent, task+plan present")
            else:
                log_fail("Terse mode + iteration 2 (mt): recipe absent, task+plan present",
                         "no recipe, task and plan present", out_mt_iter2[:300])

            out_mt_v = run_script_verbose(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "When APPROVED" in out_mt_v:
                log_pass("Verbose mode (mt): full recipe present")
            else:
                log_fail("Verbose mode (mt): full recipe present",
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

            # Test show bl (compact default)
            stdout, stderr, code = run_crew_state(["show", "bl"], test_path)
            if code == 0 and stdout.startswith("loop=bl") and "active=true" in stdout and "iter=" in stdout and 'task=' in stdout:
                log_pass("show bl - compact one-line summary (default)")
            else:
                log_fail("show bl - compact one-line summary (default)",
                         "single line starting with loop=bl, active=, iter=, task=", stdout)

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

            # Test set bl (use --verbose for JSON assertion)
            stdout, stderr, code = run_crew_state(["set", "bl", "iteration", "5"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl", "--verbose"], test_path)
            if code == 0 and '"iteration": 5' in stdout2:
                log_pass("set bl - changes field value")
            else:
                log_fail("set bl - changes field value", "iteration: 5", stdout2)

            # Test set bl active false (bool coercion, use --verbose for JSON assertion)
            stdout, stderr, code = run_crew_state(["set", "bl", "active", "false"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl", "--verbose"], test_path)
            if code == 0 and '"active": false' in stdout2:
                log_pass("set bl - bool coercion works")
            else:
                log_fail("set bl - bool coercion works", "active: false", stdout2)

            # Reset for increment test
            run_crew_state(["set", "bl", "active", "true"], test_path)
            run_crew_state(["set", "bl", "iteration", "5"], test_path)

            # Test increment bl (use --verbose for JSON assertion)
            stdout, stderr, code = run_crew_state(["increment", "bl", "iteration"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl", "--verbose"], test_path)
            if code == 0 and '"iteration": 6' in stdout2:
                log_pass("increment bl - bumps counter")
            else:
                log_fail("increment bl - bumps counter", "iteration: 6", stdout2)

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
            if code == 1 and "--prompt required" in stderr:
                log_pass("init bl without --prompt - exits with error")
            else:
                log_fail("init bl without --prompt - exits with error", "exit 1 with error message", f"exit {code}, stderr: {stderr}")

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
            # SESSION-SCOPED PERSISTENT MODE TESTS
            # =========================================================================
            log_section("persistent-mode.py (session-scoped)")

            # s1 has active bl, s2 does not
            (crew_dir / "build-state-s1.json").write_text(
                '{"active": true, "prompt": "s1 task", "iteration": 1, "max_iterations": 10, "session_id": "s1"}')

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
                '{"active": true, "prompt": "legacy task", "iteration": 1, "max_iterations": 10}')

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
                '{"active": true, "prompt": "My task", "iteration": 2, "max_iterations": 10, "session_id": "mysess"}')

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

            # Clean up
            for f in crew_dir.glob("*-state*.json"):
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
