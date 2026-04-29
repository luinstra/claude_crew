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
            test_equals(
                "No state - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{"continue": true}',
            )

            # Test with active build loop
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "Complete the task", "iteration": 1, "max_iterations": 10, "completion_promise": "DONE"}'
            )

            test_contains(
                "Active build loop - blocks stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '"continue": false',
            )

            test_contains(
                "Active build loop - shows iteration",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                "Build Loop",
            )

            # Test with inactive build loop
            (crew_dir / "build-state.json").write_text(
                '{"active": false, "prompt": "Old task"}'
            )

            test_equals(
                "Inactive build loop - allows stop",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{"continue": true}',
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
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()

            # Test init bl
            stdout, stderr, code = run_crew_state(["init", "bl", "--prompt", "Test task"], test_path)
            if code == 0 and (crew_dir / "build-state.json").exists():
                log_pass("init bl - creates state file")
            else:
                log_fail("init bl - creates state file", "exit 0 and file exists", f"exit {code}, stderr: {stderr}")

            # Test show bl
            stdout, stderr, code = run_crew_state(["show", "bl"], test_path)
            if code == 0 and '"active": true' in stdout and '"prompt": "Test task"' in stdout:
                log_pass("show bl - outputs JSON with expected fields")
            else:
                log_fail("show bl - outputs JSON with expected fields", "JSON with active and prompt", stdout)

            # Test set bl
            stdout, stderr, code = run_crew_state(["set", "bl", "iteration", "5"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl"], test_path)
            if code == 0 and '"iteration": 5' in stdout2:
                log_pass("set bl - changes field value")
            else:
                log_fail("set bl - changes field value", "iteration: 5", stdout2)

            # Test set bl active false (bool coercion)
            stdout, stderr, code = run_crew_state(["set", "bl", "active", "false"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl"], test_path)
            if code == 0 and '"active": false' in stdout2:
                log_pass("set bl - bool coercion works")
            else:
                log_fail("set bl - bool coercion works", "active: false", stdout2)

            # Reset for increment test
            run_crew_state(["set", "bl", "active", "true"], test_path)
            run_crew_state(["set", "bl", "iteration", "5"], test_path)

            # Test increment bl
            stdout, stderr, code = run_crew_state(["increment", "bl", "iteration"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl"], test_path)
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

            # Test init mt
            stdout, stderr, code = run_crew_state(["init", "mt", "--task", "Build auth", "--plan-file", ".crew/plans/auth.md"], test_path)
            stdout2, _, _ = run_crew_state(["show", "mt"], test_path)
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

            # Test show on missing file returns default state
            (crew_dir / "build-state.json").unlink(missing_ok=True)
            stdout, stderr, code = run_crew_state(["show", "bl"], test_path)
            if code == 0 and '"active": false' in stdout:
                log_pass("show bl missing file - returns default state")
            else:
                log_fail("show bl missing file - returns default state", "default state JSON", stdout)

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

            # Verify state has the auto-derived plan file
            stdout2, _, _ = run_crew_state(["show", "mt"], test_path)
            if '"plan_file": ".crew/plans/build-auth-system.md"' in stdout2:
                log_pass("init mt --auto-plan - stores derived path in state")
            else:
                log_fail("init mt --auto-plan - stores derived path in state", "plan_file with derived path", stdout2)

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

            # show bl --session-id s1 shows s1's state
            stdout, stderr, code = run_crew_state(
                ["show", "bl", "--session-id", "s1"], test_path)
            if code == 0 and '"prompt": "Task for s1"' in stdout:
                log_pass("show bl s1 - shows session-specific state")
            else:
                log_fail("show bl s1 - shows session-specific state",
                         "prompt: Task for s1", stdout)

            # deactivate bl --session-id s1 only deactivates s1
            stdout, stderr, code = run_crew_state(
                ["deactivate", "bl", "--reason", "done", "--session-id", "s1"], test_path)
            stdout2, _, _ = run_crew_state(
                ["show", "bl", "--session-id", "s2"], test_path)
            if code == 0 and '"active": true' in stdout2:
                log_pass("deactivate s1 - s2 remains active")
            else:
                log_fail("deactivate s1 - s2 remains active",
                         "s2 still active", stdout2)

            # session_id stored in state
            stdout, stderr, code = run_crew_state(
                ["show", "bl", "--session-id", "s2"], test_path)
            if '"session_id": "s2"' in stdout:
                log_pass("init stores session_id in state JSON")
            else:
                log_fail("init stores session_id in state JSON",
                         'session_id: s2', stdout)

            # CLAUDE_SESSION_ID env var fallback
            env_with_session = {**os.environ, "CLAUDE_PROJECT_DIR": str(test_path), "CLAUDE_SESSION_ID": "s2"}
            result = subprocess.run(
                [sys.executable, str(crew_state), "show", "bl"],
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
                '"continue": false',
            )

            # Stop hook with session_id=s2 allows
            test_equals(
                "Stop hook s2 - allows when only s1 has active loop",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "s2"}),
                '{"continue": true}',
            )

            # Stop hook with empty session_id only checks legacy files
            test_equals(
                "Stop hook no session - allows (no legacy file)",
                persistent_mode,
                json.dumps({"directory": str(test_path)}),
                '{"continue": true}',
            )

            # File with no session_id is claimed by session that has one
            (crew_dir / "build-state-s1.json").unlink()
            (crew_dir / "build-state.json").write_text(
                '{"active": true, "prompt": "legacy task", "iteration": 1, "max_iterations": 10}')

            test_contains(
                "Stop hook session claims file (no session_id in file)",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "any-session"}),
                '"continue": false',
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

            todos_dir = Path.home() / ".claude" / "todos"
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
            # SLUGIFY TESTS
            # =========================================================================
            log_section("slugify()")

            # Import slugify from crew-state.py (hyphenated filename requires importlib)
            import importlib.util
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
            # native Claude Code todo handling covers this now)
            todos_dir = Path.home() / ".claude" / "todos"
            todos_dir.mkdir(parents=True, exist_ok=True)
            test_session_id = f"test-session-{os.getpid()}"
            todo_file = todos_dir / f"{test_session_id}-agent-{test_session_id}.json"
            todo_file.write_text('[{"content": "Test task", "status": "pending"}]')

            try:
                test_equals(
                    "Incomplete todos do NOT block stop (no active loop)",
                    persistent_mode,
                    json.dumps({"directory": str(test_path), "session_id": test_session_id}),
                    '{"continue": true}',
                )
            finally:
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
