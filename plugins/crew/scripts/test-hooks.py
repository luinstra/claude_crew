#!/usr/bin/env python3
"""
Test script for claude-crew hooks
Run from project root: python3 scripts/test-hooks.py
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

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent


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


def run_script(script: Path, input_data: str) -> str:
    """Run a Python script with input and return output."""
    result = subprocess.run(
        [sys.executable, str(script)],
        input=input_data,
        capture_output=True,
        text=True,
        cwd=SCRIPT_DIR,  # Run from scripts dir so imports work
    )
    return result.stdout.strip()


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


def main():
    # Isolate from global state by temporarily renaming todos directory
    home = Path.home()
    global_todos = home / ".claude" / "todos"
    backup_todos = home / ".claude" / "todos.test-backup"

    # Move global todos out of the way if they exist
    has_backup = False
    if global_todos.exists():
        global_todos.rename(backup_todos)
        has_backup = True

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

            test_contains(
                "Detects Kotlin stack - shows skills with IMPORTANT",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "sk:kotlin",
            )

            test_contains(
                "Kotlin stack triggers IMPORTANT wrapper",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "<IMPORTANT>",
            )

            # Clean up kotlin file
            (test_path / "Test.kt").unlink()

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

            # Test with feedback loop state
            (crew_dir / "feedback-loop-state.json").write_text(
                '{"active": true, "prompt": "Test task", "iteration": 2, "max_iterations": 10}'
            )

            test_contains(
                "Detects active feedback loop",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "Feedback Loop Active",
            )

            # Clean up feedback loop state for next test
            (crew_dir / "feedback-loop-state.json").unlink()

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

            # Clean up measure-twice state for next test
            (crew_dir / "measure-twice-state.json").unlink()

            # Test with session breadcrumb
            (crew_dir / "session-breadcrumb.json").write_text(
                '{"last_session": "2024-01-15T10:00:00", "directory": "/test", "clean_exit": true}'
            )

            test_contains(
                "Detects session breadcrumb",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "Previous Session",
            )

            # =========================================================================
            # PERSISTENT MODE TESTS
            # =========================================================================
            log_section("persistent-mode.py")
            persistent_mode = SCRIPT_DIR / "persistent-mode.py"

            # Clean test directory
            for f in crew_dir.glob("*"):
                f.unlink()

            # Test with no state - should allow stop
            test_equals(
                "No state - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{"continue": true}',
            )

            # Test with active feedback loop
            (crew_dir / "feedback-loop-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "iteration": 1, "max_iterations": 10, "completion_promise": "DONE"}'
            )

            test_contains(
                "Active feedback loop - blocks stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '"continue": false',
            )

            test_contains(
                "Active feedback loop - shows iteration",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Feedback Loop",
            )

            # Test with inactive feedback loop
            (crew_dir / "feedback-loop-state.json").write_text(
                '{"active": false, "prompt": "Old task"}'
            )

            test_equals(
                "Inactive feedback loop - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{"continue": true}',
            )

            # Clean up feedback loop state
            (crew_dir / "feedback-loop-state.json").unlink()

            # Test with active measure-twice loop
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Design auth", "plan_file": ".crew/plans/auth.md", "iteration": 1, "max_iterations": 10}'
            )

            test_contains(
                "Active measure-twice loop - blocks stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '"continue": false',
            )

            test_contains(
                "Active measure-twice loop - shows iteration",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Measure-Twice Loop",
            )

            # Test with inactive measure-twice loop
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": false, "task_description": "Old plan"}'
            )

            test_equals(
                "Inactive measure-twice loop - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{"continue": true}',
            )

            # Test priority: feedback-loop takes precedence over measure-twice
            (crew_dir / "feedback-loop-state.json").write_text(
                '{"active": true, "prompt": "Feedback task", "iteration": 1, "max_iterations": 10}'
            )
            (crew_dir / "measure-twice-state.json").write_text(
                '{"active": true, "task_description": "Navel task", "plan_file": ".crew/plans/test.md", "iteration": 1, "max_iterations": 10}'
            )

            test_contains(
                "Feedback-loop takes priority over measure-twice",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Feedback Loop",  # Should show feedback loop, not measure-twice
            )

            # Clean up both state files
            (crew_dir / "feedback-loop-state.json").unlink()
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
                    env={**os.environ, "CLAUDE_PROJECT_DIR": str(cwd)},
                )
                return result.stdout.strip(), result.stderr.strip(), result.returncode

            # Clean up any existing state
            for f in crew_dir.glob("*-state.json"):
                f.unlink()

            # Test init fl
            stdout, stderr, code = run_crew_state(["init", "fl", "--prompt", "Test task"], test_path)
            if code == 0 and (crew_dir / "feedback-loop-state.json").exists():
                log_pass("init fl - creates state file")
            else:
                log_fail("init fl - creates state file", "exit 0 and file exists", f"exit {code}, stderr: {stderr}")

            # Test show fl
            stdout, stderr, code = run_crew_state(["show", "fl"], test_path)
            if code == 0 and '"active": true' in stdout and '"prompt": "Test task"' in stdout:
                log_pass("show fl - outputs JSON with expected fields")
            else:
                log_fail("show fl - outputs JSON with expected fields", "JSON with active and prompt", stdout)

            # Test set fl
            stdout, stderr, code = run_crew_state(["set", "fl", "iteration", "5"], test_path)
            stdout2, _, _ = run_crew_state(["show", "fl"], test_path)
            if code == 0 and '"iteration": 5' in stdout2:
                log_pass("set fl - changes field value")
            else:
                log_fail("set fl - changes field value", "iteration: 5", stdout2)

            # Test set fl active false (bool coercion)
            stdout, stderr, code = run_crew_state(["set", "fl", "active", "false"], test_path)
            stdout2, _, _ = run_crew_state(["show", "fl"], test_path)
            if code == 0 and '"active": false' in stdout2:
                log_pass("set fl - bool coercion works")
            else:
                log_fail("set fl - bool coercion works", "active: false", stdout2)

            # Reset for increment test
            run_crew_state(["set", "fl", "active", "true"], test_path)
            run_crew_state(["set", "fl", "iteration", "5"], test_path)

            # Test increment fl
            stdout, stderr, code = run_crew_state(["increment", "fl", "iteration"], test_path)
            stdout2, _, _ = run_crew_state(["show", "fl"], test_path)
            if code == 0 and '"iteration": 6' in stdout2:
                log_pass("increment fl - bumps counter")
            else:
                log_fail("increment fl - bumps counter", "iteration: 6", stdout2)

            # Test deactivate fl
            stdout, stderr, code = run_crew_state(["deactivate", "fl", "--reason", "Test done"], test_path)
            with open(crew_dir / "feedback-loop-state.json") as f:
                data = json.load(f)
            if code == 0 and data.get("active") == False and "completed_at" in data and data.get("reason") == "Test done":
                log_pass("deactivate fl - sets active=false and adds metadata")
            else:
                log_fail("deactivate fl - sets active=false and adds metadata", "active=false, completed_at, reason", json.dumps(data))

            # Test init mt
            stdout, stderr, code = run_crew_state(["init", "mt", "--task", "Build auth", "--plan-file", ".crew/plans/auth.md"], test_path)
            stdout2, _, _ = run_crew_state(["show", "mt"], test_path)
            if code == 0 and '"task_description": "Build auth"' in stdout2:
                log_pass("init mt - creates measure-twice state")
            else:
                log_fail("init mt - creates measure-twice state", "task_description field", stdout2)

            # Test error: missing --prompt for fl (clean up first to avoid conflict error)
            for f in crew_dir.glob("*-state.json"):
                f.unlink()
            stdout, stderr, code = run_crew_state(["init", "fl"], test_path)
            if code == 1 and "--prompt required" in stderr:
                log_pass("init fl without --prompt - exits with error")
            else:
                log_fail("init fl without --prompt - exits with error", "exit 1 with error message", f"exit {code}, stderr: {stderr}")

            # Test error: invalid field name
            stdout, stderr, code = run_crew_state(["set", "fl", "nonexistent_field", "value"], test_path)
            if code == 1 and "has no field" in stderr:
                log_pass("set invalid field - exits with error")
            else:
                log_fail("set invalid field - exits with error", "exit 1 with error message", f"exit {code}, stderr: {stderr}")

            # Test show on missing file returns default state
            (crew_dir / "feedback-loop-state.json").unlink(missing_ok=True)
            stdout, stderr, code = run_crew_state(["show", "fl"], test_path)
            if code == 0 and '"active": false' in stdout:
                log_pass("show fl missing file - returns default state")
            else:
                log_fail("show fl missing file - returns default state", "default state JSON", stdout)

            # Test is-active when inactive (no state file)
            (crew_dir / "feedback-loop-state.json").unlink(missing_ok=True)
            stdout, stderr, code = run_crew_state(["is-active", "fl"], test_path)
            if code == 1:
                log_pass("is-active fl (inactive) - exits with code 1")
            else:
                log_fail("is-active fl (inactive) - exits with code 1", "exit 1", f"exit {code}")

            # Test is-active when active (clean up first to allow init)
            for f in crew_dir.glob("*-state.json"):
                f.unlink()
            run_crew_state(["init", "fl", "--prompt", "Test"], test_path)
            stdout, stderr, code = run_crew_state(["is-active", "fl"], test_path)
            if code == 0:
                log_pass("is-active fl (active) - exits with code 0")
            else:
                log_fail("is-active fl (active) - exits with code 0", "exit 0", f"exit {code}")

            # Test --auto-plan for mt (clean up all state to avoid conflicts)
            for f in crew_dir.glob("*-state.json"):
                f.unlink()
            stdout, stderr, code = run_crew_state(["init", "mt", "--task", "Build Auth System", "--auto-plan"], test_path)
            if code == 0 and ".crew/plans/build-auth-system.md" in stdout:
                log_pass("init mt --auto-plan - derives plan filename")
            else:
                log_fail("init mt --auto-plan - derives plan filename", ".crew/plans/build-auth-system.md", stdout)

            # Verify state has the auto-derived plan file
            stdout2, _, _ = run_crew_state(["show", "mt"], test_path)
            if '"plan_file": ".crew/plans/build-auth-system.md"' in stdout2:
                log_pass("init mt --auto-plan - stores derived path in state")
            else:
                log_fail("init mt --auto-plan - stores derived path in state", "plan_file with derived path", stdout2)

            # Test check-conflicts with no active loops
            for f in crew_dir.glob("*-state.json"):
                f.unlink()
            stdout, stderr, code = run_crew_state(["check-conflicts"], test_path)
            if code == 0:
                log_pass("check-conflicts (no active) - exits with code 0")
            else:
                log_fail("check-conflicts (no active) - exits with code 0", "exit 0", f"exit {code}, stderr: {stderr}")

            # Test check-conflicts with fl active
            run_crew_state(["init", "fl", "--prompt", "Test"], test_path)
            stdout, stderr, code = run_crew_state(["check-conflicts"], test_path)
            if code == 1 and "feedback-loop is already active" in stderr:
                log_pass("check-conflicts (fl active) - exits with error")
            else:
                log_fail("check-conflicts (fl active) - exits with error", "exit 1 with fl error", f"exit {code}, stderr: {stderr}")

            # Test check-conflicts with mt active
            (crew_dir / "feedback-loop-state.json").unlink(missing_ok=True)
            run_crew_state(["init", "mt", "--task", "Test", "--auto-plan"], test_path)
            stdout, stderr, code = run_crew_state(["check-conflicts"], test_path)
            if code == 1 and "measure-twice loop is already active" in stderr:
                log_pass("check-conflicts (mt active) - exits with error")
            else:
                log_fail("check-conflicts (mt active) - exits with error", "exit 1 with mt error", f"exit {code}, stderr: {stderr}")

            # Test init rejects when another loop is active
            stdout, stderr, code = run_crew_state(["init", "fl", "--prompt", "Should fail"], test_path)
            if code == 1 and "measure-twice loop is already active" in stderr:
                log_pass("init fl (mt active) - rejects with conflict error")
            else:
                log_fail("init fl (mt active) - rejects with conflict error", "exit 1 with conflict", f"exit {code}, stderr: {stderr}")

            # Clean up state files for subsequent tests
            for f in crew_dir.glob("*-state.json"):
                f.unlink()

            # Test with incomplete todos (create in home todos dir)
            todos_dir = Path.home() / ".claude" / "todos"
            todos_dir.mkdir(parents=True, exist_ok=True)
            todo_file = todos_dir / f"test-session-{os.getpid()}.json"

            todo_file.write_text('[{"content": "Test task", "status": "pending"}]')

            try:
                test_contains(
                    "Incomplete todos - blocks stop",
                    persistent_mode,
                    json.dumps({"directory": str(test_path)}),
                    "tasks remaining",
                )
            finally:
                # Clean up todo file
                todo_file.unlink(missing_ok=True)

    finally:
        # Restore global todos
        if has_backup and backup_todos.exists():
            # Remove any test todos directory if it was created
            if global_todos.exists():
                import shutil
                shutil.rmtree(global_todos)
            backup_todos.rename(global_todos)

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
