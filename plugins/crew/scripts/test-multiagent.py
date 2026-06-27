#!/usr/bin/env python3
"""Tests for the multiagent review engine.

Matches test-hooks.py style: custom log_pass/log_fail/log_section harness, run
via `python plugins/crew/scripts/test-multiagent.py`, exits nonzero on failure.

Covers Step 1.7:
  1. CodexProvider (mocked subprocess): argv, stdin prompt, -o read, timeout,
     nonzero exit.
  2. AgyProvider: argv shape (-p + distinct positional prompt), stdin=DEVNULL,
     --model default/override, ANSI strip, empty->ok=False, auth-banner-at-
     exit-0->ok=False, effective timeout >= --print-timeout, oversized prompt
     degradation, timeout/hang kill+reap.
  3. is_available() three outcomes for both providers.
  4. render: side-by-side + --json; failed block; skipped block; model=None.
  5. targets: mocked + real-git (dirty, clean-vs-base, detached HEAD, no
     commits, shallow clone, base ahead, outside-repo).
  6. fan-out: one seat fails, the other survives; hung seat doesn't block.
  7. production invocation path: bare-script cli.py runs w/o ModuleNotFoundError.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# Colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
NC = "\033[0m"

PASS_COUNT = 0
FAIL_COUNT = 0

SCRIPT_DIR = Path(__file__).parent
MULTIAGENT_DIR = SCRIPT_DIR / "multiagent"

# Make `import multiagent...` resolve (the package parent is SCRIPT_DIR).
sys.path.insert(0, str(SCRIPT_DIR))

from multiagent import prompts, render, rounds, targets  # noqa: E402
from multiagent.providers import (  # noqa: E402
    ProviderResult,
    get_provider,
    available_seats,
)
from multiagent.providers.codex import CodexProvider  # noqa: E402
from multiagent.providers.agy import (  # noqa: E402
    AgyProvider,
    strip_ansi,
    detect_auth_or_error,
    parse_timeout_to_seconds,
    AGY_PROMPT_MAX_BYTES,
)


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


def check(name: str, cond: bool, expected: str = "", got: str = "") -> None:
    if cond:
        log_pass(name)
    else:
        log_fail(name, expected or "True", got or "False")


# =============================================================================
# Fake executable helpers — write a tiny script onto a temp PATH dir.
# =============================================================================

def make_fake_bin(dir_path: Path, name: str, script_body: str) -> Path:
    p = dir_path / name
    p.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(script_body))
    p.chmod(0o755)
    return p


def path_with(*dirs: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(str(d) for d in dirs) + os.pathsep + env.get("PATH", "")
    return env


# =============================================================================
# CodexProvider tests (mocked subprocess via a fake `codex` on PATH)
# =============================================================================

def test_codex():
    log_section("CodexProvider")
    prov = CodexProvider()

    # is_available outcomes
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # absent
        old_path = os.environ["PATH"]
        os.environ["PATH"] = str(d)
        try:
            avail, diag = prov.is_available()
            check("codex is_available absent -> (False, diag)",
                  avail is False and bool(diag), "(False, non-empty)", f"({avail}, {diag!r})")
            # present
            make_fake_bin(d, "codex", "import sys\n")
            avail, diag = prov.is_available()
            check("codex is_available present -> (True, '')",
                  avail is True and diag == "", "(True, '')", f"({avail}, {diag!r})")
        finally:
            os.environ["PATH"] = old_path

    # run() success: capture argv, stdin, write -o file
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        capture = d / "capture.json"
        body = f"""
        import sys, json
        args = sys.argv[1:]
        stdin = sys.stdin.read()
        # find -o file
        out = None
        for i, a in enumerate(args):
            if a == "-o":
                out = args[i + 1]
        with open({str(capture)!r}, "w") as f:
            json.dump({{"args": args, "stdin": stdin}}, f)
        with open(out, "w") as f:
            f.write("CODEX REVIEW OK")
        sys.exit(0)
        """
        make_fake_bin(d, "codex", body)
        env_path = os.environ["PATH"]
        os.environ["PATH"] = str(d) + os.pathsep + env_path
        try:
            res = prov.run("PROMPT-BODY", model="o3", timeout=30)
        finally:
            os.environ["PATH"] = env_path

        cap = json.loads(capture.read_text())
        check("codex argv: exec -, --ignore-user-config, -c reasoning=xhigh, --sandbox, --skip-git-repo-check, -o",
              cap["args"][0] == "exec" and cap["args"][1] == "-"
              and "--ignore-user-config" in cap["args"]
              and "-c" in cap["args"] and "model_reasoning_effort=xhigh" in cap["args"]
              and "--sandbox" in cap["args"] and "--skip-git-repo-check" in cap["args"]
              and "-o" in cap["args"],
              "exec - --ignore-user-config -c model_reasoning_effort=xhigh --sandbox ... -o", str(cap["args"]))
        check("codex argv carries NO prompt body (stdin only)",
              "PROMPT-BODY" not in cap["args"], "no prompt in argv", str(cap["args"]))
        check("codex prompt delivered via stdin", cap["stdin"] == "PROMPT-BODY",
              "PROMPT-BODY", cap["stdin"])
        check("codex --model passed through", "--model" in cap["args"] and "o3" in cap["args"],
              "--model o3", str(cap["args"]))
        check("codex reads output from -o file -> ok",
              res.ok and res.output == "CODEX REVIEW OK", "ok=True 'CODEX REVIEW OK'",
              f"ok={res.ok} {res.output!r}")

    # run() nonzero exit -> error populated
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_fake_bin(d, "codex", """
        import sys
        sys.stderr.write("not authenticated")
        sys.exit(1)
        """)
        env_path = os.environ["PATH"]
        os.environ["PATH"] = str(d) + os.pathsep + env_path
        try:
            res = prov.run("p", timeout=30)
        finally:
            os.environ["PATH"] = env_path
        check("codex nonzero exit -> ok=False with error (auth-fail path)",
              res.ok is False and res.error and "not authenticated" in res.error,
              "ok=False, error contains stderr", f"ok={res.ok} error={res.error!r}")

    # run() timeout -> ok=False
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_fake_bin(d, "codex", """
        import time
        time.sleep(10)
        """)
        env_path = os.environ["PATH"]
        os.environ["PATH"] = str(d) + os.pathsep + env_path
        try:
            res = prov.run("p", timeout=1)
        finally:
            os.environ["PATH"] = env_path
        check("codex timeout -> ok=False with timeout error",
              res.ok is False and res.error and "timed out" in res.error,
              "ok=False timed out", f"ok={res.ok} error={res.error!r}")


# =============================================================================
# AgyProvider tests
# =============================================================================

def test_agy_helpers():
    log_section("AgyProvider helpers")
    check("strip_ansi removes escape codes",
          strip_ansi("\x1b[31mred\x1b[0m") == "red", "red", strip_ansi("\x1b[31mred\x1b[0m"))
    check("detect_auth_or_error catches auth-timeout banner",
          detect_auth_or_error("authentication timed out, visit https://x") is not None,
          "non-None", "None")
    check("detect_auth_or_error catches oauth url",
          detect_auth_or_error("please visit https://accounts.google.com/o/oauth2 to login")
          is not None, "non-None", "None")
    check("detect_auth_or_error passes a real review",
          detect_auth_or_error("Clarity: PASS. [MINOR] tighten naming. APPROVED") is None,
          "None", "non-None")
    # False-positive guard: a review OF auth code quotes the auth markers but is
    # a real review — must NOT be flagged (else the agy seat is silently dropped).
    check("detect_auth_or_error passes a SHORT structured review of auth code",
          detect_auth_or_error(
              "[BLOCKING] the oauth token is not validated before use in auth.py; "
              "authentication required path skips the check. REVISE"
          ) is None,
          "None", "non-None")
    long_auth_review = (
        "Reviewing the OAuth login flow. "
        + "The authentication required handling and sign in to continue path look correct. " * 20
    )
    check("detect_auth_or_error passes a LONG review that discusses auth (>banner size)",
          detect_auth_or_error(long_auth_review) is None, "None", "non-None")
    # Still catches the real failure: a short, structureless auth banner.
    check("detect_auth_or_error still flags a structureless auth banner",
          detect_auth_or_error(
              "sign in to continue: https://accounts.google.com/o/oauth2/auth"
          ) is not None,
          "non-None", "None")
    # False-NEGATIVE guard: a real auth banner that happens to contain the bare
    # word "approved" must STILL be flagged (i.e. "approved" is not a structure
    # marker — else the banner masks itself and slips through as a "review").
    check("detect_auth_or_error still flags an auth banner containing 'approved'",
          detect_auth_or_error(
              "your device is not approved — please sign in at "
              "https://accounts.google.com/o/oauth2/auth to continue"
          ) is not None,
          "non-None", "None")
    # False-NEGATIVE guard: an auth banner that says "confidence level: low"
    # (no colon RIGHT AFTER the word "confidence") must STILL be flagged — the
    # structure marker is "confidence:" (rubric form), not bare "confidence".
    check("detect_auth_or_error flags 'confidence level:' auth banner (marker is 'confidence:')",
          detect_auth_or_error(
              "confidence level: low — please authenticate at "
              "https://accounts.google.com"
          ) is not None,
          "non-None", "None")
    check("parse_timeout 8m -> 480s", parse_timeout_to_seconds("8m") == 480.0,
          "480.0", str(parse_timeout_to_seconds("8m")))
    check("parse_timeout 300s -> 300", parse_timeout_to_seconds("300s") == 300.0,
          "300.0", str(parse_timeout_to_seconds("300s")))


def test_agy_timeout_floor():
    log_section("AgyProvider effective timeout >= --print-timeout (BLOCKING #1)")
    prov = AgyProvider()
    old = os.environ.get("CREW_MA_AGY_PRINT_TIMEOUT")
    os.environ["CREW_MA_AGY_PRINT_TIMEOUT"] = "8m"
    try:
        eff = prov.effective_timeout(300)  # generic ABC default
        check("agy effective timeout not capped at 300 when print-timeout is 8m",
              eff >= 480.0, ">= 480", str(eff))
        eff2 = prov.effective_timeout(900)  # explicitly larger caller wins
        check("agy effective timeout honors larger caller timeout",
              eff2 == 900.0, "900.0", str(eff2))
    finally:
        if old is None:
            os.environ.pop("CREW_MA_AGY_PRINT_TIMEOUT", None)
        else:
            os.environ["CREW_MA_AGY_PRINT_TIMEOUT"] = old


def test_agy_oversized():
    log_section("AgyProvider oversized prompt degradation (should-fix #4)")
    prov = AgyProvider()
    # Make agy "present" so we know the skip is from the size check, not PATH.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_fake_bin(d, "agy", "import sys; sys.exit(0)")
        env_path = os.environ["PATH"]
        os.environ["PATH"] = str(d) + os.pathsep + env_path
        try:
            big = "x" * (AGY_PROMPT_MAX_BYTES + 1)
            res = prov.run(big, timeout=5)
        finally:
            os.environ["PATH"] = env_path
        check("agy oversized prompt -> ok=False with size-degradation diagnostic",
              res.ok is False and res.error and "ARG_MAX" in res.error,
              "ok=False ARG_MAX diag", f"ok={res.ok} error={res.error!r}")


def _run_agy_with_fake(script_body: str, prompt="PROMPT", timeout=10, env_extra=None):
    prov = AgyProvider()
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        capture = d / "capture.json"
        # Inject the capture path into the fake.
        full = f"import os\nCAPTURE = {str(capture)!r}\n" + textwrap.dedent(script_body)
        make_fake_bin(d, "agy", full)
        env_path = os.environ["PATH"]
        os.environ["PATH"] = str(d) + os.pathsep + env_path
        saved = {}
        if env_extra:
            for k, v in env_extra.items():
                saved[k] = os.environ.get(k)
                os.environ[k] = v
        try:
            res = prov.run(prompt, timeout=timeout)
        finally:
            os.environ["PATH"] = env_path
            for k, v in (saved or {}).items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        cap = json.loads(capture.read_text()) if capture.exists() else None
        return res, cap


def test_agy_run():
    log_section("AgyProvider run() (fake agy)")

    capture_args = """
    import sys, json
    args = sys.argv[1:]
    try:
        stdin_data = sys.stdin.read()
    except Exception:
        stdin_data = "<no-stdin>"
    with open(CAPTURE, "w") as f:
        json.dump({"args": args, "stdin": stdin_data}, f)
    # emit ANSI-wrapped review
    sys.stdout.write("\\x1b[32mClarity: PASS. APPROVED\\x1b[0m")
    sys.exit(0)
    """
    res, cap = _run_agy_with_fake(capture_args)
    check("agy success -> ok=True, ANSI stripped",
          res.ok and res.output == "Clarity: PASS. APPROVED",
          "ok=True 'Clarity: PASS. APPROVED'", f"ok={res.ok} {res.output!r}")
    args = cap["args"]
    check("agy argv: -p flag present", "-p" in args, "-p in argv", str(args))
    # -p and the prompt are DISTINCT elements; prompt is its own positional.
    p_idx = args.index("-p")
    check("agy: prompt is its OWN positional element (not -p's value)",
          "PROMPT" in args and args.index("PROMPT") != p_idx,
          "PROMPT a distinct element", str(args))
    # -p is a BOOLEAN flag; the prompt is a separate argv element passed via
    # subprocess (never a shell string, never fused into -p). The only thing
    # that matters: the prompt is its own element and not concatenated into the
    # flag (e.g. "-pPROMPT") or shell-quoted.
    check("agy: prompt is a standalone argv element (not fused into -p / shell-quoted)",
          "PROMPT" in args and "-pPROMPT" not in args
          and all('"' not in a and "'" not in a for a in args),
          "standalone unquoted positional", str(args))
    check("agy: no --prompt-file", "--prompt-file" not in args, "absent", str(args))
    check("agy: --model defaults to Gemini 3.1 Pro (High)",
          "--model" in args and "Gemini 3.1 Pro (High)" in args,
          "--model 'Gemini 3.1 Pro (High)'", str(args))
    check("agy: --print-timeout present", "--print-timeout" in args, "present", str(args))
    check("agy: --sandbox present (confined, not a system-wide bypass)",
          "--sandbox" in args, "present", str(args))
    check("agy: --dangerously-skip-permissions NOT used (no auto-approve-all)",
          "--dangerously-skip-permissions" not in args, "absent", str(args))
    check("agy: stdin is DEVNULL (fake saw no stdin / empty)",
          cap["stdin"] in ("", "<no-stdin>"), "empty/no stdin", cap["stdin"])

    # --model override
    res, cap = _run_agy_with_fake(capture_args, env_extra={"CREW_MA_AGY_MODEL": "Gemini 3.5 Flash (High)"})
    check("agy: --model overridable via CREW_MA_AGY_MODEL",
          "Gemini 3.5 Flash (High)" in cap["args"], "override model present", str(cap["args"]))

    # empty output -> ok=False
    res, _ = _run_agy_with_fake("""
    import sys
    with open(CAPTURE, "w") as f:
        f.write('{"args": [], "stdin": ""}')
    sys.exit(0)
    """)
    check("agy empty output -> ok=False with explicit error",
          res.ok is False and res.error and "no output" in res.error,
          "ok=False 'no output'", f"ok={res.ok} error={res.error!r}")

    # auth banner with EXIT 0 -> ok=False (BLOCKING #2)
    res, _ = _run_agy_with_fake("""
    import sys
    with open(CAPTURE, "w") as f:
        f.write('{"args": [], "stdin": ""}')
    sys.stdout.write("Please visit https://accounts.google.com/o/oauth2/auth ... authentication timed out")
    sys.exit(0)
    """)
    check("agy auth banner at exit 0 -> ok=False (NOT ok=True)",
          res.ok is False and res.error and "auth" in res.error.lower(),
          "ok=False auth diag", f"ok={res.ok} error={res.error!r}")

    # hang -> timeout kill + reap within deadline
    start = time.monotonic()
    res, _ = _run_agy_with_fake("""
    import sys, time
    with open(CAPTURE, "w") as f:
        f.write('{"args": [], "stdin": ""}')
    time.sleep(30)
    """, timeout=1, env_extra={"CREW_MA_AGY_PRINT_TIMEOUT": "1s"})
    elapsed = time.monotonic() - start
    check("agy hang -> ok=False with timeout error",
          res.ok is False and res.error and "timed out" in res.error,
          "ok=False timed out", f"ok={res.ok} error={res.error!r}")
    check("agy hang -> reaped within hard deadline (no indefinite block)",
          elapsed < 25, "< 25s", f"{elapsed:.1f}s")


def test_agy_is_available():
    log_section("AgyProvider is_available()")
    prov = AgyProvider()
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = str(d)
        try:
            avail, diag = prov.is_available()
            check("agy is_available absent -> (False, diag)",
                  avail is False and bool(diag), "(False, non-empty)", f"({avail}, {diag!r})")
            make_fake_bin(d, "agy", "import sys")
            avail, diag = prov.is_available()
            check("agy is_available present -> (True, '')",
                  avail is True and diag == "", "(True, '')", f"({avail}, {diag!r})")
        finally:
            os.environ["PATH"] = old_path


# =============================================================================
# Registry
# =============================================================================

def test_default_seats():
    log_section("default subprocess seats")
    from multiagent.cli import _default_subprocess_seats  # noqa: E402
    saved = os.environ.get("CREW_MA_SEATS")
    try:
        os.environ.pop("CREW_MA_SEATS", None)
        seats = _default_subprocess_seats()
        check("default subprocess panel == codex + agy + cursor-auto/composer (cursor-gemini/glm/gpt opt-in)",
              seats == ["codex", "agy", "cursor-auto", "cursor-composer"],
              "['codex', 'agy', 'cursor-auto', 'cursor-composer']", str(seats))
        # opus/sonnet (Task seats) AND unknown names in CREW_MA_SEATS are dropped;
        # agy is registered, so it's honored as an opt-in seat.
        os.environ["CREW_MA_SEATS"] = "codex,agy,opus,sonnet,bogus"
        seats = _default_subprocess_seats()
        check("CREW_MA_SEATS filters to registered subprocess seats only",
              seats == ["codex", "agy"], "['codex', 'agy']", str(seats))
        # The 'cursor' group token expands in the env-var path too (not just the
        # CLI --seats path), so CREW_MA_SEATS=cursor behaves like --seats cursor.
        from multiagent.providers.cursor import CURSOR_SEATS  # noqa: E402
        os.environ["CREW_MA_SEATS"] = "cursor"
        seats = _default_subprocess_seats()
        check("CREW_MA_SEATS=cursor expands to all cursor-* seats",
              set(seats) == set(CURSOR_SEATS) and len(seats) == len(CURSOR_SEATS),
              "all cursor-* seats", str(seats))
    finally:
        if saved is None:
            os.environ.pop("CREW_MA_SEATS", None)
        else:
            os.environ["CREW_MA_SEATS"] = saved


def test_resolve_timeout():
    log_section("default per-seat timeout")
    from multiagent.cli import _resolve_timeout  # noqa: E402
    saved = os.environ.get("CREW_MA_TIMEOUT")
    try:
        os.environ.pop("CREW_MA_TIMEOUT", None)
        # Generous default: reference mode + thorough seats need room; better
        # than silently dropping a default seat on a large review.
        check("default per-seat timeout is generous (>= 600s)",
              _resolve_timeout(None) >= 600, ">=600", str(_resolve_timeout(None)))
        # explicit arg wins over the default and the env.
        os.environ["CREW_MA_TIMEOUT"] = "120"
        check("--timeout arg overrides default and env",
              _resolve_timeout(45) == 45, "45", str(_resolve_timeout(45)))
        check("CREW_MA_TIMEOUT overrides the default when no arg given",
              _resolve_timeout(None) == 120, "120", str(_resolve_timeout(None)))
        os.environ["CREW_MA_TIMEOUT"] = "not-an-int"
        check("bad CREW_MA_TIMEOUT falls back to the default (no crash)",
              _resolve_timeout(None) >= 600, ">=600", str(_resolve_timeout(None)))
    finally:
        if saved is None:
            os.environ.pop("CREW_MA_TIMEOUT", None)
        else:
            os.environ["CREW_MA_TIMEOUT"] = saved


def test_stage():
    log_section("--stage session-scoped prompt staging")
    from multiagent import cli  # noqa: E402

    # _resolve_session_id: --session-id arg wins; CLAUDE_SESSION_ID env fallback;
    # empty when neither is present (NOT a shell ${…} expansion).
    saved = os.environ.get("CLAUDE_SESSION_ID")
    try:
        os.environ["CLAUDE_SESSION_ID"] = "envsess"
        check("session id: --session-id arg wins over env",
              cli._resolve_session_id("argsess") == "argsess",
              "argsess", cli._resolve_session_id("argsess"))
        check("session id: falls back to CLAUDE_SESSION_ID env",
              cli._resolve_session_id(None) == "envsess",
              "envsess", cli._resolve_session_id(None))
        os.environ.pop("CLAUDE_SESSION_ID", None)
        check("session id: empty when neither arg nor env given",
              cli._resolve_session_id(None) == "", "''", repr(cli._resolve_session_id(None)))
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_SESSION_ID"] = saved

    # _stage_path: derives .crew/reviews/<session>/prompt-<role>.txt from
    # seat-role + session; flat when no session; charset-guards the segment.
    check("stage path derives from session id + seat-role",
          cli._stage_path("abc123", "opus") == ".crew/reviews/abc123/prompt-opus.txt",
          ".crew/reviews/abc123/prompt-opus.txt", cli._stage_path("abc123", "opus"))
    check("stage path is flat when session id is empty",
          cli._stage_path("", "sonnet") == ".crew/reviews/prompt-sonnet.txt",
          ".crew/reviews/prompt-sonnet.txt", cli._stage_path("", "sonnet"))
    check("stage path charset-guards the session segment (no traversal)",
          cli._stage_path("../../etc", "opus") == ".crew/reviews/etc/prompt-opus.txt",
          ".crew/reviews/etc/prompt-opus.txt", cli._stage_path("../../etc", "opus"))
    check("stage path charset-guards the seat-role too (no traversal)",
          cli._stage_path("s", "../../evil") == ".crew/reviews/s/prompt-evil.txt",
          ".crew/reviews/s/prompt-evil.txt", cli._stage_path("s", "../../evil"))
    check("stage path defaults role to 'seat' when seat-role missing",
          cli._stage_path("s", None) == ".crew/reviews/s/prompt-seat.txt",
          ".crew/reviews/s/prompt-seat.txt", cli._stage_path("s", None))

    # _seat_role_slug: the ONE canonical role→filename-slug source _stage_path /
    # --stage-all funnel through. A `.`-bearing role like opus-4.6 is charset-
    # stripped to opus-46, plain roles pass through, empty falls back to 'seat'.
    check("seat-role slug strips '.' (opus-4.6 -> opus-46)",
          cli._seat_role_slug("opus-4.6") == "opus-46",
          "opus-46", cli._seat_role_slug("opus-4.6"))
    check("seat-role slug passes a plain role through",
          cli._seat_role_slug("opus") == "opus",
          "opus", cli._seat_role_slug("opus"))
    check("seat-role slug defaults to 'seat' when empty",
          cli._seat_role_slug("") == "seat",
          "seat", cli._seat_role_slug(""))
    check("stage path derives prompt-opus-46.txt for opus-4.6 (dot stripped)",
          cli._stage_path("s", "opus-4.6").endswith("prompt-opus-46.txt"),
          "ends with prompt-opus-46.txt", cli._stage_path("s", "opus-4.6"))

    # End-to-end: render --stage writes the prompt to the derived path and prints
    # that path — no -o, no shell expansion. Run in a temp cwd so nothing leaks
    # into the repo. discuss -q needs no git repo.
    with tempfile.TemporaryDirectory() as td:
        proc = _run_cli(
            ["render", "--mode", "discuss", "-q", "Redis?", "--seat-role", "opus",
             "--stage", "--session-id", "sess-XYZ"],
            cwd=td, timeout=30,
        )
        staged = Path(td) / ".crew" / "reviews" / "sess-XYZ" / "prompt-opus.txt"
        check("render --stage writes the derived session-scoped path",
              proc.returncode == 0 and staged.is_file(),
              "file at .crew/reviews/sess-XYZ/prompt-opus.txt",
              f"rc={proc.returncode} exists={staged.is_file()} err={proc.stderr[:150]}")
        check("render --stage prints the staged path to stdout",
              ".crew/reviews/sess-XYZ/prompt-opus.txt" in proc.stdout,
              "path on stdout", proc.stdout.strip()[:200])
        check("render --stage staged file contains the rendered prompt",
              staged.is_file() and "Redis?" in staged.read_text(),
              "prompt body present", staged.read_text()[:120] if staged.is_file() else "(missing)")

    # --stage and -o are mutually exclusive (no ambiguous double destination).
    with tempfile.TemporaryDirectory() as td:
        proc = _run_cli(
            ["render", "--mode", "discuss", "-q", "X", "--seat-role", "opus",
             "--stage", "-o", str(Path(td) / "x.txt")],
            cwd=td, timeout=30,
        )
        check("render --stage + -o together -> nonzero error",
              proc.returncode != 0, "nonzero", str(proc.returncode))

    # An unsubstituted <session-id> placeholder is rejected (loud fail) so a
    # templating slip can't silently collapse to a constant shared staging dir.
    with tempfile.TemporaryDirectory() as td:
        proc = _run_cli(
            ["render", "--mode", "discuss", "-q", "X", "--seat-role", "opus",
             "--stage", "--session-id", "<session-id>"],
            cwd=td, timeout=30,
        )
        check("render --stage rejects an unsubstituted <session-id> placeholder",
              proc.returncode != 0 and "placeholder" in proc.stderr.lower(),
              "nonzero + placeholder error", f"rc={proc.returncode} err={proc.stderr[:150]}")


def test_registry():
    log_section("Provider registry")
    check("get_provider('codex') -> CodexProvider",
          isinstance(get_provider("codex"), CodexProvider), "CodexProvider", "?")
    check("get_provider('agy') -> AgyProvider",
          isinstance(get_provider("agy"), AgyProvider), "AgyProvider", "?")
    try:
        get_provider("nope")
        check("unknown seat raises", False, "ValueError", "no raise")
    except ValueError as exc:
        check("unknown seat raises clear error",
              "nope" in str(exc), "mentions 'nope'", str(exc))
    seats = available_seats(["codex", "agy"])
    check("available_seats returns instances", len(seats) == 2, "2", str(len(seats)))
    # Cursor model-seats are registered (one per CURSOR_SEATS entry).
    from multiagent.providers import known_seat_names
    from multiagent.providers.cursor import CursorProvider, CURSOR_SEATS
    for seat_name, (model, env) in CURSOR_SEATS.items():
        check(f"get_provider('{seat_name}') -> CursorProvider pinned to {model}",
              isinstance(get_provider(seat_name), CursorProvider)
              and get_provider(seat_name)._default_model == model
              and get_provider(seat_name)._model_env_var == env,
              f"CursorProvider({model})", repr(get_provider(seat_name).__dict__))
    check("each cursor seat is a DISTINCT model (no closure late-binding bug)",
          len({get_provider(s)._default_model for s in CURSOR_SEATS}) == len(CURSOR_SEATS),
          "all distinct models", str([get_provider(s)._default_model for s in CURSOR_SEATS]))


# =============================================================================
# ProviderResult contract
# =============================================================================

def test_result_contract():
    log_section("ProviderResult contract (Step 1.0 / 1.1)")
    r = ProviderResult(name="codex", model="o3", ok=True, output="x", error=None, elapsed=1.5)
    d = r.to_dict()
    check("to_dict has exactly the six fields",
          set(d.keys()) == {"name", "model", "ok", "output", "error", "elapsed"},
          "six fields", str(set(d.keys())))
    import dataclasses
    check("to_dict equals dataclasses.asdict", d == dataclasses.asdict(r), "equal", "differ")


# =============================================================================
# Render tests
# =============================================================================

def test_render():
    log_section("render")
    ok = ProviderResult("codex", "o3", True, "looks good APPROVED", None, 2.0)
    fail = ProviderResult("agy", "Gemini 3.1 Pro (High)", False, "", "agy returned no output", 1.0)
    none_model = ProviderResult("agy", None, True, "review body", None, 1.0)

    panel = render.render_panel([ok, fail])
    check("render_panel shows both seats", "codex" in panel and "agy" in panel,
          "both", panel[:80])
    check("render_panel failed seat shows ERROR block",
          "FAILED" in panel and "agy returned no output" in panel,
          "FAILED + error", panel)
    check("render_panel does not suppress OK seat when one fails",
          "looks good APPROVED" in panel, "ok output present", panel)

    # model=None must not raise TypeError
    try:
        block = render.render_block(none_model)
        check("render_block model=None renders without TypeError",
              "(default)" in block, "(default) placeholder", block)
    except TypeError as exc:
        check("render_block model=None renders without TypeError", False,
              "no TypeError", str(exc))

    skipped = render.render_skipped("codex", "codex not found on PATH")
    check("render_skipped shows marked skipped block",
          "SKIPPED" in skipped and "not found" in skipped, "SKIPPED + diag", skipped)

    # An unavailable seat (CLI marks it ok=False, error="skipped: <diag>") must
    # render as a SKIPPED block in non-JSON output — distinct from FAILED.
    skipped_result = ProviderResult(
        "codex", None, False, "", "skipped: codex not found on PATH", 0.0
    )
    skip_block = render.render_block(skipped_result)
    check("render_block routes a skipped seat to a SKIPPED block (not FAILED)",
          "SKIPPED" in skip_block and "FAILED" not in skip_block
          and "not found" in skip_block,
          "SKIPPED block, no FAILED", skip_block)
    skip_panel = render.render_panel([ok, skipped_result])
    check("render_panel marks skipped seat SKIPPED while keeping OK seat",
          "SKIPPED" in skip_panel and "looks good APPROVED" in skip_panel
          and "FAILED" not in skip_panel,
          "SKIPPED + OK, no FAILED", skip_panel)
    check("is_skipped true for skip marker, false for plain failure",
          render.is_skipped(skipped_result) is True and render.is_skipped(fail) is False,
          "True/False", f"{render.is_skipped(skipped_result)}/{render.is_skipped(fail)}")

    j = render.render_json([ok, fail])
    arr = json.loads(j)
    check("render_json emits a JSON array, one entry per seat",
          isinstance(arr, list) and len(arr) == 2, "list of 2", str(type(arr)))
    check("render_json entries have six fields",
          set(arr[0].keys()) == {"name", "model", "ok", "output", "error", "elapsed"},
          "six fields", str(set(arr[0].keys())))


# =============================================================================
# Prompts tests
# =============================================================================

def test_prompts():
    log_section("prompts")
    code = prompts.code_review("DIFF-HERE", "code: working-tree")
    check("code_review contains [BLOCKING]/[MINOR] instruction",
          "[BLOCKING]" in code and "[MINOR]" in code, "severity tags", code[:120])
    check("code_review includes the diff content", "DIFF-HERE" in code, "diff body", "?")
    plan = prompts.plan_review("PLAN-BODY", "plan: x.md")
    check("plan_review contains rubric criteria (Clarity/Testability)",
          "Clarity" in plan and "Testability" in plan, "criteria", plan[:120])

    # Reference-mode builders (the default) point seats at the target instead of
    # embedding it: no payload, no ARG_MAX pressure.
    code_ref = prompts.code_review_ref("git diff abc123..HEAD", "code: branch")
    check("code_review_ref names the git command, embeds NO diff body",
          "git diff abc123..HEAD" in code_ref and "DIFF-HERE" not in code_ref
          and "--- BEGIN DIFF ---" not in code_ref,
          "references the diff cmd", code_ref[:200])
    check("code_review_ref keeps the rubric + severity tags",
          "[BLOCKING]" in code_ref and "Correctness" in code_ref,
          "rubric intact", code_ref[:160])
    code_ref_nocmd = prompts.code_review_ref(None, "code: working-tree (no commits yet)")
    check("code_review_ref with no diff_cmd tells the seat to inspect the tree",
          "working-tree" in code_ref_nocmd and "--- BEGIN DIFF ---" not in code_ref_nocmd,
          "tree-inspect fallback", code_ref_nocmd[:200])
    plan_ref = prompts.plan_review_ref(".crew/plans/foo.md", "plan: foo.md")
    check("plan_review_ref names the file path, embeds NO plan body",
          ".crew/plans/foo.md" in plan_ref and "PLAN-BODY" not in plan_ref
          and "--- BEGIN PLAN ---" not in plan_ref,
          "references the plan path", plan_ref[:200])

    # build_prompt switch: reference by default, inline when asked.
    code_tgt = targets.Target(
        kind="code", scope="branch vs main", content="THE-DIFF-BYTES",
        descriptor="code: branch", diff_cmd="git diff dead..HEAD",
    )
    bp_ref = prompts.build_prompt(code_tgt)
    bp_inline = prompts.build_prompt(code_tgt, inline=True)
    check("build_prompt defaults to reference (cmd in, content out)",
          "git diff dead..HEAD" in bp_ref and "THE-DIFF-BYTES" not in bp_ref,
          "reference default", bp_ref[:160])
    check("build_prompt(inline=True) embeds the content",
          "THE-DIFF-BYTES" in bp_inline, "inline embeds", bp_inline[:160])
    plan_tgt = targets.Target(
        kind="plan", scope="p.md", content="PLAN-BYTES",
        descriptor="plan: p.md", ref_path="p.md",
    )
    check("build_prompt plan reference names path, not body",
          "p.md" in prompts.build_prompt(plan_tgt)
          and "PLAN-BYTES" not in prompts.build_prompt(plan_tgt),
          "plan reference", "?")
    check("prompts has NO multi-round debate template",
          not hasattr(prompts, "debate") and not hasattr(prompts, "debate_round"),
          "no debate fn", "has debate fn")
    check("prompts has NO SEAT/ROUND header text",
          "SEAT:" not in code and "ROUND:" not in code, "no SEAT/ROUND", "present")

    # council template — single-round structured-independent-critical stance.
    cncl = prompts.council("Is X better than Y?")
    check("council includes the question", "Is X better than Y?" in cncl,
          "question body", cncl[:120])
    check("council asks for direct take + objection + risks/tradeoffs",
          "DIRECT TAKE" in cncl and "OBJECTION" in cncl
          and ("RISK" in cncl or "TRADEOFF" in cncl),
          "the three asks", cncl[:200])
    check("council forbids rubber-stamp / manufactured contrarianism",
          "rubber-stamp" in cncl.lower() and "contrarian" in cncl.lower(),
          "no-rubber-stamp/contrarian framing", cncl)
    check("council is single-round (no SEAT/ROUND/rounds machinery)",
          "ROUND:" not in cncl and "SEAT:" not in cncl and "rebuttal" not in cncl.lower(),
          "no multi-round machinery", cncl[:200])


# =============================================================================
# Targets tests — mocked + real git
# =============================================================================

def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(path, with_commit=True):
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t.t"], path)
    _git(["config", "user.name", "t"], path)
    _git(["config", "commit.gpgsign", "false"], path)
    if with_commit:
        (Path(path) / "a.txt").write_text("hello\n")
        _git(["add", "."], path)
        _git(["commit", "-q", "-m", "init"], path)


def test_targets():
    log_section("targets — plan + real git")

    # plan target
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "plan.md"
        p.write_text("# Plan\nbody")
        t = targets.resolve(str(p))
        check("plan target reads .md", t.kind == "plan" and "body" in t.content,
              "kind=plan, body", f"kind={t.kind}")
        check("plan target carries ref_path for reference mode",
              t.ref_path == str(p), str(p), str(t.ref_path))

    # plan missing
    try:
        targets.resolve("/nonexistent/plan.md")
        check("missing plan raises TargetError", False, "TargetError", "no raise")
    except targets.TargetError:
        check("missing plan raises TargetError", True)

    # outside a repo -> clear error
    with tempfile.TemporaryDirectory() as td:
        try:
            targets.resolve("working-tree", cwd=td)
            check("code review outside git -> TargetError", False, "TargetError", "no raise")
        except targets.TargetError as exc:
            check("code review outside git -> TargetError",
                  "git repository" in str(exc), "git repo msg", str(exc))

    # dirty working tree
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        (Path(td) / "a.txt").write_text("hello\nchanged\n")
        t = targets.resolve("auto", cwd=td)
        check("auto in dirty repo -> working-tree diff",
              t.kind == "code" and "working-tree" in t.scope and "changed" in t.content,
              "working-tree w/ change", f"scope={t.scope}")

    # working-tree includes staged + unstaged tracked changes AND untracked
    # files rendered as new-file diffs (not merely listed by path).
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        (Path(td) / "a.txt").write_text("hello\nstaged\n")
        _git(["add", "a.txt"], td)
        (Path(td) / "b.txt").write_text("new untracked\n")  # untracked
        t = targets.resolve("working-tree", cwd=td)
        check("working-tree includes staged change",
              "staged" in t.content, "staged content", t.content[:80])
        check("working-tree renders untracked file as a new-file diff in content",
              "b.txt" in t.content and "new untracked" in t.content,
              "b.txt new-file diff in content", t.content[:200])
        check("working-tree notes untracked files are included as new-file diffs",
              any("b.txt" in n and "new-file diff" in n for n in t.notes),
              "untracked-included note", str(t.notes))
        check("working-tree diff_cmd is the compound cmd when untracked present",
              t.diff_cmd == targets._WORKTREE_DIFF_CMD,
              "compound _WORKTREE_DIFF_CMD", str(t.diff_cmd))

    # working-tree with ONLY tracked changes (no untracked) -> plain diff cmd
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        (Path(td) / "a.txt").write_text("hello\nonly-tracked\n")
        t = targets.resolve("working-tree", cwd=td)
        check("working-tree diff_cmd is 'git --no-pager diff HEAD' with no untracked",
              t.diff_cmd == "git --no-pager diff HEAD",
              "git --no-pager diff HEAD", str(t.diff_cmd))

    # ref range A..B (and A...B) -> diff between pinned SHAs
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        (Path(td) / "a.txt").write_text("hello\nv2\n")
        _git(["commit", "-aqm", "v2"], td)
        (Path(td) / "a.txt").write_text("hello\nv3\n")
        _git(["commit", "-aqm", "v3"], td)
        t = targets.resolve("HEAD~2..HEAD", cwd=td)
        check("ref range resolves to a diff target",
              t.kind == "code" and "v3" in t.content, "range diff w/ v3", f"scope={t.scope}")
        head = _git(["rev-parse", "HEAD"], td).stdout.strip()
        base = _git(["rev-parse", "HEAD~2"], td).stdout.strip()
        check("ref range diff_cmd pins both SHAs (deterministic, --no-pager)",
              t.diff_cmd == f"git --no-pager diff {base}..{head}",
              f"git --no-pager diff {base}..{head}", str(t.diff_cmd))
        t3 = targets.resolve("HEAD~2...HEAD", cwd=td)
        check("three-dot ref range A...B resolves and keeps the '...' separator",
              t3.kind == "code" and "..." in (t3.diff_cmd or ""),
              "three-dot range", str(t3.diff_cmd))

    # invalid ref range -> TargetError
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        try:
            targets.resolve("HEAD..nonexistent-ref", cwd=td)
            check("unresolvable ref range raises TargetError", False, "TargetError", "no raise")
        except targets.TargetError:
            check("unresolvable ref range raises TargetError", True)

    # clean repo -> last commit vs base (auto)
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        _git(["checkout", "-q", "-b", "feature"], td)
        (Path(td) / "a.txt").write_text("hello\nfeature\n")
        _git(["commit", "-aqm", "feat"], td)
        t = targets.resolve("auto", base="main", cwd=td)
        check("auto clean repo -> branch diff vs base",
              t.kind == "code" and "feature" in t.content, "feature diff", f"scope={t.scope}")
        mb = _git(["merge-base", "main", "HEAD"], td).stdout.strip()
        check("branch diff_cmd pins the merge-base SHA (deterministic, --no-pager)",
              t.diff_cmd == f"git --no-pager diff {mb}..HEAD",
              f"git --no-pager diff {mb}..HEAD", str(t.diff_cmd))

    # no commits yet
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td, with_commit=False)
        (Path(td) / "a.txt").write_text("new\n")
        t = targets.resolve("auto", cwd=td)
        check("no-commits-yet -> clear note, no traceback",
              t.kind == "code" and any("no commits" in n for n in t.notes),
              "no-commits note", str(t.notes))
        # BLOCKING regression guard: an unborn repo has no HEAD, so the
        # reproduced command must NOT use `git diff HEAD` — it diffs against the
        # empty tree instead (and still includes the untracked file).
        check("unborn-repo diff_cmd avoids 'diff HEAD', uses the empty-tree base",
              t.diff_cmd is not None and "diff HEAD" not in t.diff_cmd
              and "hash-object -t tree" in t.diff_cmd,
              "empty-tree base, no HEAD", str(t.diff_cmd))

    # no commits yet — DIVERGENT case (staged content != working tree). Pins the
    # working-tree semantics decision: `git diff <empty-tree>` reads the WORKING
    # TREE, not the index (no --cached), consistent with the `git diff HEAD` path.
    # A staged-then-modified file must show its WORKING-TREE version.
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td, with_commit=False)
        (Path(td) / "a.txt").write_text("alpha-staged\n")
        _git(["add", "a.txt"], td)                       # index: alpha-staged
        (Path(td) / "a.txt").write_text("beta-working\n")  # working tree: beta-working
        t = targets.resolve("working-tree", cwd=td)
        check("unborn staged-then-modified shows the WORKING-TREE version (no --cached)",
              "beta-working" in t.content and "alpha-staged" not in t.content,
              "beta-working present, alpha-staged absent", t.content[:160])

    # detached HEAD
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        (Path(td) / "a.txt").write_text("hello\nsecond\n")
        _git(["commit", "-aqm", "second"], td)
        sha = _git(["rev-parse", "HEAD"], td).stdout.strip()
        _git(["checkout", "-q", sha], td)  # detached
        t = targets.resolve("commit:HEAD", cwd=td)
        check("detached HEAD commit target resolves (no traceback)",
              t.kind == "code", "code target", f"kind={t.kind}")

    # base ahead of HEAD -> empty/zero-change reported
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        _git(["checkout", "-q", "-b", "behind"], td)
        # branch 'behind' is at the same commit as main -> empty diff vs base
        t = targets.resolve("branch", base="main", cwd=td)
        check("base-ahead/equal -> empty-diff note, not error",
              t.kind == "code" and (not t.content.strip() or any("empty" in n for n in t.notes)),
              "empty diff handled", f"notes={t.notes} content={t.content[:40]!r}")

    # shallow clone: merge-base against a missing base -> clear diagnostic
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        try:
            targets.resolve("branch", base="does-not-exist", cwd=td)
            check("missing base ref -> TargetError (shallow/clone-style)", False,
                  "TargetError", "no raise")
        except targets.TargetError as exc:
            check("missing base ref -> TargetError with clear message",
                  "merge-base" in str(exc) or "base" in str(exc), "base msg", str(exc))


# =============================================================================
# Fan-out + production invocation path (via cli.py bare-script)
# =============================================================================

def _run_cli(args, env=None, cwd=None, input_text=None, timeout=60):
    cli = MULTIAGENT_DIR / "cli.py"
    return subprocess.run(
        [sys.executable, str(cli), *args],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout,
    )


# PLUGIN_ROOT = plugins/crew (the dispatcher lives at its top level, NOT in
# scripts/). The dispatcher is invoked BARE (relying on its shebang + exec bit) —
# the REAL shipped path — so these tests exercise exactly what the commands do.
PLUGIN_ROOT = SCRIPT_DIR.parent
CREW_DISPATCHER = PLUGIN_ROOT / "crew"


def _run_dispatcher(args, env=None, cwd=None, timeout=60):
    """Invoke the bare ``plugins/crew/crew`` dispatcher directly (no ``python``).

    Defensively re-set the exec bit so a CI checkout that strips it still runs the
    bare-invocation tests — this is SEPARATE from the committed-mode assertion in
    test_dispatcher, which asserts the SHIPPED file's mode.
    """
    try:
        os.chmod(CREW_DISPATCHER, 0o755)
    except OSError:
        pass
    return subprocess.run(
        [str(CREW_DISPATCHER), *args],
        capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout,
    )


def test_production_invocation_and_fanout():
    log_section("production invocation path + fan-out (bare-script cli.py)")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        repo = d / "repo"
        repo.mkdir()
        _init_repo(str(repo))
        (repo / "a.txt").write_text("hello\nchanged\n")

        bins = d / "bin"
        bins.mkdir()
        # fake codex: succeed
        make_fake_bin(bins, "codex", """
        import sys
        out = None
        a = sys.argv[1:]
        for i, x in enumerate(a):
            if x == "-o":
                out = a[i+1]
        sys.stdin.read()
        with open(out, "w") as f:
            f.write("CODEX OK APPROVED")
        sys.exit(0)
        """)
        # fake agy: hang (to test it doesn't block codex's result)
        make_fake_bin(bins, "agy", """
        import time
        time.sleep(30)
        """)

        env = path_with(bins)
        env["CREW_MA_AGY_PRINT_TIMEOUT"] = "1s"

        # production path: bare-script cli.py, must not ModuleNotFoundError
        proc = _run_cli(
            ["review", "working-tree", "--seats", "codex,agy", "--json", "--timeout", "2"],
            env=env, cwd=str(repo), timeout=60,
        )
        check("cli.py bare-script runs without ModuleNotFoundError",
              "ModuleNotFoundError" not in proc.stderr, "no ModuleNotFoundError", proc.stderr[:200])
        check("cli.py exits 0", proc.returncode == 0, "0", f"{proc.returncode}: {proc.stderr[:200]}")
        try:
            arr = json.loads(proc.stdout)
        except Exception as exc:
            arr = None
            check("cli.py emits JSON array", False, "valid json", f"{exc}: {proc.stdout[:200]}")
        if arr is not None:
            check("cli.py JSON: one entry per requested seat",
                  len(arr) == 2, "2 entries", str(len(arr)))
            by_name = {r["name"]: r for r in arr}
            check("fan-out: codex result collected despite hung agy",
                  by_name.get("codex", {}).get("ok") is True,
                  "codex ok=True", str(by_name.get("codex")))
            check("fan-out: hung agy seat -> ok=False (timed out), didn't sink panel",
                  by_name.get("agy", {}).get("ok") is False,
                  "agy ok=False", str(by_name.get("agy")))

        # unavailable seat skipped with diagnostic, no hang.
        # Provide a PATH that has git (so target resolution works) but NOT
        # codex, so the seat is genuinely unavailable.
        gitonly = d / "gitonly"
        gitonly.mkdir()
        import shutil as _sh
        real_git = _sh.which("git")
        if real_git:
            os.symlink(real_git, gitonly / "git")
        env2 = dict(os.environ)
        env2["PATH"] = str(gitonly)
        proc2 = _run_cli(
            ["review", "working-tree", "--seats", "codex", "--json"],
            env=env2, cwd=str(repo), timeout=30,
        )
        ok_json = False
        try:
            arr2 = json.loads(proc2.stdout)
            ok_json = (len(arr2) == 1 and arr2[0]["ok"] is False
                       and "skipped" in (arr2[0]["error"] or ""))
        except Exception:
            ok_json = False
        check("cli.py: unavailable seat skipped with diagnostic (no hang)",
              ok_json, "ok=False skipped diag", proc2.stdout[:200])

    # ALL seats fail -> all ok=False, nonzero exit, clear all-failed message,
    # no traceback. Use a codex+agy panel where BOTH fakes fail (nonzero exit).
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        repo = d / "repo"
        repo.mkdir()
        _init_repo(str(repo))
        (repo / "a.txt").write_text("hello\nchanged\n")

        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", """
        import sys
        sys.stderr.write("codex boom")
        sys.exit(1)
        """)
        make_fake_bin(bins, "agy", """
        import sys
        sys.stderr.write("agy boom")
        sys.exit(1)
        """)
        env = path_with(bins)
        env["CREW_MA_AGY_PRINT_TIMEOUT"] = "1s"
        proc = _run_cli(
            ["review", "working-tree", "--seats", "codex,agy", "--json", "--timeout", "5"],
            env=env, cwd=str(repo), timeout=60,
        )
        check("all-seats-fail: no traceback",
              "Traceback" not in proc.stderr, "no traceback", proc.stderr[:200])
        check("all-seats-fail: nonzero exit",
              proc.returncode != 0, "nonzero", str(proc.returncode))
        check("all-seats-fail: clear 'all N seats failed' message on stderr",
              "all 2 seats failed" in proc.stderr, "all 2 seats failed", proc.stderr[:200])
        try:
            arr = json.loads(proc.stdout)
        except Exception as exc:
            arr = None
            check("all-seats-fail: still emits JSON array (no crash)", False,
                  "valid json", f"{exc}: {proc.stdout[:200]}")
        if arr is not None:
            check("all-seats-fail: one entry per seat, all ok=False",
                  len(arr) == 2 and all(r["ok"] is False for r in arr),
                  "2 entries all ok=False", str(arr))

    # -m invocation also works (PYTHONPATH = scripts dir)
    env3 = dict(os.environ)
    env3["PYTHONPATH"] = str(SCRIPT_DIR) + os.pathsep + env3.get("PYTHONPATH", "")
    proc3 = subprocess.run(
        [sys.executable, "-m", "multiagent.cli", "review", "--help"],
        capture_output=True, text=True, env=env3, timeout=30,
    )
    check("python -m multiagent.cli review --help works",
          proc3.returncode == 0 and "review" in (proc3.stdout + proc3.stderr).lower(),
          "help ok", f"{proc3.returncode}: {proc3.stderr[:120]}")


def test_run_subcommand():
    log_section("run subcommand (single seat, bare-script cli.py)")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        # fake codex: write the -o file from stdin so we can confirm dispatch.
        make_fake_bin(bins, "codex", """
        import sys
        a = sys.argv[1:]
        out = None
        for i, x in enumerate(a):
            if x == "-o":
                out = a[i + 1]
        body = sys.stdin.read()
        with open(out, "w") as f:
            f.write("RAN:" + body.strip())
        sys.exit(0)
        """)
        env = path_with(bins)

        # -f <file> reads the prompt and dispatches.
        pf = d / "prompt.txt"
        pf.write_text("FROM-FILE-PROMPT")
        proc = _run_cli(["run", "codex", "-f", str(pf)], env=env, timeout=30)
        check("run -f reads prompt file and dispatches",
              proc.returncode == 0 and "RAN:FROM-FILE-PROMPT" in proc.stdout,
              "exit0 + RAN:FROM-FILE-PROMPT", f"{proc.returncode}: {proc.stdout!r} {proc.stderr!r}")

        # direct <prompt-string> dispatches.
        proc = _run_cli(["run", "codex", "DIRECT-PROMPT"], env=env, timeout=30)
        check("run direct prompt-string dispatches",
              proc.returncode == 0 and "RAN:DIRECT-PROMPT" in proc.stdout,
              "exit0 + RAN:DIRECT-PROMPT", f"{proc.returncode}: {proc.stdout!r} {proc.stderr!r}")

        # --json emits the six-field shape, exit 0 even though ok is in the JSON.
        proc = _run_cli(["run", "codex", "X", "--json"], env=env, timeout=30)
        ok_json = False
        try:
            obj = json.loads(proc.stdout)
            ok_json = (set(obj.keys())
                       == {"name", "model", "ok", "output", "error", "elapsed"}
                       and obj["name"] == "codex" and obj["ok"] is True)
        except Exception:
            ok_json = False
        check("run --json emits six-field object, exit 0",
              proc.returncode == 0 and ok_json,
              "exit0 + six fields", f"{proc.returncode}: {proc.stdout!r}")

    # Task seat (orchestrator-owned) -> LOUD, specific failure: exit 2 with a
    # message naming "Task seat" + "orchestrator" (NOT the generic unknown one).
    for task_seat in ("opus", "sonnet", "opus-4.6"):
        proc = _run_cli(["run", task_seat, "hi"], timeout=30)
        check(f"run {task_seat} (Task seat) -> exit 2 + 'Task seat'/'orchestrator' message",
              proc.returncode == 2
              and "Task seat" in proc.stderr
              and "orchestrator" in proc.stderr
              and task_seat in proc.stderr,
              "exit2 + 'Task seat'/'orchestrator'", f"{proc.returncode}: {proc.stderr!r}")

    # A genuine typo (not a Task seat, not registered) still gets the GENERIC
    # unknown-seat message -> the Task-seat branch is scoped, not over-broad.
    proc = _run_cli(["run", "notaseat", "hi"], timeout=30)
    check("run unknown/typo seat -> generic 'subprocess seat' error (NOT Task-seat msg)",
          proc.returncode != 0
          and "subprocess seat" in proc.stderr
          and "Task seat" not in proc.stderr,
          "nonzero + generic 'subprocess seat'", f"{proc.returncode}: {proc.stderr!r}")

    # REGRESSION GUARD: codex (a real subprocess seat) gets PAST the seat-validation
    # guard. We do NOT invoke/meter it — calling with no prompt source proves it
    # cleared the Task-seat/unknown rejection (it hits the 'no prompt' error, NOT
    # 'Task seat' or 'subprocess seat').
    proc = _run_cli(["run", "codex"], timeout=30)
    check("run codex passes seat-validation (no Task-seat/unknown rejection)",
          proc.returncode != 0
          and "no prompt" in proc.stderr
          and "Task seat" not in proc.stderr
          and "subprocess seat" not in proc.stderr,
          "'no prompt' (past seat guard)", f"{proc.returncode}: {proc.stderr!r}")

    # neither prompt source -> error.
    proc = _run_cli(["run", "codex"], timeout=30)
    check("run with neither -f nor prompt-string -> nonzero error",
          proc.returncode != 0 and "no prompt" in proc.stderr,
          "nonzero + 'no prompt'", f"{proc.returncode}: {proc.stderr!r}")

    # both prompt sources -> error.
    with tempfile.TemporaryDirectory() as td:
        pf = Path(td) / "p.txt"
        pf.write_text("x")
        proc = _run_cli(["run", "codex", "STR", "-f", str(pf)], timeout=30)
        check("run with BOTH -f and prompt-string -> nonzero error",
              proc.returncode != 0 and "not both" in proc.stderr,
              "nonzero + 'not both'", f"{proc.returncode}: {proc.stderr!r}")

    # ok=False (nonzero provider exit) -> nonzero exit in non-json mode, error to stderr.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", """
        import sys
        sys.stderr.write("codex boom")
        sys.exit(1)
        """)
        env = path_with(bins)
        proc = _run_cli(["run", "codex", "p"], env=env, timeout=30)
        check("run ok=False -> nonzero exit, error to stderr (non-json)",
              proc.returncode != 0 and "boom" in proc.stderr,
              "nonzero + error on stderr", f"{proc.returncode}: out={proc.stdout!r} err={proc.stderr!r}")
        # same failure with --json -> exit 0, ok=False in the JSON.
        proc = _run_cli(["run", "codex", "p", "--json"], env=env, timeout=30)
        ok_json = False
        try:
            obj = json.loads(proc.stdout)
            ok_json = obj["ok"] is False and bool(obj["error"])
        except Exception:
            ok_json = False
        check("run --json on failure -> exit 0 with ok=False in JSON",
              proc.returncode == 0 and ok_json,
              "exit0 + ok=False JSON", f"{proc.returncode}: {proc.stdout!r}")

    # -------------------------------------------------------------------------
    # --session-id derivation: run DERIVES -f (prompt-seat.txt) and -o
    # (<seat>.json) from .crew/reviews/<session-id>/ when omitted. Each case runs
    # in an ISOLATED temp cwd (fresh .crew/reviews tree) so cross-case residue can
    # never false-pass a "not written" negative assertion. NO metered seat — the
    # fake codex (echoes stdin -> its own -o tmp) drives every case.
    _CODEX_ECHO = """
    import sys
    a = sys.argv[1:]
    out = None
    for i, x in enumerate(a):
        if x == "-o":
            out = a[i + 1]
    body = sys.stdin.read()
    with open(out, "w") as f:
        f.write("RAN:" + body.strip())
    sys.exit(0)
    """

    def _sid_env(cwd):
        bins = cwd / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", _CODEX_ECHO)
        return path_with(bins)

    # Case 1: derive BOTH -f and -o. Stage prompt-seat.txt; run with only
    # --session-id; assert the derived <seat>.json is the six-field object and
    # stdout did NOT also carry the JSON (output went to the derived -o).
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        env = _sid_env(cwd)
        rd = cwd / ".crew" / "reviews" / "S"
        rd.mkdir(parents=True)
        (rd / "prompt-seat.txt").write_text("SHARED-PROMPT")
        proc = _run_cli(["run", "codex", "--session-id", "S", "--json"],
                        env=env, cwd=str(cwd), timeout=30)
        derived = rd / "codex.json"
        ok_json = False
        if derived.exists():
            try:
                obj = json.loads(derived.read_text())
                ok_json = (set(obj.keys())
                           == {"name", "model", "ok", "output", "error", "elapsed"}
                           and obj["name"] == "codex"
                           and "SHARED-PROMPT" in obj["output"])
            except Exception:
                ok_json = False
        check("run --session-id derives BOTH -f (prompt-seat.txt) and -o (<seat>.json)",
              proc.returncode == 0 and ok_json and proc.stdout.strip() == "",
              "exit0 + derived codex.json six-field + empty stdout",
              f"{proc.returncode}: exists={derived.exists()} out={proc.stdout!r}")

    # Case 2: explicit -o OVERRIDES output derivation — writes <X>, NOT the
    # derived .crew/reviews/S/codex.json.
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        env = _sid_env(cwd)
        rd = cwd / ".crew" / "reviews" / "S"
        rd.mkdir(parents=True)
        (rd / "prompt-seat.txt").write_text("SHARED-PROMPT")
        x = cwd / "explicit-out.json"
        proc = _run_cli(["run", "codex", "--session-id", "S", "-o", str(x), "--json"],
                        env=env, cwd=str(cwd), timeout=30)
        check("run --session-id + explicit -o overrides (writes -o, not <seat>.json)",
              proc.returncode == 0 and x.exists() and not (rd / "codex.json").exists(),
              "exit0 + -o written + codex.json absent",
              f"{proc.returncode}: x={x.exists()} derived={(rd / 'codex.json').exists()}")

    # Case 3: explicit -f OVERRIDES input derivation (reads <Y>), still derives -o
    # to .crew/reviews/S/codex.json.
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        env = _sid_env(cwd)
        rd = cwd / ".crew" / "reviews" / "S"
        rd.mkdir(parents=True)
        (rd / "prompt-seat.txt").write_text("SHARED-PROMPT")
        y = cwd / "explicit-in.txt"
        y.write_text("OVERRIDE-INPUT")
        proc = _run_cli(["run", "codex", "--session-id", "S", "-f", str(y), "--json"],
                        env=env, cwd=str(cwd), timeout=30)
        derived = rd / "codex.json"
        read_y = False
        if derived.exists():
            try:
                obj = json.loads(derived.read_text())
                read_y = ("OVERRIDE-INPUT" in obj["output"]
                          and "SHARED-PROMPT" not in obj["output"])
            except Exception:
                read_y = False
        check("run --session-id + explicit -f overrides input (reads -f), still derives -o",
              proc.returncode == 0 and read_y,
              "exit0 + derived codex.json from -f body",
              f"{proc.returncode}: exists={derived.exists()} out={proc.stdout!r}")

    # Case 4: path-traversal seat name is rejected by the EXISTING unknown-seat
    # guard BEFORE derivation — exit 2, no .crew/reviews/S file attempted.
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        proc = _run_cli(["run", "../evil", "--session-id", "S"],
                        cwd=str(cwd), timeout=30)
        leaked = (cwd / ".crew" / "reviews" / "S").exists()
        check("run ../evil --session-id S -> exit 2 via unknown-seat guard, no path derived",
              proc.returncode == 2
              and "unknown or non-subprocess seat" in proc.stderr
              and not leaked,
              "exit2 + 'unknown or non-subprocess seat' + no .crew/reviews/S",
              f"{proc.returncode}: {proc.stderr!r} leaked={leaked}")

    # Case 5: literal placeholder session-id -> exit 2 with the placeholder message
    # (mirrors collect's <>/guard).
    proc = _run_cli(["run", "codex", "--session-id", "<session-id>", "--json"], timeout=30)
    check("run --session-id '<session-id>' (placeholder) -> exit 2 + placeholder message",
          proc.returncode == 2 and "unsubstituted placeholder" in proc.stderr,
          "exit2 + 'unsubstituted placeholder'", f"{proc.returncode}: {proc.stderr!r}")

    # Case 6: no --session-id AND no -f/prompt -> unchanged 'no prompt' error.
    proc = _run_cli(["run", "codex"], timeout=30)
    check("run codex (no --session-id, no source) -> unchanged 'no prompt' error",
          proc.returncode != 0 and "no prompt" in proc.stderr,
          "nonzero + 'no prompt'", f"{proc.returncode}: {proc.stderr!r}")

    # Case 7: CLAUDE_SESSION_ID env set but NO --session-id arg -> does NOT derive;
    # ad-hoc output stays on stdout (Decision A: env fallback never triggers derive).
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        env = _sid_env(cwd)
        env["CLAUDE_SESSION_ID"] = "S"
        proc = _run_cli(["run", "codex", "hi"], env=env, cwd=str(cwd), timeout=30)
        derived = cwd / ".crew" / "reviews" / "S" / "codex.json"
        check("run with CLAUDE_SESSION_ID env but no --session-id -> no derive (stdout)",
              proc.returncode == 0 and "RAN:hi" in proc.stdout and not derived.exists(),
              "exit0 + stdout output + codex.json absent",
              f"{proc.returncode}: derived={derived.exists()} out={proc.stdout!r}")

    # Case 8: explicit --session-id "" (empty) does NOT derive even with env set —
    # the truthy gate blocks it; output stays on stdout.
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        env = _sid_env(cwd)
        env["CLAUDE_SESSION_ID"] = "S"
        proc = _run_cli(["run", "codex", "hi", "--session-id", "", "--json"],
                        env=env, cwd=str(cwd), timeout=30)
        derived = cwd / ".crew" / "reviews" / "S" / "codex.json"
        on_stdout = False
        try:
            on_stdout = json.loads(proc.stdout)["name"] == "codex"
        except Exception:
            on_stdout = False
        check("run --session-id '' (empty) does NOT derive (truthy gate) -> stdout",
              proc.returncode == 0 and on_stdout and not derived.exists(),
              "exit0 + JSON on stdout + codex.json absent",
              f"{proc.returncode}: derived={derived.exists()} out={proc.stdout!r}")

    # Case 9: explicit --session-id "   " (whitespace-only) does NOT derive — the
    # .strip() in the gate rejects it before it would strip to "" and derive the
    # FLAT .crew/reviews/ dir. Neither the per-session nor the flat file is written.
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        env = _sid_env(cwd)
        env["CLAUDE_SESSION_ID"] = "S"
        proc = _run_cli(["run", "codex", "hi", "--session-id", "   ", "--json"],
                        env=env, cwd=str(cwd), timeout=30)
        per_session = cwd / ".crew" / "reviews" / "S" / "codex.json"
        flat = cwd / ".crew" / "reviews" / "codex.json"
        on_stdout = False
        try:
            on_stdout = json.loads(proc.stdout)["name"] == "codex"
        except Exception:
            on_stdout = False
        check("run --session-id '   ' (whitespace) does NOT derive (non-blank gate) -> stdout",
              proc.returncode == 0 and on_stdout
              and not per_session.exists() and not flat.exists(),
              "exit0 + JSON on stdout + neither codex.json written",
              f"{proc.returncode}: per_session={per_session.exists()} "
              f"flat={flat.exists()} out={proc.stdout!r}")

    # -m invocation path reaches `run` too.
    env3 = dict(os.environ)
    env3["PYTHONPATH"] = str(SCRIPT_DIR) + os.pathsep + env3.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "multiagent.cli", "run", "--help"],
        capture_output=True, text=True, env=env3, timeout=30,
    )
    check("python -m multiagent.cli run --help works",
          proc.returncode == 0 and "run" in (proc.stdout + proc.stderr).lower(),
          "help ok", f"{proc.returncode}: {proc.stderr[:120]}")


def test_council_subcommand():
    log_section("council subcommand (free-form fan-out, bare-script cli.py)")

    # A fake codex that echoes the stdin prompt body into the -o file, so we can
    # confirm the council prompt (not a review prompt) reached the seat.
    codex_echo = """
    import sys
    a = sys.argv[1:]
    out = None
    for i, x in enumerate(a):
        if x == "-o":
            out = a[i + 1]
    body = sys.stdin.read()
    with open(out, "w") as f:
        f.write("COUNCIL-TAKE :: " + body[:60])
    sys.exit(0)
    """

    # direct <question> string, single seat, --json six-field shape.
    with tempfile.TemporaryDirectory() as td:
        bins = Path(td) / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)
        env = path_with(bins)
        proc = _run_cli(
            ["council", "Is X better than Y?", "--seats", "codex", "--json"],
            env=env, timeout=30,
        )
        check("council direct question -> exit 0",
              proc.returncode == 0, "0", f"{proc.returncode}: {proc.stderr[:200]}")
        ok_json = False
        try:
            arr = json.loads(proc.stdout)
            ok_json = (
                isinstance(arr, list) and len(arr) == 1
                and set(arr[0].keys())
                == {"name", "model", "ok", "output", "error", "elapsed"}
                and arr[0]["name"] == "codex" and arr[0]["ok"] is True
            )
        except Exception:
            ok_json = False
        check("council --json emits six-field array, one entry per seat",
              ok_json, "six-field array len 1", f"{proc.stdout[:200]}")
        check("council fed the council prompt (not a review prompt)",
              "QUESTION" in proc.stdout or "council" in proc.stdout.lower(),
              "council framing reached the seat", proc.stdout[:200])

    # -f <file> reads the question from a file. Echo the FULL prompt body so we
    # can confirm the file's question made it into the assembled council prompt.
    codex_echo_full = """
    import sys
    a = sys.argv[1:]
    out = None
    for i, x in enumerate(a):
        if x == "-o":
            out = a[i + 1]
    body = sys.stdin.read()
    with open(out, "w") as f:
        f.write("COUNCIL-TAKE :: " + body)
    sys.exit(0)
    """
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo_full)
        env = path_with(bins)
        qf = d / "question.txt"
        qf.write_text("Should we adopt approach Z?")
        proc = _run_cli(
            ["council", "-f", str(qf), "--seats", "codex", "--json"],
            env=env, timeout=30,
        )
        ok = False
        try:
            arr = json.loads(proc.stdout)
            ok = (proc.returncode == 0 and len(arr) == 1
                  and "Should we adopt approach Z?" in arr[0]["output"])
        except Exception:
            ok = False
        check("council -f reads the question file and fans it out",
              ok, "exit0 + question in seat output", f"{proc.returncode}: {proc.stdout[:200]}")

    # default subprocess seats = codex,agy (no --seats given).
    with tempfile.TemporaryDirectory() as td:
        bins = Path(td) / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)
        # fake agy succeeds too
        make_fake_bin(bins, "agy", """
        import sys
        sys.stdout.write("AGY COUNCIL TAKE")
        sys.exit(0)
        """)
        env = path_with(bins)
        env["CREW_MA_AGY_PRINT_TIMEOUT"] = "1s"
        # Pin the two fake subprocess seats so this exercises the council fan-out
        # deterministically, independent of the default panel (covered separately
        # in test_default_seats, which is now codex + cursor-*).
        env["CREW_MA_SEATS"] = "codex,agy"
        proc = _run_cli(
            ["council", "Pick one.", "--json", "--timeout", "5"],
            env=env, timeout=60,
        )
        names = []
        try:
            arr = json.loads(proc.stdout)
            names = sorted(r["name"] for r in arr)
        except Exception:
            names = []
        check("council pinned seats fan out (codex,agy)",
              names == ["agy", "codex"], "['agy', 'codex']", str(names))

    # one seat fails, the other still returns (graceful degradation).
    with tempfile.TemporaryDirectory() as td:
        bins = Path(td) / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)
        make_fake_bin(bins, "agy", """
        import sys
        sys.stderr.write("agy boom")
        sys.exit(1)
        """)
        env = path_with(bins)
        env["CREW_MA_AGY_PRINT_TIMEOUT"] = "1s"
        proc = _run_cli(
            ["council", "Q?", "--seats", "codex,agy", "--json", "--timeout", "5"],
            env=env, timeout=60,
        )
        by_name = {}
        try:
            by_name = {r["name"]: r for r in json.loads(proc.stdout)}
        except Exception:
            by_name = {}
        check("council one-seat-fails: codex still returns ok=True",
              by_name.get("codex", {}).get("ok") is True,
              "codex ok=True", str(by_name.get("codex")))
        check("council one-seat-fails: agy ok=False, didn't sink the council",
              by_name.get("agy", {}).get("ok") is False,
              "agy ok=False", str(by_name.get("agy")))
        check("council partial panel -> exit 0 (not all-failed)",
              proc.returncode == 0, "0", f"{proc.returncode}: {proc.stderr[:200]}")

    # ALL seats fail -> nonzero exit, clear all-failed signal, no traceback.
    with tempfile.TemporaryDirectory() as td:
        bins = Path(td) / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", """
        import sys
        sys.stderr.write("codex boom")
        sys.exit(1)
        """)
        make_fake_bin(bins, "agy", """
        import sys
        sys.stderr.write("agy boom")
        sys.exit(1)
        """)
        env = path_with(bins)
        env["CREW_MA_AGY_PRINT_TIMEOUT"] = "1s"
        proc = _run_cli(
            ["council", "Q?", "--seats", "codex,agy", "--json", "--timeout", "5"],
            env=env, timeout=60,
        )
        check("council all-fail: no traceback",
              "Traceback" not in proc.stderr, "no traceback", proc.stderr[:200])
        check("council all-fail: nonzero exit",
              proc.returncode != 0, "nonzero", str(proc.returncode))
        check("council all-fail: clear 'all N seats failed' on stderr",
              "all 2 seats failed" in proc.stderr, "all 2 seats failed", proc.stderr[:200])

    # neither prompt source -> error; both -> error.
    proc = _run_cli(["council"], timeout=30)
    check("council with neither -f nor question -> nonzero error",
          proc.returncode != 0 and "no question" in proc.stderr,
          "nonzero + 'no question'", f"{proc.returncode}: {proc.stderr!r}")
    with tempfile.TemporaryDirectory() as td:
        qf = Path(td) / "q.txt"
        qf.write_text("x")
        proc = _run_cli(["council", "STR", "-f", str(qf)], timeout=30)
        check("council with BOTH -f and question -> nonzero error",
              proc.returncode != 0 and "not both" in proc.stderr,
              "nonzero + 'not both'", f"{proc.returncode}: {proc.stderr!r}")

    # --out writes results to a file (no shell redirect needed) and keeps stdout
    # clean. This is what makes the whole call allowlistable: a literal-path
    # `python cli.py council … --out <file>` with no `> "$dir/file"` redirect and
    # no `$(…)`-derived path for the permission matcher to choke on.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)
        env = path_with(bins)
        outf = d / "results.json"
        proc = _run_cli(
            ["council", "Q?", "--seats", "codex", "--json", "--out", str(outf)],
            env=env, timeout=30,
        )
        check("council --out: exit 0",
              proc.returncode == 0, "0", f"{proc.returncode}: {proc.stderr[:200]}")
        check("council --out: stdout stays clean (nothing redirected by shell)",
              proc.stdout.strip() == "", "empty stdout", repr(proc.stdout[:120]))
        wrote_ok = False
        try:
            arr = json.loads(outf.read_text())
            wrote_ok = (isinstance(arr, list) and len(arr) == 1
                        and arr[0]["name"] == "codex" and arr[0]["ok"] is True)
        except Exception:
            wrote_ok = False
        check("council --out: six-field JSON array written to the file",
              wrote_ok, "results.json holds the panel", f"exists={outf.exists()}")

    # -m invocation path reaches council too.
    env3 = dict(os.environ)
    env3["PYTHONPATH"] = str(SCRIPT_DIR) + os.pathsep + env3.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "multiagent.cli", "council", "--help"],
        capture_output=True, text=True, env=env3, timeout=30,
    )
    check("python -m multiagent.cli council --help works",
          proc.returncode == 0 and "council" in (proc.stdout + proc.stderr).lower(),
          "help ok", f"{proc.returncode}: {proc.stderr[:120]}")


def test_debate_subcommand():
    log_section("debate subcommand (SCAFFOLD-ONLY — never runs subprocess seats internally)")

    # codex stub is present on PATH but must NEVER be invoked: debate is
    # scaffold-only, so even `--seats codex` runs no seat. If cmd_debate ever
    # regressed to internal fan-out this stub would write a non-empty
    # subprocess.json and the [] assertions below would fail.
    codex_echo = """
    import sys
    a = sys.argv[1:]
    out = None
    for i, x in enumerate(a):
        if x == "-o":
            out = a[i + 1]
    body = sys.stdin.read()
    with open(out, "w") as f:
        f.write("COUNCIL-TAKE :: " + body[:60])
    sys.exit(0)
    """

    # positional question + --slug + `--seats codex`: SCAFFOLD-ONLY. The dir +
    # question.md are scaffolded, subprocess.json is ALWAYS [], the printed
    # summary has `seats == []`, a stderr advisory is emitted, and exit is 0 —
    # NO codex seat is run (the split-brain internal `_fan_out` is gone).
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)  # present, but must NOT be invoked
        env = path_with(bins)
        base = d / "debates"
        proc = _run_cli(
            ["debate", "Is X better than Y?", "--seats", "codex",
             "--slug", "x-vs-y", "--base-dir", str(base)],
            env=env, timeout=30,
        )
        check("debate --seats codex: exit 0 (scaffold-only, no seat run)",
              proc.returncode == 0, "0", f"{proc.returncode}: {proc.stderr[:200]}")
        info = {}
        try:
            info = json.loads(proc.stdout)
        except Exception:
            info = {}
        ddir = Path(info["dir"]) if info.get("dir") else None
        check("debate: prints JSON summary with a created dir path",
              bool(ddir) and ddir.exists(), "dir created", proc.stdout[:200])
        check("debate: dir name is <timestamp>-<slug>",
              bool(ddir) and "x-vs-y" in ddir.name and ddir.name[:8].isdigit(),
              "<ts>-x-vs-y", str(ddir.name if ddir else None))
        check("debate: question.md written into the dir",
              bool(ddir) and (ddir / "question.md").exists()
              and "Is X better than Y?" in (ddir / "question.md").read_text(),
              "question.md holds the question", "?")
        check("debate --seats codex: summary seats == [] (no seat executed)",
              info.get("seats") == [], "[]", repr(info.get("seats")))
        is_empty = False
        if ddir and (ddir / "subprocess.json").exists():
            try:
                is_empty = json.loads((ddir / "subprocess.json").read_text()) == []
            except Exception:
                is_empty = False
        check("debate --seats codex: subprocess.json is [] (codex NEVER run)",
              is_empty, "[]", "?")
        check("debate --seats codex: stderr advisory printed (no internal fan-out)",
              "no longer runs subprocess seats internally" in proc.stderr,
              "advisory on stderr", repr(proc.stderr[:200]))

    # -f + `--seats codex`: same scaffold-only contract via the question-file path
    # (auto-slug when --slug omitted). subprocess.json [] + advisory + no seat run.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)  # present, but must NOT be invoked
        env = path_with(bins)
        base = d / "debates"
        qf = d / "q.txt"
        qf.write_text("Should we adopt approach Zeta now?")
        proc = _run_cli(
            ["debate", "-f", str(qf), "--seats", "codex", "--base-dir", str(base)],
            env=env, timeout=30,
        )
        ddir = None
        try:
            ddir = Path(json.loads(proc.stdout)["dir"])
        except Exception:
            ddir = None
        check("debate -f --seats codex: reads question file and scaffolds the dir",
              proc.returncode == 0 and bool(ddir) and ddir.exists()
              and (ddir / "question.md").exists(),
              "exit0 + dir + question.md", f"{proc.returncode}: {proc.stdout[:160]}")
        check("debate: auto-slug derived from the question when --slug omitted",
              bool(ddir) and "should-we-adopt" in ddir.name,
              "slug from first words", str(ddir.name if ddir else None))
        ff_empty = False
        if ddir and (ddir / "subprocess.json").exists():
            try:
                ff_empty = json.loads((ddir / "subprocess.json").read_text()) == []
            except Exception:
                ff_empty = False
        check("debate -f --seats codex: subprocess.json is [] (codex NEVER run)",
              ff_empty, "[]", "?")
        check("debate -f --seats codex: stderr advisory printed",
              "no longer runs subprocess seats internally" in proc.stderr,
              "advisory on stderr", repr(proc.stderr[:200]))

    # malicious --slug is sanitized (no path traversal) + --consume deletes staging.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)
        env = path_with(bins)
        base = d / "debates"
        staging = d / "staged.txt"
        staging.write_text("Traversal attempt?")
        proc = _run_cli(
            ["debate", "-f", str(staging), "--slug", "../../pwned",
             "--seats", "codex", "--base-dir", str(base), "--consume"],
            env=env, timeout=30,
        )
        ddir = None
        try:
            ddir = Path(json.loads(proc.stdout)["dir"])
        except Exception:
            ddir = None
        check("debate: malicious --slug can't escape --base-dir (path traversal)",
              bool(ddir) and base.resolve() in ddir.resolve().parents
              and ".." not in ddir.parts,
              "dir under base, no '..'", str(ddir))
        check("debate: --slug sanitized to [a-z0-9-] (../../pwned -> ...-pwned)",
              bool(ddir) and ddir.name.endswith("-pwned"),
              "<ts>-pwned", str(ddir.name if ddir else None))
        check("debate --consume: staging file deleted after copy into dir",
              not staging.exists() and bool(ddir) and (ddir / "question.md").exists(),
              "staging gone, question.md present", f"staging_exists={staging.exists()}")

    # dir uniqueness: two same-slug debates never share a dir (no silent overwrite).
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)
        env = path_with(bins)
        base = d / "debates"
        dirs = []
        for _ in range(2):
            proc = _run_cli(
                ["debate", "Same question?", "--slug", "dup", "--seats", "codex",
                 "--base-dir", str(base)],
                env=env, timeout=30,
            )
            try:
                dirs.append(json.loads(proc.stdout)["dir"])
            except Exception:
                pass
        check("debate: two same-slug debates get distinct dirs (no overwrite)",
              len(dirs) == 2 and dirs[0] != dirs[1]
              and all(Path(x).exists() for x in dirs),
              "two distinct dirs", str(dirs))

    # Claude-only panel: --seats none scaffolds the dir but runs NO subprocess seats.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)  # present, but must NOT be invoked
        env = path_with(bins)
        base = d / "debates"
        proc = _run_cli(
            ["debate", "Claude only?", "--seats", "none", "--base-dir", str(base)],
            env=env, timeout=30,
        )
        ddir = None
        try:
            ddir = Path(json.loads(proc.stdout)["dir"])
        except Exception:
            ddir = None
        is_empty = False
        if ddir and (ddir / "subprocess.json").exists():
            try:
                is_empty = json.loads((ddir / "subprocess.json").read_text()) == []
            except Exception:
                is_empty = False
        check("debate --seats none: scaffolds dir + question.md, exit 0",
              proc.returncode == 0 and bool(ddir) and (ddir / "question.md").exists(),
              "exit0 + dir + question.md", f"{proc.returncode}: {proc.stdout[:160]}")
        check("debate --seats none: subprocess.json is [] (no codex/agy run)",
              is_empty, "[]", str(ddir))
        # NEGATIVE advisory assertion: an EXPLICIT `--seats none` is the silent
        # no-op — the advisory must be ABSENT. Guards against an accidental flip
        # of the inverted condition (which would otherwise pass silently).
        check("debate --seats none: NO stderr advisory (explicit silent no-op)",
              "no longer runs subprocess seats internally" not in proc.stderr,
              "advisory ABSENT", repr(proc.stderr[:200]))

    # OMITTED --seats (the BLOCKING-fix case): a bare `debate "q"` with NO --seats
    # at all pre-Step-5 RAN the default panel; now it scaffolds-only AND prints the
    # advisory (None falls through the `args.seats is None` branch). Still empty
    # subprocess.json, seats == [], exit 0 — no seat is ever run.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)  # present, but must NOT be invoked
        env = path_with(bins)
        base = d / "debates"
        proc = _run_cli(
            ["debate", "Omitted seats?", "--base-dir", str(base)],
            env=env, timeout=30,
        )
        info = {}
        try:
            info = json.loads(proc.stdout)
        except Exception:
            info = {}
        ddir = Path(info["dir"]) if info.get("dir") else None
        is_empty = False
        if ddir and (ddir / "subprocess.json").exists():
            try:
                is_empty = json.loads((ddir / "subprocess.json").read_text()) == []
            except Exception:
                is_empty = False
        check("debate (omitted --seats): scaffolds dir + question.md, exit 0, seats []",
              proc.returncode == 0 and bool(ddir) and (ddir / "question.md").exists()
              and info.get("seats") == [],
              "exit0 + dir + question.md + seats []",
              f"{proc.returncode}: {proc.stdout[:160]}")
        check("debate (omitted --seats): subprocess.json is [] (no codex/agy run)",
              is_empty, "[]", str(ddir))
        check("debate (omitted --seats): stderr advisory PRESENT (BLOCKING fix)",
              "no longer runs subprocess seats internally" in proc.stderr,
              "advisory PRESENT", repr(proc.stderr[:200]))

    # GUARD: cmd_debate must NEVER reach _fan_out, even with `--seats codex`.
    # We monkeypatch cli._fan_out to RAISE and call cmd_debate IN-PROCESS — if it
    # still calls _fan_out the exception would surface (nonzero / traceback);
    # instead it must scaffold-only and exit 0 with empty results. This in-process
    # stub also guarantees NO real codex/cursor seat is ever invoked here (no
    # metered calls). (The subprocess sub-cases above can't monkeypatch the child,
    # so this complements them by proving the call path itself never branches in.)
    import argparse as _argparse
    import contextlib as _contextlib
    import io as _io
    from multiagent import cli as _cli

    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "debates"

        def _boom(*a, **k):  # pragma: no cover - must never be called
            raise AssertionError("cmd_debate reached _fan_out (split-brain regression)")

        orig_fan_out = _cli._fan_out
        _cli._fan_out = _boom
        try:
            ns = _argparse.Namespace(
                question="Guarded question?", file=None, slug="guard",
                base_dir=str(base), seats="codex", timeout=None, consume=False,
            )
            buf = _io.StringIO()
            try:
                with _contextlib.redirect_stdout(buf):
                    rc = _cli.cmd_debate(ns)
                raised = False
            except AssertionError:
                rc = None
                raised = True
            summary = {}
            try:
                summary = json.loads(buf.getvalue())
            except Exception:
                summary = {}
            check("debate guard: cmd_debate NEVER calls _fan_out (--seats codex)",
                  not raised, "no _fan_out call", "AssertionError raised" if raised else "ok")
            check("debate guard: cmd_debate exits 0 with empty results (scaffold-only)",
                  rc == 0 and summary.get("seats") == [],
                  "rc=0 + seats []", f"rc={rc} seats={summary.get('seats')!r}")
        finally:
            _cli._fan_out = orig_fan_out

    # neither prompt source -> clean error.
    proc = _run_cli(["debate"], timeout=30)
    check("debate with neither -f nor question -> nonzero error",
          proc.returncode != 0 and "no question" in proc.stderr,
          "nonzero + 'no question'", f"{proc.returncode}: {proc.stderr!r}")


def test_rounds():
    log_section("rounds — debate run lifecycle (pure FS, no model calls)")

    check("slugify lowercases + collapses to [a-z0-9-]",
          rounds.slugify("Redis vs In-Memory!!") == "redis-vs-in-memory",
          "redis-vs-in-memory", rounds.slugify("Redis vs In-Memory!!"))
    check("slugify empty -> 'debate'", rounds.slugify("") == "debate",
          "debate", rounds.slugify(""))

    rid = rounds.new_run_id("My Topic", ts="2026-06-23T10:11:12")
    check("new_run_id has run- prefix + matches RUN_ID_RE",
          rid.startswith("run-") and bool(rounds.RUN_ID_RE.match(rid)),
          "valid run id", rid)
    check("new_run_id with no slug still valid",
          bool(rounds.RUN_ID_RE.match(rounds.new_run_id(ts="20260623"))),
          "valid", "?")

    # traversal guard: a valid run-id can only ever name a child of base_dir.
    for bad in ("../evil", "run-../x", "run-a/b", "nope", "run-..", ""):
        try:
            rounds.validate_run_id(bad)
            check(f"validate_run_id rejects {bad!r}", False, "RoundError", "no raise")
        except rounds.RoundError:
            check(f"validate_run_id rejects {bad!r}", True)
    try:
        rounds.run_dir("../../etc", base_dir="/tmp/x")
        check("run_dir rejects traversal run-id", False, "RoundError", "no raise")
    except rounds.RoundError:
        check("run_dir rejects traversal run-id", True)

    with tempfile.TemporaryDirectory() as td:
        d = rounds.ensure_run_dir(rid, base_dir=td)
        check("ensure_run_dir creates the dir", d.is_dir(), "dir exists", str(d))
        rounds.write_question(d, "Redis or in-memory?")
        check("question round-trips",
              rounds.read_question(d) == "Redis or in-memory?",
              "Redis or in-memory?", str(rounds.read_question(d)))
        rounds.write_round(d, 1, "r1 positions")
        rounds.write_round(d, 2, "r2 positions")
        check("round-NN.md is zero-padded",
              (d / "round-01.md").is_file() and (d / "round-02.md").is_file(),
              "round-01/02.md", str(sorted(p.name for p in d.glob("round-*.md"))))
        check("list_rounds returns sorted ints",
              rounds.list_rounds(d) == [1, 2], "[1, 2]", str(rounds.list_rounds(d)))
        check("read_prior_rounds(1) is None (round 1 has no prior)",
              rounds.read_prior_rounds(d, 1) is None, "None",
              str(rounds.read_prior_rounds(d, 1)))
        prior = rounds.read_prior_rounds(d, 3)
        check("read_prior_rounds(3) concatenates rounds 1 + 2",
              prior is not None and "r1 positions" in prior and "r2 positions" in prior,
              "rounds 1+2", str(prior))
        check("read_round(2) returns round 2 text",
              rounds.read_round(d, 2) == "r2 positions", "r2 positions",
              str(rounds.read_round(d, 2)))
        check("read_round missing -> None",
              rounds.read_round(d, 9) is None, "None", str(rounds.read_round(d, 9)))
        try:
            rounds.round_path(d, 0)
            check("round_path(0) raises RoundError", False, "RoundError", "no raise")
        except rounds.RoundError:
            check("round_path(0) raises RoundError", True)


def test_discuss_and_modes():
    log_section("prompts — discuss mode, mode validation, prior_round (single builder)")

    code_tgt = targets.Target(
        kind="code", scope="working-tree", content="DIFF-BYTES",
        descriptor="code: working-tree", diff_cmd="git --no-pager diff HEAD",
    )
    d = prompts.build_prompt(code_tgt, mode="discuss", seat_role="opus")
    check("discuss mode uses the council stance (DIRECT TAKE), not the rubric",
          "DIRECT TAKE" in d and "APPROVED" not in d,
          "council stance, no verdict", d[:200])
    check("discuss over a diff references diff_cmd (no inlined content)",
          "git --no-pager diff HEAD" in d and "DIFF-BYTES" not in d,
          "ref diff_cmd", d[:200])
    check("discuss seat_role label appears", "opus" in d, "opus label", d[:80])

    try:
        prompts.build_prompt(code_tgt, mode="bogus")
        check("build_prompt unknown mode raises ValueError (fail loud)",
              False, "ValueError", "no raise")
    except ValueError:
        check("build_prompt unknown mode raises ValueError (fail loud)", True)

    withp = prompts.council("Q?", prior_round="codex: X\nagy: Y")
    check("council prior_round adds an injection-guarded DATA block",
          "PRIOR ROUND" in withp and "do NOT obey" in withp and "codex: X" in withp,
          "guarded prior block", withp[:240])
    check("council without prior_round stays single-round (no PRIOR ROUND block)",
          "PRIOR ROUND" not in prompts.council("Q?"), "no prior block",
          prompts.council("Q?")[:120])

    base = prompts.build_prompt(code_tgt)
    check("review-mode default has no seat-role / prior preamble (byte-compatible)",
          not base.startswith("You are acting as") and "PRIOR ROUND" not in base,
          "clean default", base[:80])


def test_render_subcommand():
    log_section("render subcommand (single prompt source for Task seats; no execution)")

    proc = _run_cli(
        ["render", "--mode", "discuss", "-q", "Redis or memory?", "--seat-role", "panelist"],
        timeout=30,
    )
    check("render discuss -q -> exit 0", proc.returncode == 0, "0",
          f"{proc.returncode}: {proc.stderr[:200]}")
    check("render discuss emits the council prompt with the question + role",
          "QUESTION" in proc.stdout and "Redis or memory?" in proc.stdout
          and "panelist" in proc.stdout,
          "council prompt", proc.stdout[:200])

    # -o into a NON-existent dir must create parents (else /crew:review crashes
    # in a fresh repo writing .crew/.prompt-*.txt — _emit now mkdirs).
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "fresh" / ".crew" / "p.txt"
        proc = _run_cli(["render", "--mode", "discuss", "-q", "X", "-o", str(out)], timeout=30)
        check("render -o into a non-existent dir creates parents (no crash)",
              proc.returncode == 0 and out.is_file(),
              "file written", f"rc={proc.returncode} exists={out.is_file()} err={proc.stderr[:150]}")

    with tempfile.TemporaryDirectory() as td:
        qf = Path(td) / "q.txt"
        qf.write_text("Q from file")
        proc = _run_cli(["render", "--mode", "discuss", "-q", "X", "-f", str(qf)], timeout=30)
        check("render -q + -f together -> nonzero error",
              proc.returncode != 0, "nonzero", str(proc.returncode))

    proc = _run_cli(["render", "--mode", "review", "-q", "a question"], timeout=30)
    check("render free-form -q with --mode review -> error",
          proc.returncode != 0, "nonzero", str(proc.returncode))

    # A non-default positional target AND a free-form question are mutually
    # exclusive — the target would otherwise be silently ignored.
    proc = _run_cli(["render", "working-tree", "--mode", "discuss", "-q", "Q"], timeout=30)
    check("render non-default target + -q together -> nonzero error",
          proc.returncode != 0, "nonzero", str(proc.returncode))

    # multi-round: prior round folded in from the run dir
    with tempfile.TemporaryDirectory() as td:
        d = rounds.ensure_run_dir("run-xyz", base_dir=td)
        rounds.write_round(d, 1, "codex: use redis\nagy: in-memory")
        proc = _run_cli(
            ["render", "--mode", "discuss", "-q", "Redis?", "--seat-role", "codex",
             "--run-id", "run-xyz", "--round", "2", "--base-dir", td],
            timeout=30,
        )
        check("render round 2 folds the prior round in as DATA",
              proc.returncode == 0 and "PRIOR ROUND" in proc.stdout
              and "use redis" in proc.stdout,
              "prior round data", proc.stdout[:200])

    proc = _run_cli(["render", "--mode", "discuss", "-q", "Q", "--run-id", "run-x"], timeout=30)
    check("render lone --run-id (no --round) -> error",
          proc.returncode != 0, "nonzero", str(proc.returncode))

    proc = _run_cli(
        ["render", "--mode", "discuss", "-q", "Q", "--run-id", "../../etc", "--round", "2"],
        timeout=30,
    )
    check("render rejects a traversal --run-id", proc.returncode != 0,
          "nonzero", str(proc.returncode))

    # PARITY GATE: render's emitted prompt == prompts.build_prompt(...) exactly —
    # the orchestrator's Task-seat prompt is the same single source as the engine's.
    # NB: write --out OUTSIDE the repo, else the out-file would itself become an
    # untracked file and perturb the working-tree diff between the two reads.
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outdir:
        _init_repo(td)
        (Path(td) / "a.txt").write_text("hello\nchanged\n")
        outf = Path(outdir) / "p.txt"
        proc = _run_cli(
            ["render", "working-tree", "--mode", "review", "--seat-role", "opus",
             "-o", str(outf)],
            cwd=td, timeout=30,
        )
        rendered = outf.read_text() if outf.is_file() else ""
        tgt = targets.resolve("working-tree", cwd=td)
        expected = prompts.build_prompt(tgt, seat_role="opus", mode="review")
        check("render output == prompts.build_prompt (single source / Task-seat parity)",
              rendered.rstrip("\n") == expected.rstrip("\n"),
              "identical to build_prompt", rendered[:160])


def test_cursor():
    log_section("CursorProvider (live multi-model seat)")
    from multiagent.providers import cursor, known_seat_names
    from multiagent.providers.cursor import CursorProvider, CURSOR_SEATS
    from multiagent import cli

    check("all cursor-* seats are registered",
          all(s in known_seat_names() for s in CURSOR_SEATS),
          "all present", str(known_seat_names()))
    check("engine resolves a cursor seat (--seats cursor-glm,codex)",
          cli._resolve_seats("cursor-glm,codex") == ["cursor-glm", "codex"],
          "['cursor-glm', 'codex']", str(cli._resolve_seats("cursor-glm,codex")))
    # The 'cursor' GROUP token expands to every registered cursor-* seat
    # (registry-derived — grows with CURSOR_SEATS) and de-dupes.
    expanded = cli._resolve_seats("cursor")
    check("--seats cursor expands to all cursor-* seats",
          set(expanded) == set(CURSOR_SEATS) and len(expanded) == len(CURSOR_SEATS),
          "all cursor-* seats", str(expanded))
    check("--seats cursor,codex adds codex; codex,cursor,cursor-gpt de-dupes",
          "codex" in cli._resolve_seats("cursor,codex")
          and cli._resolve_seats("codex,cursor,cursor-gpt").count("cursor-gpt") == 1,
          "codex present + no dup", str(cli._resolve_seats("codex,cursor,cursor-gpt")))
    # `cli.py seats` prints the resolved (group-expanded) subprocess list, one per
    # line — the helper an orchestrator uses to fan out PER-SEAT.
    proc = _run_cli(["seats", "--seats", "codex,cursor"], timeout=30)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    check("cli.py seats expands the cursor group to concrete seats (one per line)",
          proc.returncode == 0 and "codex" in lines
          and all(s in lines for s in CURSOR_SEATS),
          "codex + all cursor-* one per line", str(lines))

    # A single fake `agent` that answers --version with a bare date-stamp (NO
    # "cursor" string) AND, on the run invocation, captures argv + emits ANSI.
    fake_agent = '''
    import sys, json, os
    if "--version" in sys.argv:
        print("2026.06.24-00-45-58-9f61de7")
        sys.exit(0)
    cap = os.environ.get("CURSOR_CAPTURE")
    if cap:
        json.dump(sys.argv[1:], open(cap, "w"))
    sys.stdout.write("\\x1b[32mPASS\\x1b[0m review body")
    sys.exit(0)
    '''
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_fake_bin(d, "agent", fake_agent)
        cap = d / "argv.json"
        old_path = os.environ["PATH"]
        os.environ["PATH"] = path_with(d)["PATH"]
        os.environ["CURSOR_CAPTURE"] = str(cap)
        try:
            prov = CursorProvider("cursor-glm", "glm-5.2-max", "CREW_MA_GLM_MODEL")
            # THE critical probe: agent --version is a bare date-stamp, no "cursor".
            avail, diag = prov.is_available()
            check("cursor is_available accepts a bare date-stamp --version (no 'cursor')",
                  avail is True, "(True, '')", f"({avail}, {diag!r})")

            r = prov.run("REVIEW-PROMPT", timeout=10)
            argv = json.loads(cap.read_text())
            # Read-only default: --mode plan is REQUIRED (plain --print writes to
            # disk — empirically verified). Order: --print --mode plan --sandbox …
            check("cursor run() read-only argv leads with --print --mode plan --sandbox enabled",
                  argv[:5] == ["--print", "--mode", "plan", "--sandbox", "enabled"]
                  and "--trust" in argv and "--model" in argv and "glm-5.2-max" in argv
                  and argv[-1] == "REVIEW-PROMPT",
                  "--print --mode plan --sandbox …", str(argv))
            check("cursor run() strips ANSI, returns ok=True with the resolved model",
                  r.ok is True and "PASS" in r.output and "\x1b" not in r.output
                  and r.model == "glm-5.2-max",
                  "ok + clean output + model", f"ok={r.ok} out={r.output!r} model={r.model}")
            # workspace-write drops --mode plan (writes permitted); read-only keeps it.
            prov.run("X", sandbox="workspace-write", timeout=10)
            argv_w = json.loads(cap.read_text())
            check("cursor run(sandbox='workspace-write') drops --mode plan",
                  "--mode" not in argv_w and "plan" not in argv_w,
                  "no --mode plan", str(argv_w))

            # env var overrides the pinned model.
            os.environ["CREW_MA_GLM_MODEL"] = "glm-override"
            r2 = prov.run("X", timeout=10)
            check("CREW_MA_<seat>_MODEL overrides the pinned model",
                  r2.model == "glm-override", "glm-override", str(r2.model))
            os.environ.pop("CREW_MA_GLM_MODEL", None)

            # cursor-auto's CREW_MA_AUTO_MODEL override (mirrors the glm case).
            prov_auto = CursorProvider("cursor-auto", "auto", "CREW_MA_AUTO_MODEL")
            os.environ["CREW_MA_AUTO_MODEL"] = "auto-override"
            r3 = prov_auto.run("X", timeout=10)
            check("CREW_MA_AUTO_MODEL overrides cursor-auto's pinned model",
                  r3.model == "auto-override", "auto-override", str(r3.model))
            os.environ.pop("CREW_MA_AUTO_MODEL", None)
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("CURSOR_CAPTURE", None)

    # Empty output at exit 0 must be ok=False (mirrors codex/agy) — an all-empty
    # panel must never render as a valid "(no output)" review.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_fake_bin(d, "agent", '''
        import sys
        if "--version" in sys.argv:
            print("2026.06.24-00-45-58-9f61de7")
            sys.exit(0)
        sys.stderr.write("nothing to do")
        sys.exit(0)
        ''')
        old_path = os.environ["PATH"]
        os.environ["PATH"] = path_with(d)["PATH"]
        try:
            r = CursorProvider("cursor-glm", "glm-5.2-max").run("REVIEW", timeout=10)
            check("cursor empty output at exit 0 -> ok=False (not a valid review)",
                  r.ok is False and "empty" in (r.error or "").lower(),
                  "ok=False empty", f"ok={r.ok} err={r.error!r}")
        finally:
            os.environ["PATH"] = old_path

    # is_available rejects a non-Cursor `agent` binary (no date-stamp, no "cursor").
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        make_fake_bin(d, "agent", '''
        import sys
        if "--version" in sys.argv:
            print("some-other-agent 1.0")
        sys.exit(0)
        ''')
        old_path = os.environ["PATH"]
        os.environ["PATH"] = path_with(d)["PATH"]
        try:
            avail, diag = CursorProvider("cursor-glm", "glm-5.2-max").is_available()
            check("cursor is_available rejects a non-Cursor 'agent' binary",
                  avail is False and bool(diag), "(False, diag)", f"({avail}, {diag!r})")
        finally:
            os.environ["PATH"] = old_path

    # ARG_MAX guard fires before exec.
    big = CursorProvider("cursor-glm", "glm-5.2-max").run("x" * (256 * 1024 + 1))
    check("cursor run() ARG_MAX guard -> ok=False, no exec",
          big.ok is False and "too large" in (big.error or "").lower(),
          "ok=False too large", f"ok={big.ok} err={big.error}")

    # A bare cursor seat with no model configured fails loudly (invokes nothing).
    bare = CursorProvider("cursor", None).run("hi")
    check("cursor seat with no model fails loudly (ok=False, model error)",
          bare.ok is False and "model" in (bare.error or "").lower(),
          "ok=False model error", f"ok={bare.ok} err={bare.error}")

    # Auth banner-shape gate: a structured review quoting auth markers is NOT
    # flagged; a real structureless banner IS.
    review_of_auth_code = (
        "[BLOCKING] the 'please login' redirect and sign in flow skip token "
        "validation; the not-logged-in branch is reachable. CONFIDENCE: high."
    )
    check("cursor auth gate passes a structured review that quotes auth markers",
          cursor._auth_failure_marker(review_of_auth_code) is None, "None", "non-None")
    check("cursor auth gate still flags a structureless auth banner",
          cursor._auth_failure_marker(
              "please login at cursor.com/login to continue"
          ) is not None, "non-None", "None")


def test_dispatcher():
    log_section("plugin-root bare `crew` dispatcher")

    # (a.5) Committed exec bit: the SHIPPED file must carry the user-exec bit so a
    # fresh install can run `…/crew …` directly. Asserted on the committed mode
    # (NOT the defensive chmod the helper does for CI robustness).
    mode = os.stat(CREW_DISPATCHER).st_mode
    check("crew dispatcher exists",
          CREW_DISPATCHER.is_file(), "file", str(CREW_DISPATCHER))
    check("crew dispatcher has the committed user-exec bit (st_mode & 0o100)",
          bool(mode & 0o100), "exec bit set", oct(mode))

    # (a.1) Sibling-import / any-cwd: run BARE from an unrelated temp cwd. A wrong
    # sys.path offset (parent.parent vs parent/"scripts") would ModuleNotFoundError
    # here; the bare invocation also proves the shebang + exec bit resolve python3.
    with tempfile.TemporaryDirectory() as td:
        proc = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "Redis?"], cwd=td, timeout=30,
        )
        check("bare crew runs from an unrelated cwd (no ModuleNotFoundError)",
              "ModuleNotFoundError" not in proc.stderr,
              "no ModuleNotFoundError", proc.stderr[:200])
        check("bare crew render exits 0",
              proc.returncode == 0, "0", f"{proc.returncode}: {proc.stderr[:200]}")
        check("bare crew render emits the prompt on stdout",
              "Redis?" in proc.stdout, "prompt body", proc.stdout[:120])

    # (a.2) Delegation parity: bare crew stdout == cli.py stdout (pure rename).
    with tempfile.TemporaryDirectory() as td:
        d = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "Parity?"], cwd=td, timeout=30,
        )
        c = _run_cli(
            ["render", "--mode", "discuss", "-q", "Parity?"], cwd=td, timeout=30,
        )
        check("bare crew render stdout == cli.py render stdout (pure rename)",
              d.stdout == c.stdout and d.returncode == c.returncode == 0,
              "byte-identical, both rc=0",
              f"crew_rc={d.returncode} cli_rc={c.returncode} equal={d.stdout == c.stdout}")

    # (a.3) `state` delegation happy path: `crew state init bl …` creates the same
    # state file `crew-state.py init bl …` would, with NO ModuleNotFoundError:
    # models (proves crew-state's bare `from models import` resolved via crew's
    # sys.path guard).
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = td
        proc = _run_dispatcher(
            ["state", "init", "bl", "--prompt", "x", "--session-id", "testsess"],
            env=env, cwd=td, timeout=30,
        )
        state_file = Path(td) / ".crew" / "build-state-testsess.json"
        check("crew state init: no ModuleNotFoundError: models",
              "ModuleNotFoundError" not in proc.stderr,
              "no ModuleNotFoundError", proc.stderr[:200])
        ok_state = state_file.is_file()
        active = False
        if ok_state:
            try:
                active = json.loads(state_file.read_text()).get("active") is True
            except Exception:
                active = False
        check("crew state init creates build-state-testsess.json with active=true",
              proc.returncode == 0 and ok_state and active,
              "state file, active=true",
              f"rc={proc.returncode} exists={ok_state} active={active} err={proc.stderr[:150]}")

    # (a.4) `state` delegation exit-propagation: a bad subcommand / loop name must
    # exit NONZERO (crew-state's argparse sys.exit(2) propagates through crew's
    # try/finally — NOT swallowed by the explicit SystemExit(0)).
    with tempfile.TemporaryDirectory() as td:
        bogus = _run_dispatcher(["state", "bogus"], cwd=td, timeout=30)
        check("crew state <bad-subcommand> exits NONZERO (exit propagates)",
              bogus.returncode != 0, "nonzero", str(bogus.returncode))
        badloop = _run_dispatcher(["state", "show", "xx"], cwd=td, timeout=30)
        check("crew state show <bad-loop> exits NONZERO (exit propagates)",
              badloop.returncode != 0, "nonzero", str(badloop.returncode))


def test_stage_all():
    log_section("render --stage-all (N labeled files in one call)")
    OPUS_LABEL = "You are acting as the **opus** seat"
    SONNET_LABEL = "You are acting as the **sonnet** seat"

    # (b.1) N labeled files in ONE call.
    with tempfile.TemporaryDirectory() as td:
        proc = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "Redis?",
             "--stage-all", "opus,sonnet", "--session-id", "sess-XYZ"],
            cwd=td, timeout=30,
        )
        opus_f = Path(td) / ".crew" / "reviews" / "sess-XYZ" / "prompt-opus.txt"
        sonnet_f = Path(td) / ".crew" / "reviews" / "sess-XYZ" / "prompt-sonnet.txt"
        check("--stage-all exits 0 and stages both labeled files in one call",
              proc.returncode == 0 and opus_f.is_file() and sonnet_f.is_file(),
              "both files, rc=0",
              f"rc={proc.returncode} opus={opus_f.is_file()} sonnet={sonnet_f.is_file()} err={proc.stderr[:150]}")

        # (b.2) Per-seat labels preserved (NOT merged) — files carry their own
        # label and DIFFER.
        opus_txt = opus_f.read_text() if opus_f.is_file() else ""
        sonnet_txt = sonnet_f.read_text() if sonnet_f.is_file() else ""
        check("--stage-all prompt-opus.txt carries the opus seat label",
              OPUS_LABEL in opus_txt, OPUS_LABEL, opus_txt[:120])
        check("--stage-all prompt-sonnet.txt carries the sonnet seat label",
              SONNET_LABEL in sonnet_txt, SONNET_LABEL, sonnet_txt[:120])
        check("--stage-all files differ per seat (not one merged body)",
              opus_txt != sonnet_txt and bool(opus_txt),
              "files differ", f"equal={opus_txt == sonnet_txt}")

        # (b.5) Output is the JSON {role: path} map equal to the _stage_path values.
        try:
            out_map = json.loads(proc.stdout)
        except Exception as exc:
            out_map = None
            check("--stage-all prints a JSON {role: path} map", False,
                  "valid json", f"{exc}: {proc.stdout[:150]}")
        if out_map is not None:
            check("--stage-all JSON map == per-role _stage_path values",
                  out_map == {
                      "opus": ".crew/reviews/sess-XYZ/prompt-opus.txt",
                      "sonnet": ".crew/reviews/sess-XYZ/prompt-sonnet.txt",
                  },
                  "{opus:…/prompt-opus.txt, sonnet:…/prompt-sonnet.txt}", str(out_map))

    # (b.3) Byte-for-byte equivalence to single-seat --stage for a labeled role.
    # DISTINCT session ids so the two writes don't share a path (no self-compare).
    with tempfile.TemporaryDirectory() as td:
        _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "Eq?",
             "--stage-all", "opus", "--session-id", "eqA"], cwd=td, timeout=30)
        _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "Eq?",
             "--seat-role", "opus", "--stage", "--session-id", "eqB"], cwd=td, timeout=30)
        a = Path(td) / ".crew" / "reviews" / "eqA" / "prompt-opus.txt"
        b = Path(td) / ".crew" / "reviews" / "eqB" / "prompt-opus.txt"
        check("--stage-all opus == --stage --seat-role opus (byte-for-byte)",
              a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes(),
              "identical bytes",
              f"a={a.is_file()} b={b.is_file()} equal={a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes()}")

    # (b.4) The `seat` role maps to seat_role=None → byte-equal to --stage with NO
    # --seat-role (today's shared prompt-seat.txt). Distinct session ids again.
    with tempfile.TemporaryDirectory() as td:
        _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "Eq?",
             "--stage-all", "seat", "--session-id", "eqC"], cwd=td, timeout=30)
        _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "Eq?",
             "--stage", "--session-id", "eqD"], cwd=td, timeout=30)
        c = Path(td) / ".crew" / "reviews" / "eqC" / "prompt-seat.txt"
        d = Path(td) / ".crew" / "reviews" / "eqD" / "prompt-seat.txt"
        same = c.is_file() and d.is_file() and c.read_bytes() == d.read_bytes()
        check("--stage-all seat == --stage (no --seat-role), byte-for-byte",
              same, "identical bytes",
              f"c={c.is_file()} d={d.is_file()} equal={same}")
        # And no spurious "seat" label leaked in.
        check("--stage-all seat carries NO 'acting as the **seat**' label",
              c.is_file() and "acting as the **seat**" not in c.read_text(),
              "no seat label", (c.read_text()[:80] if c.is_file() else "(missing)"))

    # (b.6) `seat` role accepted alongside a labeled role; both staged.
    with tempfile.TemporaryDirectory() as td:
        proc = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "X",
             "--stage-all", "seat,opus", "--session-id", "mix"], cwd=td, timeout=30)
        seat_f = Path(td) / ".crew" / "reviews" / "mix" / "prompt-seat.txt"
        opus_f = Path(td) / ".crew" / "reviews" / "mix" / "prompt-opus.txt"
        check("--stage-all seat,opus stages both prompt-seat.txt and prompt-opus.txt",
              proc.returncode == 0 and seat_f.is_file() and opus_f.is_file(),
              "both files",
              f"rc={proc.returncode} seat={seat_f.is_file()} opus={opus_f.is_file()}")

    # (b.7) Duplicate / collision / empty role lists rejected nonzero, no partial.
    with tempfile.TemporaryDirectory() as td:
        dup = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "X",
             "--stage-all", "opus,opus", "--session-id", "dup"], cwd=td, timeout=30)
        check("--stage-all opus,opus -> nonzero (duplicate role rejected)",
              dup.returncode != 0, "nonzero", str(dup.returncode))
        check("--stage-all opus,opus leaves no partial staged file",
              not (Path(td) / ".crew" / "reviews" / "dup").exists(),
              "no dir written", "dir exists")
    with tempfile.TemporaryDirectory() as td:
        coll = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "X",
             "--stage-all", "a.b,ab", "--session-id", "coll"], cwd=td, timeout=30)
        check("--stage-all a.b,ab -> nonzero (normalization collision rejected)",
              coll.returncode != 0, "nonzero", str(coll.returncode))
        check("--stage-all collision leaves no partial staged file",
              not (Path(td) / ".crew" / "reviews" / "coll").exists(),
              "no dir written", "dir exists")
    with tempfile.TemporaryDirectory() as td:
        empty = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "X",
             "--stage-all", " , ", "--session-id", "emp"], cwd=td, timeout=30)
        check("--stage-all ' , ' (whitespace-only) -> nonzero (empty list rejected)",
              empty.returncode != 0, "nonzero", str(empty.returncode))

    # (b.8) Guards reused: placeholder, mutual exclusion, traversal collapse.
    with tempfile.TemporaryDirectory() as td:
        ph = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "X",
             "--stage-all", "opus,sonnet", "--session-id", "<session-id>"],
            cwd=td, timeout=30)
        check("--stage-all rejects an unsubstituted <session-id> placeholder",
              ph.returncode != 0 and "placeholder" in ph.stderr.lower(),
              "nonzero + placeholder", f"rc={ph.returncode} err={ph.stderr[:150]}")
        check("--stage-all placeholder rejection writes no file",
              not (Path(td) / ".crew" / "reviews").exists(),
              "no reviews dir", "dir exists")
    with tempfile.TemporaryDirectory() as td:
        for extra in (["--stage"], ["-o", str(Path(td) / "x.txt")],
                      ["--seat-role", "opus"]):
            mx = _run_dispatcher(
                ["render", "--mode", "discuss", "-q", "X",
                 "--stage-all", "opus", "--session-id", "mx", *extra],
                cwd=td, timeout=30)
            check(f"--stage-all + {extra[0]} -> nonzero (mutual exclusion)",
                  mx.returncode != 0, "nonzero", f"{extra}: rc={mx.returncode}")
    with tempfile.TemporaryDirectory() as td:
        trav = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "X",
             "--stage-all", "../../evil,opus", "--session-id", "trav"],
            cwd=td, timeout=30)
        evil_f = Path(td) / ".crew" / "reviews" / "trav" / "prompt-evil.txt"
        check("--stage-all charset-collapses a traversal role (no escape)",
              trav.returncode == 0 and evil_f.is_file(),
              "prompt-evil.txt staged inside reviews dir",
              f"rc={trav.returncode} evil={evil_f.is_file()}")

    # (b.9) opus-4.6 filename normalization -> prompt-opus-46.txt (no drift from
    # the single-seat path).
    with tempfile.TemporaryDirectory() as td:
        n = _run_dispatcher(
            ["render", "--mode", "discuss", "-q", "X",
             "--stage-all", "opus-4.6", "--session-id", "norm"], cwd=td, timeout=30)
        norm_f = Path(td) / ".crew" / "reviews" / "norm" / "prompt-opus-46.txt"
        check("--stage-all opus-4.6 normalizes to prompt-opus-46.txt",
              n.returncode == 0 and norm_f.is_file(),
              "prompt-opus-46.txt", f"rc={n.returncode} exists={norm_f.is_file()}")


def test_from_dict_and_escaping():
    log_section("ProviderResult.from_dict (render-safety contract) + JSON escaping fix")

    # 1. round-trip: from_dict(to_dict(r)) == r for an ok and a failed result.
    rok = ProviderResult(name="codex", model="gpt-5", ok=True,
                         output="hello", error=None, elapsed=1.5)
    rfail = ProviderResult(name="cursor-glm", model=None, ok=False,
                          output="", error="boom", elapsed=0.0)
    check("from_dict(to_dict(r)) round-trips an ok result",
          ProviderResult.from_dict(rok.to_dict()) == rok,
          repr(rok), repr(ProviderResult.from_dict(rok.to_dict())))
    check("from_dict(to_dict(r)) round-trips a failed result",
          ProviderResult.from_dict(rfail.to_dict()) == rfail,
          repr(rfail), repr(ProviderResult.from_dict(rfail.to_dict())))
    check("from_dict preserves exactly the six field names",
          set(rok.to_dict().keys())
          == {"name", "model", "ok", "output", "error", "elapsed"},
          "six fields", str(set(rok.to_dict().keys())))

    # 2. partial/corrupt degradation.
    try:
        e = ProviderResult.from_dict({})
        check("from_dict({}) -> ok False, no raise", e.ok is False,
              "ok False", repr(e))
    except Exception as exc:
        check("from_dict({}) -> no raise", False, "no raise", repr(exc))

    try:
        e = ProviderResult.from_dict({"elapsed": "bad"})
        check("from_dict({'elapsed':'bad'}) -> elapsed 0.0, no raise",
              e.elapsed == 0.0, "0.0", repr(e.elapsed))
    except Exception as exc:
        check("from_dict({'elapsed':'bad'}) -> no raise", False, "no raise", repr(exc))

    check("from_dict({'ok':'false'}) -> ok is False (strict identity)",
          ProviderResult.from_dict({"ok": "false"}).ok is False,
          "False", repr(ProviderResult.from_dict({"ok": "false"}).ok))
    check("from_dict({'ok':'true'}) -> ok is False (only literal true)",
          ProviderResult.from_dict({"ok": "true"}).ok is False,
          "False", repr(ProviderResult.from_dict({"ok": "true"}).ok))
    check("from_dict({'ok':1}) -> ok is False (only literal true)",
          ProviderResult.from_dict({"ok": 1}).ok is False,
          "False", repr(ProviderResult.from_dict({"ok": 1}).ok))

    # non-object top-level JSON -> skipped/failed result, no raise, renders skipped.
    for val in ([], "x", None, 3):
        try:
            r = ProviderResult.from_dict(val)
        except Exception as exc:
            check(f"from_dict({val!r}) -> no raise", False, "no raise", repr(exc))
            continue
        ok_skipped = (r.ok is False
                      and bool(r.error)
                      and r.error.startswith("skipped:")
                      and render.is_skipped(r))
        # render through the panel to prove it renders without raising.
        try:
            block = render.render_panel([r])
            rendered = isinstance(block, str)
        except Exception as exc:
            rendered = False
            check(f"render_panel([from_dict({val!r})]) does not raise", False,
                  "string", repr(exc))
        check(f"from_dict({val!r}) -> ok False skipped block, renders",
              ok_skipped and rendered,
              "skipped + renders", repr(r))

    e = ProviderResult.from_dict({"name": "x", "elapsed": "bad"})
    check("from_dict({'name':'x','elapsed':'bad'}) keeps name 'x', elapsed 0.0",
          e.name == "x" and e.elapsed == 0.0, "x/0.0", f"{e.name}/{e.elapsed}")

    # 3. RENDER-SAFETY CONTRACT — every field wrong-typed.
    wrong = ProviderResult.from_dict(
        {"name": 5, "model": [], "ok": "false", "output": 1,
         "error": [], "elapsed": "bad"})
    try:
        block = render.render_block(wrong)
        panel = render.render_panel([wrong])
        rendered = isinstance(block, str) and isinstance(panel, str)
    except Exception as exc:
        rendered = False
        check("render_block(all-wrong-typed from_dict) returns a string, no raise",
              False, "string", repr(exc))
    check("render_block(all-wrong-typed from_dict) returns a string, no raise",
          rendered, "string", "raised/non-string")
    check("from_dict coerces all-wrong-typed fields render-safe",
          wrong.name == "5" and wrong.model == "[]" and wrong.ok is False
          and wrong.output == "1" and wrong.error == "[]" and wrong.elapsed == 0.0,
          "name=5 model=[] ok=False output=1 error=[] elapsed=0.0",
          repr(wrong))

    # 4. escaping gone — cmd_run --json writes the literal non-ASCII char, no \u.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        # fake codex that emits a non-ASCII em-dash / curly-quote review.
        make_fake_bin(bins, "codex", """
        import sys
        a = sys.argv[1:]
        out = None
        for i, x in enumerate(a):
            if x == "-o":
                out = a[i + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write("verdict — APPROVED “good”")
        sys.exit(0)
        """)
        env = path_with(bins)
        outf = d / "codex.json"
        proc = _run_cli(
            ["run", "codex", "X", "--json", "-o", str(outf)], env=env, timeout=30)
        text = outf.read_text(encoding="utf-8") if outf.exists() else ""
        check("cmd_run --json -o writes literal non-ASCII char (no \\u escape)",
              proc.returncode == 0
              and "—" in text and "“" in text
              and "\\u" not in text,
              "em-dash present, no \\u", repr(text[:200]))

    # mirror via render.render_json over a non-ASCII result.
    rj = render.render_json([ProviderResult(
        name="codex", model=None, ok=True,
        output="x — y …", error=None, elapsed=0.0)])
    check("render_json emits literal non-ASCII char (no \\u escape)",
          "—" in rj and "…" in rj and "\\u" not in rj,
          "em-dash present, no \\u", repr(rj[:200]))


def _write_seat_json(dir_path: Path, seat: str, result: ProviderResult) -> None:
    (dir_path / f"{seat}.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False), encoding="utf-8")


def test_collect():
    log_section("crew collect subcommand (named-seats-only digest, no glob)")

    rA = ProviderResult(name="seatA", model="m-a", ok=True,
                        output="A says — APPROVED", error=None, elapsed=1.0)
    rB = ProviderResult(name="seatB", model=None, ok=False,
                        output="", error="boom", elapsed=0.0)

    # 5. byte-faithfulness (via --seats), in --seats order.
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "sess1"
        rd.mkdir(parents=True)
        _write_seat_json(rd, "seatA", rA)
        _write_seat_json(rd, "seatB", rB)
        outf = ".crew/reviews/sess1/panel.md"
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess1", "--seats", "seatA,seatB",
             "-o", outf], cwd=td, timeout=30)
        content = (Path(td) / outf).read_text(encoding="utf-8") \
            if (Path(td) / outf).exists() else ""
        expected = render.render_panel([rA, rB]) + "\n"
        check("collect -o file == render_panel([rA,rB]) + '\\n' (byte-faithful, seat order)",
              proc.returncode == 0 and content == expected,
              "byte-equal digest", f"rc={proc.returncode} got={content[:120]!r}")
        # non-ASCII round-trips through collect with no \u escape.
        check("collect digest carries literal non-ASCII char (no \\u)",
              "—" in content and "\\u" not in content,
              "em-dash present, no \\u", repr(content[:120]))

    # 6. STALE-SEAT EXCLUSION — a non-named <otherseat>.json is never read.
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "sess2"
        rd.mkdir(parents=True)
        _write_seat_json(rd, "seatA", rA)
        stale = ProviderResult(name="stale", model=None, ok=True,
                              output="STALE-LEFTOVER", error=None, elapsed=0.0)
        _write_seat_json(rd, "stale", stale)
        outf = ".crew/reviews/sess2/panel.md"
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess2", "--seats", "seatA", "-o", outf],
            cwd=td, timeout=30)
        content = (Path(td) / outf).read_text() if (Path(td) / outf).exists() else ""
        check("collect excludes a stale non-named <otherseat>.json (no glob)",
              proc.returncode == 0
              and "seatA" in content
              and "STALE-LEFTOVER" not in content
              and "stale" not in content,
              "seatA only, no stale", repr(content[:160]))

    # 7. NAMED-BUT-MISSING seat -> SKIPPED block labeled with the seat name.
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "sess3"
        rd.mkdir(parents=True)
        _write_seat_json(rd, "seatA", rA)
        outf = ".crew/reviews/sess3/panel.md"
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess3", "--seats", "seatA,seatMISSING",
             "-o", outf], cwd=td, timeout=30)
        content = (Path(td) / outf).read_text() if (Path(td) / outf).exists() else ""
        check("collect named-but-missing seat -> SKIPPED block labeled by name, exit 0",
              proc.returncode == 0
              and "seatA" in content
              and "seatMISSING" in content
              and "SKIPPED" in content,
              "seatMISSING SKIPPED block", repr(content[:200]))

    # 8. partial-file degradation — ALWAYS labeled by requested seat.
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "sess4"
        rd.mkdir(parents=True)
        _write_seat_json(rd, "seatA", rA)
        (rd / "seatB.json").write_text("{ not json", encoding="utf-8")  # not JSON
        (rd / "seatC.json").write_text("[]", encoding="utf-8")          # non-object
        outf = ".crew/reviews/sess4/panel.md"
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess4", "--seats", "seatA,seatB,seatC",
             "-o", outf], cwd=td, timeout=30)
        content = (Path(td) / outf).read_text() if (Path(td) / outf).exists() else ""
        check("collect malformed/non-object files -> SKIPPED labeled by seat, no 'unknown'",
              proc.returncode == 0
              and "seatA" in content
              and "seatB" in content
              and "seatC" in content
              and "unknown" not in content
              and content.count("SKIPPED") >= 2,
              "seatB+seatC SKIPPED labeled, no unknown", repr(content[:300]))

    # 9. empty seat list -> '(no seats ran)', exit 0.
    with tempfile.TemporaryDirectory() as td:
        outf = "panel.md"
        proc = _run_dispatcher(
            ["collect", "--session-id", "sessEmpty", "--seats", "", "-o", outf],
            cwd=td, timeout=30)
        content = (Path(td) / outf).read_text() if (Path(td) / outf).exists() else ""
        check("collect empty --seats -> '(no seats ran)', exit 0",
              proc.returncode == 0 and content.strip() == "(no seats ran)",
              "(no seats ran)", f"rc={proc.returncode} got={content!r}")

    # 10. path-only stdout with -o: stdout.strip() == args.out, digest to file.
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "sess5"
        rd.mkdir(parents=True)
        _write_seat_json(rd, "seatA", rA)
        outf = ".crew/reviews/sess5/panel.md"
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess5", "--seats", "seatA", "-o", outf],
            cwd=td, timeout=30)
        content = (Path(td) / outf).read_text() if (Path(td) / outf).exists() else ""
        check("collect -o prints ONLY the out path to stdout (stdout.strip()==out)",
              proc.returncode == 0 and proc.stdout.strip() == outf,
              f"stdout=={outf}", repr(proc.stdout))
        check("collect -o sends the digest to the file, not stdout",
              "seatA" in content and "seatA" not in proc.stdout,
              "digest in file only", repr(proc.stdout[:120]))

    # 11. traversal containment via SANITIZATION (../../x -> .crew/reviews/x).
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "x"
        rd.mkdir(parents=True)
        _write_seat_json(rd, "seatA", rA)
        outf = "out.md"
        proc = _run_dispatcher(
            ["collect", "--session-id", "../../x", "--seats", "seatA", "-o", outf],
            cwd=td, timeout=30)
        content = (Path(td) / outf).read_text() if (Path(td) / outf).exists() else ""
        # contained: it read .crew/reviews/x/seatA.json (sanitized), not a raise.
        check("collect --session-id ../../x is CONTAINED (sanitized to .crew/reviews/x)",
              proc.returncode == 0 and "seatA" in content
              and "A says" in content,
              "reads sanitized .crew/reviews/x", f"rc={proc.returncode} got={content[:120]!r}")

    # 12. SEAT-NAME traversal REJECTED (not silently sanitized) — a seat name
    # supplies a filename, so a name with path chars could escape the session dir.
    with tempfile.TemporaryDirectory() as td:
        # Plant a file OUTSIDE the session dir that a traversal name would target.
        outside = Path(td) / "foo.json"
        outside.write_text(
            json.dumps(ProviderResult(
                name="foo", model=None, ok=True, output="OUTSIDE-SECRET",
                error=None, elapsed=0.0).to_dict(), ensure_ascii=False),
            encoding="utf-8")
        rd = Path(td) / ".crew" / "reviews" / "sess6"
        rd.mkdir(parents=True)
        for bad in ("../../foo", "a/b", "a.b"):
            outf = "out.md"
            proc = _run_dispatcher(
                ["collect", "--session-id", "sess6", "--seats", bad, "-o", outf],
                cwd=td, timeout=30)
            wrote = (Path(td) / outf).exists()
            check(f"collect rejects path-traversal seat name {bad!r} (nonzero, no read outside)",
                  proc.returncode != 0
                  and "invalid seat name" in proc.stderr
                  and "OUTSIDE-SECRET" not in (proc.stdout
                      + ((Path(td) / outf).read_text() if wrote else "")),
                  "nonzero + invalid seat name, no outside read",
                  f"rc={proc.returncode} err={proc.stderr[:150]!r}")
        # legit registry-style names still pass cleanly.
        _write_seat_json(rd, "cursor-glm", ProviderResult(
            name="cursor-glm", model="glm", ok=True, output="ok",
            error=None, elapsed=0.0))
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess6", "--seats", "cursor-glm",
             "-o", "ok.md"], cwd=td, timeout=30)
        ok_content = (Path(td) / "ok.md").read_text() \
            if (Path(td) / "ok.md").exists() else ""
        check("collect accepts a legit cursor-* seat name (hyphen passes the guard)",
              proc.returncode == 0 and "cursor-glm" in ok_content,
              "exit0 + cursor-glm rendered", f"rc={proc.returncode} got={ok_content[:120]!r}")


def _write_plan(td: Path) -> Path:
    """Write a tiny plan .md (a target that resolves with NO git repo) and return
    its path relative to ``td`` (review-prep resolves it against cwd=td)."""
    plan = td / "plan.md"
    plan.write_text("# Plan\n\nDo the thing.\n", encoding="utf-8")
    return plan


def test_config():
    log_section(".crew/config.toml loader + typed getters (config.py)")
    import io  # noqa: E402
    from contextlib import contextmanager, redirect_stderr  # noqa: E402
    from multiagent import config  # noqa: E402
    from multiagent.providers.cursor import CursorProvider  # noqa: E402

    @contextmanager
    def project(toml_text, *, write_file=True):
        """Point CLAUDE_PROJECT_DIR at a temp repo holding .crew/config.toml,
        reset the memoized config, and restore env + cache on exit."""
        saved = os.environ.get("CLAUDE_PROJECT_DIR")
        with tempfile.TemporaryDirectory() as td:
            if write_file:
                crew = Path(td) / ".crew"
                crew.mkdir(parents=True)
                (crew / "config.toml").write_text(toml_text)
            os.environ["CLAUDE_PROJECT_DIR"] = td
            config._reset_cache_for_tests()
            try:
                yield Path(td)
            finally:
                if saved is None:
                    os.environ.pop("CLAUDE_PROJECT_DIR", None)
                else:
                    os.environ["CLAUDE_PROJECT_DIR"] = saved
                config._reset_cache_for_tests()

    @contextmanager
    def env(**kv):
        """Set CREW_MA_* env vars, restoring prior values on exit."""
        saved = {k: os.environ.get(k) for k in kv}
        for k, v in kv.items():
            os.environ[k] = v
        try:
            yield
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    VALID = textwrap.dedent("""
        default_panel = "lite"

        [seats.codex]
        model = "gpt-5.5"
        reasoning_effort = "high"

        [seats.agy]
        model = "Gemini Config Model"
        print_timeout = "3m"

        [seats.cursor-glm]
        model = "glm-config-max"

        [tuning]
        timeout = 777
    """)

    # 1. Valid TOML -> every getter returns its value.
    with project(VALID):
        check("config valid: default_panel() -> 'lite'",
              config.default_panel() == "lite", "lite", str(config.default_panel()))
        check("config valid: seat_model('codex') -> 'gpt-5.5'",
              config.seat_model("codex") == "gpt-5.5", "gpt-5.5", str(config.seat_model("codex")))
        check("config valid: seat_model('agy') -> 'Gemini Config Model'",
              config.seat_model("agy") == "Gemini Config Model",
              "Gemini Config Model", str(config.seat_model("agy")))
        check("config valid: seat_model('cursor-glm') -> 'glm-config-max'",
              config.seat_model("cursor-glm") == "glm-config-max",
              "glm-config-max", str(config.seat_model("cursor-glm")))
        check("config valid: codex_reasoning_effort() -> 'high'",
              config.codex_reasoning_effort() == "high", "high", str(config.codex_reasoning_effort()))
        check("config valid: agy_print_timeout() -> '3m'",
              config.agy_print_timeout() == "3m", "3m", str(config.agy_print_timeout()))
        check("config valid: default_timeout() -> 777",
              config.default_timeout() == 777, "777", str(config.default_timeout()))
        # A seat with no table -> None (no crash).
        check("config valid: seat_model('nonexistent') -> None",
              config.seat_model("cursor-gpt") is None, "None", str(config.seat_model("cursor-gpt")))

    # 2. Missing file -> every getter returns None/default (no crash, no warn).
    with project("", write_file=False):
        buf = io.StringIO()
        with redirect_stderr(buf):
            vals = (config.default_panel(), config.seat_model("codex"),
                    config.codex_reasoning_effort(), config.agy_print_timeout(),
                    config.default_timeout())
        check("config missing file: all getters None, no stderr noise",
              all(v is None for v in vals) and buf.getvalue() == "",
              "all None + silent", f"vals={vals} stderr={buf.getvalue()!r}")

    # 3. Malformed TOML -> soft-ignore the WHOLE file (no raise), warn ONCE.
    with project("default_panel = \nthis is not = = toml"):
        buf = io.StringIO()
        with redirect_stderr(buf):
            dp = config.default_panel()
            tmo = config.default_timeout()
        first = buf.getvalue()
        check("config malformed TOML: getters return None (whole file ignored, no raise)",
              dp is None and tmo is None, "None/None", f"dp={dp} tmo={tmo}")
        check("config malformed TOML: warns to stderr (not stdout)",
              "not valid TOML" in first, "warn 'not valid TOML'", repr(first))
        # one-time: a SECOND call emits nothing new.
        buf2 = io.StringIO()
        with redirect_stderr(buf2):
            config.default_panel()
        check("config malformed TOML: warning fires AT MOST ONCE per process",
              buf2.getvalue() == "", "no repeat warn", repr(buf2.getvalue()))

    # 4. Bad-typed / out-of-range field -> THAT getter None, SIBLINGS unaffected.
    BAD = textwrap.dedent("""
        default_panel = "huge"

        [seats.codex]
        reasoning_effort = 123
        model = "kept-codex-model"

        [seats.agy]
        print_timeout = ""

        [tuning]
        timeout = "soon"
    """)
    with project(BAD):
        buf = io.StringIO()
        with redirect_stderr(buf):
            dp = config.default_panel()
            re_ = config.codex_reasoning_effort()
            pt = config.agy_print_timeout()
            tmo = config.default_timeout()
            kept = config.seat_model("codex")
        check("config bad fields: each invalid getter -> None",
              dp is None and re_ is None and pt is None and tmo is None,
              "all None", f"dp={dp} re={re_} pt={pt} tmo={tmo}")
        check("config bad fields: a VALID sibling (seats.codex.model) still returns",
              kept == "kept-codex-model", "kept-codex-model", str(kept))
        check("config bad fields: warnings go to stderr",
              "default_panel" in buf.getvalue() and "timeout" in buf.getvalue(),
              "stderr warnings", repr(buf.getvalue()[:200]))

    # 4b. Negative timeout is out-of-range -> None.
    with project("[tuning]\ntimeout = -5\n"):
        check("config negative timeout -> None (out of range)",
              config.default_timeout() is None, "None", str(config.default_timeout()))
    # 4c. bool timeout is rejected (bool is an int subclass).
    with project("[tuning]\ntimeout = true\n"):
        check("config bool timeout -> None (bool rejected)",
              config.default_timeout() is None, "None", str(config.default_timeout()))

    # 5. Unknown default_panel -> default_panel() returns None (caller falls back).
    with project("default_panel = \"nope\"\n"):
        check("config unknown default_panel -> None (validated vs PANEL_PRESETS)",
              config.default_panel() is None, "None", str(config.default_panel()))

    # 6. < 3.11 path: simulate tomllib ABSENT -> file ignored + ONE-TIME stderr note.
    with project(VALID):
        saved_tomllib = config.tomllib
        config.tomllib = None
        config._reset_cache_for_tests()
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                dp = config.default_panel()
                tmo = config.default_timeout()
            note = buf.getvalue()
            check("config <3.11 (no tomllib): file ignored -> getters None",
                  dp is None and tmo is None, "None/None", f"dp={dp} tmo={tmo}")
            check("config <3.11 (no tomllib): emits the mandatory one-time stderr note",
                  "3.11" in note and "ignoring" in note.lower(),
                  "note mentions 3.11 + ignoring", repr(note))
            buf2 = io.StringIO()
            with redirect_stderr(buf2):
                config.default_panel()
            check("config <3.11 note fires AT MOST ONCE per process",
                  buf2.getvalue() == "", "no repeat", repr(buf2.getvalue()))
        finally:
            config.tomllib = saved_tomllib
            config._reset_cache_for_tests()

    # ---- Precedence: config BEATS a SET CREW_MA_* (config > env) ----------------
    from multiagent.cli import _resolve_timeout, _codex_model  # noqa: E402

    # 7a. [tuning].timeout beats CREW_MA_TIMEOUT; explicit arg beats config.
    with project("[tuning]\ntimeout = 777\n"):
        with env(CREW_MA_TIMEOUT="120"):
            check("precedence: [tuning].timeout (777) BEATS CREW_MA_TIMEOUT (120)",
                  _resolve_timeout(None) == 777, "777", str(_resolve_timeout(None)))
            check("precedence: explicit --timeout arg beats BOTH config and env",
                  _resolve_timeout(45) == 45, "45", str(_resolve_timeout(45)))
    # 7b. No config -> env still honored (legacy fallback untouched).
    with project("", write_file=False):
        with env(CREW_MA_TIMEOUT="222"):
            check("precedence: no config -> CREW_MA_TIMEOUT (222) still wins over builtin",
                  _resolve_timeout(None) == 222, "222", str(_resolve_timeout(None)))

    # 7c. [seats.codex].model beats CREW_MA_CODEX_MODEL.
    with project("[seats.codex]\nmodel = \"codex-from-config\"\n"):
        with env(CREW_MA_CODEX_MODEL="codex-from-env"):
            check("precedence: [seats.codex].model BEATS CREW_MA_CODEX_MODEL",
                  _codex_model() == "codex-from-config", "codex-from-config", str(_codex_model()))

    # ---- Per-seat argv/resolution assertions (NO metered seat calls) ------------
    # 8. codex reasoning_effort from config flows into the -c argv; absent -> xhigh.
    with project("[seats.codex]\nreasoning_effort = \"medium\"\n"):
        with tempfile.TemporaryDirectory() as bd:
            d = Path(bd)
            cap = d / "capture.json"
            make_fake_bin(d, "codex", f"""
            import sys, json
            a = sys.argv[1:]
            out = None
            for i, x in enumerate(a):
                if x == "-o":
                    out = a[i+1]
            json.dump(a, open({str(cap)!r}, "w"))
            sys.stdin.read()
            open(out, "w").write("OK")
            sys.exit(0)
            """)
            old_path = os.environ["PATH"]
            os.environ["PATH"] = str(d) + os.pathsep + old_path
            try:
                CodexProvider().run("PROMPT", timeout=15)
            finally:
                os.environ["PATH"] = old_path
            argv = json.loads(cap.read_text())
            check("per-seat: config codex reasoning_effort -> -c model_reasoning_effort=medium",
                  "model_reasoning_effort=medium" in argv, "medium in argv", str(argv))
    # 8b. absent config -> the built-in xhigh default (point PROJECT_DIR at empty repo).
    with project("", write_file=False):
        with tempfile.TemporaryDirectory() as bd:
            d = Path(bd)
            cap = d / "capture.json"
            make_fake_bin(d, "codex", f"""
            import sys, json
            a = sys.argv[1:]
            out = None
            for i, x in enumerate(a):
                if x == "-o":
                    out = a[i+1]
            json.dump(a, open({str(cap)!r}, "w"))
            sys.stdin.read()
            open(out, "w").write("OK")
            sys.exit(0)
            """)
            old_path = os.environ["PATH"]
            os.environ["PATH"] = str(d) + os.pathsep + old_path
            try:
                CodexProvider().run("PROMPT", timeout=15)
            finally:
                os.environ["PATH"] = old_path
            argv = json.loads(cap.read_text())
            check("per-seat: no config -> codex reasoning_effort falls back to xhigh",
                  "model_reasoning_effort=xhigh" in argv, "xhigh in argv", str(argv))

    # 9. cursor seat model from config reaches CursorProvider.run()'s --model;
    #    config BEATS the CREW_MA_*_MODEL env.
    fake_agent = '''
    import sys, json, os
    if "--version" in sys.argv:
        print("2026.06.24-00-45-58-9f61de7")
        sys.exit(0)
    cap = os.environ.get("CURSOR_CAPTURE")
    if cap:
        json.dump(sys.argv[1:], open(cap, "w"))
    sys.stdout.write("review body output")
    sys.exit(0)
    '''
    with project("[seats.cursor-glm]\nmodel = \"glm-from-config\"\n"):
        with tempfile.TemporaryDirectory() as bd:
            d = Path(bd)
            make_fake_bin(d, "agent", fake_agent)
            cap = d / "argv.json"
            old_path = os.environ["PATH"]
            os.environ["PATH"] = str(d) + os.pathsep + old_path
            os.environ["CURSOR_CAPTURE"] = str(cap)
            try:
                r = CursorProvider("cursor-glm", "glm-5.2-max", "CREW_MA_GLM_MODEL").run("P", timeout=10)
                argv = json.loads(cap.read_text())
                check("per-seat: config cursor model reaches run() --model argv",
                      "glm-from-config" in argv and r.model == "glm-from-config",
                      "glm-from-config", f"argv_model={'glm-from-config' in argv} r.model={r.model}")
                with env(CREW_MA_GLM_MODEL="glm-from-env"):
                    r2 = CursorProvider("cursor-glm", "glm-5.2-max", "CREW_MA_GLM_MODEL").run("P", timeout=10)
                    check("per-seat: config cursor model BEATS CREW_MA_GLM_MODEL env",
                          r2.model == "glm-from-config", "glm-from-config", str(r2.model))
            finally:
                os.environ["PATH"] = old_path
                os.environ.pop("CURSOR_CAPTURE", None)

    # 10. agy model + print_timeout from config reach the argv; config BEATS env.
    with project("[seats.agy]\nmodel = \"Agy Config Model\"\nprint_timeout = \"4m\"\n"):
        res, cap = _run_agy_with_fake("""
        import sys, json
        json.dump(sys.argv[1:], open(CAPTURE, "w"))
        sys.stdout.write("review body")
        sys.exit(0)
        """)
        check("per-seat: config agy model + print_timeout reach argv",
              cap is not None and "Agy Config Model" in cap and "4m" in cap,
              "model + 4m in argv", str(cap))
        with env(CREW_MA_AGY_MODEL="Env Model", CREW_MA_AGY_PRINT_TIMEOUT="9m"):
            res2, cap2 = _run_agy_with_fake("""
            import sys, json
            json.dump(sys.argv[1:], open(CAPTURE, "w"))
            sys.stdout.write("review body")
            sys.exit(0)
            """)
            check("per-seat: config agy model/print_timeout BEAT CREW_MA_AGY_* env",
                  cap2 is not None and "Agy Config Model" in cap2 and "4m" in cap2
                  and "Env Model" not in cap2 and "9m" not in cap2,
                  "config wins over env", str(cap2))

    # ---- review-prep default_panel wiring (subprocess BUILDS the panel) ---------
    # 11. config default_panel="lite" + NO flags -> BOTH branches fire for lite
    #     (subprocess split [] AND task split [opus,sonnet]) — not just subprocess.
    with project("default_panel = \"lite\"\n") as proj:
        _write_plan(proj)
        proc = _run_dispatcher(["review-prep", "plan.md", "--session-id", "cfgl"],
                               cwd=str(proj), timeout=30)
        obj = json.loads(proc.stdout)
        staged = proj / ".crew" / "reviews" / "cfgl" / "prompt-seat.txt"
        check("review-prep config default_panel=lite + no flags -> lite split (subprocess [] AND task [opus,sonnet])",
              proc.returncode == 0
              and obj["subprocess_seats"] == []
              and obj["task_seats"] == ["opus", "sonnet"]
              and obj["prompt_path"] == "" and not staged.exists(),
              "lite split both branches", str(obj))

    # 12. config default_panel="cursor" -> the subprocess branch is ALSO driven by
    #     config (all cursor-* seats), task_seats==[].
    from multiagent.providers import known_seat_names as _kn  # noqa: E402
    _cursor_all = sorted(n for n in _kn() if n.startswith("cursor-"))
    with project("default_panel = \"cursor\"\n") as proj:
        _write_plan(proj)
        proc = _run_dispatcher(["review-prep", "plan.md", "--session-id", "cfgc"],
                               cwd=str(proj), timeout=30)
        obj = json.loads(proc.stdout)
        check("review-prep config default_panel=cursor + no flags -> all cursor-* subprocess, no task",
              proc.returncode == 0
              and sorted(obj["subprocess_seats"]) == _cursor_all
              and obj["task_seats"] == [],
              str(_cursor_all), str(obj))

    # 13. Explicit --panel full OVERRIDES config default_panel="lite".
    with project("default_panel = \"lite\"\n") as proj:
        _write_plan(proj)
        proc = _run_dispatcher(["review-prep", "plan.md", "--panel", "full",
                                "--session-id", "cfgo"], cwd=str(proj), timeout=30)
        obj = json.loads(proc.stdout)
        check("review-prep explicit --panel full OVERRIDES config default_panel=lite",
              proc.returncode == 0
              and obj["subprocess_seats"] == ["codex", "agy", "cursor-auto", "cursor-composer"]
              and obj["task_seats"] == ["opus", "sonnet"],
              "full overrides config", str(obj))

    # 14. Explicit --seats "" OVERRIDES config default_panel (named-empty != omitted).
    with project("default_panel = \"full\"\n") as proj:
        _write_plan(proj)
        proc = _run_dispatcher(["review-prep", "plan.md", "--seats", "",
                                "--session-id", "cfge"], cwd=str(proj), timeout=30)
        obj = json.loads(proc.stdout)
        check("review-prep explicit --seats '' OVERRIDES config default_panel (stays empty)",
              proc.returncode == 0
              and obj["subprocess_seats"] == [] and obj["task_seats"] == [],
              "named-empty beats config", str(obj))

    # 15. Invalid config default_panel -> falls back to built-in 'full' (no KeyError).
    with project("default_panel = \"bogus\"\n") as proj:
        _write_plan(proj)
        proc = _run_dispatcher(["review-prep", "plan.md", "--session-id", "cfgb"],
                               cwd=str(proj), timeout=30)
        obj = json.loads(proc.stdout) if proc.returncode == 0 else None
        check("review-prep invalid config default_panel -> built-in 'full' (no KeyError)",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex", "agy", "cursor-auto", "cursor-composer"]
              and obj["task_seats"] == ["opus", "sonnet"],
              "full fallback on invalid", f"rc={proc.returncode} obj={obj}")


def test_review_prep():
    log_section("crew review-prep subcommand (PREP only — resolves+stages, runs nothing)")

    # 1. Output shape + pinned JSON contract (keys IN ORDER, no \uXXXX).
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        _write_plan(tdp)
        proc = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "codex,cursor",
             "--session-id", "s1", "--task-seats", "opus,sonnet"],
            cwd=td, timeout=30)
        obj = None
        try:
            obj = json.loads(proc.stdout)
        except Exception as exc:
            check("review-prep prints valid JSON", False, "valid json",
                  f"{exc}: {proc.stdout[:150]!r} err={proc.stderr[:150]!r}")
        if obj is not None:
            check("review-prep JSON keys are [prompt_path, subprocess_seats, task_seats, task_seat_models] IN ORDER, exit 0",
                  proc.returncode == 0
                  and list(obj.keys()) == ["prompt_path", "subprocess_seats", "task_seats", "task_seat_models"],
                  "exact key order", f"rc={proc.returncode} keys={list(obj.keys()) if obj else None}")
        check("review-prep stdout has no \\uXXXX escaping (ensure_ascii=False)",
              "\\u" not in proc.stdout, "no \\u", repr(proc.stdout[:160]))

    # 2. Subprocess-seats-only + opaque task echo.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        _write_plan(tdp)
        # group `cursor` expands; opus/sonnet NEVER appear in subprocess_seats.
        proc = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "codex,cursor,opus,sonnet",
             "--session-id", "s2", "--task-seats", "opus,sonnet"],
            cwd=td, timeout=30)
        obj = json.loads(proc.stdout)
        subs = obj["subprocess_seats"]
        check("review-prep subprocess_seats are registry seats only (no opus/sonnet)",
              proc.returncode == 0 and "opus" not in subs and "sonnet" not in subs
              and "codex" in subs and any(s.startswith("cursor-") for s in subs),
              "registry subprocess seats only", str(subs))
        check("review-prep task_seats echoes --task-seats verbatim",
              obj["task_seats"] == ["opus", "sonnet"], "['opus','sonnet']",
              str(obj["task_seats"]))
        # nonsense labels echo unchanged — NEVER registry-filtered.
        proc = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "codex",
             "--session-id", "s2b", "--task-seats", "foo,bar"],
            cwd=td, timeout=30)
        obj = json.loads(proc.stdout)
        check("review-prep task_seats echoes nonsense labels verbatim (opaque, no filter)",
              obj["task_seats"] == ["foo", "bar"], "['foo','bar']", str(obj["task_seats"]))
        # omitted --task-seats -> [].
        proc = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "codex", "--session-id", "s2c"],
            cwd=td, timeout=30)
        obj = json.loads(proc.stdout)
        check("review-prep omitted --task-seats -> []",
              obj["task_seats"] == [], "[]", str(obj["task_seats"]))

    # 3. prompt_path BYTE-IDENTICAL to `render --mode review --stage` (BOTH
    #    inline-diff modes). Distinct session ids so the two writes don't share a
    #    path (no self-compare).
    for flag in ([], ["--inline-diff"]):
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _write_plan(tdp)
            prep = _run_dispatcher(
                ["review-prep", "plan.md", "--seats", "codex",
                 "--session-id", "pa", *flag], cwd=td, timeout=30)
            stage = _run_dispatcher(
                ["render", "plan.md", "--mode", "review", "--stage",
                 "--session-id", "sb", *flag], cwd=td, timeout=30)
            prep_path = json.loads(prep.stdout)["prompt_path"]
            prep_f = tdp / prep_path
            stage_f = tdp / ".crew" / "reviews" / "sb" / "prompt-seat.txt"
            same = (prep_f.is_file() and stage_f.is_file()
                    and prep_f.read_bytes() == stage_f.read_bytes())
            label = "with --inline-diff" if flag else "no flag"
            check(f"review-prep prompt_path file == render --stage file ({label}, byte-identical)",
                  prep.returncode == 0 and stage.returncode == 0 and same,
                  "identical bytes",
                  f"prep={prep_f.is_file()} stage={stage_f.is_file()} equal={same}")

    # 3b. WORKING-TREE byte-identity (locks the build.md/measure-twice git path —
    #     the plan.md case above doesn't exercise targets.resolve's git diff).
    #     Two IDENTICAL temp git repos (identical init + identical change) so
    #     prep's staged .crew/ file can't perturb render's working-tree diff;
    #     reference mode embeds the diff COMMAND, not content, so the prompts are
    #     deterministic and byte-identical across the matched repos.
    with tempfile.TemporaryDirectory() as tda, tempfile.TemporaryDirectory() as tdb:
        for repo in (tda, tdb):
            _init_repo(repo)                                   # commits a.txt=hello
            (Path(repo) / "a.txt").write_text("hello\nchanged\n")
        prep = _run_dispatcher(
            ["review-prep", "working-tree", "--seats", "codex", "--session-id", "wa"],
            cwd=tda, timeout=30)
        stage = _run_dispatcher(
            ["render", "working-tree", "--mode", "review", "--stage",
             "--session-id", "wb"], cwd=tdb, timeout=30)
        prep_path = json.loads(prep.stdout)["prompt_path"] if prep.returncode == 0 else ""
        prep_f = Path(tda) / prep_path if prep_path else Path(tda) / "missing"
        stage_f = Path(tdb) / ".crew" / "reviews" / "wb" / "prompt-seat.txt"
        same = (prep_f.is_file() and stage_f.is_file()
                and prep_f.read_bytes() == stage_f.read_bytes())
        check("review-prep working-tree prompt_path == render working-tree --stage (byte-identical)",
              prep.returncode == 0 and stage.returncode == 0 and same
              and prep_path == ".crew/reviews/wa/prompt-seat.txt",
              "identical bytes (git working-tree path)",
              f"prep={prep_f.is_file()} stage={stage_f.is_file()} equal={same} path={prep_path!r}")

    # 4. No internal fan-out / runs nothing / stages ONLY the subprocess prompt.
    #    A fake codex on PATH that writes a sentinel must NEVER be invoked by prep.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        _write_plan(tdp)
        bins = tdp / "bin"
        bins.mkdir()
        sentinel = tdp / "codex_ran.sentinel"
        make_fake_bin(bins, "codex", f"""
        import sys
        open({str(sentinel)!r}, "w").write("RAN")
        sys.exit(0)
        """)
        env = path_with(bins)
        proc = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "codex", "--session-id", "s4",
             "--task-seats", "opus,sonnet"], cwd=td, env=env, timeout=30)
        prompt_seat = tdp / ".crew" / "reviews" / "s4" / "prompt-seat.txt"
        opus_f = tdp / ".crew" / "reviews" / "s4" / "prompt-opus.txt"
        sonnet_f = tdp / ".crew" / "reviews" / "s4" / "prompt-sonnet.txt"
        check("review-prep never invokes a seat (fake codex sentinel absent — prep runs nothing)",
              proc.returncode == 0 and not sentinel.exists(),
              "no sentinel", f"rc={proc.returncode} sentinel={sentinel.exists()}")
        check("review-prep stages ONLY the subprocess prompt-seat.txt (no per-role Task prompts)",
              prompt_seat.is_file() and not opus_f.exists() and not sonnet_f.exists(),
              "only prompt-seat.txt",
              f"seat={prompt_seat.is_file()} opus={opus_f.exists()} sonnet={sonnet_f.exists()}")

    # 5. Placeholder + traversal guards (SANITIZE, not raise).
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        _write_plan(tdp)
        ph = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "codex",
             "--session-id", "<session-id>"], cwd=td, timeout=30)
        check("review-prep rejects an unsubstituted <session-id> placeholder (nonzero)",
              ph.returncode != 0 and "placeholder" in ph.stderr.lower(),
              "nonzero + placeholder", f"rc={ph.returncode} err={ph.stderr[:150]}")
        trav = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "codex",
             "--session-id", "../../x"], cwd=td, timeout=30)
        # traversal id sanitizes/collapses to .crew/reviews/x (contained), not a raise.
        staged = tdp / ".crew" / "reviews" / "x" / "prompt-seat.txt"
        prep_path = json.loads(trav.stdout)["prompt_path"] if trav.returncode == 0 else ""
        check("review-prep traversal session id is CONTAINED (sanitized to .crew/reviews/x)",
              trav.returncode == 0 and staged.is_file()
              and prep_path == ".crew/reviews/x/prompt-seat.txt",
              "contained sanitized path", f"rc={trav.returncode} path={prep_path!r} exists={staged.is_file()}")

    # 6. Group-token expansion: --seats cursor -> every registered cursor-* seat.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        _write_plan(tdp)
        proc = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "cursor", "--session-id", "s6"],
            cwd=td, timeout=30)
        subs = json.loads(proc.stdout)["subprocess_seats"]
        from multiagent.providers import known_seat_names  # noqa: E402
        expected_cursor = sorted(n for n in known_seat_names() if n.startswith("cursor-"))
        check("review-prep --seats cursor expands to every registered cursor-* seat",
              proc.returncode == 0 and sorted(subs) == expected_cursor and len(subs) >= 2,
              str(expected_cursor), str(subs))

    # 7. EXPLICIT-EMPTY --seats "" -> subprocess_seats==[] AND prompt_path=="" AND
    #    no prompt-seat.txt staged (the lite/solo regression guard). An EXPLICIT
    #    empty seats flag is "the user named seats" (just none) — it does NOT fall
    #    through to the default panel (that's the truly-omitted case, test 14).
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        _write_plan(tdp)
        proc = _run_dispatcher(
            ["review-prep", "plan.md", "--seats", "", "--session-id", "s7",
             "--task-seats", "opus,sonnet"], cwd=td, timeout=30)
        obj = json.loads(proc.stdout)
        staged = tdp / ".crew" / "reviews" / "s7" / "prompt-seat.txt"
        check("review-prep --seats '' -> subprocess_seats==[], prompt_path=='', no staged file, task echoed, exit 0",
              proc.returncode == 0
              and obj["subprocess_seats"] == []
              and obj["prompt_path"] == ""
              and not staged.exists()
              and obj["task_seats"] == ["opus", "sonnet"],
              "empty subprocess + no stage + task echoed",
              f"rc={proc.returncode} obj={obj} staged={staged.exists()}")

    # ---- Step 2 (panel catalog): --panel resolution + unified --seats split ----
    from multiagent.providers import known_seat_names as _known_seats  # noqa: E402
    _registered_cursor = sorted(n for n in _known_seats() if n.startswith("cursor-"))

    def _prep(args, cwd):
        proc = _run_dispatcher(["review-prep", "plan.md", *args], cwd=cwd, timeout=30)
        return proc, (json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else None)

    # 8. --panel full: subprocess subset == today's default panel, task seats +
    #    model pins from the catalog.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--panel", "full", "--session-id", "pf"], td)
        check("review-prep --panel full -> default subprocess subset + task_seats/models",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex", "agy", "cursor-auto", "cursor-composer"]
              and obj["task_seats"] == ["opus", "sonnet"]
              and obj["task_seat_models"] == {"opus": "opus", "sonnet": "sonnet"},
              "full preset split", str(obj))

    # 9. --panel lite: subprocess_seats==[], prompt_path=="", task path still
    #    resolved (the BLOCKING lite/solo Task-path fix — review-prep ALWAYS emits
    #    the task split even when the subprocess fan-out is skipped).
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--panel", "lite", "--session-id", "pl"], td)
        staged = Path(td) / ".crew" / "reviews" / "pl" / "prompt-seat.txt"
        check("review-prep --panel lite -> subprocess_seats==[], prompt_path=='', task path intact",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == [] and obj["prompt_path"] == ""
              and not staged.exists()
              and obj["task_seats"] == ["opus", "sonnet"]
              and obj["task_seat_models"] == {"opus": "opus", "sonnet": "sonnet"},
              "lite: empty subprocess, full task split", str(obj))

    # 10. --panel solo: one Claude seat, no subprocess.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--panel", "solo", "--session-id", "ps"], td)
        check("review-prep --panel solo -> subprocess_seats==[], task_seats==['opus']",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == []
              and obj["task_seats"] == ["opus"]
              and obj["task_seat_models"] == {"opus": "opus"},
              "solo split", str(obj))

    # 11. --panel cursor: every registered cursor-* seat (group token expanded IN
    #     cli.py), NO Task seats.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--panel", "cursor", "--session-id", "pc"], td)
        check("review-prep --panel cursor -> all cursor-* subprocess seats, task_seats==[]",
              proc.returncode == 0 and obj is not None
              and sorted(obj["subprocess_seats"]) == _registered_cursor
              and len(obj["subprocess_seats"]) >= 2
              and obj["task_seats"] == [] and obj["task_seat_models"] == {},
              str(_registered_cursor), str(obj))

    # 12. Mixed unified --seats split: subprocess vs TASK_SEAT_NAMES, with model pin.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--seats", "codex,opus,opus-4.6", "--session-id", "ms"], td)
        check("review-prep --seats codex,opus,opus-4.6 splits subprocess vs task + pins opus-4.6",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex"]
              and obj["task_seats"] == ["opus", "opus-4.6"]
              and obj["task_seat_models"] == {"opus": "opus", "opus-4.6": "claude-opus-4-6"},
              "mixed split + model pin", str(obj))

    # 13. Unknown unified --seats name -> silently DROPPED (back-compat with
    #     _resolve_seats's drop behavior); exit 0.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--seats", "codex,opus,bogus", "--session-id", "us"], td)
        check("review-prep --seats codex,opus,bogus drops 'bogus' from BOTH lists (exit 0)",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex"]
              and obj["task_seats"] == ["opus"]
              and obj["task_seat_models"] == {"opus": "opus"}
              and "bogus" not in obj["subprocess_seats"]
              and "bogus" not in obj["task_seats"]
              and "bogus" not in obj["task_seat_models"],
              "bogus dropped", str(obj))

    # 14. Both --panel AND --seats omitted + NO config -> the BUILT-IN default
    #     panel ("full"), INCLUDING its opus/sonnet task seats. This is the exact
    #     regression the orchestrator change (commands stop hardcoding --panel
    #     full) could introduce — patching only the subprocess branch would keep
    #     codex/agy but silently drop the Claude voices. (The temp dir has no
    #     .crew/config.toml, so default_panel() is None → "full".)
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--session-id", "bo"], td)
        staged = Path(td) / ".crew" / "reviews" / "bo" / "prompt-seat.txt"
        check("review-prep both --panel and --seats omitted + no config -> built-in 'full' (subprocess AND task seats)",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex", "agy", "cursor-auto", "cursor-composer"]
              and obj["task_seats"] == ["opus", "sonnet"]
              and obj["task_seat_models"] == {"opus": "opus", "sonnet": "sonnet"}
              and obj["prompt_path"] == ".crew/reviews/bo/prompt-seat.txt"
              and staged.exists(),
              "built-in full default both-omitted", str(obj))

    # 15. Precedence: explicit --task-seats OVERRIDES the panel-derived task list;
    #     explicit subprocess --seats OVERRIDES the panel subprocess list.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--panel", "full", "--task-seats", "opus", "--session-id", "ov1"], td)
        check("review-prep --panel full --task-seats opus -> task list overridden to ['opus']",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex", "agy", "cursor-auto", "cursor-composer"]
              and obj["task_seats"] == ["opus"]
              and obj["task_seat_models"] == {"opus": "opus"},
              "task-seats override", str(obj))
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--panel", "full", "--seats", "codex", "--session-id", "ov2"], td)
        check("review-prep --panel full --seats codex -> subprocess overridden, task still full preset",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex"]
              and obj["task_seats"] == ["opus", "sonnet"]
              and obj["task_seat_models"] == {"opus": "opus", "sonnet": "sonnet"},
              "seats override subprocess, panel supplies task", str(obj))

    # 16. Staging<->spawn consistency: the comma-joined task_seats (incl. opus-4.6)
    #     fed to `render --stage-all` stages prompt-opus-46.txt (dot stripped) — the
    #     SAME filename the spawn line reads, so they can never diverge.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        prep, obj = _prep(["--seats", "opus,sonnet,opus-4.6", "--session-id", "sc"], td)
        joined = ",".join(obj["task_seats"]) if obj else ""
        render = _run_dispatcher(
            ["render", "plan.md", "--mode", "review", "--stage-all", joined,
             "--session-id", "sc"], cwd=td, timeout=30)
        opus46 = Path(td) / ".crew" / "reviews" / "sc" / "prompt-opus-46.txt"
        opus = Path(td) / ".crew" / "reviews" / "sc" / "prompt-opus.txt"
        sonnet = Path(td) / ".crew" / "reviews" / "sc" / "prompt-sonnet.txt"
        check("review-prep task_seats join feeds --stage-all -> stages prompt-opus-46.txt (dot stripped)",
              prep.returncode == 0 and render.returncode == 0
              and obj["task_seats"] == ["opus", "sonnet", "opus-4.6"]
              and opus.is_file() and sonnet.is_file() and opus46.is_file(),
              "prompt-opus-46.txt staged from joined task_seats",
              f"join={joined!r} rc={render.returncode} opus46={opus46.is_file()}")


def test_panel_catalog():
    log_section("plain-data panel catalog (seats.py)")
    from multiagent import seats  # noqa: E402
    from multiagent import cli  # noqa: E402

    # Keys present, exactly the four presets.
    check("PANEL_PRESETS keys == {full, lite, solo, cursor}",
          set(seats.PANEL_PRESETS.keys()) == {"full", "lite", "solo", "cursor"},
          "{full, lite, solo, cursor}", str(set(seats.PANEL_PRESETS.keys())))

    # Task seats (strings) superset of the known Claude seats incl. opt-in 4.6.
    check("TASK_SEAT_NAMES superset of {opus, sonnet, opus-4.6}",
          {"opus", "sonnet", "opus-4.6"} <= set(seats.TASK_SEAT_NAMES),
          "superset of {opus, sonnet, opus-4.6}", str(seats.TASK_SEAT_NAMES))

    # The only model override today.
    check("MODEL_OVERRIDES['opus-4.6'] == 'claude-opus-4-6'",
          seats.MODEL_OVERRIDES.get("opus-4.6") == "claude-opus-4-6",
          "claude-opus-4-6", str(seats.MODEL_OVERRIDES.get("opus-4.6")))

    # ROSTER-FIDELITY drift guard: the subprocess subset of full (the names that
    # are NOT Task seats) equals cli._DEFAULT_SUBPROCESS_PANEL as a sequence.
    subprocess_subset = [n for n in seats.PANEL_PRESETS["full"]
                         if n not in seats.TASK_SEAT_NAMES]
    check("full's subprocess subset == cli._DEFAULT_SUBPROCESS_PANEL (sequence)",
          subprocess_subset == list(cli._DEFAULT_SUBPROCESS_PANEL),
          str(list(cli._DEFAULT_SUBPROCESS_PANEL)), str(subprocess_subset))

    # The other presets, verbatim. cursor is the literal group TOKEN, NOT an
    # expanded cursor-* list (seats.py is pure data; cli.py expands it later).
    check("lite == ['opus', 'sonnet']",
          seats.PANEL_PRESETS["lite"] == ["opus", "sonnet"],
          "['opus', 'sonnet']", str(seats.PANEL_PRESETS["lite"]))
    check("solo == ['opus']",
          seats.PANEL_PRESETS["solo"] == ["opus"],
          "['opus']", str(seats.PANEL_PRESETS["solo"]))
    check("cursor == ['cursor'] (literal group token, unexpanded)",
          seats.PANEL_PRESETS["cursor"] == ["cursor"],
          "['cursor']", str(seats.PANEL_PRESETS["cursor"]))

    # NO-PROVIDER-IMPORT, enforced STRUCTURALLY in a CLEAN subprocess: importing
    # seats must NOT pull in multiagent.providers / build the registry. (A clean
    # interpreter is required because earlier tests already imported providers
    # into THIS process's sys.modules.)
    probe = (
        "import sys; import multiagent.seats as s; "
        "assert 'multiagent.providers' not in sys.modules, "
        "'seats imported providers'; "
        "assert 'multiagent.cli' not in sys.modules, 'seats imported cli'; "
        "assert not any('get_provider' in repr(v) for v in s.__dict__.values()), "
        "'seats references get_provider'; "
        "print('OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(SCRIPT_DIR), capture_output=True, text=True, timeout=30)
    check("importing seats in a clean interpreter pulls in NO provider/registry module",
          proc.returncode == 0 and proc.stdout.strip() == "OK",
          "rc=0 stdout=OK", f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}")


def test_catalog_registry_disjoint():
    log_section("TASK_SEAT_NAMES ⟂ subprocess registry (the safety the catalog buys)")
    from multiagent import seats  # noqa: E402
    from multiagent.providers import known_seat_names, get_provider  # noqa: E402

    # The invariant: no Task-seat NAME is a registered subprocess seat.
    check("TASK_SEAT_NAMES ∩ known_seat_names() == ∅ (disjoint)",
          set(seats.TASK_SEAT_NAMES) & set(known_seat_names()) == set(),
          "empty intersection",
          str(set(seats.TASK_SEAT_NAMES) & set(known_seat_names())))

    # Each Task-seat name fails to resolve through the executor registry.
    for n in seats.TASK_SEAT_NAMES:
        raised = False
        try:
            get_provider(n)
        except ValueError:
            raised = True
        check(f"get_provider({n!r}) raises ValueError (no executor)",
              raised, "ValueError raised", "no exception")

    # CONVERSE guard: the SUBPROCESS entries of a preset (codex + cursor-*, i.e.
    # the non-Task-seat names) ARE registry seats and DO resolve. This proves the
    # disjointness assertion is scoped to TASK_SEAT_NAMES ONLY, not over-broad.
    subprocess_entries = [n for n in seats.PANEL_PRESETS["full"]
                          if n not in seats.TASK_SEAT_NAMES]
    for n in subprocess_entries:
        in_registry = n in known_seat_names()
        resolves = False
        try:
            get_provider(n)
            resolves = True
        except ValueError:
            resolves = False
        check(f"subprocess preset entry {n!r} IS in registry + resolves",
              in_registry and resolves,
              "in known_seat_names() + get_provider ok",
              f"in_registry={in_registry} resolves={resolves}")


def test_no_dotkept_staged_filename():
    log_section("no dot-kept opus-4.6 staged FILENAME drift under plugins/crew/")
    import re as _re

    # FILENAME-SCOPED guard: the staged-prompt filename for the opus-4.6 seat must
    # always be the dot-stripped `prompt-opus-46` (what _seat_role_slug derives).
    # debate.md hand-writes its render `-o` filenames, so _seat_role_slug can't
    # reach them — this scan is what stops the dot-KEPT staged-filename drift from
    # silently recurring. SCOPED to the FILENAME spelling ONLY: we deliberately do
    # NOT scan a bare `opus-4.6`, which would false-positive on the LEGIT
    # `--seat-role opus-4.6` render token, the `opus-4.6` seat name in
    # `--seats`/prose, and MODEL_OVERRIDES/TASK_SEAT_NAMES entries — all of which
    # STAY. Only the dot-kept FILENAME spelling is forbidden.
    crew_root = SCRIPT_DIR.parent  # plugins/crew/
    forbidden = _re.compile(r"prompt-opus-4\.6|opus-4\.6\.txt")
    offenders = []
    for path in crew_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{path.relative_to(crew_root)}:{lineno}: {line.strip()}")
    check("no dot-kept opus-4.6 staged FILENAME anywhere under plugins/crew/",
          not offenders, "zero matches",
          "; ".join(offenders) if offenders else "zero matches")


def main():
    print(f"{YELLOW}Running multiagent engine tests...{NC}")
    test_result_contract()
    test_default_seats()
    test_resolve_timeout()
    test_stage()
    test_stage_all()
    test_dispatcher()
    test_registry()
    test_codex()
    test_agy_helpers()
    test_agy_timeout_floor()
    test_agy_oversized()
    test_agy_run()
    test_agy_is_available()
    test_render()
    test_prompts()
    test_targets()
    test_production_invocation_and_fanout()
    test_run_subcommand()
    test_council_subcommand()
    test_debate_subcommand()
    test_rounds()
    test_discuss_and_modes()
    test_render_subcommand()
    test_cursor()
    test_from_dict_and_escaping()
    test_collect()
    test_config()
    test_review_prep()
    test_panel_catalog()
    test_catalog_registry_disjoint()
    test_no_dotkept_staged_filename()

    print()
    print(f"{YELLOW}=== Results ==={NC}")
    print(f"{GREEN}Passed: {PASS_COUNT}{NC}")
    if FAIL_COUNT:
        print(f"{RED}Failed: {FAIL_COUNT}{NC}")
        sys.exit(1)
    print(f"{GREEN}All tests passed!{NC}")
    sys.exit(0)


if __name__ == "__main__":
    main()
