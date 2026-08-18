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
    # These subprocesses run with cwd=SCRIPT_DIR, but a hook resolves its `.crew`
    # via crew_base() (CLAUDE_PROJECT_DIR, else cwd, except a terminal `.crew`
    # fallback cwd re-anchors to its parent with a one-time stderr advisory) and
    # the payload no longer carries a directory hint. So mimic the harness: derive
    # CLAUDE_PROJECT_DIR from the stdin payload's `directory`/`cwd` when a test did not pin it. The
    # parse is defensive (some tests feed malformed/non-JSON stdin on purpose)
    # and only a non-empty STRING sets it, so `{"directory": 123}` does not.
    if "CLAUDE_PROJECT_DIR" not in env:
        try:
            _payload = json.loads(input_data)
        except (json.JSONDecodeError, ValueError, TypeError):
            _payload = None
        if isinstance(_payload, dict):
            _proj = _payload.get("directory") or _payload.get("cwd")
            if isinstance(_proj, str) and _proj:
                env["CLAUDE_PROJECT_DIR"] = _proj
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
    # channel the post-import crash handler (session-start.py bottom) emits on;
    # the pre-import version-guard fallback emits systemMessage instead only
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

    _cursor_block = json.loads(HookResult.block("stop reason").to_cursor_json())
    _cursor_allow = json.loads(HookResult.allow_with_diagnostic("diagnostic").to_cursor_json())
    _cursor_context = json.loads(
        SessionStartResult.with_context("cursor context").to_cursor_json()
    )
    if (_cursor_block == {"followup_message": "stop reason"}
            and _cursor_allow == {}
            and _cursor_context == {"additional_context": "cursor context"}):
        log_pass("Cursor serializers emit followup, allow, and additional_context shapes")
    else:
        log_fail(
            "Cursor serializers emit followup, allow, and additional_context shapes",
            "Cursor-native JSON shapes",
            repr((_cursor_block, _cursor_allow, _cursor_context)),
        )

    import host_detect as _host_detect
    from host_detect import (
        _CLAUDE_HOST_MARKERS,
        _CODEX_HOST_MARKERS,
        _CURSOR_HOST_MARKERS,
        _detect_host,
    )
    from multiagent import channels as _channels
    _host_matrix = (
        ({"CREW_HOST": "CuRsOr"}, "cursor"),
        ({"CREW_HOST": "CoDeX"}, "codex"),
        ({"CREW_HOST": "claude"}, "claude"),
        ({"CREW_HOST": "invalid"}, "unknown"),
        ({"CODEX_THREAD_ID": "1", "CLAUDECODE": "1"}, "codex"),
        ({"CLAUDECODE": "1"}, "claude"),
        ({}, "unknown"),
    )
    if all(_detect_host(env) == _channels._detect_host(env) == expected
           for env, expected in _host_matrix):
        log_pass("host_detect agrees with channels across the precedence matrix")
    else:
        log_fail("host_detect agrees with channels across the precedence matrix",
                 "matching host names", repr([(_detect_host(env), _channels._detect_host(env))
                                                for env, _ in _host_matrix]))
    if (_CODEX_HOST_MARKERS == _channels._CODEX_HOST_MARKERS
            and _CURSOR_HOST_MARKERS == _channels._CURSOR_HOST_MARKERS
            and _CLAUDE_HOST_MARKERS == _channels._CLAUDE_HOST_MARKERS
            and "import multiagent" not in (SCRIPT_DIR / "host_detect.py").read_text()
            and "from multiagent" not in (SCRIPT_DIR / "host_detect.py").read_text()):
        log_pass("host_detect marker tuples are literal-equal and engine-free")
    else:
        log_fail("host_detect marker tuples are literal-equal and engine-free",
                 "matching marker tuples and no multiagent import",
                 repr((_CODEX_HOST_MARKERS, _channels._CODEX_HOST_MARKERS,
                       _CURSOR_HOST_MARKERS, _channels._CURSOR_HOST_MARKERS,
                       _CLAUDE_HOST_MARKERS, _channels._CLAUDE_HOST_MARKERS)))

    _saved_host_cursor_markers = _host_detect._CURSOR_HOST_MARKERS
    _saved_channels_cursor_markers = _channels._CURSOR_HOST_MARKERS
    try:
        _host_detect._CURSOR_HOST_MARKERS = ("CURSOR_SESSION_MARKER",)
        _channels._CURSOR_HOST_MARKERS = ("CURSOR_SESSION_MARKER",)
        _cursor_marker_env = {"CURSOR_SESSION_MARKER": "1"}
        _cursor_marker_ok = (
            _host_detect._detect_host(_cursor_marker_env) == "cursor"
            and _channels._detect_host(_cursor_marker_env) == "cursor"
        )
    finally:
        _host_detect._CURSOR_HOST_MARKERS = _saved_host_cursor_markers
        _channels._CURSOR_HOST_MARKERS = _saved_channels_cursor_markers
    if _cursor_marker_ok:
        log_pass("host detectors agree when cursor markers are populated")
    else:
        log_fail("host detectors agree when cursor markers are populated",
                 "both detectors return cursor", repr(_cursor_marker_env))

    _session_start_script = SCRIPT_DIR / "session-start.py"
    _persistent_script = SCRIPT_DIR / "persistent-mode.py"
    with tempfile.TemporaryDirectory() as _cursor_emit_dir:
        _cursor_emit_root = Path(_cursor_emit_dir)
        _cursor_emit_crew = _cursor_emit_root / ".crew"
        _cursor_emit_crew.mkdir()
        (_cursor_emit_crew / "build-state.json").write_text(
            '{"active": true, "task": "cursor emit", "schema": 3}'
        )
        _session_cursor = run_script(
            _session_start_script,
            json.dumps({"directory": str(_cursor_emit_root)}),
            extra_env={"CREW_HOST": "cursor"},
        )
        _stop_cursor = run_script(
            _persistent_script,
            json.dumps({"directory": str(_cursor_emit_root), "session_id": "s1"}),
            extra_env={"CREW_HOST": "cursor"},
        )
        try:
            _session_cursor_obj = json.loads(_session_cursor)
            _stop_cursor_obj = json.loads(_stop_cursor)
        except json.JSONDecodeError:
            _session_cursor_obj = {}
            _stop_cursor_obj = {}
        if ("additional_context" in _session_cursor_obj
                and "hookSpecificOutput" not in _session_cursor_obj
                and "followup_message" in _stop_cursor_obj
                and "decision" not in _stop_cursor_obj):
            log_pass("both hook entry scripts emit Cursor shapes under CREW_HOST=cursor")
        else:
            log_fail("both hook entry scripts emit Cursor shapes under CREW_HOST=cursor",
                     "additional_context and followup_message only",
                     repr((_session_cursor_obj, _stop_cursor_obj)))

    with tempfile.TemporaryDirectory() as _claude_emit_dir:
        _claude_emit_root = Path(_claude_emit_dir)
        _claude_emit_crew = _claude_emit_root / ".crew"
        _claude_emit_crew.mkdir()
        (_claude_emit_crew / "build-state.json").write_text(
            '{"active": true, "task": "claude emit", "schema": 3}'
        )
        _session_claude = run_script(
            _session_start_script,
            json.dumps({"directory": str(_claude_emit_root)}),
            extra_env={"CREW_HOST": "claude"},
        )
        _stop_claude = run_script(
            _persistent_script,
            json.dumps({"directory": str(_claude_emit_root), "session_id": "s1"}),
            extra_env={"CREW_HOST": "claude"},
        )
        try:
            _session_claude_obj = json.loads(_session_claude)
            _stop_claude_obj = json.loads(_stop_claude)
        except json.JSONDecodeError:
            _session_claude_obj = {}
            _stop_claude_obj = {}
        if ("hookSpecificOutput" in _session_claude_obj
                and "additional_context" not in _session_claude_obj
                and _stop_claude_obj.get("decision") == "block"
                and "followup_message" not in _stop_claude_obj
                and _session_claude == SessionStartResult.with_context(
                    _session_claude_obj["hookSpecificOutput"]["additionalContext"]
                ).to_json()
                and _stop_claude == HookResult.block(
                    _stop_claude_obj["reason"]
                ).to_json()):
            log_pass("Claude host hook stdout keeps the existing JSON bytes")
        else:
            log_fail("Claude host hook stdout keeps the existing JSON bytes",
                     "Claude serializer bytes and legacy keys",
                     repr((_session_claude_obj, _stop_claude_obj)))

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

    # =========================================================================
    # MODELS: record_hook_payload_keys, the CREW_HOOK_DEBUG payload-key capture
    # aid for both hook entries. SessionStart records after parsing; Stop records
    # once in main()'s finally after mutation. Three contracts: gated-off touches
    # nothing, gated-on writes key names plus only the Stop whitelist values and
    # post-mutation crew extras, and a write failure is swallowed (it must never
    # raise into a hook or pollute the hook's stdout JSON).
    # =========================================================================
    log_section("models.record_hook_payload_keys (CREW_HOOK_DEBUG capture aid)")
    import contextlib
    import io
    from models import record_hook_payload_keys

    _hpk_payload = {
        "session_id": "",
        "transcript_path": "/secret/SENTINEL-VALUE-123.jsonl",
        "hook_event_name": "SomeEvent",
    }

    _saved_debug = os.environ.pop("CREW_HOOK_DEBUG", None)
    _saved_proj = os.environ.get("CLAUDE_PROJECT_DIR")
    try:
        # (a) gated off: no file is created, no .crew dir is minted
        with tempfile.TemporaryDirectory() as _hpk_dir:
            os.environ["CLAUDE_PROJECT_DIR"] = _hpk_dir
            record_hook_payload_keys("Stop", _hpk_payload)
            _hpk_file = Path(_hpk_dir) / ".crew" / "hook-payload-keys.txt"
            if not _hpk_file.exists():
                log_pass("record_hook_payload_keys: gate unset -> no file created")
            else:
                log_fail("record_hook_payload_keys: gate unset -> no file created",
                         "no hook-payload-keys.txt", f"file exists: {_hpk_file}")

        # (b) gated on: one line with sorted key names, (empty) suffix on the
        # empty session_id, and NO payload value anywhere in the file
        with tempfile.TemporaryDirectory() as _hpk_dir:
            os.environ["CLAUDE_PROJECT_DIR"] = _hpk_dir
            os.environ["CREW_HOOK_DEBUG"] = "1"
            record_hook_payload_keys("Stop", _hpk_payload)
            _hpk_file = Path(_hpk_dir) / ".crew" / "hook-payload-keys.txt"
            _hpk_text = _hpk_file.read_text(encoding="utf-8") if _hpk_file.exists() else ""
            if (" Stop: " in _hpk_text
                    and "session_id(empty)" in _hpk_text
                    and "transcript_path" in _hpk_text
                    and "hook_event_name" in _hpk_text
                    and "SENTINEL-VALUE-123" not in _hpk_text
                    and "SomeEvent" not in _hpk_text):
                log_pass("record_hook_payload_keys: gate set -> key names + (empty) suffix, no values")
            else:
                log_fail("record_hook_payload_keys: gate set -> key names + (empty) suffix, no values",
                         "line with Stop, session_id(empty), other key names, no values",
                         repr(_hpk_text))

        # (c) never raises: the .crew path is occupied by a FILE, so mkdir and
        # the append both fail; the helper must swallow it and print nothing
        # (hooks own stdout, so even a diagnostic there would corrupt the JSON)
        with tempfile.TemporaryDirectory() as _hpk_dir:
            os.environ["CLAUDE_PROJECT_DIR"] = _hpk_dir
            os.environ["CREW_HOOK_DEBUG"] = "1"
            (Path(_hpk_dir) / ".crew").write_text("not a dir", encoding="utf-8")
            _hpk_out = io.StringIO()
            try:
                with contextlib.redirect_stdout(_hpk_out):
                    record_hook_payload_keys("SessionStart", _hpk_payload)
                _hpk_raised = None
            except Exception as exc:
                _hpk_raised = exc
            if _hpk_raised is None and _hpk_out.getvalue() == "":
                log_pass("record_hook_payload_keys: write failure -> swallowed, stdout untouched")
            else:
                log_fail("record_hook_payload_keys: write failure -> swallowed, stdout untouched",
                         "no exception, empty stdout",
                         f"raised={_hpk_raised!r}, stdout={_hpk_out.getvalue()!r}")
    finally:
        if _saved_debug is None:
            os.environ.pop("CREW_HOOK_DEBUG", None)
        else:
            os.environ["CREW_HOOK_DEBUG"] = _saved_debug
        if _saved_proj is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = _saved_proj

    with tempfile.TemporaryDirectory() as _value_dir:
        os.environ["CLAUDE_PROJECT_DIR"] = _value_dir
        os.environ["CREW_HOOK_DEBUG"] = "1"
        record_hook_payload_keys(
            "Stop",
            {"loop_count": 3, "status": "blocked", "crew.stop_fires": 99,
             "transcript_path": "/private/path"},
            extra={"crew.stop_fires": 2, "crew.parked_fires": 0, "crew.emit": "followup"},
        )
        record_hook_payload_keys("Stop", {}, extra={"crew.emit": True})
        record_hook_payload_keys(
            "SessionStart",
            {"status": "blocked", "loop_count": 3},
        )
        _value_lines = (
            Path(_value_dir) / ".crew" / "hook-payload-keys.txt"
        ).read_text(encoding="utf-8").splitlines()
        _value_ok = (
            len(_value_lines) == 3
            and "loop_count=3" in _value_lines[0]
            and "status=blocked" in _value_lines[0]
            and "crew.stop_fires=2" in _value_lines[0]
            and "crew.parked_fires=0" in _value_lines[0]
            and "crew.emit=followup" in _value_lines[0]
            and "crew.stop_fires=99" not in _value_lines[0]
            and "crew.emit=True" in _value_lines[1]
            and "status" in _value_lines[2]
            and "status=blocked" not in _value_lines[2]
            and "loop_count=3" not in _value_lines[2]
            and "/private/path" not in "\n".join(_value_lines)
        )
        if _value_ok:
            log_pass("record_hook_payload_keys: stop whitelist values and extra mapping are scoped")
        else:
            log_fail("record_hook_payload_keys: stop whitelist values and extra mapping are scoped",
                     "stop values rendered, other events names-only",
                     repr(_value_lines))
        os.environ.pop("CREW_HOOK_DEBUG", None)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    # The recorder is called once for each normal and early-return path. The
    # exception path is checked through the module directly because its outer
    # entry-point handler owns the final allow payload.
    def _capture_lines(state_text=None, payload=None):
        with tempfile.TemporaryDirectory() as _capture_dir:
            _capture_root = Path(_capture_dir)
            if state_text is not None:
                (_capture_root / ".crew").mkdir()
                (_capture_root / ".crew" / "build-state.json").write_text(state_text)
            _payload = payload or {"directory": str(_capture_root), "session_id": "s1"}
            _output = run_script(
                SCRIPT_DIR / "persistent-mode.py",
                json.dumps(_payload),
                extra_env={"CREW_HOOK_DEBUG": "1"},
            )
            _keys = _capture_root / ".crew" / "hook-payload-keys.txt"
            return _output, (_keys.read_text().splitlines() if _keys.exists() else [])

    _unarmed_output, _unarmed_lines = _capture_lines()
    _armed_output, _armed_lines = _capture_lines(
        '{"active": true, "task": "record", "schema": 3}'
    )
    # The parked case uses a payload rooted at its temporary directory.
    with tempfile.TemporaryDirectory() as _park_dir:
        _park_root = Path(_park_dir)
        (_park_root / ".crew").mkdir()
        (_park_root / ".crew" / "build-state.json").write_text(
            '{"active": true, "task": "park", "schema": 3}'
        )
        _parked_output = run_script(
            SCRIPT_DIR / "persistent-mode.py",
            json.dumps({"directory": str(_park_root), "session_id": "s1",
                         "background_tasks": [{"status": "running"}]}),
            extra_env={"CREW_HOOK_DEBUG": "1"},
        )
        _parked_file = _park_root / ".crew" / "hook-payload-keys.txt"
        _parked_lines = _parked_file.read_text().splitlines()
    _corrupt_output, _corrupt_lines = _capture_lines("not-json")
    _future_output, _future_lines = _capture_lines(
        '{"active": true, "schema": 999}'
    )
    if (all(len(lines) == 1 for lines in
            (_unarmed_lines, _armed_lines, _parked_lines, _corrupt_lines, _future_lines))
            and "crew.stop_fires=" not in _unarmed_lines[0]
            and "crew.stop_fires=1" in _armed_lines[0]
            and "crew.emit=followup" in _armed_lines[0]
            and "crew.parked_fires=1" in _parked_lines[0]
            and "crew.emit=allow" in _parked_lines[0]
            and "crew.stop_fires" not in _corrupt_lines[0]
            and "crew.stop_fires" not in _future_lines[0]):
        log_pass("persistent-mode records exactly once across unarmed/armed/parked/corrupt/future state matrix")
    else:
        log_fail("persistent-mode records exactly once across unarmed/armed/parked/corrupt/future state matrix",
                 "one line per fire with post-mutation crew values only when armed",
                 repr((_unarmed_lines, _armed_lines, _parked_lines, _corrupt_lines, _future_lines)))

    import importlib.util
    _persistent_spec = importlib.util.spec_from_file_location(
        "persistent_mode_recorder_test", SCRIPT_DIR / "persistent-mode.py"
    )
    _persistent_module = importlib.util.module_from_spec(_persistent_spec)
    _persistent_spec.loader.exec_module(_persistent_module)
    _session_spec = importlib.util.spec_from_file_location(
        "session_start_fail_open_test", SCRIPT_DIR / "session-start.py"
    )
    _session_module = importlib.util.module_from_spec(_session_spec)
    _session_spec.loader.exec_module(_session_module)

    def _raise_detect_host():
        raise RuntimeError("host detection failed")

    _saved_persistent_detect = _persistent_module.detect_host
    _saved_session_detect = _session_module.detect_host
    _fail_stop_stdout = io.StringIO()
    _fail_session_stdout = io.StringIO()
    try:
        _persistent_module.detect_host = _raise_detect_host
        _session_module.detect_host = _raise_detect_host
        with contextlib.redirect_stdout(_fail_stop_stdout):
            _persistent_module._emit(HookResult.block("fallback reason"))
        with contextlib.redirect_stdout(_fail_session_stdout):
            _session_module._emit(SessionStartResult.with_context("fallback context"))
    finally:
        _persistent_module.detect_host = _saved_persistent_detect
        _session_module.detect_host = _saved_session_detect
    _fail_stop_obj = json.loads(_fail_stop_stdout.getvalue())
    _fail_session_obj = json.loads(_fail_session_stdout.getvalue())
    _fail_open_ok = (
        _fail_stop_obj == {"decision": "block", "reason": "fallback reason"}
        and "followup_message" not in _fail_stop_obj
        and _fail_session_obj == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "fallback context",
            }
        }
        and "additional_context" not in _fail_session_obj
    )
    if _fail_open_ok:
        log_pass("host detector failure falls back to Claude shapes in both hook entries")
    else:
        log_fail("host detector failure falls back to Claude shapes in both hook entries",
                 "Claude decision/reason and hookSpecificOutput.additionalContext",
                 repr((_fail_stop_obj, _fail_session_obj)))

    class _StuckStatePath:
        name = "build-state.json"

        def with_name(self, _name):
            return self

        def replace(self, _target):
            raise OSError("rename blocked")

        def unlink(self):
            raise OSError("delete blocked")

    _cursor_diag_stdout = io.StringIO()
    _cursor_diag_stderr = io.StringIO()
    _saved_detect = _persistent_module.detect_host
    try:
        _persistent_module.detect_host = lambda: "cursor"
        with (contextlib.redirect_stdout(_cursor_diag_stdout),
              contextlib.redirect_stderr(_cursor_diag_stderr)):
            _persistent_module._handle_load_status(
                _StuckStatePath(), _persistent_module.LOAD_CORRUPT
            )
            _persistent_module._handle_load_status(
                Path("future-schema.json"), _persistent_module.LOAD_FUTURE_SCHEMA
            )
    finally:
        _persistent_module.detect_host = _saved_detect
    _cursor_diag_ok = (
        _cursor_diag_stdout.getvalue() == "{}\n{}\n"
        and "corrupt JSON" in _cursor_diag_stderr.getvalue()
        and "newer crew version" in _cursor_diag_stderr.getvalue()
    )
    if _cursor_diag_ok:
        log_pass("Cursor allow diagnostics stay in stderr for corrupt and future state")
    else:
        log_fail("Cursor allow diagnostics stay in stderr for corrupt and future state",
                 "two {} stdout results and both diagnostics on stderr",
                 repr((_cursor_diag_stdout.getvalue(), _cursor_diag_stderr.getvalue())))

    _recorder_calls = []
    _saved_read_hook_input = _persistent_module.read_hook_input
    _saved_record_hook_payload_keys = _persistent_module.record_hook_payload_keys
    try:
        _persistent_module.read_hook_input = lambda: (_ for _ in ()).throw(RuntimeError("capture"))
        _persistent_module.record_hook_payload_keys = lambda *args, **kwargs: _recorder_calls.append(
            (args, kwargs)
        )
        try:
            _persistent_module.main()
        except RuntimeError:
            pass
    finally:
        _persistent_module.read_hook_input = _saved_read_hook_input
        _persistent_module.record_hook_payload_keys = _saved_record_hook_payload_keys
    if len(_recorder_calls) == 1:
        log_pass("persistent-mode recorder runs once when the hook body raises")
    else:
        log_fail("persistent-mode recorder runs once when the hook body raises",
                 "one recorder call", repr(_recorder_calls))

    log_section("Cursor hook configuration and agents dir")
    _cursor_hooks_path = PROJECT_DIR / "hooks" / "cursor-hooks.json"
    try:
        _cursor_hooks = json.loads(_cursor_hooks_path.read_text(encoding="utf-8"))
        _session_hooks = _cursor_hooks["hooks"]["sessionStart"]
        _stop_hooks = _cursor_hooks["hooks"]["stop"]
        _hook_config_ok = (
            _cursor_hooks.get("version") == 1
            and set(_cursor_hooks.get("hooks", {})) == {"sessionStart", "stop"}
            and len(_session_hooks) == 1
            and _session_hooks[0] == {
                "command": "python3 ./scripts/session-start.py",
                "timeout": 10,
            }
            and len(_stop_hooks) == 1
            and _stop_hooks[0] == {
                "command": "python3 ./scripts/persistent-mode.py",
                "timeout": 5,
                "loop_limit": 3,
            }
            and all(not hook["command"].startswith("${CLAUDE_PLUGIN_ROOT}")
                    for hook in (_session_hooks + _stop_hooks))
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as _exc:
        _hook_config_ok = False
        _cursor_hooks = repr(_exc)
    if _hook_config_ok:
        log_pass("Cursor hook manifest pins python3 commands and 10/5 second timeouts")
    else:
        log_fail("Cursor hook manifest pins python3 commands and 10/5 second timeouts",
                 "sessionStart=10, stop=5, loop_limit=3", repr(_cursor_hooks))

    # The probe fixtures were temporary: the manifest's agents dir must exist
    # (its .gitkeep) and ship NO subagent definitions.
    _agents_cursor_md = sorted((PROJECT_DIR / "agents-cursor").glob("*.md"))
    if (not _agents_cursor_md
            and (PROJECT_DIR / "agents-cursor" / ".gitkeep").is_file()):
        log_pass("agents-cursor ships no subagent definitions (dir retained via .gitkeep)")
    else:
        log_fail("agents-cursor ships no subagent definitions (dir retained via .gitkeep)",
                 "empty agents-cursor with .gitkeep",
                 repr(_agents_cursor_md))

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

            # Terse mode: one-line agent hint
            test_contains(
                "Terse mode - one-line agent hint present",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "advisor, executor, document-writer, reader",
            )

            # Terse mode: no <system-reminder> block at all
            test_not_contains(
                "Terse mode - no <system-reminder> tag",
                session_start,
                json.dumps({"directory": str(test_path)}),
                "<system-reminder>",
            )

            # Verbose mode: full <system-reminder> agent block
            output_v = run_script_verbose(session_start, json.dumps({"directory": str(test_path)}))
            if "<system-reminder>" in output_v:
                log_pass("Verbose mode - <system-reminder> agent block present")
            else:
                log_fail("Verbose mode - <system-reminder> agent block present",
                         "contains <system-reminder>", output_v[:200])

            # Tech-stack detection and skill advertisement moved to the sk plugin
            # (plugins/sk/scripts/session-start.py). A Kotlin source in the tree
            # is what used to trigger crew's sk + LSP block, so detect the leak
            # with one present: crew cannot see whether sk is installed, and must
            # name neither it nor a language server.
            (test_path / "Leak.kt").write_text("fun main() {}")
            for _leak in ("sk:", "select:LSP", "Project stack detected"):
                test_not_contains(
                    f"crew guidance names no cross-plugin {_leak!r} on a Kotlin tree",
                    session_start,
                    json.dumps({"directory": str(test_path)}),
                    _leak,
                )
            (test_path / "Leak.kt").unlink()

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

            # A restored phase=done loop is routed to `deactivate`, which REQUIRES
            # the loop positional: a bare `crew state deactivate` errors, so the one
            # step the guidance names has to be the runnable command.
            for state_name, payload, alias in (
                ("build-state.json",
                 '{"active": true, "prompt": "Test task", "phase": "done", '
                 '"last_verdict": "APPROVED", "schema": 3}', "bl"),
                ("measure-twice-state.json",
                 '{"active": true, "task_description": "Design auth system", '
                 '"plan_file": ".crew/plans/auth-system.md", "phase": "done", '
                 '"last_verdict": "APPROVED", "schema": 3}', "mt"),
            ):
                (crew_dir / state_name).write_text(payload)
                test_contains(
                    f"SessionStart - a phase=done loop names `crew state deactivate {alias}`",
                    session_start,
                    json.dumps({"directory": str(test_path)}),
                    f"`crew state deactivate {alias}`",
                )
                (crew_dir / state_name).unlink()

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
            test_contains_verbose(
                "Build loop nudge (verbose) - build-executor carries session flag",
                persistent_mode,
                json.dumps({"directory": str(test_path), "session_id": "s9"}),
                "crew build-executor --session-id s9",
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

            # The banner is JUST the loop name: where the loop stands lives on
            # the budget line, which carries the only two bounds that can end it.
            if "[Build Loop]" in out_terse_bl and "stop fires: 1/150" in out_terse_bl:
                log_pass("Terse nudge (bl): '[Build Loop]' banner + elapsed/stop-fires budget line")
            else:
                log_fail("Terse nudge (bl): '[Build Loop]' banner + elapsed/stop-fires budget line",
                         "contains '[Build Loop]' and 'stop fires: 1/150'", out_terse_bl[:300])

            # The status line also says WHERE the loop is: phase names which verb
            # is legal next, round is the review verbs' count (the hook reports
            # both, it writes neither).
            if "phase=drafting" in out_terse_bl and "round=0" in out_terse_bl:
                log_pass("Terse nudge (bl): status line reports phase + round")
            else:
                log_fail("Terse nudge (bl): status line reports phase + round",
                         "contains 'phase=drafting' and 'round=0'", out_terse_bl[:300])

            # The status line's `round=` (asserted above) is the ONLY round the
            # nudge prints. A `Round X/Y` display stays banned: nothing caps
            # revision rounds, so the Y would be invented, and the bounds that do
            # end the loop are the stop fires and the clock on the budget line.
            if "Round" not in out_terse_bl:
                log_pass("Terse nudge (bl): no 'Round X/Y' progress display")
            else:
                log_fail("Terse nudge (bl): no 'Round X/Y' progress display",
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

            # The recipe must route a resumed loop through the review verbs: a
            # deactivate with no recorded verdict is refused, so a recipe that
            # still said prep-then-deactivate would hand back a dead end.
            if ("state begin-review bl" in out_verbose_bl
                    and "state record-verdict bl" in out_verbose_bl):
                log_pass("Verbose nudge (bl): recipe prescribes begin-review + record-verdict")
            else:
                log_fail("Verbose nudge (bl): recipe prescribes begin-review + record-verdict",
                         "contains 'state begin-review bl' and 'state record-verdict bl'",
                         out_verbose_bl[:600])

            # EXACTLY ONE step owns record-verdict (see the mt recipe's twin
            # check): a second prescribed call is refused from phase=done, which
            # left the flow uncompletable for an orchestrator following it
            # literally.
            if out_verbose_bl.count("state record-verdict bl") == 1:
                log_pass("Verbose nudge (bl): record-verdict prescribed exactly once")
            else:
                log_fail("Verbose nudge (bl): record-verdict prescribed exactly once",
                         "1 occurrence of 'state record-verdict bl'",
                         f"{out_verbose_bl.count('state record-verdict bl')} occurrences")

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

            if "phase=drafting" in out_terse_mt and "round=0" in out_terse_mt:
                log_pass("Terse nudge (mt): status line reports phase + round")
            else:
                log_fail("Terse nudge (mt): status line reports phase + round",
                         "contains 'phase=drafting' and 'round=0'", out_terse_mt[:300])

            out_terse_mt2 = run_script(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "[Measure-Twice Loop]" in out_terse_mt2 and "stop fires: 2/150" in out_terse_mt2:
                log_pass("Terse nudge (mt): second fire bumps stop_fires")
            else:
                log_fail("Terse nudge (mt): second fire bumps stop_fires",
                         "contains '[Measure-Twice Loop]' and 'stop fires: 2/150'", out_terse_mt2[:300])

            out_mt_v = run_script_verbose(persistent_mode, json.dumps({"directory": str(test_path)}))
            if "Verify via the multi-model panel" in out_mt_v:
                log_pass("Verbose nudge (mt): full recipe present")
            else:
                log_fail("Verbose nudge (mt): full recipe present",
                         "contains 'Verify via the multi-model panel'", out_mt_v[:300])

            if ("state begin-review mt" in out_mt_v
                    and "state record-verdict mt" in out_mt_v):
                log_pass("Verbose nudge (mt): recipe prescribes begin-review + record-verdict")
            else:
                log_fail("Verbose nudge (mt): recipe prescribes begin-review + record-verdict",
                         "contains 'state begin-review mt' and 'state record-verdict mt'",
                         out_mt_v[:600])

            # EXACTLY ONE step owns record-verdict. A recipe that recorded, then
            # said "record it, then deactivate" a step later prescribed a second
            # call that record-verdict refuses from phase=done: an orchestrator
            # following the steps literally could never complete the loop.
            if out_mt_v.count("state record-verdict mt") == 1:
                log_pass("Verbose nudge (mt): record-verdict prescribed exactly once")
            else:
                log_fail("Verbose nudge (mt): record-verdict prescribed exactly once",
                         "1 occurrence of 'state record-verdict mt'",
                         f"{out_mt_v.count('state record-verdict mt')} occurrences")

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

            # Resolved executor settings are stamped at init,
            # while omitted flags preserve the LoopState defaults in a fresh
            # session. Both cases use a real prompt so the assertion reaches
            # the write path rather than the pre-write validation failure.
            stamp_project = test_path / "step5-stamp-project"
            stamp_project.mkdir()
            _, stamp_err, stamp_code = run_crew_state(
                ["init", "bl", "--prompt", "stamped task", "--executor", "codex-luna",
                 "--resume-executor", "false", "--session-id", "stampS"], stamp_project)
            stamped_path = stamp_project / ".crew" / "build-state-stampS.json"
            stamped = json.loads(stamped_path.read_text()) if stamped_path.exists() else {}
            if (stamp_code == 0 and stamped.get("executor") == "codex-luna"
                    and stamped.get("resume_executor") is False):
                log_pass("init bl stamps executor + resume_executor settings")
            else:
                log_fail("init bl stamps executor + resume_executor settings",
                         "executor=codex-luna, resume_executor=false",
                         f"code={stamp_code} state={stamped} err={stamp_err[:120]}")

            _, default_err, default_code = run_crew_state(
                ["init", "bl", "--prompt", "default settings", "--session-id", "stampDefault"],
                stamp_project)
            default_path = stamp_project / ".crew" / "build-state-stampDefault.json"
            default_state = json.loads(default_path.read_text()) if default_path.exists() else {}
            if (default_code == 0 and default_state.get("executor") == ""
                    and default_state.get("resume_executor") is None):
                log_pass("init bl omitted settings preserves executor/resume_executor defaults")
            else:
                log_fail("init bl omitted settings preserves executor/resume_executor defaults",
                         "executor='', resume_executor=null",
                         f"code={default_code} state={default_state} err={default_err[:120]}")

            _, garbage_err, garbage_code = run_crew_state(
                ["init", "bl", "--prompt", "garbage toggle", "--resume-executor", "bogus",
                 "--session-id", "stampGarbage"], stamp_project)
            garbage_path = stamp_project / ".crew" / "build-state-stampGarbage.json"
            if garbage_code == 2 and not garbage_path.exists():
                log_pass("init rejects garbage --resume-executor before writing state")
            else:
                log_fail("init rejects garbage --resume-executor before writing state",
                         "argparse exit 2, no state file",
                         f"code={garbage_code} exists={garbage_path.exists()} err={garbage_err[:160]}")

            # Test show bl (compact default). The compact line reports the REAL
            # budget (the two bounds that can end the loop), never a round
            # counter: nothing writes one, so it could only report a frozen lie.
            stdout, stderr, code = run_crew_state(["show", "bl"], test_path)
            # The expected bound comes off the state file init just wrote, so
            # this stays green if the built-in default moves again.
            written_deadline = json.loads(
                (crew_dir / "build-state.json").read_text()).get("deadline_minutes")
            if (code == 0 and stdout.startswith("loop=bl") and "active=true" in stdout
                    and "fires=0/150" in stdout and "elapsed=" in stdout
                    and f"/{written_deadline}min" in stdout
                    and 'task=' in stdout
                    and "iter=" not in stdout):
                log_pass("show bl - compact summary reports fires/elapsed budget, no round counter")
            else:
                log_fail("show bl - compact summary reports fires/elapsed budget, no round counter",
                         f"loop=bl active=true fires=0/150 elapsed=N/{written_deadline}min task=…, no iter=",
                         stdout)

            # phase/round say WHERE the loop is: which verb is legal next, and how
            # many times the panel sent it back. A fresh loop is drafting/0.
            if code == 0 and "phase=drafting" in stdout and "round=0" in stdout:
                log_pass("show bl - compact summary reports phase + round")
            else:
                log_fail("show bl - compact summary reports phase + round",
                         "phase=drafting round=0", stdout)

            # …and it reports what is ON DISK, not a constant. Patched in place,
            # then restored, so the later CLI tests see the file init wrote.
            bl_path = crew_dir / "build-state.json"
            bl_original = bl_path.read_text()
            bl_data = json.loads(bl_original)
            bl_path.write_text(json.dumps(
                {**bl_data, "phase": "reviewing", "revision_round": 3}))
            stdout_mid, _, code_mid = run_crew_state(["show", "bl"], test_path)
            bl_path.write_text(bl_original)
            if code_mid == 0 and "phase=reviewing" in stdout_mid and "round=3" in stdout_mid:
                log_pass("show bl - compact summary reports a mid-review phase/round from disk")
            else:
                log_fail("show bl - compact summary reports a mid-review phase/round from disk",
                         "phase=reviewing round=3", stdout_mid)

            # Compact show: exactly one line
            if code == 0 and len(stdout.strip().splitlines()) == 1:
                log_pass("show bl - output is exactly one line")
            else:
                log_fail("show bl - output is exactly one line", "1 line", f"{len(stdout.strip().splitlines())} lines: {stdout[:200]}")

            # show bl --verbose: JSON output
            stdout_v, _, code_v = run_crew_state(["show", "bl", "--verbose"], test_path)
            if (code_v == 0 and '"active": true' in stdout_v
                    and '"task": "Test task"' in stdout_v
                    and '"loop": "bl"' in stdout_v):
                log_pass("show bl --verbose - pretty JSON with expected fields")
            else:
                log_fail("show bl --verbose - pretty JSON with expected fields",
                         "JSON with active, task, and the loop stamp", stdout_v)

            # show bl -v: same as --verbose
            stdout_sv, _, code_sv = run_crew_state(["show", "bl", "-v"], test_path)
            if code_sv == 0 and '"active": true' in stdout_sv:
                log_pass("show bl -v - short flag works same as --verbose")
            else:
                log_fail("show bl -v - short flag works same as --verbose",
                         "JSON with active: true", stdout_sv)

            # Test set bl on an allowlisted field (use --verbose for JSON assertion).
            # A relative value is ANCHORED to crew_base at write time, so the
            # stored path is the absolute project-root form.
            stdout, stderr, code = run_crew_state(
                ["set", "bl", "plan_file", "SHIPPED"], test_path)
            stdout2, _, _ = run_crew_state(["show", "bl", "--verbose"], test_path)
            anchored_shipped = str(test_path / "SHIPPED")
            if code == 0 and f'"plan_file": "{anchored_shipped}"' in stdout2:
                log_pass("set bl - changes an allowlisted field value (anchored)")
            else:
                log_fail("set bl - changes an allowlisted field value (anchored)",
                         f'plan_file: "{anchored_shipped}"', f"exit {code}, {stdout2}")

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

            # Test deactivate bl (--cancel: this loop recorded no verdict, and the
            # metadata under test is written on both sanctioned exits)
            stdout, stderr, code = run_crew_state(
                ["deactivate", "bl", "--cancel", "--reason", "Test done"], test_path)
            with open(crew_dir / "build-state.json") as f:
                data = json.load(f)
            if code == 0 and data.get("active") == False and "completed_at" in data and data.get("reason") == "Test done":
                log_pass("deactivate bl - sets active=false and adds metadata")
            else:
                log_fail("deactivate bl - sets active=false and adds metadata", "active=false, completed_at, reason", json.dumps(data))

            # Test init mt (use --verbose for JSON assertion). A relative
            # --plan-file is ANCHORED to crew_base at init time, so the stored
            # value is the absolute project-root form.
            stdout, stderr, code = run_crew_state(["init", "mt", "--task", "Build auth", "--plan-file", ".crew/plans/auth.md"], test_path)
            stdout2, _, _ = run_crew_state(["show", "mt", "--verbose"], test_path)
            anchored_plan = str(test_path / ".crew" / "plans" / "auth.md")
            if (code == 0 and '"task": "Build auth"' in stdout2
                    and '"loop": "mt"' in stdout2
                    and f'"plan_file": "{anchored_plan}"' in stdout2):
                log_pass("init mt - creates measure-twice state (plan_file anchored)")
            else:
                log_fail("init mt - creates measure-twice state (plan_file anchored)",
                         f'task field + loop stamp + plan_file "{anchored_plan}"', stdout2)

            # Test error: missing --prompt for bl (clean up first to avoid conflict error)
            for f in crew_dir.glob("*-state*.json"):
                f.unlink()
            stdout, stderr, code = run_crew_state(["init", "bl"], test_path)
            if code == 1 and "--prompt or -f required" in stderr:
                log_pass("init bl without --prompt - exits with error")
            else:
                log_fail("init bl without --prompt - exits with error", "exit 1 with error message", f"exit {code}, stderr: {stderr}")

            # --deadline-minutes is a cost ceiling and `init` is invoked BY the
            # agent, so an out-of-policy bound is rejected at parse time with NO
            # state file created. 0 is in this list DELIBERATELY: the flag is
            # the agent's channel, and a bound the bounded thing can delete is
            # not a bound. The no-deadline opt-out is config-only (tested below).
            for loop_arg, text_flag, text in (("bl", "--prompt", "bounded"),
                                              ("mt", "--task", "bounded")):
                for bad in ("-1", "0", "1441"):
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
                    ["init", loop_arg, text_flag, text, "--deadline-minutes", "1440"] + extra,
                    test_path)
                stdout_dl, _, _ = run_crew_state(["show", loop_arg, "--verbose"], test_path)
                if code == 0 and '"deadline_minutes": 1440' in stdout_dl:
                    log_pass(f"init {loop_arg} --deadline-minutes 1440 - accepted at the policy maximum")
                else:
                    log_fail(f"init {loop_arg} --deadline-minutes 1440 - accepted at the policy maximum",
                             "exit 0, deadline_minutes=1440",
                             f"exit {code}, stderr={stderr[:120]!r}, state={stdout_dl[:160]}")

                # The no-deadline opt-out arrives ONLY via the GLOBAL config
                # (the per-repo file sits in the tree the agent writes to): a
                # global [tuning].deadline_minutes = 0 init writes BOTH marker
                # halves, and `show` reports the clock as off instead of a
                # bound that will never trip.
                for f in crew_dir.glob("*-state*.json"):
                    f.unlink()
                global_cfg = Path(_NEUTRAL_HOME) / ".crew-config.toml"
                global_cfg.write_text("[tuning]\ndeadline_minutes = 0\n")
                try:
                    _, stderr, code = run_crew_state(
                        ["init", loop_arg, text_flag, text] + extra, test_path)
                    stdout_dl, _, _ = run_crew_state(["show", loop_arg, "--verbose"], test_path)
                    stdout_line, _, _ = run_crew_state(["show", loop_arg], test_path)
                finally:
                    global_cfg.unlink()
                if (code == 0 and '"deadline_minutes": 0' in stdout_dl
                        and '"no_deadline": true' in stdout_dl
                        and "elapsed=" in stdout_line and "/none" in stdout_line):
                    log_pass(f"init {loop_arg} GLOBAL config deadline 0 - writes BOTH marker halves, show reports none")
                else:
                    log_fail(f"init {loop_arg} GLOBAL config deadline 0 - writes BOTH marker halves, show reports none",
                             "exit 0, deadline_minutes=0 + no_deadline=true, show line has /none",
                             f"exit {code}, stderr={stderr[:120]!r}, line={stdout_line[:120]!r}")

                # A PER-REPO 0 is refused with a warn and the bound stays: the
                # repo config is agent-writable, so honoring 0 there would put
                # the opt-out one Write away from the loop it bounds.
                for f in crew_dir.glob("*-state*.json"):
                    f.unlink()
                repo_cfg = crew_dir / "config.toml"
                repo_cfg.write_text("[tuning]\ndeadline_minutes = 0\n")
                try:
                    _, stderr, code = run_crew_state(
                        ["init", loop_arg, text_flag, text] + extra, test_path)
                    stdout_dl, _, _ = run_crew_state(["show", loop_arg, "--verbose"], test_path)
                finally:
                    repo_cfg.unlink()
                written = json.loads(
                    (crew_dir / f"{'build-state' if loop_arg == 'bl' else 'measure-twice-state'}.json")
                    .read_text()) if code == 0 else {}
                if (code == 0 and written.get("deadline_minutes", 0) > 0
                        and written.get("no_deadline") is False):
                    log_pass(f"init {loop_arg} PER-REPO config deadline 0 - refused, bound kept")
                else:
                    log_fail(f"init {loop_arg} PER-REPO config deadline 0 - refused, bound kept",
                             "a positive deadline_minutes and no_deadline=false",
                             f"exit {code}, state={stdout_dl[:160]!r}")

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

            # Verify state stores the auto-derived plan file as an ANCHORED ABSOLUTE
            # path (crew_base()/.crew/plans/...), not a cwd-relative string: later
            # plan writes and review-prep consume plan_file as-is, so a relative
            # value would re-resolve against a possibly-different cwd. Here
            # CLAUDE_PROJECT_DIR == test_path, so the stored value must be exactly
            # <test_path>/.crew/plans/build-auth-system.md.
            expected_plan = str(test_path / ".crew" / "plans" / "build-auth-system.md")
            stdout2, _, _ = run_crew_state(["show", "mt", "--verbose"], test_path)
            if f'"plan_file": "{expected_plan}"' in stdout2:
                log_pass("init mt --auto-plan - stores ANCHORED absolute path in state")
            else:
                log_fail("init mt --auto-plan - stores ANCHORED absolute path in state",
                         f'plan_file == {expected_plan}', stdout2)

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
            if code == 0 and '"task": "Task for s1"' in stdout:
                log_pass("show bl s1 - shows session-specific state")
            else:
                log_fail("show bl s1 - shows session-specific state",
                         "prompt: Task for s1", stdout)

            # deactivate bl --session-id s1 only deactivates s1 (--cancel: these
            # loops record no verdict; the test is about which FILE is touched)
            stdout, stderr, code = run_crew_state(
                ["deactivate", "bl", "--cancel", "--reason", "done",
                 "--session-id", "s1"], test_path)
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
            if result.returncode == 0 and '"task": "Task for s2"' in result.stdout:
                log_pass("CLAUDE_SESSION_ID env var fallback works")
            else:
                log_fail("CLAUDE_SESSION_ID env var fallback works",
                         "task: Task for s2", result.stdout.strip())

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
                ["deactivate", "bl", "--cancel", "--reason", "done",
                 "--session-id", "symS"], test_path)
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
                ["set", "bl", "plan_file", "SHIPPED", "--session-id", "futS"],
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
                    fresh_ok = _d.get("active") is True and _d.get("task") == "fresh task"
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
                ["set", "bl", "plan_file", "SHIPPED", "--session-id", "legS"], test_path)
            legacy_after_set = json.loads(legacy.read_text())
            set_ok = (
                set_code == 0
                # relative value anchored to crew_base at write time
                and legacy_after_set.get("plan_file") == str(test_path / "SHIPPED")
                and not scoped_stray.exists()
            )
            if set_ok:
                log_pass("set --session-id over adopted legacy file - mutates legacy in place, no stray scoped file")
            else:
                log_fail(
                    "set --session-id over adopted legacy file - mutates legacy in place, no stray scoped file",
                    "legacy plan_file=<anchored SHIPPED>, no build-state-legS.json",
                    f"code={set_code} legacy_plan={legacy_after_set.get('plan_file')} "
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
                ["deactivate", "bl", "--cancel", "--reason", "done",
                 "--session-id", "legS"], test_path)
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

            # --- Other-session ACTIVE loops carry the exact cancel command ---
            # A /resume gives the session a new id, so the old id's loop is
            # unreachable by a bare deactivate while its Stop hook keeps blocking.
            # The banner must hand over the RUNNABLE line (loop positional + the
            # OLD session's FULL id), and must never adopt or cancel anything.
            def _context_of(payload: dict) -> str:
                raw = run_script(session_start, json.dumps(payload))
                try:
                    return json.loads(raw)["hookSpecificOutput"]["additionalContext"]
                except (ValueError, KeyError, TypeError):
                    return raw

            (crew_dir / "measure-twice-state-other999.json").write_text(json.dumps({
                "active": True, "task": "Plan the auth rework",
                "session_id": "other999", "plan_file": ".crew/plans/auth.md"}))
            orphan_ctx = _context_of({"directory": str(test_path), "session_id": "mine111"})
            expected_cancel = ('"${CLAUDE_PLUGIN_ROOT}/crew" state deactivate mt '
                               '--session-id other999 --cancel --reason "orphaned session"')

            if expected_cancel in orphan_ctx:
                log_pass("Orphan line carries the exact --cancel command (loop positional + full old session id)")
            else:
                log_fail("Orphan line carries the exact --cancel command (loop positional + full old session id)",
                         expected_cancel, orphan_ctx[-400:])

            if "Plan the auth rework" in orphan_ctx and "Other Sessions" in orphan_ctx:
                log_pass("Orphan line names the other session's loop and task")
            else:
                log_fail("Orphan line names the other session's loop and task",
                         "task text under [Other Sessions]", orphan_ctx[-400:])

            # The state file is only REPORTED: never adopted, never cancelled.
            _orphan_state = json.loads((crew_dir / "measure-twice-state-other999.json").read_text())
            if _orphan_state.get("active") is True and _orphan_state.get("session_id") == "other999":
                log_pass("Orphan surfacing never adopts or deactivates the other session's loop")
            else:
                log_fail("Orphan surfacing never adopts or deactivates the other session's loop",
                         "state file untouched (active, other999)", json.dumps(_orphan_state))

            # The OWNING session gets its own banner, not an orphan cancel line.
            own_ctx = _context_of({"directory": str(test_path), "session_id": "other999"})
            if "Measure-Twice Loop Active" in own_ctx and "--cancel" not in own_ctx:
                log_pass("No orphan cancel line for the session that owns the loop")
            else:
                log_fail("No orphan cancel line for the session that owns the loop",
                         "own banner, no --cancel line", own_ctx[-400:])

            # An INACTIVE other-session file is not an orphan: nothing to end.
            (crew_dir / "measure-twice-state-other999.json").write_text(json.dumps({
                "active": False, "task": "Plan the auth rework",
                "session_id": "other999"}))
            inactive_ctx = _context_of({"directory": str(test_path), "session_id": "mine111"})
            if "--cancel" not in inactive_ctx and "Other Sessions" not in inactive_ctx:
                log_pass("No orphan line for an INACTIVE other-session state file")
            else:
                log_fail("No orphan line for an INACTIVE other-session state file",
                         "no [Other Sessions] section", inactive_ctx[-400:])

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

            # normalize_overrides: the --force audit stamp, read defensively (the
            # key is often absent and the file is hand-editable, so only a list of
            # non-empty strings counts; anything else reads as a clean sign-off).
            normalize_overrides = models_module.normalize_overrides
            for value, expected, label in (
                (["quorum-not-met", "target-drift"], ["quorum-not-met", "target-drift"], "list of strings survives"),
                (None, [], "None reads as []"),
                ("quorum-not-met", [], "a bare string reads as []"),
                ([1, "target-drift", "", None], ["target-drift"], "non-string / empty members are dropped"),
            ):
                if normalize_overrides(value) == expected:
                    log_pass(f"normalize_overrides: {label}")
                else:
                    log_fail(f"normalize_overrides: {label}", str(expected), str(normalize_overrides(value)))

            # override_completion_note: the honest fragment keyed on the stamp
            # (non-empty overrides say "recorded over" + names, empty says nothing).
            note_fn = models_module.override_completion_note
            note = note_fn(["quorum-not-met", "target-drift"])
            if ("recorded over" in note and "quorum-not-met" in note
                    and "target-drift" in note and "--force" in note):
                log_pass("override_completion_note: non-empty stamp names the advisories + --force")
            else:
                log_fail("override_completion_note: non-empty stamp names the advisories + --force",
                         "'recorded over' + both names + '--force'", note)
            if note_fn([]) == "" and note_fn(None) == "":
                log_pass("override_completion_note: a clean (empty/absent) stamp is the empty string")
            else:
                log_fail("override_completion_note: a clean (empty/absent) stamp is the empty string",
                         "''", f"[]={note_fn([])!r} None={note_fn(None)!r}")

            # LoopState carries last_verdict_overrides so the banner + nudge can
            # describe a forced completion honestly. record-verdict WRITES the key;
            # the model must round-trip it (present / absent-defaults-[] / non-list-[]).
            LoopStateT = models_module.LoopState
            override_rt = crew_dir / "override-roundtrip.json"
            LoopStateT(active=True, loop="bl", task="t",
                       last_verdict_overrides=["quorum-not-met", "target-drift"]).save(override_rt)
            rt = LoopStateT.load(override_rt)
            if rt.last_verdict_overrides == ["quorum-not-met", "target-drift"]:
                log_pass("LoopState round-trips a non-empty last_verdict_overrides")
            else:
                log_fail("LoopState round-trips a non-empty last_verdict_overrides",
                         "['quorum-not-met', 'target-drift']", str(rt.last_verdict_overrides))

            override_rt.write_text(json.dumps(
                {"active": True, "loop": "bl", "task": "t", "schema": 3}))
            rt = LoopStateT.load(override_rt)
            if rt.last_verdict_overrides == []:
                log_pass("LoopState defaults last_verdict_overrides to [] when the key is absent")
            else:
                log_fail("LoopState defaults last_verdict_overrides to [] when the key is absent",
                         "[]", str(rt.last_verdict_overrides))

            override_rt.write_text(json.dumps(
                {"active": True, "loop": "bl", "task": "t",
                 "last_verdict_overrides": "quorum-not-met", "schema": 3}))
            rt = LoopStateT.load(override_rt)
            if rt.last_verdict_overrides == []:
                log_pass("LoopState reads a non-list last_verdict_overrides as []")
            else:
                log_fail("LoopState reads a non-list last_verdict_overrides as []",
                         "[]", str(rt.last_verdict_overrides))
            override_rt.unlink()

            # =========================================================================
            # SESSION-START ARTIFACT CLEANUP (report-only rewiring)
            # =========================================================================
            # The destructive review-run + debate rmtree sweeps are GONE from the
            # unattended hook (that venue moved to the attended `crew swab`). What
            # remains: signal markers still auto-reap, and stale review-run/debate
            # orphans are REPORTED (never deleted) via the shared artifact_prune
            # enumerator.
            log_section("session-start artifact cleanup (report-only)")

            ss_spec = importlib.util.spec_from_file_location("crew_session_start", SCRIPT_DIR / "session-start.py")
            ss_module = importlib.util.module_from_spec(ss_spec)
            ss_spec.loader.exec_module(ss_module)
            sweep_signal_markers_all = ss_module.sweep_signal_markers_all
            report_stale_artifacts = ss_module.report_stale_artifacts

            # The restore banner is a third deadline consumer: the no-deadline
            # opt-out must read as the clock being OFF, never as "elapsed X/0"
            # (an exhausted limit), and bounded values render unchanged.
            line_off = ss_module.loop_budget_line(
                {"started_at": "", "deadline_minutes": 0, "no_deadline": True,
                 "stop_fires": 3})
            line_lone0 = ss_module.loop_budget_line(
                {"started_at": "", "deadline_minutes": 0, "stop_fires": 3})
            line_on = ss_module.loop_budget_line(
                {"started_at": "", "deadline_minutes": 90, "stop_fires": 3})
            default_dl = str(ss_module.DEFAULT_DEADLINE_MINUTES)
            if ("/none min" in line_off and "/0 min" not in line_off
                    and f"/{default_dl} min" in line_lone0
                    and "/90 min" in line_on):
                log_pass("loop_budget_line: marker renders none, lone 0 stays bounded")
            else:
                log_fail("loop_budget_line: marker renders none, lone 0 stays bounded",
                         f"'/none min' with marker, '/{default_dl} min' for lone 0, '/90 min' for 90",
                         f"off={line_off!r} lone0={line_lone0!r} on={line_on!r}")

            # The destructive deletion functions and their dead helpers are GONE.
            for _gone in ("cleanup_stale_review_runs", "cleanup_stale_debate_dirs",
                          "_live_run_keys", "_DEBATE_DIR_RE"):
                if not hasattr(ss_module, _gone):
                    log_pass(f"session-start no longer defines {_gone} (destructive path removed)")
                else:
                    log_fail(f"session-start no longer defines {_gone} (destructive path removed)",
                             "absent", "still present")

            rp_crew_dir = test_path / "report-only" / ".crew"
            rp_reviews = rp_crew_dir / "reviews"
            rp_debates = rp_crew_dir / "debates"
            rp_sess1 = rp_reviews / "sess1"
            rp_sess1.mkdir(parents=True, exist_ok=True)
            rp_debates.mkdir(parents=True, exist_ok=True)

            over_mtime = _time.time() - (8 * 86400)    # over both the 1-day and 7-day rules
            ancient_mtime = _time.time() - (30 * 86400)

            def _age(path: Path, mtime: float) -> None:
                os.utime(path, (mtime, mtime))

            # A stale orphaned review run (minted name + run.json marker, no loop,
            # no pointer): a swab CANDIDATE, so report_stale_artifacts must SURFACE
            # it, and the hook must NOT delete it.
            rp_orphan = rp_sess1 / "run-aaaaaaaaaaaa"
            rp_orphan.mkdir()
            (rp_orphan / "run.json").write_text("{}")
            (rp_orphan / "codex.json").write_text("{}")

            # A review run frozen in an ACTIVE loop state: PROTECTED (not a
            # candidate), so it is neither reported nor deleted nor errors.
            rp_live = rp_sess1 / "run-cccccccccccc"
            rp_live.mkdir()
            (rp_live / "run.json").write_text("{}")
            (rp_live / "codex.json").write_text("{}")
            rp_live_state = rp_crew_dir / "build-state-sess1.json"
            rp_live_state.write_text(json.dumps({
                "active": True, "task": "live loop", "session_id": "sess1",
                "phase": "reviewing", "run_id": "run-cccccccccccc"}))

            # A stale INCOMPLETE debate (generated name, no synthesis.md, aged past
            # the 1-day staleness rule): also a swab candidate to SURFACE + KEEP.
            rp_debate = rp_debates / "run-20250101-abc"
            rp_debate.mkdir()
            (rp_debate / "question.md").write_text("q")

            # Signal markers: aged one reaped, fresh one kept, a foreign name never
            # touched (the ONLY auto-delete session-start still performs).
            rp_signals = rp_sess1 / "signals"
            rp_signals.mkdir()
            rp_sig_old = rp_signals / "exec-abandoned.json"
            rp_sig_old.write_text('{"tag": "exec-abandoned", "status": "done"}')
            rp_sig_fresh = rp_signals / "exec-live.json"
            rp_sig_fresh.write_text('{"tag": "exec-live", "status": "done"}')
            rp_sig_foreign = rp_signals / "my.report.json"
            rp_sig_foreign.write_text("not a crew marker")

            for _p in (rp_orphan, rp_debate, rp_sig_old, rp_sig_foreign):
                _age(_p, over_mtime)
            _age(rp_live, ancient_mtime)

            # --- The report: RETURNS the notice (for main() to inject on the
            # additionalContext channel), deletes nothing. The notice is COUNT-ONLY
            # (no byte total): exact sizing is the attended swab's job. ---
            _report = report_stale_artifacts(rp_crew_dir)

            if "2 stale crew artifact(s)" in _report and "crew swab" in _report:
                log_pass("report_stale_artifacts surfaces the 2 candidates + the swab hint")
            else:
                log_fail("report_stale_artifacts surfaces the 2 candidates + the swab hint",
                         "'2 stale crew artifact(s)' and 'crew swab' in the returned notice", repr(_report))

            # Count-only: no byte figure rides the notice (no 'B'/'KB'/'MB' unit, no
            # parenthesized size). The reporter must not pay the per-dir sizing walk.
            if not any(u in _report for u in (" B)", " KB", " MB", " GB", " TB", " PB", "bytes")):
                log_pass("report_stale_artifacts notice carries NO byte figure (count-only)")
            else:
                log_fail("report_stale_artifacts notice carries NO byte figure (count-only)",
                         "no byte-size unit in the notice", repr(_report))

            # The reporter does NO sizing walk: with dir_size patched to raise, the
            # count-only path still returns its notice (a with-sizes call would blow up).
            _saved_dir_size = ss_module.artifact_prune.dir_size
            try:
                def _boom(_p):
                    raise AssertionError("dir_size must not be called by the count-only reporter")
                ss_module.artifact_prune.dir_size = _boom
                _report_nosize = report_stale_artifacts(rp_crew_dir)
                if "2 stale crew artifact(s)" in _report_nosize:
                    log_pass("report_stale_artifacts does NO per-dir sizing walk (dir_size never called)")
                else:
                    log_fail("report_stale_artifacts does NO per-dir sizing walk (dir_size never called)",
                             "notice returned with dir_size patched to raise", repr(_report_nosize))
                # Swab's default (with_sizes=True) STILL sizes: the same enumerator
                # with the sizing path would hit the raising dir_size.
                raised = False
                try:
                    ss_module.artifact_prune.collect_prunable(rp_crew_dir, _time.time())
                except AssertionError:
                    raised = True
                if raised:
                    log_pass("collect_prunable default (with_sizes=True, as cmd_swab calls it) STILL sizes")
                else:
                    log_fail("collect_prunable default (with_sizes=True, as cmd_swab calls it) STILL sizes",
                             "default path calls dir_size", "default path skipped sizing")
            finally:
                ss_module.artifact_prune.dir_size = _saved_dir_size

            # Sanity: the default sizing path records nonzero bytes for a candidate,
            # while with_sizes=False records 0 for the same set (accurate count, no walk).
            _sized = ss_module.artifact_prune.collect_prunable(rp_crew_dir, _time.time())
            _unsized = ss_module.artifact_prune.collect_prunable(rp_crew_dir, _time.time(), with_sizes=False)
            if (len(_sized) == len(_unsized) == 2
                    and any(i.bytes > 0 for i in _sized)
                    and all(i.bytes == 0 for i in _unsized)):
                log_pass("with_sizes toggles bytes (default sizes, False zeroes) with the SAME orphan count")
            else:
                log_fail("with_sizes toggles bytes (default sizes, False zeroes) with the SAME orphan count",
                         "same 2 items, sized>0 vs unsized==0",
                         f"sized={[i.bytes for i in _sized]} unsized={[i.bytes for i in _unsized]}")

            if rp_orphan.exists() and (rp_orphan / "run.json").exists():
                log_pass("report_stale_artifacts does NOT delete the stale orphan review run")
            else:
                log_fail("report_stale_artifacts does NOT delete the stale orphan review run",
                         "dir kept", "dir removed by the report")

            if rp_debate.exists():
                log_pass("report_stale_artifacts does NOT delete the stale incomplete debate")
            else:
                log_fail("report_stale_artifacts does NOT delete the stale incomplete debate",
                         "dir kept", "dir removed by the report")

            if rp_live.exists():
                log_pass("report_stale_artifacts leaves an ACTIVE loop's run dir (protected, no error)")
            else:
                log_fail("report_stale_artifacts leaves an ACTIVE loop's run dir (protected, no error)",
                         "dir kept", "dir removed")

            # Non-vacuity: with the two candidates removed the report must return
            # "" (proves the earlier surfacing was not an unconditional string).
            shutil.rmtree(rp_orphan)
            shutil.rmtree(rp_debate)
            _clean_report = report_stale_artifacts(rp_crew_dir)
            if _clean_report == "":
                log_pass("report_stale_artifacts is silent when there are no candidates (non-vacuity)")
            else:
                log_fail("report_stale_artifacts is silent when there are no candidates (non-vacuity)",
                         "empty string", repr(_clean_report))

            # --- The OBSERVABLE hook output: main() must ride the swab notice on
            # the SessionStart additionalContext channel (not stderr), since that is
            # the only carrier a successful hook reliably delivers. The notice
            # resolves `.crew` through the ANCHORED crew_base() root; the helper
            # below sets CLAUDE_PROJECT_DIR to the tree under test (and matches cwd
            # to it), so the sweep lands on that tree. ---
            def _session_start_context(cwd: Path) -> str:
                env = _neutral_env()
                env.pop("CREW_VERBOSE", None)
                env["CLAUDE_PROJECT_DIR"] = str(cwd)
                proc = subprocess.run(
                    [sys.executable, str(session_start)],
                    input=json.dumps({"directory": str(cwd), "session_id": "sess1"}),
                    capture_output=True, text=True, cwd=str(cwd), env=env,
                )
                raw = proc.stdout.strip()
                try:
                    return json.loads(raw)["hookSpecificOutput"]["additionalContext"]
                except (ValueError, KeyError, TypeError):
                    return raw

            fx1_proj = test_path / "fix1-payload"
            fx1_reviews = fx1_proj / ".crew" / "reviews" / "sess1"
            fx1_reviews.mkdir(parents=True, exist_ok=True)
            fx1_orphan = fx1_reviews / "run-aaaaaaaaaaaa"
            fx1_orphan.mkdir()
            (fx1_orphan / "run.json").write_text("{}")
            (fx1_orphan / "codex.json").write_text("{}")

            fx1_dirty_ctx = _session_start_context(fx1_proj)
            if "stale crew artifact(s)" in fx1_dirty_ctx and "crew swab" in fx1_dirty_ctx:
                log_pass("main() additionalContext CARRIES the swab notice when stale artifacts exist")
            else:
                log_fail("main() additionalContext CARRIES the swab notice when stale artifacts exist",
                         "'stale crew artifact(s)' + 'crew swab' in additionalContext", fx1_dirty_ctx[-400:])

            # Clean tree: the notice is absent from the SAME channel (non-vacuity,
            # proving the payload carries it only when there are candidates).
            shutil.rmtree(fx1_orphan)
            fx1_clean_ctx = _session_start_context(fx1_proj)
            if "stale crew artifact(s)" not in fx1_clean_ctx and "crew swab" not in fx1_clean_ctx:
                log_pass("main() additionalContext OMITS the swab notice when the tree is clean")
            else:
                log_fail("main() additionalContext OMITS the swab notice when the tree is clean",
                         "no swab notice in additionalContext", fx1_clean_ctx[-400:])

            # --- The signal sweep: still auto-reaps, still anchored to the grammar. ---
            sweep_signal_markers_all(rp_crew_dir)

            if not rp_sig_old.exists():
                log_pass("sweep_signal_markers_all reaps an 8-day-old signal marker")
            else:
                log_fail("sweep_signal_markers_all reaps an 8-day-old signal marker",
                         "marker removed", "marker exists")

            if rp_sig_fresh.exists():
                log_pass("sweep_signal_markers_all keeps a fresh signal marker")
            else:
                log_fail("sweep_signal_markers_all keeps a fresh signal marker",
                         "marker kept", "marker removed")

            if rp_sig_foreign.exists():
                log_pass("sweep_signal_markers_all preserves signals/my.report.json (outside the tag grammar)")
            else:
                log_fail("sweep_signal_markers_all preserves signals/my.report.json (outside the tag grammar)",
                         "file kept", "file swept by a bare *.json glob")

            # The signal sweep touches no run dir (the live one is still ancient).
            if rp_live.exists():
                log_pass("sweep_signal_markers_all deletes no review-run dir")
            else:
                log_fail("sweep_signal_markers_all deletes no review-run dir",
                         "dir kept", "dir removed")

            # No reviews dir → both are no-ops, no error.
            no_reviews = test_path / "no-reviews" / ".crew"
            no_reviews.mkdir(parents=True, exist_ok=True)
            try:
                sweep_signal_markers_all(no_reviews)
                report_stale_artifacts(no_reviews)
                log_pass("sweep_signal_markers_all + report_stale_artifacts no-op when reviews dir absent")
            except Exception as e:
                log_fail("sweep_signal_markers_all + report_stale_artifacts no-op when reviews dir absent",
                         "no exception", repr(e))

            # --- The signal sweep refuses a SYMLINKED dir segment at every level
            # (own_dir = is_dir AND not is_symlink). This sweep unlink()s aged
            # <tag>.json UNATTENDED every session start, so a symlinked `.crew` root,
            # `reviews`, session, or `signals` dir must NOT let it delete
            # normally-named JSON at the link target OUTSIDE the project. The `.crew`
            # root case is the one-level-up gap: a symlinked `.crew` resolves to a
            # real reviews/ at the target, so guarding reviews/ alone would miss it. ---
            _ap = ss_module.artifact_prune

            def _build_symlink_case(kind: str, tag: str):
                """A .crew tree where the `kind` segment is a SYMLINK to an external
                dir holding a crew-grammar victim.json aged past the 7-day rule. The
                target layout is chosen so that IF the sweep followed the link it
                would reach `.../signals/exec-victim.json` and unlink it. For the
                `crew` kind the SYMLINK is `.crew` itself (the root)."""
                base = test_path / f"sl-{tag}"
                crew_dir = base / ".crew"
                external = test_path / f"sl-{tag}-ext"
                if kind == "signals":
                    victim = external / "exec-victim.json"       # link stands in for signals/
                    link = crew_dir / "reviews" / "sess1" / "signals"
                elif kind == "session":
                    victim = external / "signals" / "exec-victim.json"  # link is the session dir
                    link = crew_dir / "reviews" / "sess1"
                elif kind == "reviews":
                    victim = external / "sess1" / "signals" / "exec-victim.json"  # link is reviews/
                    link = crew_dir / "reviews"
                else:  # crew: the `.crew` ROOT itself is the symlink
                    victim = external / "reviews" / "sess1" / "signals" / "exec-victim.json"
                    link = crew_dir
                victim.parent.mkdir(parents=True, exist_ok=True)
                victim.write_text('{"tag": "exec-victim", "status": "done"}')
                _age(victim, over_mtime)
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(external, link)
                return crew_dir, victim

            for _kind in ("crew", "signals", "session", "reviews"):
                _cd, _victim = _build_symlink_case(_kind, _kind)
                sweep_signal_markers_all(_cd)
                if _victim.exists():
                    log_pass(f"sweep_signal_markers_all refuses a symlinked {_kind} segment (external JSON kept)")
                else:
                    log_fail(f"sweep_signal_markers_all refuses a symlinked {_kind} segment (external JSON kept)",
                             "external file survives (symlink skipped)",
                             "external file deleted THROUGH the symlink")

            # Non-vacuity: swap own_dir for a symlink-FOLLOWING is_dir (the pre-fix
            # behavior) and the SAME sweep DOES delete the external file, proving the
            # own_dir guard is what blocks the delete-through. Both the `signals`
            # segment AND the `.crew` ROOT (the one-level-up guard) are checked, so
            # neither guard can be silently a no-op.
            _saved_own_dir = _ap.own_dir
            try:
                _ap.own_dir = lambda p: p.is_dir()
                for _nv_kind in ("signals", "crew"):
                    _cd_nv, _victim_nv = _build_symlink_case(_nv_kind, f"{_nv_kind}-nv")
                    sweep_signal_markers_all(_cd_nv)
                    if not _victim_nv.exists():
                        log_pass(f"sweep_signal_markers_all non-vacuity: is_dir (symlink-following) DELETES through a symlinked {_nv_kind}")
                    else:
                        log_fail(f"sweep_signal_markers_all non-vacuity: is_dir (symlink-following) DELETES through a symlinked {_nv_kind}",
                                 "external file deleted (guard is load-bearing)",
                                 "external file kept even without the own_dir guard (test is vacuous)")
            finally:
                _ap.own_dir = _saved_own_dir

            # --- Anchoring: both resolve `.crew` through the shared crew_base()
            # (CLAUDE_PROJECT_DIR or cwd, except a terminal `.crew` fallback cwd
            # re-anchors to its parent with a one-time stderr advisory), never
            # cwd-relative. The default-arg calls (as `main()` makes them) act on the CLAUDE_PROJECT_DIR tree the
            # WRITERS/swab now also resolve, so with cwd and CLAUDE_PROJECT_DIR
            # pointing at DIFFERENT trees the report/sweep name the
            # CLAUDE_PROJECT_DIR tree's artifacts, not the cwd tree's. Mirrors the
            # swab anchoring test in test-multiagent.py. ---
            dv_cwd = test_path / "divergence" / "cwd"
            dv_proj = test_path / "divergence" / "projdir"

            def _mk_orphan(crew_dir: Path, name: str) -> Path:
                d = crew_dir / "reviews" / "sess1" / name
                d.mkdir(parents=True, exist_ok=True)
                (d / "run.json").write_text("{}")
                (d / "codex.json").write_text("{}")
                return d

            def _mk_aged_marker(crew_dir: Path, tag: str) -> Path:
                sig = crew_dir / "reviews" / "sess1" / "signals"
                sig.mkdir(parents=True, exist_ok=True)
                m = sig / f"{tag}.json"
                m.write_text(json.dumps({"tag": tag, "status": "done"}))
                _age(m, over_mtime)
                return m

            # The CWD tree: exactly ONE orphan review run + one aged marker. After
            # anchoring, the default-arg sweeps must IGNORE this tree entirely.
            dv_cwd_orphan = _mk_orphan(dv_cwd / ".crew", "run-dddddddddddd")
            dv_cwd_marker = _mk_aged_marker(dv_cwd / ".crew", "exec-cwd")
            # The CLAUDE_PROJECT_DIR tree: a DIFFERENT count (TWO orphans) + its own
            # aged marker. This is the tree crew_base() resolves, so the report must
            # say "2" and the sweep must reap THIS marker; the cwd tree's ONE and its
            # marker are the non-vacuity discriminators (a cwd-relative regression
            # would report "1" and spare this marker).
            dv_proj_orphan_a = _mk_orphan(dv_proj / ".crew", "run-eeeeeeeeeeee")
            dv_proj_orphan_b = _mk_orphan(dv_proj / ".crew", "run-ffffffffffff")
            dv_proj_marker = _mk_aged_marker(dv_proj / ".crew", "exec-proj")

            _dv_saved_cwd = os.getcwd()
            _dv_saved_proj = os.environ.get("CLAUDE_PROJECT_DIR")
            os.chdir(str(dv_cwd))
            os.environ["CLAUDE_PROJECT_DIR"] = str(dv_proj)
            try:
                _dvreport = report_stale_artifacts()   # no arg -> crew_base() root
                sweep_signal_markers_all()             # no arg -> crew_base() root
            finally:
                os.chdir(_dv_saved_cwd)
                if _dv_saved_proj is None:
                    os.environ.pop("CLAUDE_PROJECT_DIR", None)
                else:
                    os.environ["CLAUDE_PROJECT_DIR"] = _dv_saved_proj

            # Report names the CLAUDE_PROJECT_DIR tree's TWO artifacts, not the cwd tree's ONE.
            if "2 stale crew artifact(s)" in _dvreport:
                log_pass("report_stale_artifacts (default arg) reports the CLAUDE_PROJECT_DIR tree, not cwd")
            else:
                log_fail("report_stale_artifacts (default arg) reports the CLAUDE_PROJECT_DIR tree, not cwd",
                         "'2 stale crew artifact(s)' (projdir tree)", repr(_dvreport))

            # Signal sweep reaped the projdir marker, left the cwd marker (non-vacuity).
            if not dv_proj_marker.exists() and dv_cwd_marker.exists():
                log_pass("sweep_signal_markers_all (default arg) reaps the CLAUDE_PROJECT_DIR tree's marker, spares cwd's")
            else:
                log_fail("sweep_signal_markers_all (default arg) reaps the CLAUDE_PROJECT_DIR tree's marker, spares cwd's",
                         "projdir marker gone, cwd marker kept",
                         f"proj_exists={dv_proj_marker.exists()} cwd_exists={dv_cwd_marker.exists()}")

            # The report deleted nothing in either tree.
            if (dv_cwd_orphan.exists() and dv_proj_orphan_a.exists()
                    and dv_proj_orphan_b.exists()):
                log_pass("report_stale_artifacts (default arg) deletes no orphan in either tree")
            else:
                log_fail("report_stale_artifacts (default arg) deletes no orphan in either tree",
                         "all three orphans kept",
                         f"cwd={dv_cwd_orphan.exists()} a={dv_proj_orphan_a.exists()} b={dv_proj_orphan_b.exists()}")

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
            models_module.LoopState(active=True, loop="bl", task="p5").save(p5_state)
            mode = _stat.S_IMODE(os.stat(p5_state).st_mode)
            if mode == 0o600:
                log_pass("save() creates state file 0600 (no chmod window)")
            else:
                log_fail("save() creates state file 0600", "0o600", oct(mode))

            with open(p5_state) as f:
                _saved = json.load(f)
            # Schema 3 unified both loops into one LoopState (task/loop/review
            # fields); older schema-1/2 files still load, with coalesced task.
            if _saved.get("schema") == 3:
                log_pass("save() stamps schema:3")
            else:
                log_fail("save() stamps schema:3", "3", str(_saved.get("schema")))

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
                    [sys.executable, str(crew_state), "set", "bl", "plan_file", _val],
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
            # The relative values land anchored to crew_base.
            _race_vals = (str(test_path / "RACE_A"), str(test_path / "RACE_B"))
            if (parseable and raced.get("plan_file") in _race_vals
                    and raced.get("task") == "race" and not race_stray):
                log_pass("concurrent set: final JSON parseable, valid state, no temp collision")
            else:
                log_fail("concurrent set: final JSON parseable, valid state, no temp collision",
                         "parseable JSON, plan_file in {anchored RACE_A,RACE_B}, task intact, no stray temp",
                         f"parseable={parseable}, plan={raced.get('plan_file')}, "
                         f"task={raced.get('task')!r}, stray={[s.name for s in race_stray]}")

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
                ("bl", "Build Loop", "plan_file", "SHIPPED-BL"),
                ("mt", "Measure-Twice Loop", "plan_file", ".crew/plans/revised.md"),
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

                # --- an allowlisted field still writes (a relative plan_file
                # value lands ANCHORED to crew_base) ---
                _, err, code = run_crew_state(["set", loop, ok_field, ok_value], test_path)
                after = _read_state_json(state_path)
                anchored_ok = str(test_path / ok_value)
                if code == 0 and after.get(ok_field) == anchored_ok:
                    log_pass(f"set {loop} {ok_field} - allowlisted field still writes")
                else:
                    log_fail(f"set {loop} {ok_field} - allowlisted field still writes",
                             f"exit 0, {ok_field}={anchored_ok}",
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
                # on later inspection: it stamps the same completed_at + reason,
                # plus the exit_kind that says a bound ended this loop (the reason
                # alongside says WHICH bound).
                if (after.get("completed_at")
                        and models_module.parse_iso(after.get("completed_at")) is not None
                        and "livelock circuit breaker" in after.get("reason", "")
                        and after.get("exit_kind") == "force_exit"):
                    log_pass(f"{loop}: a stop_fires force-exit stamps completed_at + reason + exit_kind")
                else:
                    log_fail(f"{loop}: a stop_fires force-exit stamps completed_at + reason + exit_kind",
                             "parseable completed_at + livelock reason + exit_kind=force_exit on disk",
                             f"completed_at={after.get('completed_at')!r}, reason={after.get('reason')!r}, "
                             f"exit_kind={after.get('exit_kind')!r}")

                out_after_trip = run_script(persistent_mode, stop_payload)
                if out_after_trip == "{}":
                    log_pass(f"{loop}: the Stop after a stop_fires force-exit ALLOWS (never wedges)")
                else:
                    log_fail(f"{loop}: the Stop after a stop_fires force-exit ALLOWS (never wedges)",
                             "{}", out_after_trip[:200])

                # --- no-deadline opt-out: an ancient clock NEVER trips when
                # BOTH marker halves init writes are present (deadline_minutes
                # exactly 0 AND no_deadline true; the stop-fires cap remains the
                # bound), and the nudge reports the clock as off. ---
                _clear_state_files()
                ancient_start = (_dt.now(_tz.utc) - _td(minutes=100000)).isoformat()
                state_path = _write_loop_state(
                    loop, started_at=ancient_start, deadline_minutes=0,
                    no_deadline=True, stop_fires=0)
                out_nodl = json.loads(run_script(persistent_mode, stop_payload))
                after_nodl = _read_state_json(state_path)
                nodl_reason = out_nodl.get("reason", "")
                if (out_nodl.get("decision") == "block"
                        and "deadline reached" not in nodl_reason
                        and "/none min" in nodl_reason
                        and after_nodl.get("active") is True):
                    log_pass(f"{loop}: deadline_minutes=0 never trips the clock (nudge shows /none)")
                else:
                    log_fail(f"{loop}: deadline_minutes=0 never trips the clock (nudge shows /none)",
                             "block nudge, no deadline reason, '/none min' in nudge, active stays True",
                             f"decision={out_nodl.get('decision')}, active={after_nodl.get('active')}, "
                             f"reason={nodl_reason[:120]!r}")

                # ...while a corruption-shaped value (negative) still reads as
                # the BOUNDED default, never as the opt-out.
                _clear_state_files()
                state_path = _write_loop_state(
                    loop, started_at=ancient_start, deadline_minutes=-5, stop_fires=0)
                out_neg = json.loads(run_script(persistent_mode, stop_payload))
                after_neg = _read_state_json(state_path)
                if (out_neg.get("decision") == "block"
                        and "deadline reached" in out_neg.get("reason", "")
                        and after_neg.get("active") is False):
                    log_pass(f"{loop}: a NEGATIVE deadline still reads as bounded (default), not as opt-out")
                else:
                    log_fail(f"{loop}: a NEGATIVE deadline still reads as bounded (default), not as opt-out",
                             "deadline force-exit at the default bound",
                             f"decision={out_neg.get('decision')}, active={after_neg.get('active')}, "
                             f"reason={out_neg.get('reason', '')[:120]!r}")

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

                # --- a hand-edited deadline cannot DELETE the ceiling by
                # ACCIDENT: a negative, non-int, or LONE-ZERO value on disk must
                # read as the default (DEFAULT_DEADLINE_MINUTES), never as
                # "unbounded". 0 is in this list deliberately: without the
                # paired no_deadline marker only init writes, it is
                # corruption-shaped (and under the pre-opt-out semantics a 0 on
                # disk was ALWAYS bounded, so honoring a lone 0 would flip the
                # meaning of pre-upgrade bytes). The elapsed clock below is set
                # past the default so a deleted ceiling would show up as a
                # MISSING force-exit. ---
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
                        and after.get("schema") == 3):
                    log_pass(f"{loop}: schema-1 state with no started_at is stamped on the first Stop fire")
                else:
                    log_fail(f"{loop}: schema-1 state with no started_at is stamped on the first Stop fire",
                             "block + parseable started_at + schema:3",
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

            for loop, ok_field, ok_value in (
                ("bl", "plan_file", "SHIPPED-BL"),
                ("mt", "plan_file", ".crew/plans/revised.md"),
            ):
                state_cls = models_module.LoopState

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
                # cmd_set anchors a relative plan_file value to crew_base.
                if (code == 0 and after.get(ok_field) == str(test_path / ok_value)
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
                    "plan_file": "INIT", "stop_fires": 0, "parked_fires": 0,
                    "started_at": _dt.now(_tz.utc).isoformat(), "schema": 2,
                }))
                return path

            # Terminal continuation cleanup deliberately remains with the
            # next fresh build-loop init. A persistent-mode force exit must not
            # remove the chain it did not create or own.
            from multiagent import continuations as continuations_live
            chain_path = continuations_live.chain_dir(
                "chainS", "build-executor", base=test_path / ".crew" / "reviews")
            chain_path.mkdir(parents=True, exist_ok=True)
            (chain_path / "record.json").write_text("{}")
            force_chain_path = crew_dir / "build-state-chainS.json"
            force_chain_path.write_text(json.dumps({
                "active": True, "prompt": "chain task", "session_id": "chainS",
                "schema": 3,
            }))
            force_chain_state = models_live.LoopState(
                active=True, loop="bl", task="chain task", session_id="chainS")
            pm_module._force_exit(force_chain_path, force_chain_state, "livelock circuit breaker")
            force_chain_after = _read_state_json(force_chain_path)
            if (force_chain_after.get("active") is False
                    and force_chain_after.get("exit_kind") == "force_exit"
                    and chain_path.exists()):
                log_pass("force exit leaves the build-executor continuation chain for init cleanup")
            else:
                log_fail("force exit leaves the build-executor continuation chain for init cleanup",
                         "force_exit state + existing chain directory",
                         f"state={force_chain_after} chain_exists={chain_path.exists()}")

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
                    st = models_live.LoopState.load(conc_path)
                    pm_module._normalize_bounds(st)
                    pm_module._persist_fires(conc_path, st, stop_delta=1, parked="reset")
                except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                    race_errors.append(f"hook writer: {exc!r}")

            def _cli_writer():
                try:
                    _time_mod.sleep(0.05)  # read INSIDE the hook's window
                    crew_state_module._apply_field(
                        models_live.LoopState, conc_path, "plan_file", "SHIPPED")
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
                    and after.get("plan_file") == "SHIPPED"):
                log_pass("concurrent hook bump + `set` (widened window): BOTH survive (no lost update)")
            else:
                log_fail("concurrent hook bump + `set` (widened window): BOTH survive (no lost update)",
                         "stop_fires=1 AND plan_file=SHIPPED",
                         f"stop_fires={after.get('stop_fires')!r}, "
                         f"plan_file={after.get('plan_file')!r}, "
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
                            ["set", "bl", "plan_file", f"v{i}",
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
            # relative plan_file values land anchored to crew_base
            if (not e2e_errors and after.get("stop_fires") == conc_rounds
                    and after.get("plan_file") == str(test_path / f"v{conc_rounds - 1}")):
                log_pass(f"{conc_rounds} concurrent Stop fires + {conc_rounds} `set` calls: "
                         f"no counter bump lost, no agent field lost")
            else:
                log_fail(f"{conc_rounds} concurrent Stop fires + {conc_rounds} `set` calls: "
                         f"no counter bump lost, no agent field lost",
                         f"stop_fires={conc_rounds}, plan_file=anchored v{conc_rounds - 1}",
                         f"stop_fires={after.get('stop_fires')!r}, "
                         f"plan_file={after.get('plan_file')!r}, "
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
                        ["set", "bl", "plan_file", "LOCKED",
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

            # --- 4. The FIRST terminal writer wins. `_force_exit`'s pre-lock read
            # said the loop was on, but another writer's transaction can land
            # first; restamping exit_kind=force_exit over it would make the audit
            # metadata describe an ending that never happened. Both cases hand
            # `_force_exit` a stale ACTIVE snapshot over an already-off file, which
            # is exactly what the race produces. ---
            for label, ended in (
                ("cancelled", {"exit_kind": "cancelled", "reason": "User cancelled"}),
                ("review_failed", {"exit_kind": "review_failed",
                                   "reason": "two consecutive all-failed panels"}),
            ):
                _clear_state_files()
                fx_path = crew_dir / "build-state-conc.json"
                fx_path.write_text(json.dumps({
                    "active": False, "task": "raced task", "session_id": "conc",
                    "completed_at": "2026-01-01T00:00:00+00:00", "schema": 3,
                    **ended,
                }))
                stale = models_live.LoopState(
                    active=True, loop="bl", task="raced task", session_id="conc")
                pm_module._force_exit(fx_path, stale, "livelock circuit breaker")
                after = _read_state_json(fx_path)
                if (after.get("exit_kind") == ended["exit_kind"]
                        and after.get("reason") == ended["reason"]
                        and after.get("completed_at") == "2026-01-01T00:00:00+00:00"
                        and "force_exit" not in after):
                    log_pass(f"force-exit over an already-{label} loop: original exit_kind stands")
                else:
                    log_fail(f"force-exit over an already-{label} loop: original exit_kind stands",
                             f"exit_kind={ended['exit_kind']}, original reason + completed_at, "
                             f"no force_exit marker",
                             f"exit_kind={after.get('exit_kind')!r}, "
                             f"reason={after.get('reason')!r}, "
                             f"completed_at={after.get('completed_at')!r}, "
                             f"force_exit={after.get('force_exit')!r}")

            # A truthy-non-True `active` is ON everywhere (the shared rule), so a
            # force-exit over it MUST still stamp: the no-op guard reads the same
            # rule the Stop hook blocks on, never a literal True.
            _clear_state_files()
            fx_path = crew_dir / "build-state-conc.json"
            fx_path.write_text(json.dumps({
                "active": 1, "task": "truthy task", "session_id": "conc", "schema": 3,
            }))
            stale = models_live.LoopState(
                active=True, loop="bl", task="truthy task", session_id="conc")
            pm_module._force_exit(fx_path, stale, "livelock circuit breaker")
            after = _read_state_json(fx_path)
            if (after.get("active") is False and after.get("exit_kind") == "force_exit"
                    and after.get("force_exit") is True):
                log_pass("force-exit over a truthy-non-True `active`: still stamps (shared truthiness rule)")
            else:
                log_fail("force-exit over a truthy-non-True `active`: still stamps (shared truthiness rule)",
                         "active=False, exit_kind=force_exit, force_exit=true",
                         f"active={after.get('active')!r}, "
                         f"exit_kind={after.get('exit_kind')!r}, "
                         f"force_exit={after.get('force_exit')!r}")

            _clear_state_files()

            # --- 5. A loop that already recorded its completing verdict sits at
            # phase=done, active=true until `deactivate` stamps the outcome. A
            # bound tripping in that handoff window must NOT relabel signed-off
            # work as a safety failure: the loop outran the bounds, so it routes
            # to the one legal step left instead of force-exiting. ---
            for loop, banner, deactivate_cmd in (
                ("bl", "Build Loop", "state deactivate bl"),
                ("mt", "Measure-Twice Loop", "state deactivate mt"),
            ):
                for bound, extra in (
                    ("stop-fires cap", {"stop_fires": 2, "max_stop_fires": 2}),
                    ("deadline", {"started_at": (_dt.now(_tz.utc) - _td(minutes=90)).isoformat(),
                                  "deadline_minutes": 60, "stop_fires": 0}),
                ):
                    _clear_state_files()
                    done_path = _write_loop_state(
                        loop, phase="done", last_verdict="APPROVED", schema=3, **extra)
                    out = json.loads(run_script(persistent_mode, stop_payload))
                    after = _read_state_json(done_path)
                    body = out.get("reason", "")
                    if (out.get("decision") == "block"
                            and "Safety Limit Reached" not in body
                            and deactivate_cmd in body
                            and not after.get("exit_kind")
                            and "force_exit" not in after
                            and after.get("active") is True):
                        log_pass(f"{loop}: a phase=done loop at the {bound} keeps exit_kind UNSET "
                                 f"and routes to deactivate")
                    else:
                        log_fail(f"{loop}: a phase=done loop at the {bound} keeps exit_kind UNSET "
                                 f"and routes to deactivate",
                                 f"block naming `{deactivate_cmd}`, no Safety Limit banner, "
                                 f"exit_kind unset, active=True",
                                 f"decision={out.get('decision')}, "
                                 f"exit_kind={after.get('exit_kind')!r}, "
                                 f"force_exit={after.get('force_exit')!r}, "
                                 f"active={after.get('active')!r}, body={body[:160]!r}")

                    # The completion path SURVIVES the bound trip: the verdict the
                    # panel signed off is still what gets recorded.
                    run_crew_state(["deactivate", loop, "--reason", "Panel approved"],
                                   test_path)
                    ended = _read_state_json(done_path)
                    if (ended.get("exit_kind") == "approved"
                            and ended.get("active") is False):
                        log_pass(f"{loop}: deactivate after a {bound} trip still stamps approved")
                    else:
                        log_fail(f"{loop}: deactivate after a {bound} trip still stamps approved",
                                 "exit_kind=approved, active=False",
                                 f"exit_kind={ended.get('exit_kind')!r}, "
                                 f"active={ended.get('active')!r}")

                # A loop still WORKING faces both bounds exactly as before: the
                # carve-out is for completion, not for every unfinished phase.
                for phase in ("drafting", "reviewing"):
                    _clear_state_files()
                    working_path = _write_loop_state(
                        loop, phase=phase, stop_fires=2, max_stop_fires=2, schema=3)
                    out = json.loads(run_script(persistent_mode, stop_payload))
                    after = _read_state_json(working_path)
                    if (f"[{banner} - Safety Limit Reached]" in out.get("reason", "")
                            and after.get("exit_kind") == "force_exit"
                            and after.get("active") is False):
                        log_pass(f"{loop}: a {phase} loop at the stop-fires cap still force-exits")
                    else:
                        log_fail(f"{loop}: a {phase} loop at the stop-fires cap still force-exits",
                                 f"'[{banner} - Safety Limit Reached]', exit_kind=force_exit, active=False",
                                 f"exit_kind={after.get('exit_kind')!r}, "
                                 f"active={after.get('active')!r}, "
                                 f"reason={out.get('reason', '')[:120]!r}")

            _clear_state_files()

            # --- 6. The hook announces a deactivation only when it performed one.
            # `_force_exit` re-checks INSIDE the lock, so a bound tripping against a
            # pre-lock read of a still-working loop can find the loop completed by
            # the time the lock takes, and leave the bytes alone. The two branches
            # are driven in-process: only a stale state object can hold the pre-lock
            # and locked reads apart, which is exactly what the race produces. ---
            import contextlib as _contextlib
            import io as _io

            def _drive_handle_loop(path: Path, stale) -> str:
                buf = _io.StringIO()
                with _contextlib.redirect_stdout(buf):
                    pm_module._handle_loop(
                        path, stale, False,
                        banner="Build Loop",
                        task_text="raced task",
                        detail_lines=[],
                        recipe="RECIPE BODY",
                        cancel_cmd="/crew:cancel-build",
                        done_body='ONE step remains: "crew" state deactivate bl',
                    )
                return json.loads(buf.getvalue()).get("reason", "")

            _clear_state_files()
            fx_path = crew_dir / "build-state-conc.json"
            fx_path.write_text(json.dumps({
                "active": True, "task": "raced task", "session_id": "conc",
                "phase": "drafting", "schema": 3,
            }))
            body = _drive_handle_loop(fx_path, models_live.LoopState(
                active=True, loop="bl", task="raced task", session_id="conc",
                phase="drafting", stop_fires=2, max_stop_fires=2))
            after = _read_state_json(fx_path)
            if ("[Build Loop - Safety Limit Reached]" in body
                    and after.get("exit_kind") == "force_exit"
                    and after.get("active") is False):
                log_pass("a real force exit announces the safety limit (it did deactivate)")
            else:
                log_fail("a real force exit announces the safety limit (it did deactivate)",
                         "'[Build Loop - Safety Limit Reached]', exit_kind=force_exit, active=False",
                         f"exit_kind={after.get('exit_kind')!r}, "
                         f"active={after.get('active')!r}, body={body[:160]!r}")

            _clear_state_files()
            fx_path = crew_dir / "build-state-conc.json"
            fx_path.write_text(json.dumps({
                "active": True, "task": "raced task", "session_id": "conc",
                "phase": "done", "last_verdict": "APPROVED", "schema": 3,
            }))
            body = _drive_handle_loop(fx_path, models_live.LoopState(
                active=True, loop="bl", task="raced task", session_id="conc",
                phase="drafting", stop_fires=2, max_stop_fires=2))
            after = _read_state_json(fx_path)
            if ("Safety Limit Reached" not in body
                    and "state deactivate bl" in body
                    and not after.get("exit_kind")
                    and "force_exit" not in after
                    and after.get("active") is True):
                log_pass("a force exit no-opped by a concurrent completion announces the "
                         "done guidance, not a deactivation")
            else:
                log_fail("a force exit no-opped by a concurrent completion announces the "
                         "done guidance, not a deactivation",
                         "no Safety Limit banner, names `state deactivate bl`, "
                         "exit_kind unset, active=True",
                         f"exit_kind={after.get('exit_kind')!r}, "
                         f"force_exit={after.get('force_exit')!r}, "
                         f"active={after.get('active')!r}, body={body[:160]!r}")

            _clear_state_files()

            # =========================================================================
            # `deactivate` stamps completed_at on the SAME clock the hook's
            # force-exit does: parse_iso reads a naive stamp AS UTC, so a local
            # naive one would be off by the machine's offset on every later read.
            # =========================================================================
            run_crew_state(["init", "bl", "--prompt", "clock task"], test_path)
            run_crew_state(["deactivate", "bl", "--cancel", "--reason", "done"], test_path)
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
                ("[tuning]\ndeadline_minutes = 2000\n", [], builtin_deadline,
                 "an out-of-policy config deadline is dropped for the builtin"),
                ("[tuning]\ndeadline_minutes = 0\n", [], builtin_deadline,
                 "a PER-REPO no-deadline 0 is refused for the builtin (global-only opt-out)"),
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

                # --- the budget bounds UNBROKEN runs of parks, never the
                # session's lifetime total: park under the cap, take one
                # blocking fire, and a fresh run of parks is fully allowed
                # (a cumulative counter would exhaust a cap of 5 on the sixth
                # lifetime park) ---
                _clear_state_files()
                state_path = _write_loop_state(
                    loop, stop_fires=0, parked_fires=0, max_parked_fires=5)
                run_outs = [run_script(persistent_mode, _parked_payload(one_task))
                            for _ in range(3)]
                run_script(persistent_mode, stop_payload)  # blocking fire: resets
                run_outs += [run_script(persistent_mode, _parked_payload(one_task))
                             for _ in range(3)]
                after = _read_state_json(state_path)
                all_parks_allowed = all(
                    '"decision": "block"' not in o for o in run_outs)
                if (all_parks_allowed and after.get("parked_fires") == 3
                        and after.get("stop_fires") == 1):
                    log_pass(f"{loop}: 3 parks + a blocking fire + 3 parks all allow "
                             f"(the run resets; a lifetime total would exhaust cap 5)")
                else:
                    log_fail(f"{loop}: 3 parks + a blocking fire + 3 parks all allow "
                             f"(the run resets; a lifetime total would exhaust cap 5)",
                             "6 parked allows, parked_fires=3, stop_fires=1",
                             f"allowed={all_parks_allowed}, "
                             f"parked_fires={after.get('parked_fires')}, "
                             f"stop_fires={after.get('stop_fires')}")

                # --- the past-cap NUDGE also resets the run: the nudge forces a
                # working turn, so the next parks start a NEW run instead of
                # every later park being a blocked stop fire (the kept counter
                # once let panel waits exhaust the cap cumulatively in a
                # session whose every turn-end was parked) ---
                _clear_state_files()
                state_path = _write_loop_state(
                    loop, stop_fires=0, parked_fires=3, max_parked_fires=3)
                out_trip = json.loads(run_script(persistent_mode, _parked_payload(one_task)))
                after_trip = _read_state_json(state_path)
                out_next = json.loads(run_script(persistent_mode, _parked_payload(one_task)))
                after_next = _read_state_json(state_path)
                if (out_trip.get("decision") == "block"
                        and after_trip.get("stop_fires") == 1
                        and after_trip.get("parked_fires") == 0
                        and "decision" not in out_next
                        and after_next.get("parked_fires") == 1
                        and after_next.get("stop_fires") == 1):
                    log_pass(f"{loop}: the cap trips on cap consecutive parks, and the nudge "
                             f"RESETS the run (next park is allowed again)")
                else:
                    log_fail(f"{loop}: the cap trips on cap consecutive parks, and the nudge "
                             f"RESETS the run (next park is allowed again)",
                             "trip: block + stop_fires=1 + parked_fires=0; next: allow + parked_fires=1",
                             f"trip decision={out_trip.get('decision')}, "
                             f"trip parked={after_trip.get('parked_fires')}, "
                             f"next out={json.dumps(out_next)[:60]}, "
                             f"next parked={after_next.get('parked_fires')}, "
                             f"next fires={after_next.get('stop_fires')}")

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
            # AWAITING-INPUT PAUSE: waiting on a HUMAN must be FREE, like parking
            # A loop stops on `ok=false` executor failures, exit-3 --force advisories,
            # or any AskUserQuestion with NOTHING in flight, so `is_parked` is false
            # and the old hook blocked EVERY turn-end: the agent was dragged back
            # turn after turn (the nudge livelock). `awaiting_input` is the third
            # state: active + waiting on the human. It parks the Stop under the SAME
            # bounded cap, so a forgotten flag can never disable the loop.
            # =========================================================================
            log_section("awaiting-input pause (bl + mt)")

            for loop, banner in (("bl", "Build Loop"), ("mt", "Measure-Twice Loop")):
                # --- awaiting_input + active + NOT parked: ALLOW, and the wait costs
                # no stop_fire (the whole point: the session actually stops, and the
                # human's next message re-invokes it, instead of a forced re-entry) ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=5, awaiting_input=True)
                await_outs = [
                    run_script(persistent_mode, stop_payload) for _ in range(4)
                ]
                after = _read_state_json(state_path)
                if (all(o == "{}" for o in await_outs)
                        and after.get("stop_fires") == 5
                        and after.get("parked_fires") == 4
                        and after.get("active") is True):
                    log_pass(f"{loop}: awaiting_input ALLOWS an UNPARKED Stop ({{}}), stop_fires unchanged")
                else:
                    log_fail(f"{loop}: awaiting_input ALLOWS an UNPARKED Stop ({{}}), stop_fires unchanged",
                             "4x '{}', stop_fires=5, parked_fires=4, active=True",
                             f"outs={[o[:60] for o in await_outs]}, "
                             f"stop_fires={after.get('stop_fires')}, "
                             f"parked_fires={after.get('parked_fires')}, active={after.get('active')}")

                # --- the cap bounds a STUCK flag: past max_parked_fires an
                # awaiting_input Stop nudges, burns a stop_fire, and CLEARS the flag
                # so the next yield re-arms the nudge (the agent forgot to clear it) ---
                _clear_state_files()
                state_path = _write_loop_state(
                    loop, stop_fires=0, parked_fires=3, max_parked_fires=3,
                    awaiting_input=True)
                out_trip = json.loads(run_script(persistent_mode, stop_payload))
                after = _read_state_json(state_path)
                if (out_trip.get("decision") == "block"
                        and f"[{banner}]" in out_trip.get("reason", "")
                        and after.get("stop_fires") == 1
                        and after.get("parked_fires") == 0
                        and after.get("awaiting_input") is False
                        and after.get("active") is True):
                    log_pass(f"{loop}: a stuck awaiting_input past the cap nudges, burns a fire, clears the flag")
                else:
                    log_fail(f"{loop}: a stuck awaiting_input past the cap nudges, burns a fire, clears the flag",
                             f"block '[{banner}]', stop_fires=1, parked_fires=0, awaiting_input=False",
                             f"decision={out_trip.get('decision')}, stop_fires={after.get('stop_fires')}, "
                             f"parked_fires={after.get('parked_fires')}, "
                             f"awaiting_input={after.get('awaiting_input')}")

                # --- behavior-neutral: awaiting_input False + not parked STILL nudges
                # (every existing not-parked test is this case with the key absent) ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=0, awaiting_input=False)
                out_off = json.loads(run_script(persistent_mode, stop_payload))
                after = _read_state_json(state_path)
                if out_off.get("decision") == "block" and after.get("stop_fires") == 1:
                    log_pass(f"{loop}: awaiting_input=False leaves the nudge behavior unchanged")
                else:
                    log_fail(f"{loop}: awaiting_input=False leaves the nudge behavior unchanged",
                             "block nudge, stop_fires=1",
                             f"decision={out_off.get('decision')}, stop_fires={after.get('stop_fires')}")

                # --- the `await` verb SETS the flag field-level, counters untouched
                # (it is NOT in AGENT_SETTABLE and must never carry back a stale
                # counter snapshot, exactly like `set`) ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=7, parked_fires=2)
                _, _, code_set = run_crew_state(["await", loop], test_path)
                after = _read_state_json(state_path)
                if (code_set == 0 and after.get("awaiting_input") is True
                        and after.get("stop_fires") == 7
                        and after.get("parked_fires") == 2):
                    log_pass(f"{loop}: `crew state await` sets awaiting_input without touching counters")
                else:
                    log_fail(f"{loop}: `crew state await` sets awaiting_input without touching counters",
                             "code=0, awaiting_input=True, stop_fires=7, parked_fires=2",
                             f"code={code_set}, awaiting_input={after.get('awaiting_input')}, "
                             f"stop_fires={after.get('stop_fires')}, parked_fires={after.get('parked_fires')}")

                # --- `await --clear` clears it ---
                _, _, code_clear = run_crew_state(["await", loop, "--clear"], test_path)
                after = _read_state_json(state_path)
                if code_clear == 0 and after.get("awaiting_input") is False:
                    log_pass(f"{loop}: `crew state await --clear` clears awaiting_input")
                else:
                    log_fail(f"{loop}: `crew state await --clear` clears awaiting_input",
                             "code=0, awaiting_input=False",
                             f"code={code_clear}, awaiting_input={after.get('awaiting_input')}")

                # --- key-ABSENT (legacy) still nudges: a state file that never had
                # the key loads awaiting_input=False, so behavior is unchanged ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=0)  # no awaiting_input key
                out_absent = json.loads(run_script(persistent_mode, stop_payload))
                after = _read_state_json(state_path)
                if out_absent.get("decision") == "block" and after.get("stop_fires") == 1:
                    log_pass(f"{loop}: a key-ABSENT (legacy) state nudges (loads awaiting_input=False)")
                else:
                    log_fail(f"{loop}: a key-ABSENT (legacy) state nudges (loads awaiting_input=False)",
                             "block, stop_fires=1",
                             f"decision={out_absent.get('decision')}, stop_fires={after.get('stop_fires')}")

                # --- a truthy-but-WRONG value ("yes") must STILL nudge: the strict
                # `is True` load never lets a garbage value suppress the nudge ---
                _clear_state_files()
                state_path = _write_loop_state(loop, stop_fires=0, awaiting_input="yes")
                out_garbage = json.loads(run_script(persistent_mode, stop_payload))
                after = _read_state_json(state_path)
                if out_garbage.get("decision") == "block" and after.get("stop_fires") == 1:
                    log_pass(f"{loop}: a truthy-but-wrong awaiting_input ('yes') still nudges (strict is-True load)")
                else:
                    log_fail(f"{loop}: a truthy-but-wrong awaiting_input ('yes') still nudges (strict is-True load)",
                             "block, stop_fires=1",
                             f"decision={out_garbage.get('decision')}, stop_fires={after.get('stop_fires')}")

                # --- termination BEATS waiting: a tripped bound (stop_fires cap) with
                # awaiting_input SET still force-exits, never rides the pause. The
                # bounds are evaluated before the wait branch, so the pause cannot
                # hide a runaway loop. ---
                _clear_state_files()
                state_path = _write_loop_state(
                    loop, stop_fires=2, max_stop_fires=2, awaiting_input=True)
                out_term = json.loads(run_script(persistent_mode, stop_payload))
                after = _read_state_json(state_path)
                if ("Safety Limit Reached" in out_term.get("reason", "")
                        and after.get("active") is False):
                    log_pass(f"{loop}: termination beats waiting (a tripped bound force-exits even with awaiting_input set)")
                else:
                    log_fail(f"{loop}: termination beats waiting (a tripped bound force-exits even with awaiting_input set)",
                             "'Safety Limit Reached' nudge, active=False",
                             f"reason={out_term.get('reason', '')[:100]!r}, active={after.get('active')}")

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

            # A `deactivate` writes the same two keys, and `show --verbose` must
            # display them for the same reason.
            for loop, text_flag, extra in (("bl", "--prompt", []),
                                           ("mt", "--task", ["--auto-plan"])):
                _clear_state_files()
                run_crew_state(["init", loop, text_flag, "shown task"] + extra, test_path)
                run_crew_state(
                    ["deactivate", loop, "--cancel", "--reason", "stopped by the user"],
                    test_path)
                out_v, err_v, code_v = run_crew_state(["show", loop, "--verbose"], test_path)
                try:
                    shown = json.loads(out_v)
                except json.JSONDecodeError:
                    shown = {}
                if (code_v == 0 and shown.get("active") is False
                        and shown.get("completed_at")
                        and shown.get("reason") == "stopped by the user"):
                    log_pass(f"show {loop} --verbose - a deactivated loop shows completed_at + reason")
                else:
                    log_fail(f"show {loop} --verbose - a deactivated loop shows completed_at + reason",
                             "active=false, completed_at, reason='stopped by the user'",
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
            # LOOP INIT -f (task off the shell)
            # =========================================================================
            # =========================================================================
            # Schema-2 coalesce + the shrunken allowlist (one LoopState, both loops)
            # =========================================================================
            log_section("LoopState: schema-2 task coalesce + allowlist shrink")

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # AC-17 shape: a mid-upgrade ACTIVE schema-2 file keeps its task text
            # visible through the coalesce (legacy `prompt` for bl,
            # `task_description` for mt) in BOTH `show` and the Stop-hook nudge.
            for loop, prefix, legacy_key in (
                ("bl", "build-state", "prompt"),
                ("mt", "measure-twice-state", "task_description"),
            ):
                for f in crew_dir.glob("*-state*.json*"):
                    f.unlink()
                legacy_file = crew_dir / f"{prefix}-co2.json"
                legacy_file.write_text(json.dumps({
                    "active": True, legacy_key: f"legacy {loop} task",
                    "plan_file": ".crew/plans/co2.md", "session_id": "co2",
                    "schema": 2,
                }))
                sj, _, show_code = run_crew_state(
                    ["show", loop, "--session-id", "co2"], test_path)
                if show_code == 0 and f"legacy {loop} task" in sj:
                    log_pass(f"{loop}: schema-2 `{legacy_key}` coalesces into task for `show`")
                else:
                    log_fail(f"{loop}: schema-2 `{legacy_key}` coalesces into task for `show`",
                             f"task=\"legacy {loop} task\" in compact line", sj[:160])
                nudge_raw = run_script(
                    persistent_mode,
                    json.dumps({"directory": str(test_path), "session_id": "co2"}))
                try:
                    nudge = json.loads(nudge_raw)
                except (json.JSONDecodeError, ValueError):
                    nudge = {}
                if (nudge.get("decision") == "block"
                        and f"legacy {loop} task" in nudge.get("reason", "")):
                    log_pass(f"{loop}: schema-2 `{legacy_key}` coalesces into the nudge's task line")
                else:
                    log_fail(f"{loop}: schema-2 `{legacy_key}` coalesces into the nudge's task line",
                             "block nudge carrying the legacy task text",
                             nudge_raw[:200])

            # The allowlist shrank to {plan_file}: `last_verdict` now refuses
            # (exit 2, bytes untouched; the review verbs own it), and
            # `completion_promise` is not even a field any more (exit 1).
            for loop, prefix in (("bl", "build-state"), ("mt", "measure-twice-state")):
                for f in crew_dir.glob("*-state*.json*"):
                    f.unlink()
                state_file = crew_dir / f"{prefix}-al.json"
                state_file.write_text(json.dumps({
                    "active": True, "task": "allowlist", "session_id": "al",
                    "schema": 3,
                }))
                before_bytes = state_file.read_bytes()
                _, err, code = run_crew_state(
                    ["set", loop, "last_verdict", "APPROVED", "--session-id", "al"],
                    test_path)
                if (code == 2 and state_file.read_bytes() == before_bytes
                        and "not settable" in err):
                    log_pass(f"set {loop} last_verdict - refused (exit 2), state file byte-unchanged")
                else:
                    log_fail(f"set {loop} last_verdict - refused (exit 2), state file byte-unchanged",
                             "exit 2, bytes identical, 'not settable'",
                             f"code={code} unchanged={state_file.read_bytes() == before_bytes} err={err[:140]}")
                _, err, code = run_crew_state(
                    ["set", loop, "completion_promise", "DONE", "--session-id", "al"],
                    test_path)
                if code != 0 and state_file.read_bytes() == before_bytes:
                    log_pass(f"set {loop} completion_promise - the field is gone (nonzero, bytes unchanged)")
                else:
                    log_fail(f"set {loop} completion_promise - the field is gone (nonzero, bytes unchanged)",
                             "nonzero exit, bytes identical",
                             f"code={code} err={err[:140]}")

            log_section("crew state init -f (task off the shell)")

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # bl: -f reads the file into `task`, flags preserved byte-for-byte.
            p8_taskfile = crew_dir / "task-bl-p8.txt"
            p8_taskfile.write_text("Fix the auth bug --panel full", encoding="utf-8")
            _, stderr, code = run_crew_state(
                ["init", "bl", "-f", str(p8_taskfile), "--session-id", "p8"], test_path)
            if code == 0:
                sj, _, _ = run_crew_state(["show", "bl", "--session-id", "p8", "--verbose"], test_path)
                if '"task": "Fix the auth bug --panel full"' in sj:
                    log_pass("init bl -f: reads file into task (flags preserved byte-for-byte)")
                else:
                    log_fail("init bl -f: reads file into task", "task from file", sj)
            else:
                log_fail("init bl -f: exit 0", "exit 0", f"exit {code}, stderr: {stderr}")

            # mt: -f reads the file into `task`.
            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()
            p8_mtfile = crew_dir / "task-mt-p8.txt"
            p8_mtfile.write_text("Design the profile system", encoding="utf-8")
            _, stderr, code = run_crew_state(
                ["init", "mt", "-f", str(p8_mtfile), "--auto-plan", "--session-id", "p8m"], test_path)
            if code == 0:
                sj, _, _ = run_crew_state(["show", "mt", "--session-id", "p8m", "--verbose"], test_path)
                if '"task": "Design the profile system"' in sj:
                    log_pass("init mt -f: reads file into task")
                else:
                    log_fail("init mt -f: reads file into task", "task from file", sj)
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

            # =================================================================
            # Relative -f anchoring + the --consume matrix. A RELATIVE -f
            # resolves against crew_base (CLAUDE_PROJECT_DIR), never the shell
            # cwd (the engine-side half of the no-${…}-in-recipes convention).
            # =================================================================
            log_section("crew state init: relative -f anchoring + --consume")

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            divergent_cwd = test_path / "divergent-cwd"
            divergent_cwd.mkdir(exist_ok=True)

            def run_crew_state_divergent(args: list) -> tuple:
                """crew-state with cwd != CLAUDE_PROJECT_DIR (the anchoring case)."""
                result = subprocess.run(
                    [sys.executable, str(crew_state)] + args,
                    capture_output=True, text=True, cwd=divergent_cwd,
                    env={**_neutral_env(), "CLAUDE_PROJECT_DIR": str(test_path)},
                )
                return result.stdout.strip(), result.stderr.strip(), result.returncode

            # Relative -f from a DIVERGENT cwd reads under CLAUDE_PROJECT_DIR;
            # --consume deletes the spill after the successful init.
            spill = crew_dir / "task-bl-dv.txt"
            spill.write_text("Anchored task text", encoding="utf-8")
            _, stderr, code = run_crew_state_divergent(
                ["init", "bl", "-f", ".crew/task-bl-dv.txt",
                 "--session-id", "dv", "--consume"])
            sj, _, _ = run_crew_state(
                ["show", "bl", "--session-id", "dv", "--verbose"], test_path)
            if (code == 0 and '"task": "Anchored task text"' in sj
                    and not spill.exists()
                    and not (divergent_cwd / ".crew").exists()):
                log_pass("init bl relative -f (divergent cwd): reads under crew_base; --consume deletes the spill")
            else:
                log_fail("init bl relative -f (divergent cwd) + --consume",
                         "task read from project spill, spill deleted, no cwd .crew",
                         f"exit {code}, stderr={stderr!r}, spill={spill.exists()}, "
                         f"cwdleak={(divergent_cwd / '.crew').exists()}, show={sj[:120]}")

            # Absolute -f: byte-identical caller experience, and NO --consume
            # never deletes.
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()
            spill_abs = crew_dir / "task-bl-abs.txt"
            spill_abs.write_text("Absolute spill", encoding="utf-8")
            _, stderr, code = run_crew_state_divergent(
                ["init", "bl", "-f", str(spill_abs), "--session-id", "ab"])
            if code == 0 and spill_abs.exists():
                log_pass("init bl absolute -f unchanged; absent --consume never deletes the spill")
            else:
                log_fail("init bl absolute -f / no --consume",
                         "exit 0, spill preserved",
                         f"exit {code}, stderr={stderr!r}, spill={spill_abs.exists()}")

            # FAILED init preserves the spill even WITH --consume (here: the
            # session already has an active loop, the conflict refusal).
            _, stderr, code = run_crew_state(
                ["init", "bl", "-f", str(spill_abs), "--session-id", "ab", "--consume"],
                test_path)
            if code != 0 and spill_abs.exists():
                log_pass("init bl --consume on a FAILED init: spill preserved for retry")
            else:
                log_fail("init bl --consume on a FAILED init",
                         "nonzero exit, spill preserved",
                         f"exit {code}, stderr={stderr!r}, spill={spill_abs.exists()}")
            spill_abs.unlink(missing_ok=True)

            # --consume without -f: exit 2 naming why, nothing written.
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()
            _, stderr, code = run_crew_state(
                ["init", "bl", "--prompt", "inline", "--consume", "--session-id", "cx"],
                test_path)
            if (code == 2 and "--consume requires -f" in stderr
                    and not (crew_dir / "build-state-cx.json").exists()):
                log_pass("init bl --consume without -f: exit 2 naming the fix, no state file")
            else:
                log_fail("init bl --consume without -f",
                         "exit 2 + '--consume requires -f', no file",
                         f"exit {code}, stderr={stderr!r}, "
                         f"file={(crew_dir / 'build-state-cx.json').exists()}")

            # --consume collision guard: when -f resolves to the state file
            # init just wrote, or to the mt plan file, the unlink is SKIPPED
            # with a warning (init still succeeds; live loop data survives).
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()
            cg_state = crew_dir / "build-state-cg.json"
            cg_state.write_text(
                json.dumps({"active": False, "loop": "bl", "task": "old",
                            "session_id": "cg", "schema": 3}),
                encoding="utf-8")
            _, stderr, code = run_crew_state(
                ["init", "bl", "-f", str(cg_state), "--session-id", "cg",
                 "--consume"], test_path)
            sj, _, _ = run_crew_state(
                ["show", "bl", "--session-id", "cg", "--verbose"], test_path)
            if (code == 0 and cg_state.exists()
                    and "--consume skipped" in stderr
                    and '"active": true' in sj):
                log_pass("init bl --consume -f == state file: unlink skipped, state survives")
            else:
                log_fail("init bl --consume -f == state file",
                         "exit 0, warning, state file survives active",
                         f"exit {code}, stderr={stderr!r}, "
                         f"exists={cg_state.exists()}, show={sj[:120]}")

            # mt: -f resolving to the --plan-file target keeps the plan.
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()
            cg_plans = crew_dir / "plans"
            cg_plans.mkdir(exist_ok=True)
            cg_plan = cg_plans / "cg-plan.md"
            cg_plan.write_text("plan body", encoding="utf-8")
            _, stderr, code = run_crew_state(
                ["init", "mt", "-f", str(cg_plan), "--plan-file", str(cg_plan),
                 "--session-id", "cg2", "--consume"], test_path)
            if code == 0 and cg_plan.exists() and "--consume skipped" in stderr:
                log_pass("init mt --consume -f == plan file: unlink skipped, plan survives")
            else:
                log_fail("init mt --consume -f == plan file",
                         "exit 0, warning, plan survives",
                         f"exit {code}, stderr={stderr!r}, plan={cg_plan.exists()}")

            # Symlink alias: -f through a symlinked dir still names the plan
            # file (samefile), so the guard must catch it too.
            cg_alias_dir = crew_dir / "plans-alias"
            if not cg_alias_dir.exists():
                cg_alias_dir.symlink_to(cg_plans, target_is_directory=True)
            _, stderr, code = run_crew_state(
                ["init", "mt", "-f", str(cg_alias_dir / "cg-plan.md"),
                 "--plan-file", str(cg_plan), "--session-id", "cg3",
                 "--consume"], test_path)
            if code == 0 and cg_plan.exists() and "--consume skipped" in stderr:
                log_pass("init mt --consume -f symlink-alias of plan: unlink skipped")
            else:
                log_fail("init mt --consume -f symlink-alias of plan",
                         "exit 0, warning, plan survives",
                         f"exit {code}, stderr={stderr!r}, plan={cg_plan.exists()}")
            cg_alias_dir.unlink(missing_ok=True)
            cg_plan.unlink(missing_ok=True)

            # `set plan_file` anchors a RELATIVE value at write time (the stored
            # path is absolute under crew_base), from a divergent cwd too.
            for f in crew_dir.glob("*-state*.json*"):
                f.unlink()
            _, stderr, code = run_crew_state(
                ["init", "mt", "--task", "anchor plan", "--auto-plan",
                 "--session-id", "pf"], test_path)
            _, stderr, code = run_crew_state_divergent(
                ["set", "mt", "plan_file", ".crew/plans/anchored.md",
                 "--session-id", "pf"])
            sj, _, _ = run_crew_state(
                ["show", "mt", "--session-id", "pf", "--verbose"], test_path)
            expected_plan = str(test_path / ".crew" / "plans" / "anchored.md")
            if code == 0 and json.dumps(expected_plan)[1:-1] in sj:
                log_pass("set mt plan_file: relative value stored anchored to crew_base")
            else:
                log_fail("set mt plan_file anchoring",
                         f"stored {expected_plan}",
                         f"exit {code}, stderr={stderr!r}, show={sj[:200]}")

            for f in crew_dir.glob("*"):
                if f.is_file():
                    f.unlink()

            # =========================================================================
            # REVIEW TRANSITION VERBS (both loops): begin-review freezes the
            # identity a panel is launched against; record-verdict judges the
            # results against THAT identity and nothing else. Every refusal must
            # leave the state bytes untouched: a gate that half-writes is worse
            # than no gate.
            # =========================================================================
            log_section("review verbs: begin-review / record-verdict (bl + mt)")

            from multiagent import review_runs as rr

            # A real git repo: the completion gate re-resolves the frozen target
            # spec, so every target kind has to round-trip through the resolver.
            rv_root = test_path / "review-verbs-repo"
            rv_root.mkdir()

            def _rv_git(*args) -> None:
                subprocess.run(
                    ["git", *args], cwd=rv_root, capture_output=True, text=True,
                    env={**_neutral_env(),
                         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                         "GIT_CONFIG_GLOBAL": str(rv_root / "no-such-gitconfig")},
                )

            _rv_git("init", "-b", "main")
            # .crew/ is ignored, so the state files and run dirs these tests write
            # can never leak into a working-tree target's own diff.
            (rv_root / ".gitignore").write_text(".crew/\n", encoding="utf-8")
            (rv_root / "f.txt").write_text("one\n", encoding="utf-8")
            _rv_git("add", "-A")
            _rv_git("commit", "-m", "c1")
            _rv_git("checkout", "-b", "feat")
            (rv_root / "f.txt").write_text("two\n", encoding="utf-8")
            _rv_git("add", "-A")
            _rv_git("commit", "-m", "c2")
            (rv_root / "f.txt").write_text("three\n", encoding="utf-8")  # dirty tree

            def _rv_reviews(sid: str) -> Path:
                return rv_root / ".crew" / "reviews" / sid

            def _rv_mint(sid, *, spec, base, tsha, seats, task_seats=()):
                """Mint a run dir + pointer exactly as review-prep would."""
                sigs = {n: {"kind": "subprocess", "provider": "codex", "model": "m"}
                        for n in seats}
                sigs.update({n: {"kind": "task", "model": "opus"} for n in task_seats})
                run_id, digest = rr.mint_identity(
                    target_sha256=tsha, target_spec=spec, target_base=base,
                    seat_signatures=sigs)
                reviews = _rv_reviews(sid)
                run_d = reviews / run_id
                run_d.mkdir(parents=True, exist_ok=True)
                rr.write_run_json_once(run_d, {
                    "run_id": run_id, "identity_digest": digest,
                    "target_sha256": tsha, "target_spec": spec, "target_base": base,
                    "subprocess_seats": list(seats), "task_seats": list(task_seats),
                    "seat_signatures": sigs,
                    "created_at": "2026-01-01T00:00:00+00:00",
                })
                rr.write_pointer(reviews, run_id=run_id, target_sha256=tsha,
                                 created_at="2026-01-01T00:00:00+00:00")
                return run_id, run_d

            def _rv_land(run_d: Path, seat: str, run_id: str, tsha: str, ok=True) -> None:
                (run_d / f"{seat}.json").write_text(json.dumps({
                    "name": seat, "model": "m", "ok": ok,
                    "output": "VERDICT: APPROVED", "error": None, "elapsed": 1.0,
                    "run_id": run_id, "target_sha256": tsha,
                }), encoding="utf-8")

            def _rv_state_path(loop: str, sid: str) -> Path:
                stem = "build-state" if loop == "bl" else "measure-twice-state"
                return rv_root / ".crew" / f"{stem}-{sid}.json"

            def _rv_init(loop: str, sid: str):
                args = ["init", loop, "--session-id", sid]
                if loop == "bl":
                    args += ["--prompt", "review-verb task"]
                else:
                    args += ["--task", "review-verb task", "--plan-file", ".crew/plans/x.md"]
                return run_crew_state(args, rv_root)

            def _rv_begin(loop: str, sid: str):
                return run_crew_state(["begin-review", loop, "--session-id", sid], rv_root)

            def _rv_verdict(loop: str, sid: str, verdict: str, *extra):
                return run_crew_state(
                    ["record-verdict", loop, verdict, *extra, "--session-id", sid],
                    rv_root)

            def _rv_resolve(spec: str, base: str = "main") -> dict:
                """The spec + hash the resolver itself produces for a target, from
                INSIDE the repo (the verbs' cwd contract)."""
                code = (
                    "import json, sys;"
                    f"sys.path.insert(0, {str(SCRIPT_DIR)!r});"
                    "from multiagent import targets, review_runs;"
                    "t = targets.resolve(sys.argv[1], base=sys.argv[2]);"
                    "print(json.dumps({'spec': t.replay_spec,"
                    " 'sha': review_runs.sha256_text(t.content)}))"
                )
                cp = subprocess.run(
                    [sys.executable, "-c", code, spec, base], cwd=rv_root,
                    capture_output=True, text=True, env=_neutral_env())
                return json.loads(cp.stdout)

            # The mutate/revert pairs the code-target drift cycles run. Each must
            # restore the repo EXACTLY: the kinds share one tree, and the whole
            # matrix runs again for the second loop.
            def _rv_dirty_worktree() -> None:
                (rv_root / "f.txt").write_text("three\ndrifted\n", encoding="utf-8")

            def _rv_clean_worktree() -> None:
                (rv_root / "f.txt").write_text("three\n", encoding="utf-8")

            def _rv_extra_commit() -> None:
                (rv_root / "b.txt").write_text("a commit the panel never read\n",
                                               encoding="utf-8")
                _rv_git("add", "b.txt")
                _rv_git("commit", "-m", "drift")

            def _rv_drop_extra_commit() -> None:
                # --mixed, never --hard: a hard reset would also discard the
                # unstaged f.txt edit the working-tree kind is built on.
                _rv_git("reset", "--mixed", "HEAD~1")
                (rv_root / "b.txt").unlink()

            def _rv_refused(name, path, before, code, stderr, needle):
                """A HARD refusal is exit 2 + a diagnostic naming the fix + BYTES
                UNTOUCHED. Any one of the three missing is the bug."""
                if (code == 2 and needle in stderr and "Traceback" not in stderr
                        and path.read_bytes() == before):
                    log_pass(name)
                else:
                    log_fail(name,
                             f"exit 2, {needle!r} in stderr, state bytes untouched",
                             f"exit {code}, stderr={stderr[:200]!r}, "
                             f"changed={path.read_bytes() != before}")

            def _rv_advisory(name, path, before, code, stderr, needle):
                """An ADVISORY refusal (no --force) is exit 3 (distinct from the
                hard exit-2 errors so a caller can tell "ask the human" from "you
                called it wrong") + the fix text + BYTES UNTOUCHED."""
                if (code == 3 and needle in stderr and "Traceback" not in stderr
                        and path.read_bytes() == before):
                    log_pass(name)
                else:
                    log_fail(name,
                             f"exit 3, {needle!r} in stderr, state bytes untouched",
                             f"exit {code}, stderr={stderr[:200]!r}, "
                             f"changed={path.read_bytes() != before}")

            def _rv_matrix(loop: str) -> None:
                tag = f"[{loop}]"

                # --- freeze, idempotence, quorum, partial-panel REVISE, done ---
                sid = f"rv-{loop}-a"
                plan = rv_root / f"target-{loop}.md"
                plan.write_text(f"# plan for {loop}\n", encoding="utf-8")
                tsha = rr.sha256_text(plan.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(
                    sid, spec=plan.name, base="", tsha=tsha,
                    seats=("codex", "agy"), task_seats=("opus", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)

                _, err, code = _rv_begin(loop, sid)
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "reviewing" and st["run_id"] == run_id
                        and st["target_sha256"] == tsha
                        and st["target_spec"] == plan.name and st["target_base"] == ""
                        and st["expected_seats"] == ["codex", "agy", "opus"]):
                    log_pass(f"{tag} begin-review from drafting freezes run id, full hash, "
                             f"spec, base and a deduped manifest")
                else:
                    log_fail(f"{tag} begin-review from drafting freezes the run identity",
                             f"phase=reviewing, run_id={run_id}, seats=[codex, agy, opus]",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                frozen = sp.read_bytes()
                _, err, code = _rv_begin(loop, sid)
                if code == 0 and sp.read_bytes() == frozen:
                    log_pass(f"{tag} begin-review re-entry on the same pointer is idempotent")
                else:
                    log_fail(f"{tag} begin-review re-entry on the same pointer is idempotent",
                             "exit 0, state byte-identical",
                             f"exit {code}, stderr={err[:160]!r}, "
                             f"changed={sp.read_bytes() != frozen}")

                _rv_land(run_d, "codex", run_id, tsha)
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                _rv_advisory(f"{tag} APPROVED without quorum (1 of 3 usable) is advisory",
                             sp, before, code, err, "quorum not met")

                # The same partial panel may still demand more work: fail-closed
                # bounds only the verdicts that COMPLETE the loop.
                _, err, code = _rv_verdict(loop, sid, "REVISE")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "drafting" and st["revision_round"] == 1
                        and st["last_verdict"] == "REVISE"):
                    log_pass(f"{tag} REVISE from a partial panel records and bumps the round once")
                else:
                    log_fail(f"{tag} REVISE from a partial panel records and bumps the round once",
                             "exit 0, phase=drafting, revision_round=1, last_verdict=REVISE",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "REJECT")
                _rv_refused(f"{tag} record-verdict from drafting is refused (no panel in flight)",
                            sp, before, code, err, "cannot record a verdict from phase 'drafting'")

                _rv_begin(loop, sid)
                _rv_land(run_d, "agy", run_id, tsha)
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "done" and st["last_verdict"] == "APPROVED"
                        and st["revision_round"] == 1):
                    log_pass(f"{tag} APPROVED with quorum completes the loop without bumping the round")
                else:
                    log_fail(f"{tag} APPROVED with quorum completes the loop",
                             "exit 0, phase=done, last_verdict=APPROVED, revision_round=1",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                before = sp.read_bytes()
                _, err, code = _rv_begin(loop, sid)
                _rv_refused(f"{tag} begin-review from done is refused",
                            sp, before, code, err, "cannot begin a review from phase 'done'")

                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                _rv_refused(f"{tag} record-verdict from done is refused",
                            sp, before, code, err, "cannot record a verdict from phase 'done'")

                # --- target drift: refuse, revert, succeed ---
                sid = f"rv-{loop}-drift"
                drift_plan = rv_root / f"drift-{loop}.md"
                original = f"# drift plan for {loop}\n"
                drift_plan.write_text(original, encoding="utf-8")
                tsha = rr.sha256_text(original)
                run_id, run_d = _rv_mint(sid, spec=drift_plan.name, base="", tsha=tsha,
                                         seats=("codex",))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)

                drift_plan.write_text(original + "an edit the panel never read\n",
                                      encoding="utf-8")
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                _rv_advisory(f"{tag} APPROVED after the target changed is advisory (drift)",
                             sp, before, code, err, "target drift")

                drift_plan.write_text(original, encoding="utf-8")
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                st = json.loads(sp.read_text())
                if code == 0 and st["phase"] == "done":
                    log_pass(f"{tag} reverting the target to the reviewed bytes lets APPROVED land")
                else:
                    log_fail(f"{tag} reverting the target to the reviewed bytes lets APPROVED land",
                             "exit 0, phase=done", f"exit {code}, stderr={err[:200]!r}, state={st}")

                # --- every target kind round-trips through its stored spec, and
                # every kind whose content can drift refuses when it does ---
                for kind, probe, probe_base, drift in (
                    ("plan", f"drift-{loop}.md", "main", None),
                    ("working-tree", "working-tree", "main",
                     (_rv_dirty_worktree, _rv_clean_worktree)),
                    ("branch", "branch", "main",
                     (_rv_extra_commit, _rv_drop_extra_commit)),
                    ("commit", "HEAD", "main", None),
                    ("range", "HEAD~1..HEAD", "main", None),
                ):
                    resolved = _rv_resolve(probe, probe_base)
                    # Only the branch kind derives content from the base, so that
                    # is the only kind that stores one (mirrors what prep mints).
                    stored_base = probe_base if resolved["spec"] == "branch" else ""
                    sid = f"rv-{loop}-{kind}"
                    run_id, run_d = _rv_mint(sid, spec=resolved["spec"],
                                             base=stored_base, tsha=resolved["sha"],
                                             seats=("codex",))
                    _rv_init(loop, sid)
                    sp = _rv_state_path(loop, sid)
                    _rv_begin(loop, sid)
                    _rv_land(run_d, "codex", run_id, resolved["sha"])

                    if drift is not None:
                        # The code targets drift the way real ones do (an edit, a
                        # commit), not by a hand-rewritten hash: the point is that
                        # the RE-RESOLVE of the stored spec notices.
                        make_drift, undo_drift = drift
                        make_drift()
                        drifted = _rv_resolve(probe, probe_base)["sha"]
                        before = sp.read_bytes()
                        _, err, code = _rv_verdict(loop, sid, "APPROVED")
                        if (drifted != resolved["sha"] and code == 3
                                and "target drift" in err and drifted in err
                                and resolved["sha"] in err
                                and "Traceback" not in err
                                and sp.read_bytes() == before):
                            log_pass(f"{tag} a drifted {kind} target holds APPROVED advisory, "
                                     f"naming both hashes, bytes untouched")
                        else:
                            log_fail(f"{tag} a drifted {kind} target holds APPROVED advisory",
                                     f"a re-hash != {resolved['sha'][:12]}, exit 3 naming both "
                                     f"hashes, state bytes untouched",
                                     f"exit {code}, drifted={drifted[:12]}, "
                                     f"stderr={err[:200]!r}, "
                                     f"changed={sp.read_bytes() != before}")
                        undo_drift()

                    _, err, code = _rv_verdict(loop, sid, "APPROVED")
                    st = json.loads(sp.read_text())
                    if code == 0 and st["phase"] == "done":
                        log_pass(f"{tag} the gate replays a stored {kind} spec and certifies it unchanged")
                    else:
                        log_fail(f"{tag} the gate replays a stored {kind} spec",
                                 "exit 0, phase=done",
                                 f"exit {code}, spec={resolved['spec']!r}, "
                                 f"stderr={err[:200]!r}, state={st}")

                # A stored target that is GONE refuses cleanly: the resolver's own
                # error becomes the diagnostic, never a traceback.
                sid = f"rv-{loop}-deleted"
                gone = rv_root / f"gone-{loop}.md"
                gone.write_text("# soon to be deleted\n", encoding="utf-8")
                tsha = rr.sha256_text(gone.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=gone.name, base="", tsha=tsha,
                                         seats=("codex",))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)
                gone.unlink()
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                _rv_advisory(f"{tag} a deleted stored target is advisory, cleanly (no traceback)",
                             sp, before, code, err, "no longer resolves")

                # --- pointer divergence ---
                sid = f"rv-{loop}-pointer"
                p1 = rv_root / f"ptr-{loop}.md"
                p1.write_text("# pointer plan\n", encoding="utf-8")
                tsha = rr.sha256_text(p1.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=p1.name, base="", tsha=tsha,
                                         seats=("codex",))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)
                # A re-prep of DIFFERENT content moves the pointer to a new run.
                p2 = rv_root / f"ptr2-{loop}.md"
                p2.write_text("# a second plan\n", encoding="utf-8")
                _rv_mint(sid, spec=p2.name, base="",
                         tsha=rr.sha256_text(p2.read_text(encoding="utf-8")),
                         seats=("codex",))
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                _rv_advisory(f"{tag} APPROVED after a re-prep moved the pointer is advisory",
                             sp, before, code, err, "pointer divergence")

                # A fresh begin-review re-arms the freeze onto the NEW pointer:
                # the orchestrator is never walled in, it is redirected.
                _, err, code = _rv_begin(loop, sid)
                st = json.loads(sp.read_text())
                if code == 0 and st["phase"] == "reviewing" and st["run_id"] != run_id:
                    log_pass(f"{tag} begin-review from reviewing re-arms the freeze onto a new pointer")
                else:
                    log_fail(f"{tag} begin-review from reviewing re-arms the freeze onto a new pointer",
                             f"exit 0, phase=reviewing, run_id != {run_id}",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                # --- resolution is state-only: no flat fallback ---
                sid = f"rv-{loop}-flat"
                fp = rv_root / f"flat-{loop}.md"
                fp.write_text("# flat plan\n", encoding="utf-8")
                tsha = rr.sha256_text(fp.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=fp.name, base="", tsha=tsha,
                                         seats=("codex", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                shutil.rmtree(run_d)
                for seat in ("codex", "agy"):
                    _rv_land(_rv_reviews(sid), seat, run_id, tsha)
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                _rv_advisory(f"{tag} a missing run dir counts 0 usable: flat seat files never fold in",
                             sp, before, code, err, "0 of 2")

                # --- FAILED: only when the run produced nothing ---
                sid = f"rv-{loop}-failed"
                ff = rv_root / f"failed-{loop}.md"
                ff.write_text("# failed-panel plan\n", encoding="utf-8")
                tsha = rr.sha256_text(ff.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=ff.name, base="", tsha=tsha,
                                         seats=("codex", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _, err, code = _rv_verdict(loop, sid, "FAILED")
                st = json.loads(sp.read_text())
                if (code == 0 and st["active"] is True and st["phase"] == "drafting"
                        and st["consecutive_review_failures"] == 1
                        and st["last_verdict"] == "FAILED" and not st.get("exit_kind")):
                    log_pass(f"{tag} one all-failed panel records a failure and keeps the loop alive")
                else:
                    log_fail(f"{tag} one all-failed panel records a failure and keeps the loop alive",
                             "exit 0, active, phase=drafting, consecutive_review_failures=1",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "FAILED")
                _rv_refused(f"{tag} FAILED with a usable review present is refused",
                            sp, before, code, err, "record REVISE/APPROVED/REJECT instead")

                _, err, code = _rv_verdict(loop, sid, "REJECT")
                st = json.loads(sp.read_text())
                if (code == 0 and st["consecutive_review_failures"] == 0
                        and st["revision_round"] == 1 and st["phase"] == "drafting"
                        and st["last_verdict"] == "REJECT"):
                    log_pass(f"{tag} a non-FAILED verdict resets the failure counter and bumps the round")
                else:
                    log_fail(f"{tag} a non-FAILED verdict resets the failure counter and bumps the round",
                             "exit 0, consecutive_review_failures=0, revision_round=1",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                # --- the frozen roster comes from the SIGNED seat map, never the
                # roster lists outside the hashed identity: an edited list must
                # not shrink the panel (and with it the quorum threshold) on a
                # record that still verifies ---
                for i, (case, sub, task) in enumerate((
                    ("a seat removed", ["codex"], []),
                    ("a bogus seat added", ["codex", "agy", "ghost"], ["opus", "ghost2"]),
                    ("a list replaced by a string", "codex", ["opus"]),
                )):
                    sid = f"rv-{loop}-tamper{i}"
                    tp = rv_root / f"tamper{i}-{loop}.md"
                    tp.write_text(f"# tamper plan {i}\n", encoding="utf-8")
                    tsha = rr.sha256_text(tp.read_text(encoding="utf-8"))
                    run_id, run_d = _rv_mint(sid, spec=tp.name, base="", tsha=tsha,
                                             seats=("codex", "agy"), task_seats=("opus",))
                    rj = run_d / rr.RUN_JSON_NAME
                    rec = json.loads(rj.read_text(encoding="utf-8"))
                    rec["subprocess_seats"] = sub
                    rec["task_seats"] = task
                    rj.write_text(json.dumps(rec), encoding="utf-8")
                    _rv_init(loop, sid)
                    sp = _rv_state_path(loop, sid)
                    _, err, code = _rv_begin(loop, sid)
                    st = json.loads(sp.read_text())
                    if code == 0 and st["expected_seats"] == ["codex", "agy", "opus"]:
                        log_pass(f"{tag} begin-review freezes the signed roster with "
                                 f"{case} in the lists")
                    else:
                        log_fail(f"{tag} begin-review freezes the signed roster with "
                                 f"{case} in the lists",
                                 "exit 0, expected_seats=[codex, agy, opus]",
                                 f"exit {code}, stderr={err[:160]!r}, state={st}")

                    # The threshold the gate enforces is the SIGNED panel's: read
                    # off the shrunk list this would be 1 of 1 and sign off.
                    _rv_land(run_d, "codex", run_id, tsha)
                    before = sp.read_bytes()
                    _, err, code = _rv_verdict(loop, sid, "APPROVED")
                    _rv_advisory(f"{tag} {case} in the roster lists does not lower the "
                                 f"quorum threshold",
                                 sp, before, code, err, "1 of 3 launched seats")

                # --- a non-FAILED verdict needs a usable seat: an all-failed panel
                # recorded as REVISE would look like progress, reset the failure
                # counter, and leave the loop free to re-prep forever ---
                sid = f"rv-{loop}-empty"
                ep = rv_root / f"empty-{loop}.md"
                ep.write_text("# empty-panel plan\n", encoding="utf-8")
                tsha = rr.sha256_text(ep.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=ep.name, base="", tsha=tsha,
                                         seats=("codex", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_verdict(loop, sid, "FAILED")
                _rv_begin(loop, sid)
                for verdict, extra in (("REVISE", ()), ("REVISE", ("--minor-only",)),
                                       ("REJECT", ())):
                    label = verdict + (f" {extra[0]}" if extra else "")
                    before = sp.read_bytes()
                    _, err, code = _rv_verdict(loop, sid, verdict, *extra)
                    _rv_advisory(f"{tag} {label} with no usable seat is advisory",
                                 sp, before, code, err, "Record FAILED instead")
                st = json.loads(sp.read_text())
                if st["consecutive_review_failures"] == 1 and st["phase"] == "reviewing":
                    log_pass(f"{tag} a refused zero-usable verdict leaves the failure "
                             f"counter unreset and the panel in flight")
                else:
                    log_fail(f"{tag} a refused zero-usable verdict leaves the failure counter unreset",
                             "consecutive_review_failures=1, phase=reviewing",
                             f"state={st}")

                _rv_land(run_d, "codex", run_id, tsha)
                _, err, code = _rv_verdict(loop, sid, "REVISE")
                st = json.loads(sp.read_text())
                if (code == 0 and st["consecutive_review_failures"] == 0
                        and st["revision_round"] == 1 and st["phase"] == "drafting"
                        and st["last_verdict"] == "REVISE"):
                    log_pass(f"{tag} REVISE with one usable seat still records and resets "
                             f"the failure counter")
                else:
                    log_fail(f"{tag} REVISE with one usable seat still records and resets "
                             f"the failure counter",
                             "exit 0, consecutive_review_failures=0, revision_round=1, "
                             "phase=drafting",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                # --- two consecutive all-failed panels end the loop, in one write ---
                sid = f"rv-{loop}-terminal"
                tf = rv_root / f"terminal-{loop}.md"
                tf.write_text("# terminal plan\n", encoding="utf-8")
                tsha = rr.sha256_text(tf.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=tf.name, base="", tsha=tsha,
                                         seats=("codex",))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_verdict(loop, sid, "FAILED")
                _rv_begin(loop, sid)
                _, err, code = _rv_verdict(loop, sid, "FAILED")
                st = json.loads(sp.read_text())
                # record-verdict is the only writer here, so the deactivation
                # sitting in the same file as the verdict it records IS the
                # single-transaction guarantee: there is no second call to have
                # landed it.
                if (code == 0 and st["active"] is False
                        and st["exit_kind"] == "review_failed"
                        and st["consecutive_review_failures"] == 2
                        and st["last_verdict"] == "FAILED" and st.get("completed_at")
                        and run_id in st.get("reason", "")):
                    log_pass(f"{tag} a second all-failed panel deactivates terminally in the "
                             f"verdict's own transaction")
                else:
                    log_fail(f"{tag} a second all-failed panel deactivates terminally",
                             "exit 0, active=false, exit_kind=review_failed, "
                             "consecutive_review_failures=2, reason naming the run",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "REVISE")
                _rv_refused(f"{tag} record-verdict on an inactive loop is refused",
                            sp, before, code, err, "is not active")

                before = sp.read_bytes()
                _, err, code = _rv_begin(loop, sid)
                _rv_refused(f"{tag} begin-review on an inactive loop is refused",
                            sp, before, code, err, "is not active")

                # --- REVISE --minor-only completes, so it passes the same gate ---
                sid = f"rv-{loop}-minor"
                mf = rv_root / f"minor-{loop}.md"
                mf.write_text("# minor plan\n", encoding="utf-8")
                tsha = rr.sha256_text(mf.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=mf.name, base="", tsha=tsha,
                                         seats=("codex", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "REVISE", "--minor-only")
                _rv_advisory(f"{tag} REVISE --minor-only without quorum is advisory (it completes)",
                             sp, before, code, err, "quorum not met")

                _rv_land(run_d, "agy", run_id, tsha)
                _, err, code = _rv_verdict(loop, sid, "REVISE", "--minor-only")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "done" and st["revision_round"] == 0
                        and st["last_verdict"] == "REVISE"):
                    log_pass(f"{tag} REVISE --minor-only with quorum completes without a round bump")
                else:
                    log_fail(f"{tag} REVISE --minor-only with quorum completes without a round bump",
                             "exit 0, phase=done, revision_round=0, last_verdict=REVISE",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                _, err, code = _rv_verdict(loop, sid, "APPROVED", "--minor-only")
                if code == 2 and "--minor-only" in err:
                    log_pass(f"{tag} --minor-only on a verdict it cannot qualify exits 2")
                else:
                    log_fail(f"{tag} --minor-only on a verdict it cannot qualify exits 2",
                             "exit 2 naming --minor-only", f"exit {code}, stderr={err[:160]!r}")

                # --- advisory --force: warn-and-record over a single condition ---
                # A partial panel (quorum not met) is the human's call: --force
                # records it, stamps the override name, and warns to stderr.
                sid = f"rv-{loop}-force1"
                f1 = rv_root / f"force1-{loop}.md"
                f1.write_text("# force one plan\n", encoding="utf-8")
                tsha = rr.sha256_text(f1.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=f1.name, base="", tsha=tsha,
                                         seats=("codex", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)  # 1 of 2 usable: no quorum
                _, err, code = _rv_verdict(loop, sid, "APPROVED", "--force")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "done"
                        and st["last_verdict"] == "APPROVED"
                        and st.get("last_verdict_overrides") == ["quorum-not-met"]
                        and "Warning" in err and "--force" in err):
                    log_pass(f"{tag} --force records over quorum, stamps the override, warns")
                else:
                    log_fail(f"{tag} --force records over quorum, stamps the override, warns",
                             "exit 0, phase=done, last_verdict_overrides=[quorum-not-met], "
                             "stderr warns naming --force",
                             f"exit {code}, stderr={err[:200]!r}, state={st}")

                # --- multiple simultaneous conditions: all named, one message ---
                # Quorum not met (1 of 2) AND target drift (the plan edited after
                # the panel read it) trip together. Without --force ONE combined
                # exit-3 diagnostic names BOTH; with --force both names are stamped.
                sid = f"rv-{loop}-multi"
                f2 = rv_root / f"multi-{loop}.md"
                original = f"# multi plan for {loop}\n"
                f2.write_text(original, encoding="utf-8")
                tsha = rr.sha256_text(original)
                run_id, run_d = _rv_mint(sid, spec=f2.name, base="", tsha=tsha,
                                         seats=("codex", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)  # 1 of 2 usable
                f2.write_text(original + "an edit the panel never read\n",
                              encoding="utf-8")  # drift
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                if (code == 3 and "quorum not met" in err and "target drift" in err
                        and "Traceback" not in err and sp.read_bytes() == before):
                    log_pass(f"{tag} two advisory conditions trip one exit-3 message "
                             f"naming both, bytes untouched")
                else:
                    log_fail(f"{tag} two advisory conditions trip one exit-3 message naming both",
                             "exit 3, stderr names quorum AND drift, bytes untouched",
                             f"exit {code}, stderr={err[:300]!r}, "
                             f"changed={sp.read_bytes() != before}")

                _, err, code = _rv_verdict(loop, sid, "APPROVED", "--force")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "done"
                        and st.get("last_verdict_overrides") == ["quorum-not-met",
                                                                 "target-drift"]):
                    log_pass(f"{tag} --force over two conditions stamps both override names")
                else:
                    log_fail(f"{tag} --force over two conditions stamps both override names",
                             "exit 0, phase=done, "
                             "last_verdict_overrides=[quorum-not-met, target-drift]",
                             f"exit {code}, stderr={err[:200]!r}, state={st}")
                f2.write_text(original, encoding="utf-8")  # restore for reuse hygiene

                # --- a clean completing verdict carries NO override stamp ---
                sid = f"rv-{loop}-clean"
                f3 = rv_root / f"clean-{loop}.md"
                f3.write_text("# clean plan\n", encoding="utf-8")
                tsha = rr.sha256_text(f3.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=f3.name, base="", tsha=tsha,
                                         seats=("codex", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)
                _rv_land(run_d, "agy", run_id, tsha)  # 2 of 2 usable: quorum met
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "done"
                        and st["last_verdict"] == "APPROVED"
                        and "last_verdict_overrides" not in st):
                    log_pass(f"{tag} a clean quorum-met APPROVED records with no override stamp")
                else:
                    log_fail(f"{tag} a clean quorum-met APPROVED records with no override stamp",
                             "exit 0, phase=done, no last_verdict_overrides key",
                             f"exit {code}, stderr={err[:200]!r}, state={st}")

                # --- per-condition --force: each advisory name records AND stamps
                # in isolation (not just trips exit 3). Each case is engineered so
                # exactly ONE advisory fires, so the stamped list proves the name.

                # zero-usable, isolated on a NON-completing REVISE (it faces the
                # zero-usable check but not the completing quorum/pointer/drift
                # checks): --force records, bumps the round, stamps the name.
                sid = f"rv-{loop}-force-zero"
                fz = rv_root / f"forcezero-{loop}.md"
                fz.write_text("# force-zero plan\n", encoding="utf-8")
                tsha = rr.sha256_text(fz.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=fz.name, base="", tsha=tsha,
                                         seats=("codex", "agy"))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)  # land NOTHING: 0 usable
                _, err, code = _rv_verdict(loop, sid, "REVISE", "--force")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "drafting" and st["revision_round"] == 1
                        and st["last_verdict"] == "REVISE"
                        and st.get("last_verdict_overrides") == ["zero-usable"]
                        and "Warning" in err and "--force" in err):
                    log_pass(f"{tag} --force over zero-usable records REVISE and stamps "
                             f"[zero-usable]")
                else:
                    log_fail(f"{tag} --force over zero-usable records REVISE and stamps "
                             f"[zero-usable]",
                             "exit 0, phase=drafting, round=1, "
                             "last_verdict_overrides=[zero-usable], stderr warns",
                             f"exit {code}, stderr={err[:200]!r}, state={st}")

                # pointer-divergence, isolated: a 1-seat panel (quorum met) whose
                # target is unchanged, but a re-prep of DIFFERENT content moved the
                # pointer. --force completes and stamps just [pointer-divergence].
                sid = f"rv-{loop}-force-ptr"
                fp1 = rv_root / f"forceptr-{loop}.md"
                fp1.write_text("# force-pointer plan\n", encoding="utf-8")
                tsha = rr.sha256_text(fp1.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=fp1.name, base="", tsha=tsha,
                                         seats=("codex",))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)  # 1 of 1 usable: quorum met
                fp2 = rv_root / f"forceptr2-{loop}.md"
                fp2.write_text("# a different plan\n", encoding="utf-8")
                _rv_mint(sid, spec=fp2.name, base="",
                         tsha=rr.sha256_text(fp2.read_text(encoding="utf-8")),
                         seats=("codex",))  # moves the pointer
                _, err, code = _rv_verdict(loop, sid, "APPROVED", "--force")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "done"
                        and st["last_verdict"] == "APPROVED"
                        and st.get("last_verdict_overrides") == ["pointer-divergence"]
                        and "Warning" in err and "--force" in err):
                    log_pass(f"{tag} --force over pointer-divergence completes and stamps "
                             f"[pointer-divergence]")
                else:
                    log_fail(f"{tag} --force over pointer-divergence completes and stamps "
                             f"[pointer-divergence]",
                             "exit 0, phase=done, "
                             "last_verdict_overrides=[pointer-divergence], stderr warns",
                             f"exit {code}, stderr={err[:200]!r}, state={st}")

                # target-no-longer-resolves, isolated: a 1-seat panel (quorum met),
                # pointer unchanged, but the stored target was deleted. --force
                # completes and stamps just [target-no-longer-resolves].
                sid = f"rv-{loop}-force-gone"
                fg = rv_root / f"forcegone-{loop}.md"
                fg.write_text("# force-gone plan\n", encoding="utf-8")
                tsha = rr.sha256_text(fg.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=fg.name, base="", tsha=tsha,
                                         seats=("codex",))
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                _rv_begin(loop, sid)
                _rv_land(run_d, "codex", run_id, tsha)  # 1 of 1 usable: quorum met
                fg.unlink()  # the stored target no longer resolves
                _, err, code = _rv_verdict(loop, sid, "APPROVED", "--force")
                st = json.loads(sp.read_text())
                if (code == 0 and st["phase"] == "done"
                        and st["last_verdict"] == "APPROVED"
                        and st.get("last_verdict_overrides") == ["target-no-longer-resolves"]
                        and "Warning" in err and "--force" in err):
                    log_pass(f"{tag} --force over a deleted target completes and stamps "
                             f"[target-no-longer-resolves]")
                else:
                    log_fail(f"{tag} --force over a deleted target completes and stamps "
                             f"[target-no-longer-resolves]",
                             "exit 0, phase=done, "
                             "last_verdict_overrides=[target-no-longer-resolves], stderr warns",
                             f"exit {code}, stderr={err[:200]!r}, state={st}")

                # --- fail-closed backstop: reviewing with no frozen identity ---
                sid = f"rv-{loop}-norun"
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                hand = json.loads(sp.read_text())
                hand["phase"] = "reviewing"  # a markdown that skipped the verb
                sp.write_text(json.dumps(hand), encoding="utf-8")
                before = sp.read_bytes()
                _, err, code = _rv_verdict(loop, sid, "APPROVED")
                _rv_refused(f"{tag} record-verdict with no frozen run identity is refused",
                            sp, before, code, err, "froze no run identity")

                # --- a seatless run is refused at the freeze ---
                # Freezing it would defer the complaint to a quorum line reading
                # "0 of 0 launched seats, 1 needed", which names no fix.
                sid = f"rv-{loop}-noseats"
                nf = rv_root / f"noseats-{loop}.md"
                nf.write_text("# seatless plan\n", encoding="utf-8")
                tsha = rr.sha256_text(nf.read_text(encoding="utf-8"))
                run_id, run_d = _rv_mint(sid, spec=nf.name, base="", tsha=tsha,
                                         seats=())
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                before = sp.read_bytes()
                _, err, code = _rv_begin(loop, sid)
                _rv_refused(f"{tag} begin-review on a run naming no seats is refused",
                            sp, before, code, err, "names no seats")

                # --- no pointer at all ---
                sid = f"rv-{loop}-nopointer"
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                before = sp.read_bytes()
                _, err, code = _rv_begin(loop, sid)
                _rv_refused(f"{tag} begin-review with no run pointer is a loud refusal",
                            sp, before, code, err, "review-prep")

                # --- deactivate is gated on a recorded verdict or --cancel ---
                def _rv_deact(sid_: str, *extra):
                    return run_crew_state(
                        ["deactivate", loop, *extra, "--session-id", sid_], rv_root)

                def _rv_prepped(name: str, *, verdict: str = ""):
                    """A loop with a live panel frozen over a one-seat run, carried
                    to `phase=done` through a real APPROVED when asked (the only
                    way a loop reaches done)."""
                    sid_ = f"rv-{loop}-{name}"
                    tgt = rv_root / f"{name}-{loop}.md"
                    tgt.write_text(f"# {name} plan\n", encoding="utf-8")
                    tsha_ = rr.sha256_text(tgt.read_text(encoding="utf-8"))
                    rid, rd = _rv_mint(sid_, spec=tgt.name, base="", tsha=tsha_,
                                       seats=("codex",))
                    _rv_init(loop, sid_)
                    _rv_begin(loop, sid_)
                    if verdict:
                        _rv_land(rd, "codex", rid, tsha_)
                        _rv_verdict(loop, sid_, verdict)
                    return sid_, _rv_state_path(loop, sid_)

                sid = f"rv-{loop}-gate"
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                before = sp.read_bytes()
                _, err, code = _rv_deact(sid, "--reason", "done")
                _rv_refused(f"{tag} deactivate with no verdict on record is refused",
                            sp, before, code, err, "record-verdict")
                st = json.loads(sp.read_text())
                if st["active"] is True and "--cancel" in err:
                    log_pass(f"{tag} the refused deactivate leaves the loop ACTIVE and "
                             f"names both sanctioned exits")
                else:
                    log_fail(f"{tag} the refused deactivate leaves the loop ACTIVE and "
                             f"names both sanctioned exits",
                             "active stays true, stderr names --cancel",
                             f"active={st['active']}, stderr={err[:200]!r}")

                _, err, code = _rv_deact(sid, "--cancel", "--reason", "stopped by hand")
                st = json.loads(sp.read_text())
                if (code == 0 and st["active"] is False and st["exit_kind"] == "cancelled"
                        and st["reason"] == "stopped by hand" and st.get("completed_at")):
                    log_pass(f"{tag} deactivate --cancel from drafting ends the loop, stamped "
                             f"cancelled")
                else:
                    log_fail(f"{tag} deactivate --cancel from drafting ends the loop, stamped "
                             f"cancelled",
                             "exit 0, active=false, exit_kind=cancelled, reason recorded",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                # An off loop is already at a terminal path someone recorded, so
                # saying "off" again is a success that rewrites nothing.
                off_bytes = sp.read_bytes()
                _, err, code = _rv_deact(sid)
                if code == 0 and sp.read_bytes() == off_bytes:
                    log_pass(f"{tag} deactivate on an already-inactive loop is a no-op success")
                else:
                    log_fail(f"{tag} deactivate on an already-inactive loop is a no-op success",
                             "exit 0, state byte-identical",
                             f"exit {code}, stderr={err[:160]!r}, "
                             f"changed={sp.read_bytes() != off_bytes}")

                # The escape hatch cannot be phase-gated, or it is not an escape
                # hatch: a panel in flight is exactly when a human aborts.
                sid, sp = _rv_prepped("cancel-reviewing")
                _, err, code = _rv_deact(sid, "--cancel")
                st = json.loads(sp.read_text())
                if code == 0 and st["active"] is False and st["exit_kind"] == "cancelled":
                    log_pass(f"{tag} deactivate --cancel from reviewing ends the loop mid-panel")
                else:
                    log_fail(f"{tag} deactivate --cancel from reviewing ends the loop mid-panel",
                             "exit 0, active=false, exit_kind=cancelled",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                sid, sp = _rv_prepped("finish", verdict="APPROVED")
                _, err, code = _rv_deact(sid, "--reason", "panel approved")
                st = json.loads(sp.read_text())
                if (code == 0 and st["active"] is False and st["exit_kind"] == "approved"
                        and st["phase"] == "done" and st.get("completed_at")):
                    log_pass(f"{tag} deactivate after an APPROVED verdict ends the loop, stamped "
                             f"approved")
                else:
                    log_fail(f"{tag} deactivate after an APPROVED verdict ends the loop, stamped "
                             f"approved",
                             "exit 0, active=false, exit_kind=approved, phase=done",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                # Cancelling a loop that COULD have finished cleanly is still a
                # cancel: the flag says how it ended, the phase does not.
                sid, sp = _rv_prepped("cancel-done", verdict="APPROVED")
                _, err, code = _rv_deact(sid, "--cancel")
                st = json.loads(sp.read_text())
                if code == 0 and st["exit_kind"] == "cancelled" and st["phase"] == "done":
                    log_pass(f"{tag} deactivate --cancel from done stamps cancelled, not approved")
                else:
                    log_fail(f"{tag} deactivate --cancel from done stamps cancelled, not approved",
                             "exit 0, exit_kind=cancelled, phase=done",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

                # The terminal writers do not overwrite each other: the loop below
                # ended on its second all-failed panel, and a later cancel records
                # its reason next to that exit rather than restamping it.
                sid, sp = _rv_prepped("failed-then-cancel")
                _rv_verdict(loop, sid, "FAILED")
                _rv_begin(loop, sid)
                _rv_verdict(loop, sid, "FAILED")
                _, err, code = _rv_deact(sid, "--cancel", "--reason", "cleaning up")
                st = json.loads(sp.read_text())
                if (code == 0 and st["exit_kind"] == "review_failed"
                        and st["deactivate_reason"] == "cleaning up"
                        and "no usable seat results" in st["reason"]):
                    log_pass(f"{tag} a cancel after a terminal FAILED keeps review_failed as the "
                             f"exit_kind")
                else:
                    log_fail(f"{tag} a cancel after a terminal FAILED keeps review_failed as the "
                             f"exit_kind",
                             "exit 0, exit_kind=review_failed, cancel text in deactivate_reason",
                             f"exit {code}, stderr={err[:160]!r}, state={st}")

            for _rv_loop in ("bl", "mt"):
                _rv_matrix(_rv_loop)

            # --- the verbs read `active` by the SHARED truthiness rule ---
            # A truthy-non-True `active` (1, "true") is ON to the Stop hook, so it
            # goes on blocking. A verb that read it as OFF would strand that loop:
            # blocked from stopping, refused from reviewing.
            sid = "rv-truthy"
            tplan = rv_root / "truthy-plan.md"
            tplan.write_text("# truthy plan\n", encoding="utf-8")
            tsha = rr.sha256_text(tplan.read_text(encoding="utf-8"))
            run_id, run_d = _rv_mint(sid, spec=tplan.name, base="", tsha=tsha,
                                     seats=("codex",))
            _rv_init("bl", sid)
            sp = _rv_state_path("bl", sid)
            st = json.loads(sp.read_text())
            st["active"] = "true"
            sp.write_text(json.dumps(st), encoding="utf-8")
            _, err, code = _rv_begin("bl", sid)
            st = json.loads(sp.read_text())
            if code == 0 and st.get("phase") == "reviewing" and st.get("run_id") == run_id:
                log_pass("begin-review with a truthy-non-True `active`: progresses "
                         "(same rule the Stop hook blocks on)")
            else:
                log_fail("begin-review with a truthy-non-True `active`: progresses",
                         f"exit 0, phase=reviewing, run_id={run_id}",
                         f"exit {code}, stderr={err[:160]!r}, phase={st.get('phase')!r}, "
                         f"run_id={st.get('run_id')!r}")

            # …and the verdict verb agrees, so the loop can actually finish.
            _rv_land(run_d, "codex", run_id, tsha)
            st = json.loads(sp.read_text())
            st["active"] = "true"
            sp.write_text(json.dumps(st), encoding="utf-8")
            _, err, code = _rv_verdict("bl", sid, "APPROVED")
            st = json.loads(sp.read_text())
            if code == 0 and st.get("phase") == "done" and st.get("last_verdict") == "APPROVED":
                log_pass("record-verdict with a truthy-non-True `active`: records "
                         "(same rule the Stop hook blocks on)")
            else:
                log_fail("record-verdict with a truthy-non-True `active`: records",
                         "exit 0, phase=done, last_verdict=APPROVED",
                         f"exit {code}, stderr={err[:160]!r}, phase={st.get('phase')!r}, "
                         f"last_verdict={st.get('last_verdict')!r}")

            # --- loop state and the reviews tree share ONE project root ---
            # Run from a cwd that is NOT the project: if the reviews tree resolved
            # from cwd while the state file resolved from CLAUDE_PROJECT_DIR, the
            # two would split across trees and the gate would scan a dir nobody
            # writes. The freeze landing proves one root decides both.
            rv_elsewhere = test_path / "review-verbs-elsewhere"
            rv_elsewhere.mkdir()
            sid = "rv-root"
            rp = rv_root / "root-plan.md"
            rp.write_text("# root plan\n", encoding="utf-8")
            tsha = rr.sha256_text(rp.read_text(encoding="utf-8"))
            run_id, run_d = _rv_mint(sid, spec=rp.name, base="", tsha=tsha,
                                     seats=("codex",))
            _rv_init("bl", sid)
            sp = _rv_state_path("bl", sid)
            out = subprocess.run(
                [sys.executable, str(crew_state), "begin-review", "bl",
                 "--session-id", sid],
                capture_output=True, text=True, cwd=rv_elsewhere,
                env={**_neutral_env(), "CLAUDE_PROJECT_DIR": str(rv_root)},
            )
            st = json.loads(sp.read_text())
            if (out.returncode == 0 and st["run_id"] == run_id
                    and not (rv_elsewhere / ".crew").exists()):
                log_pass("begin-review resolves the reviews tree from CLAUDE_PROJECT_DIR, "
                         "the same root as the state file (never the process cwd)")
            else:
                log_fail("begin-review resolves the reviews tree from CLAUDE_PROJECT_DIR",
                         f"exit 0, run_id={run_id}, no .crew/ under the cwd",
                         f"exit {out.returncode}, stderr={out.stderr[:200]!r}, "
                         f"state={st}, cwd_crew={(rv_elsewhere / '.crew').exists()}")

            # --- begin-review never clobbers a stored owner with "" ---
            # The `data["session_id"] = session_id` stamp is guarded by
            # `if session_id:`. On the LEGACY path (no --session-id, no env) the
            # resolved id is "", so stamping would overwrite the owner an adopted
            # legacy file already carries. Guard proven both ways: an empty resolved
            # id PRESERVES the stored owner; a real id STAMPS (and overwrites).
            def _begin_no_session() -> subprocess.CompletedProcess:
                env = {**_neutral_env(), "CLAUDE_PROJECT_DIR": str(rv_root)}
                env.pop("CLAUDE_SESSION_ID", None)   # force the empty legacy resolve
                return subprocess.run(
                    [sys.executable, str(crew_state), "begin-review", "bl"],
                    capture_output=True, text=True, cwd=rv_root, env=env)

            # Case A: empty resolved id preserves the stored owner.
            legacy_state = rv_root / ".crew" / "build-state.json"
            leg_plan = rv_root / "legacy-plan.md"
            leg_plan.write_text("# legacy plan\n", encoding="utf-8")
            leg_tsha = rr.sha256_text(leg_plan.read_text(encoding="utf-8"))
            # Flat mint (empty session) so begin-review's empty-id reviews dir
            # (`.crew/reviews/`) is the one holding the pointer + run.json.
            leg_run_id, _ = _rv_mint("", spec=leg_plan.name, base="", tsha=leg_tsha,
                                     seats=("codex",))
            legacy_state.write_text(json.dumps({
                "active": True, "task": "legacy adopt begin", "session_id": "stored-owner",
                "phase": "drafting"}), encoding="utf-8")
            cp = _begin_no_session()
            st = json.loads(legacy_state.read_text())
            if (cp.returncode == 0 and st.get("phase") == "reviewing"
                    and st.get("run_id") == leg_run_id
                    and st.get("session_id") == "stored-owner"):
                log_pass("begin-review with an empty resolved id preserves the stored session owner")
            else:
                log_fail("begin-review with an empty resolved id preserves the stored session owner",
                         "exit 0, phase=reviewing, session_id='stored-owner' (not clobbered to '')",
                         f"exit {cp.returncode}, stderr={cp.stderr[:200]!r}, "
                         f"session_id={st.get('session_id')!r}, phase={st.get('phase')!r}")

            legacy_state.unlink()   # leave the flat slot clean for later tests

            # Case B (non-vacuity): a REAL id stamps (and overwrites the stored
            # one), so the guard is conditional on `session_id` being empty, not an
            # unconditional skip. A session-scoped file whose stored owner is stale ("old-owner")
            # gets re-tied to the resolving session.
            scoped_state = rv_root / ".crew" / "build-state-real-owner.json"
            leg_run_id2, _ = _rv_mint("real-owner", spec=leg_plan.name, base="",
                                      tsha=leg_tsha, seats=("codex",))
            scoped_state.write_text(json.dumps({
                "active": True, "task": "scoped begin", "session_id": "old-owner",
                "phase": "drafting"}), encoding="utf-8")
            out = subprocess.run(
                [sys.executable, str(crew_state), "begin-review", "bl",
                 "--session-id", "real-owner"],
                capture_output=True, text=True, cwd=rv_root,
                env={**_neutral_env(), "CLAUDE_PROJECT_DIR": str(rv_root)})
            st = json.loads(scoped_state.read_text())
            if (out.returncode == 0 and st.get("phase") == "reviewing"
                    and st.get("run_id") == leg_run_id2
                    and st.get("session_id") == "real-owner"):
                log_pass("begin-review with a real session id stamps it over the stored owner")
            else:
                log_fail("begin-review with a real session id stamps it over the stored owner",
                         "exit 0, phase=reviewing, session_id='real-owner'",
                         f"exit {out.returncode}, stderr={out.stderr[:200]!r}, "
                         f"session_id={st.get('session_id')!r}, phase={st.get('phase')!r}")

            # =========================================================================
            # RECOVERY FROM `done` (interrupted between verdict and deactivate)
            # =========================================================================
            # record-verdict sets phase=done, deactivate then stamps exit_kind, so
            # a loop interrupted in between is a DESIGNED intermediate state. Both
            # review verbs refuse from `done`, so guidance that still prescribed
            # them would route a blocked loop onto two illegal transitions and it
            # could not progress until a bound tripped.
            # =========================================================================
            log_section("recovery guidance from phase=done (bl + mt)")

            for loop, cancel_cmd in (("bl", "/crew:cancel-build"),
                                     ("mt", "/crew:cancel-measure-twice")):
                sid = f"done-recovery-{loop}"
                _rv_init(loop, sid)
                sp = _rv_state_path(loop, sid)
                st = json.loads(sp.read_text())
                st["phase"] = "done"
                st["last_verdict"] = "APPROVED"
                sp.write_text(json.dumps(st), encoding="utf-8")

                payload = json.dumps({"directory": str(rv_root), "session_id": sid})
                # Terse AND verbose: at `done` there are no seats in flight, so the
                # terse wait guard is wrong too and the deactivate routing must
                # override verbosity rather than hide behind it.
                for label, out in (("terse", run_script(persistent_mode, payload)),
                                   ("verbose", run_script_verbose(persistent_mode, payload))):
                    routes_to_deactivate = f"state deactivate {loop}" in out
                    prescribes_dead_verbs = (f"state begin-review {loop}" in out
                                             or f"state record-verdict {loop}" in out)
                    if routes_to_deactivate and not prescribes_dead_verbs:
                        log_pass(f"{label} nudge ({loop}) at phase=done: routes to deactivate, "
                                 f"prescribes neither review verb")
                    else:
                        log_fail(f"{label} nudge ({loop}) at phase=done: routes to deactivate, "
                                 f"prescribes neither review verb",
                                 f"contains 'state deactivate {loop}', no prescribed "
                                 f"begin-review/record-verdict",
                                 out[:400])

                # The nudge still blocks (the loop IS active) and still names the
                # way out, so `done` is a route, never a trap.
                out = run_script(persistent_mode, payload)
                if '"decision": "block"' in out and cancel_cmd in out:
                    log_pass(f"nudge ({loop}) at phase=done still blocks + names {cancel_cmd}")
                else:
                    log_fail(f"nudge ({loop}) at phase=done still blocks + names {cancel_cmd}",
                             f"block decision + {cancel_cmd}", out[:300])

                # And the routed command is the one that actually works from here.
                _, err, code = run_crew_state(
                    ["deactivate", loop, "--reason", "Panel approved",
                     "--session-id", sid], rv_root)
                after = json.loads(sp.read_text())
                if code == 0 and after.get("active") is False and \
                        after.get("exit_kind") == "approved":
                    log_pass(f"the phase=done nudge's deactivate ({loop}) succeeds ungated")
                else:
                    log_fail(f"the phase=done nudge's deactivate ({loop}) succeeds ungated",
                             "exit 0 + active False + exit_kind=approved",
                             f"exit {code}, active={after.get('active')!r}, "
                             f"exit_kind={after.get('exit_kind')!r}, stderr={err[:200]!r}")

            # =========================================================================
            # FORCED-COMPLETION HONESTY (last_verdict_overrides on both surfaces)
            # =========================================================================
            # A completing verdict recorded on a human's --force stamps the tripped
            # advisory names in last_verdict_overrides. A completion carrying that
            # stamp must NOT read as a clean "signed off" / "Panel approved": both
            # the SessionStart banner and the Stop nudge say it was recorded OVER
            # those advisories (named) on an explicit override. An empty stamp reads
            # exactly as before. Parameterized over both loops.
            # =========================================================================
            log_section("forced-completion honesty (banner + nudge)")

            for loop, state_name, task_fields in (
                ("bl", "build-state.json", '"prompt": "Ship it"'),
                ("mt", "measure-twice-state.json",
                 '"task_description": "Design it", "plan_file": ".crew/plans/x.md"'),
            ):
                for f in crew_dir.glob("*.json"):
                    if f.is_file():
                        f.unlink()
                payload = json.dumps({"directory": str(test_path)})

                # OVERRIDDEN: a non-empty stamp reads honestly on BOTH surfaces.
                (crew_dir / state_name).write_text(
                    '{"active": true, ' + task_fields + ', "phase": "done", '
                    '"last_verdict": "APPROVED", '
                    '"last_verdict_overrides": ["quorum-not-met", "target-drift"], '
                    '"schema": 3}')

                ss_out = run_script(session_start, payload)
                if ("recorded over" in ss_out and "quorum-not-met" in ss_out
                        and "target-drift" in ss_out and "signed off" not in ss_out):
                    log_pass(f"SessionStart banner ({loop}): a forced completion reads "
                             f"'recorded over' the named advisories, not 'signed off'")
                else:
                    log_fail(f"SessionStart banner ({loop}): a forced completion reads "
                             f"'recorded over' the named advisories, not 'signed off'",
                             "'recorded over' + both names, no 'signed off'", ss_out[:500])

                pm_reason = json.loads(run_script(persistent_mode, payload)).get("reason", "")
                if ("recorded over" in pm_reason and "quorum-not-met" in pm_reason
                        and "target-drift" in pm_reason and "Panel approved" not in pm_reason):
                    log_pass(f"nudge ({loop}): a forced completion reads 'recorded over' the "
                             f"named advisories, not 'Panel approved'")
                else:
                    log_fail(f"nudge ({loop}): a forced completion reads 'recorded over' the "
                             f"named advisories, not 'Panel approved'",
                             "'recorded over' + both names, no 'Panel approved'", pm_reason[:500])

                # CLEAN: an empty stamp reads exactly as today.
                (crew_dir / state_name).write_text(
                    '{"active": true, ' + task_fields + ', "phase": "done", '
                    '"last_verdict": "APPROVED", "schema": 3}')

                ss_out = run_script(session_start, payload)
                if "signed off" in ss_out and "recorded over" not in ss_out:
                    log_pass(f"SessionStart banner ({loop}): a clean completion still reads 'signed off'")
                else:
                    log_fail(f"SessionStart banner ({loop}): a clean completion still reads 'signed off'",
                             "'signed off', no 'recorded over'", ss_out[:500])

                pm_reason = json.loads(run_script(persistent_mode, payload)).get("reason", "")
                if "Panel approved" in pm_reason and "recorded over" not in pm_reason:
                    log_pass(f"nudge ({loop}): a clean completion still reads 'Panel approved'")
                else:
                    log_fail(f"nudge ({loop}): a clean completion still reads 'Panel approved'",
                             "'Panel approved', no 'recorded over'", pm_reason[:500])

                (crew_dir / state_name).unlink()

            # =========================================================================
            # THE ESCAPE HATCH vs A GARBAGE `active`
            # =========================================================================
            # The Stop hook blocks on any TRUTHY active, so a hand-edited "true"
            # or 1 is a loop that is ON and blocking. `deactivate --cancel` must
            # turn it OFF: a stricter rule here (`is True`) made the cancel a
            # silent no-op success on exactly the broken files that need it, so
            # the hook blocked forever with no sanctioned way out.
            # =========================================================================
            log_section("deactivate --cancel vs a truthy non-bool `active`")

            for garbage in ("true", 1):
                sid = f"hatch-{garbage}"
                _rv_init("bl", sid)
                sp = _rv_state_path("bl", sid)
                st = json.loads(sp.read_text())
                st["active"] = garbage
                sp.write_text(json.dumps(st), encoding="utf-8")

                # The hook agrees this loop is ON (it blocks), so the premise holds.
                blocked = run_script(persistent_mode, json.dumps({
                    "directory": str(rv_root), "session_id": sid}))
                hook_blocks = '"decision": "block"' in blocked

                _, err, code = run_crew_state(
                    ["deactivate", "bl", "--cancel", "--reason", "User cancelled",
                     "--session-id", sid], rv_root)
                after = json.loads(sp.read_text())

                if (hook_blocks and code == 0 and after.get("active") is False
                        and after.get("exit_kind") == "cancelled"):
                    log_pass(f"deactivate --cancel turns off a loop with active={garbage!r} "
                             f"(hook blocked it, so it was ON)")
                else:
                    log_fail(f"deactivate --cancel turns off a loop with active={garbage!r}",
                             "hook blocks first, then exit 0 + active is False + "
                             "exit_kind=cancelled",
                             f"hook_blocks={hook_blocks}, exit {code}, "
                             f"active={after.get('active')!r}, "
                             f"exit_kind={after.get('exit_kind')!r}, stderr={err[:200]!r}")
                    continue

                # ... and the hook now agrees it is OFF: the whole point of the
                # hatch is that the Stop stops being blocked.
                allowed = run_script(persistent_mode, json.dumps({
                    "directory": str(rv_root), "session_id": sid}))
                if '"decision": "block"' not in allowed:
                    log_pass(f"hook allows Stop after --cancel repaired active={garbage!r}")
                else:
                    log_fail(f"hook allows Stop after --cancel repaired active={garbage!r}",
                             "no block decision", allowed[:200])

            # =========================================================================
            # SUB-FLOOR IMPORT BOMB + LOUD FAIL-OPEN
            # =========================================================================
            log_section("hooks: version guard + fail-open")

            session_start_path = SCRIPT_DIR / "session-start.py"
            state_discovery_path = SCRIPT_DIR / "state_discovery.py"

            # Static: state_discovery carries the __future__ import, keeping
            # its annotations lazy. Not load-bearing under the 3.11 floor (a
            # `-> Path | None` evaluates fine there); kept because the module
            # is imported by tooling that may not share the floor.
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
                guard_idx = src.find("sys.version_info < (3, 11)")
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
            # hookSpecificOutput.additionalContext channel). Fault: a non-object
            # JSON payload makes `from_dict`'s `.get()` raise AttributeError
            # inside main(). (A non-string "directory" is no longer a crash
            # vector: the hook does not read the payload directory anymore, it
            # resolves the project root through crew_base().)
            for hook_path in (persistent_mode, session_start_path):
                crash_env = _neutral_env()
                crash_env.pop("CLAUDE_PROJECT_DIR", None)  # fallback cwd, except terminal `.crew` re-anchors
                proc = subprocess.run(
                    [sys.executable, str(hook_path)],
                    input='[1, 2]',
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

            # Real-interpreter test (skip if no sub-floor interpreter is around):
            # both hook entry points are visible no-ops below the 3.11 floor
            # (allow + loud diagnostic, exit 0). 3.9 is the one that shows up in
            # practice, since macOS ships it as /usr/bin/python3.
            old_python = None
            for candidate in ("python3.9", "python3.10", "/usr/bin/python3"):
                cand_path = candidate if candidate.startswith("/") else shutil.which(candidate)
                if not cand_path or not Path(cand_path).exists():
                    continue
                ver = subprocess.run([cand_path, "-c", "import sys; print(sys.version_info[:2])"],
                                     capture_output=True, text=True)
                if ver.returncode == 0 and ver.stdout.strip() in ("(3, 9)", "(3, 10)"):
                    old_python = cand_path
                    break
            if old_python is None:
                print(f"{YELLOW}~ SKIP{NC}: no sub-3.11 interpreter found (real-interpreter guard test)")
            else:
                for hook_path in (persistent_mode, session_start_path):
                    proc = subprocess.run(
                        [old_python, str(hook_path)],
                        input=json.dumps({"directory": str(test_path)}),
                        capture_output=True, text=True, cwd=SCRIPT_DIR, env=_neutral_env(),
                    )
                    name = f"{hook_path.name} below the 3.11 floor: visible no-op (allow + diagnostic)"
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
