#!/usr/bin/env python3
"""Tests for the multiagent review engine.

Matches test-hooks.py style: custom log_pass/log_fail/log_section harness, run
via `python plugins/crew/scripts/tests/test-multiagent.py`, exits nonzero on failure.

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
import re
import shutil
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

# This test lives in scripts/tests/. The import root (parent of the
# `multiagent` package + the hook source) is scripts/ = parent.parent; the
# fixtures sit ALONGSIDE this file in tests/ = parent.
SCRIPT_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
MULTIAGENT_DIR = SCRIPT_DIR / "multiagent"
FIXTURES_DIR = TESTS_DIR / "fixtures" / "review-panel"

# Make `import multiagent...` resolve (the package parent is SCRIPT_DIR).
sys.path.insert(0, str(SCRIPT_DIR))

from multiagent import findings, prompts, render, rounds, targets  # noqa: E402
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


# One empty dir shared by every subprocess env this suite builds: HOME is ALWAYS
# neutralized to it so a developer's real ~/.crew-config.toml can never leak into
# a subprocess test. Without this, a real global config (e.g. [dispatch].seat =
# "cursor-gpt") reroutes dispatch/panel resolution to a REAL, BILLABLE CLI instead
# of the fake bins on PATH. Tests that need a populated HOME override env["HOME"]
# after calling path_with().
_NEUTRAL_HOME = tempfile.mkdtemp(prefix="crew-test-neutral-home-")


def _neutral_env() -> dict:
    env = dict(os.environ)
    env["HOME"] = _NEUTRAL_HOME
    return env


def path_with(*dirs: Path) -> dict:
    env = _neutral_env()
    env["PATH"] = os.pathsep.join(str(d) for d in dirs) + os.pathsep + env.get("PATH", "")
    return env


# =============================================================================
# Config helpers — point the layered loaders (per-repo + global) at temp files.
# =============================================================================

from contextlib import contextmanager  # noqa: E402


@contextmanager
def crew_config(*, project=None, glob=None):
    """Point the per-repo (``CLAUDE_PROJECT_DIR``/.crew/config.toml) AND global
    (``$HOME``/~/.crew-config.toml) loaders at FRESH temp dirs, reset the memoized
    config on enter/exit, and yield the project ``Path``.

    Either layer omitted = that file ABSENT (so the layer is empty, NOT the real
    machine's file — ``$HOME`` is always neutralized to a temp dir so a developer's
    real ~/.crew-config.toml can never leak into a test). ``Path.home()`` honors
    ``$HOME`` on POSIX, so the global loader resolves into the temp home.
    """
    from multiagent import config
    saved = {k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR", "HOME")}
    with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as home:
        if project is not None:
            crew = Path(proj) / ".crew"
            crew.mkdir(parents=True)
            (crew / "config.toml").write_text(project)
        if glob is not None:
            (Path(home) / ".crew-config.toml").write_text(glob)
        os.environ["CLAUDE_PROJECT_DIR"] = proj
        os.environ["HOME"] = home
        config._reset_cache_for_tests()
        try:
            yield Path(proj)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            config._reset_cache_for_tests()


def project_config(toml_text, *, write_file=True):
    """Per-repo config ctxmgr (global layer neutralized). ``write_file=False`` =
    no per-repo file (missing-file posture)."""
    return crew_config(project=toml_text if write_file else None)


def shipped_seats(provider):
    """The SHIPPED catalog rows for one provider, in catalog order — the roster as
    seats.toml declares it, with no config layer folded in. Config-independent, so
    an assertion about the built-in roster stays true on a machine that declares
    seats of its own."""
    from multiagent import seats as _seats
    return {n: s for n, s in _seats.shipped_catalog().items() if s.provider == provider}


def global_home(toml_text, *, write_file=True):
    """Global ~/.crew-config.toml ctxmgr (no per-repo file). ``write_file=False`` =
    no global file."""
    return crew_config(glob=toml_text if write_file else None)


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
    # No config print_timeout set -> the built-in 8m DEFAULT_PRINT_TIMEOUT applies.
    with project_config("", write_file=False):
        eff = prov.effective_timeout(300)  # generic ABC default
        check("agy effective timeout not capped at 300 when print-timeout is 8m",
              eff >= 480.0, ">= 480", str(eff))
        eff2 = prov.effective_timeout(900)  # explicitly larger caller wins
        check("agy effective timeout honors larger caller timeout",
              eff2 == 900.0, "900.0", str(eff2))


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
    # Build the agy seat THROUGH the catalog so a [seats.agy] config table (model
    # or print_timeout) reaches it — the registry binds the resolved tunes, which
    # a bare AgyProvider() no longer reads.
    from multiagent.providers import get_provider
    prov = get_provider("agy")
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

    # config [seats.agy].model override (env retired)
    with project_config('[seats.agy]\nmodel = "Gemini 3.5 Flash (High)"\n'):
        res, cap = _run_agy_with_fake(capture_args)
    check("agy: --model overridable via [seats.agy].model config",
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

    # hang -> timeout kill + reap within deadline. config [seats.agy].print_timeout
    # "1s" keeps the wall-clock floor low so the kill fires fast — without it the
    # default 8m floor would make this hang (env retired).
    start = time.monotonic()
    with project_config('[seats.agy]\nprint_timeout = "1s"\n'):
        res, _ = _run_agy_with_fake("""
        import sys, time
        with open(CAPTURE, "w") as f:
            f.write('{"args": [], "stdin": ""}')
        time.sleep(30)
        """, timeout=1)
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
    from multiagent.cli import _resolve_seats  # noqa: E402
    CURSOR_SEATS = shipped_seats("cursor")
    # No config, no env (retired): the CONFIGURED default panel (here the built-in
    # full) drives _resolve_seats; opus/sonnet (Task seats) drop via the registry
    # filter, leaving the built-in five subprocess seats.
    with project_config("", write_file=False):
        resolved = _resolve_seats(None)
        check("_resolve_seats(None) -> builtin full's subprocess subset",
              resolved == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"],
              "['codex', 'codex-luna', 'agy', 'cursor-auto', 'cursor-composer']", str(resolved))
        # The 'cursor' group token expands on the --seats path (the env path is gone).
        seats = _resolve_seats("cursor")
        check("--seats cursor expands to all cursor-* seats",
              set(seats) == set(CURSOR_SEATS) and len(seats) == len(CURSOR_SEATS),
              "all cursor-* seats", str(seats))
    # config default_panel drives the no-flag default (consistency requirement).
    with project_config('default_panel = "lite"\n'):
        check("_resolve_seats(None) honors config default_panel=lite (no subprocess seats)",
              _resolve_seats(None) == [], "[]", str(_resolve_seats(None)))
    # A [panels] override of `full` changes the resolved default subprocess set.
    with project_config('[panels]\nfull = ["codex", "opus"]\n'):
        check("_resolve_seats(None) honors [panels].full override (codex only; opus is a Task seat)",
              _resolve_seats(None) == ["codex"], "['codex']", str(_resolve_seats(None)))


def test_resolve_timeout():
    log_section("default per-seat timeout")
    from multiagent.cli import _resolve_timeout  # noqa: E402
    with project_config("", write_file=False):
        # Generous default: reference mode + thorough seats need room; better
        # than silently dropping a default seat on a large review.
        check("default per-seat timeout is generous (>= 600s)",
              _resolve_timeout(None) >= 600, ">=600", str(_resolve_timeout(None)))
        check("--timeout arg overrides the default",
              _resolve_timeout(45) == 45, "45", str(_resolve_timeout(45)))
    # per-repo [tuning].timeout beats builtin; explicit arg beats config.
    with project_config("[tuning]\ntimeout = 120\n"):
        check("[tuning].timeout overrides the default when no arg given",
              _resolve_timeout(None) == 120, "120", str(_resolve_timeout(None)))
        check("--timeout arg beats config",
              _resolve_timeout(45) == 45, "45", str(_resolve_timeout(45)))
    # global config beats builtin; per-repo beats global.
    with crew_config(glob="[tuning]\ntimeout = 222\n"):
        check("global [tuning].timeout overrides builtin when no per-repo file",
              _resolve_timeout(None) == 222, "222", str(_resolve_timeout(None)))
    with crew_config(project="[tuning]\ntimeout = 333\n", glob="[tuning]\ntimeout = 222\n"):
        check("per-repo [tuning].timeout beats global",
              _resolve_timeout(None) == 333, "333", str(_resolve_timeout(None)))
    # bad config value falls back to the builtin (no crash).
    with project_config("[tuning]\ntimeout = \"not-an-int\"\n"):
        check("bad [tuning].timeout falls back to the default (no crash)",
              _resolve_timeout(None) >= 600, ">=600", str(_resolve_timeout(None)))


def test_deadline_minutes():
    """[tuning].deadline_minutes: the persistence loops' wall clock.

    Same two-layer precedence and same never-crash posture as every other key.
    Validated against the SAME ceiling `crew state init` polices the flag with: a
    config file is no more trustworthy than the flag, and a non-positive value
    would read as "no deadline" in the Stop hook.
    """
    log_section("[tuning].deadline_minutes")
    import io  # noqa: E402
    from contextlib import redirect_stderr  # noqa: E402

    from multiagent import config  # noqa: E402
    from models import MAX_DEADLINE_MINUTES  # noqa: E402

    with project_config("", write_file=False):
        check("no config -> deadline_minutes() is None (caller keeps its builtin)",
              config.deadline_minutes() is None, "None", str(config.deadline_minutes()))
    with project_config("[tuning]\ndeadline_minutes = 90\n"):
        check("per-repo [tuning].deadline_minutes -> 90",
              config.deadline_minutes() == 90, "90", str(config.deadline_minutes()))
    with crew_config(glob="[tuning]\ndeadline_minutes = 150\n"):
        check("global [tuning].deadline_minutes applies with no per-repo file",
              config.deadline_minutes() == 150, "150", str(config.deadline_minutes()))
    with crew_config(project="[tuning]\ndeadline_minutes = 90\n",
                     glob="[tuning]\ndeadline_minutes = 150\n"):
        check("per-repo [tuning].deadline_minutes BEATS global",
              config.deadline_minutes() == 90, "90", str(config.deadline_minutes()))
    with project_config(f"[tuning]\ndeadline_minutes = {MAX_DEADLINE_MINUTES}\n"):
        check(f"[tuning].deadline_minutes at the policy ceiling ({MAX_DEADLINE_MINUTES}) is accepted",
              config.deadline_minutes() == MAX_DEADLINE_MINUTES,
              str(MAX_DEADLINE_MINUTES), str(config.deadline_minutes()))
    # Every out-of-policy shape is DROPPED (None -> builtin), never clamped, and
    # never raises: a bad value must not delete the only cost ceiling in the loop.
    for body, label in (
        (f"[tuning]\ndeadline_minutes = {MAX_DEADLINE_MINUTES + 1}\n", "past the ceiling"),
        ("[tuning]\ndeadline_minutes = 0\n", "zero"),
        ("[tuning]\ndeadline_minutes = -5\n", "negative"),
        ("[tuning]\ndeadline_minutes = true\n", "bool (an int subclass)"),
        ("[tuning]\ndeadline_minutes = \"soon\"\n", "non-int"),
    ):
        with project_config(body):
            buf = io.StringIO()
            with redirect_stderr(buf):
                val = config.deadline_minutes()
            check(f"[tuning].deadline_minutes {label} -> None + stderr warn",
                  val is None and "deadline_minutes" in buf.getvalue(),
                  "None + warn", f"val={val!r} stderr={buf.getvalue()[:120]!r}")


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
    from multiagent.providers.cursor import CursorProvider

    CURSOR_SEATS = shipped_seats("cursor")
    for seat_name, spec in CURSOR_SEATS.items():
        check(f"get_provider('{seat_name}') -> CursorProvider pinned to {spec.model}",
              isinstance(get_provider(seat_name), CursorProvider)
              and get_provider(seat_name)._default_model == spec.model,
              f"CursorProvider({spec.model})", repr(get_provider(seat_name).__dict__))
    check("each cursor seat is a DISTINCT model (no closure late-binding bug)",
          len({get_provider(s)._default_model for s in CURSOR_SEATS}) == len(CURSOR_SEATS),
          "all distinct models", str([get_provider(s)._default_model for s in CURSOR_SEATS]))
    # The opt-in premium grok seat's model pin (verified via `agent models`).
    check("CURSOR_SEATS pins cursor-grok to grok-4.5-xhigh",
          CURSOR_SEATS.get("cursor-grok").model == "grok-4.5-xhigh",
          "grok-4.5-xhigh", str(CURSOR_SEATS.get("cursor-grok")))
    # Codex model-seats mirror cursor: one CodexProvider per CODEX_SEATS entry,
    # each pinned to its model (codex + codex-luna default, codex-terra opt-in).
    CODEX_SEATS = shipped_seats("codex")
    for seat_name, spec in CODEX_SEATS.items():
        check(f"get_provider('{seat_name}') -> CodexProvider pinned to {spec.model}",
              isinstance(get_provider(seat_name), CodexProvider)
              and get_provider(seat_name)._default_model == spec.model,
              f"CodexProvider({spec.model})", repr(get_provider(seat_name).__dict__))
    check("CODEX_SEATS pins codex to gpt-5.6-sol",
          CODEX_SEATS.get("codex").model == "gpt-5.6-sol",
          "gpt-5.6-sol", str(CODEX_SEATS.get("codex")))
    check("CODEX_SEATS pins codex-luna to gpt-5.6-luna",
          CODEX_SEATS.get("codex-luna").model == "gpt-5.6-luna",
          "gpt-5.6-luna", str(CODEX_SEATS.get("codex-luna")))
    # The opt-in codex-terra seat's model pin (registered, run via --seats).
    check("CODEX_SEATS pins codex-terra to gpt-5.6-terra",
          CODEX_SEATS.get("codex-terra").model == "gpt-5.6-terra",
          "gpt-5.6-terra", str(CODEX_SEATS.get("codex-terra")))


# =============================================================================
# ProviderResult contract
# =============================================================================

def test_result_contract():
    log_section("ProviderResult contract (Step 1.0 / 1.1)")
    r = ProviderResult(name="codex", model="o3", ok=True, output="x", error=None, elapsed=1.5)
    d = r.to_dict()
    check("to_dict has exactly the six fields (repaired_output omitted when None)",
          set(d.keys()) == {"name", "model", "ok", "output", "error", "elapsed"},
          "six fields", str(set(d.keys())))
    import dataclasses
    check("to_dict equals asdict MINUS the None repaired_output",
          d == {k: v for k, v in dataclasses.asdict(r).items() if k != "repaired_output"},
          "equal (sans repaired_output)", "differ")

    # A REPAIRED seat carries the optional 7th field; it round-trips and does NOT
    # touch the original output.
    rr = ProviderResult(name="codex", model="o3", ok=True, output="ORIGINAL",
                        error=None, elapsed=1.5, repaired_output="REPAIRED")
    dd = rr.to_dict()
    check("to_dict includes repaired_output ONLY when set",
          dd.get("repaired_output") == "REPAIRED" and dd["output"] == "ORIGINAL",
          "7th field present, output intact", str(set(dd.keys())))
    back = ProviderResult.from_dict(dd)
    check("repaired_output round-trips through from_dict/to_dict",
          back.repaired_output == "REPAIRED" and back.output == "ORIGINAL",
          "round-trip holds", f"{back.repaired_output!r}/{back.output!r}")
    check("from_dict defaults repaired_output to None when absent",
          ProviderResult.from_dict({"name": "x", "ok": True}).repaired_output is None,
          "None default", "?")


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

    # Structured-output schema (the shared _rubric_footer) — appended to EVERY
    # review prompt so subprocess AND Task seats are asked for the same parseable
    # markdown findings.py groups on (Task 1/2).
    for label, body in (("code_review", code), ("plan_review", plan)):
        check(f"{label} footer carries the four structured headers",
              "## VERDICT" in body and "## CRITERIA" in body
              and "## FINDINGS" in body and "## CONFIDENCE" in body,
              "VERDICT/CRITERIA/FINDINGS/CONFIDENCE headers", body[-400:])
        check(f"{label} footer keeps APPROVED/REVISE + low/medium/high",
              "APPROVED" in body and "REVISE" in body
              and "low" in body and "medium" in body and "high" in body,
              "verdict words + confidence scale", body[-400:])
        check(f"{label} footer states the MINOR/BLOCKING detail budget + prose fallback",
              "Detail budget" in body and "WHY:" in body and "FIX:" in body
              and "plain prose" in body,
              "detail budget + fallback", body[-400:])

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
        capture_output=True, text=True, env=env if env is not None else _neutral_env(),
        cwd=cwd, timeout=timeout,
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
        capture_output=True, text=True, env=env if env is not None else _neutral_env(),
        cwd=cwd, timeout=timeout,
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

        # agy print_timeout via per-repo config (env retired); cwd=repo so the
        # loader resolves repo/.crew/config.toml. Keeps the hung agy fake from
        # waiting the 8m default and hanging the suite.
        (repo / ".crew").mkdir(parents=True, exist_ok=True)
        (repo / ".crew" / "config.toml").write_text('[seats.agy]\nprint_timeout = "1s"\n')
        env = path_with(bins)

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
        # agy print_timeout via per-repo config (env retired); cwd=repo resolves it.
        (repo / ".crew").mkdir(parents=True, exist_ok=True)
        (repo / ".crew" / "config.toml").write_text('[seats.agy]\nprint_timeout = "1s"\n')
        env = path_with(bins)
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
    for task_seat in ("opus", "sonnet", "fable"):
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
        # agy print_timeout via config; _run_cli has no cwd, so resolve the loader
        # via CLAUDE_PROJECT_DIR (env retired — no 8m hang on the agy fake).
        (Path(td) / ".crew").mkdir(parents=True, exist_ok=True)
        (Path(td) / ".crew" / "config.toml").write_text('[seats.agy]\nprint_timeout = "1s"\n')
        env = path_with(bins)
        env["CLAUDE_PROJECT_DIR"] = td
        # Pin the two fake subprocess seats EXPLICITLY via --seats so this exercises
        # the council fan-out deterministically, independent of the default panel
        # (CREW_MA_SEATS is retired — the flag is now the only override).
        proc = _run_cli(
            ["council", "Pick one.", "--seats", "codex,agy", "--json", "--timeout", "5"],
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
        # agy print_timeout via config (CLAUDE_PROJECT_DIR; env retired).
        (Path(td) / ".crew").mkdir(parents=True, exist_ok=True)
        (Path(td) / ".crew" / "config.toml").write_text('[seats.agy]\nprint_timeout = "1s"\n')
        env = path_with(bins)
        env["CLAUDE_PROJECT_DIR"] = td
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
        # agy print_timeout via config (CLAUDE_PROJECT_DIR; env retired).
        (Path(td) / ".crew").mkdir(parents=True, exist_ok=True)
        (Path(td) / ".crew" / "config.toml").write_text('[seats.agy]\nprint_timeout = "1s"\n')
        env = path_with(bins)
        env["CLAUDE_PROJECT_DIR"] = td
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
    from multiagent.providers import cursor, get_provider, known_seat_names
    from multiagent.providers.cursor import CursorProvider
    from multiagent import cli

    CURSOR_SEATS = shipped_seats("cursor")

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
            prov = CursorProvider("cursor-glm", "glm-5.2-max")
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

            # config [seats.<seat>].model overrides the pinned default (env retired).
            # The catalog folds the config over the shipped pin, so the override
            # rides the seat BUILT for that config (get_provider), not a provider
            # pinned before the config existed.
            with project_config('[seats.cursor-glm]\nmodel = "glm-override"\n'):
                r2 = get_provider("cursor-glm").run("X", timeout=10)
            check("[seats.<seat>].model config overrides the pinned model",
                  r2.model == "glm-override", "glm-override", str(r2.model))

            # cursor-auto's config model override (mirrors the glm case).
            with project_config('[seats.cursor-auto]\nmodel = "auto-override"\n'):
                r3 = get_provider("cursor-auto").run("X", timeout=10)
            check("[seats.cursor-auto].model config overrides cursor-auto's pinned model",
                  r3.model == "auto-override", "auto-override", str(r3.model))
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

    # (a.6) Backend missing/corrupt: a copy of
    # plugins/crew/ with scripts/crew-state.py REMOVED must NOT raw-traceback,
    # `crew state show bl` exits 2 with the graceful "state backend
    # missing/corrupt … reinstall" line on stderr.
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "crew"
        shutil.copytree(PLUGIN_ROOT, dst)
        (dst / "scripts" / "crew-state.py").unlink()
        crew_bin = dst / "crew"
        try:
            os.chmod(crew_bin, 0o755)
        except OSError:
            pass
        proc = subprocess.run(
            [str(crew_bin), "state", "show", "bl"],
            capture_output=True, text=True, env=_neutral_env(),
            cwd=td, timeout=30,
        )
        check("missing crew-state.py: exits 2 (graceful backend error)",
              proc.returncode == 2, "2", f"{proc.returncode}: {proc.stderr[:150]}")
        check("missing crew-state.py: emits the reinstall diagnostic",
              "state backend missing/corrupt" in proc.stderr,
              "reinstall diagnostic", proc.stderr[:150])
        check("missing crew-state.py: NO raw traceback",
              "Traceback" not in proc.stderr, "no traceback", proc.stderr[:150])

    # (a.7) Version floor: the engine needs stdlib tomllib, so the dispatcher
    # refuses < 3.11 with ONE line and no traceback. Simulated (a real 3.10
    # interpreter is not assumable): patch sys.version_info, then run the
    # dispatcher through runpy as __main__. The guard must fire BEFORE the
    # multiagent.cli import, so this also proves it sits above that import.
    for sub in (["seats"], ["state", "show", "bl"]):
        prog = (
            "import sys, runpy\n"
            "sys.version_info = (3, 10, 0)\n"
            f"sys.argv = ['crew', *{sub!r}]\n"
            f"runpy.run_path({str(CREW_DISPATCHER)!r}, run_name='__main__')\n"
        )
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [sys.executable, "-c", prog],
                capture_output=True, text=True, env=_neutral_env(), cwd=td, timeout=30,
            )
        label = " ".join(sub)
        err_lines = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
        check(f"crew {label} on simulated <3.11: exits NONZERO",
              proc.returncode != 0, "nonzero", f"{proc.returncode}: {proc.stderr[:150]}")
        check(f"crew {label} on simulated <3.11: ONE stderr line naming 3.11, no traceback",
              len(err_lines) == 1 and "3.11" in err_lines[0] and "Traceback" not in proc.stderr,
              "one line mentioning 3.11", repr(proc.stderr[:200]))


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
    log_section("layered config loaders + typed getters (config.py)")
    import io  # noqa: E402
    from contextlib import redirect_stderr  # noqa: E402
    from multiagent import config, seats  # noqa: E402
    from multiagent.providers import get_provider  # noqa: E402
    from multiagent.providers.cursor import CursorProvider  # noqa: E402

    # `project` = the module-level per-repo ctxmgr (global layer neutralized so a
    # developer's real ~/.crew-config.toml can't leak in). `global_home` /
    # `crew_config` cover the new global tier + per-repo-beats-global precedence.
    project = project_config

    VALID = textwrap.dedent("""
        default_panel = "lite"

        [seats.codex]
        model = "gpt-5.5"
        reasoning_effort = "high"

        [seats.codex-luna]
        model = "luna-config"
        reasoning_effort = "low"

        [seats.agy]
        model = "Gemini Config Model"
        print_timeout = "3m"

        [seats.cursor-glm]
        model = "glm-config-max"

        [tuning]
        timeout = 777
    """)

    # 1. Valid TOML -> the catalog folds each [seats.<name>] table over the shipped
    #    pin; default_panel/default_timeout keep their own getters.
    with project(VALID):
        check("config valid: default_panel() -> 'lite'",
              config.default_panel() == "lite", "lite", str(config.default_panel()))
        check("config valid: seat_spec('codex').model -> 'gpt-5.5'",
              seats.seat_spec("codex").model == "gpt-5.5",
              "gpt-5.5", str(seats.seat_spec("codex").model))
        check("config valid: seat_spec('agy').model -> 'Gemini Config Model'",
              seats.seat_spec("agy").model == "Gemini Config Model",
              "Gemini Config Model", str(seats.seat_spec("agy").model))
        check("config valid: seat_spec('cursor-glm').model -> 'glm-config-max'",
              seats.seat_spec("cursor-glm").model == "glm-config-max",
              "glm-config-max", str(seats.seat_spec("cursor-glm").model))
        check("config valid: seat_spec('codex').reasoning_effort -> 'high'",
              seats.seat_spec("codex").reasoning_effort == "high",
              "high", str(seats.seat_spec("codex").reasoning_effort))
        # Per-seat lookup: each codex seat reads ITS OWN table; the luna value
        # does not bleed into the bare codex seat (and vice versa).
        check("config valid: seat_spec('codex-luna').reasoning_effort -> 'low'",
              seats.seat_spec("codex-luna").reasoning_effort == "low",
              "low", str(seats.seat_spec("codex-luna").reasoning_effort))
        check("config valid: seat_spec('codex-luna').model -> 'luna-config'",
              seats.seat_spec("codex-luna").model == "luna-config",
              "luna-config", str(seats.seat_spec("codex-luna").model))
        check("config valid: seat_spec('agy').print_timeout -> '3m'",
              seats.seat_spec("agy").print_timeout == "3m",
              "3m", str(seats.seat_spec("agy").print_timeout))
        check("config valid: default_timeout() -> 777",
              config.default_timeout() == 777, "777", str(config.default_timeout()))
        # A seat the config does not tune keeps its shipped pin and no per-provider
        # tune (cursor-gpt is opt-in, untouched here).
        check("config valid: an untuned seat keeps its shipped model, no tune",
              seats.seat_spec("cursor-gpt").model == "gpt-5.5-extra-high"
              and seats.seat_spec("codex-terra").reasoning_effort is None,
              "shipped pin, tune None",
              f"{seats.seat_spec('cursor-gpt').model} / "
              f"{seats.seat_spec('codex-terra').reasoning_effort}")

    # 2. Missing file -> default_panel/default_timeout None (no crash, no warn); an
    #    untuned seat carries no per-provider tune.
    with project("", write_file=False):
        buf = io.StringIO()
        with redirect_stderr(buf):
            vals = (config.default_panel(), config.default_timeout(),
                    seats.seat_spec("codex").reasoning_effort,
                    seats.seat_spec("agy").print_timeout)
        check("config missing file: getters/tunes None, no stderr noise",
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
            re_ = seats.seat_spec("codex").reasoning_effort
            pt = seats.seat_spec("agy").print_timeout
            tmo = config.default_timeout()
            kept = seats.seat_spec("codex").model
        check("config bad fields: each invalid field dropped (tune/getter -> None)",
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

    # 5a. debate_panel() getter: valid [debate].panel -> its value.
    with project("[debate]\npanel = \"full\"\n"):
        check("config valid: debate_panel() -> 'full'",
              config.debate_panel() == "full", "full", str(config.debate_panel()))
    # 5b. Missing [debate] table -> None (no crash); a default_panel alone does NOT
    #     leak into debate_panel() (that fallback is the resolver's job, not the getter).
    with project("default_panel = \"lite\"\n"):
        check("config missing [debate] table -> debate_panel() None",
              config.debate_panel() is None, "None", str(config.debate_panel()))
    # 5c. [debate] table present but no panel key -> None.
    with project("[debate]\nrounds = 3\n"):
        check("config [debate] without panel key -> debate_panel() None",
              config.debate_panel() is None, "None", str(config.debate_panel()))
    # 5d. Unknown [debate].panel value -> None (validated vs PANEL_PRESETS), no raise.
    with project("[debate]\npanel = \"huge\"\n"):
        buf = io.StringIO()
        with redirect_stderr(buf):
            dp = config.debate_panel()
        check("config unknown [debate].panel -> None (validated, no raise)",
              dp is None, "None", str(dp))
        check("config unknown [debate].panel warns to stderr",
              "debate" in buf.getvalue().lower() and "panel" in buf.getvalue().lower(),
              "stderr warn", repr(buf.getvalue()[:160]))
    # 5e. Wrong-typed [debate].panel (non-string) -> None.
    with project("[debate]\npanel = 7\n"):
        check("config non-string [debate].panel -> None",
              config.debate_panel() is None, "None", str(config.debate_panel()))

    # ---- Precedence: per-repo BEATS global BEATS built-in (env retired) ----------
    from multiagent.cli import _resolve_timeout  # noqa: E402

    def _codex_model(seat="codex"):
        # The catalog now owns per-seat model resolution (config over the shipped
        # pin); this shim keeps the precedence assertions reading the same value
        # the deleted config.seat_model getter used to return.
        return seats.seat_spec(seat).model

    # 7a. per-repo [tuning].timeout beats global; explicit arg beats both layers.
    with crew_config(project="[tuning]\ntimeout = 777\n", glob="[tuning]\ntimeout = 120\n"):
        check("precedence: per-repo [tuning].timeout (777) BEATS global (120)",
              _resolve_timeout(None) == 777, "777", str(_resolve_timeout(None)))
        check("precedence: explicit --timeout arg beats BOTH config layers",
              _resolve_timeout(45) == 45, "45", str(_resolve_timeout(45)))
    # 7b. global-only [tuning].timeout wins over builtin (no per-repo file).
    with crew_config(glob="[tuning]\ntimeout = 222\n"):
        check("precedence: global [tuning].timeout (222) wins over builtin when no per-repo",
              _resolve_timeout(None) == 222, "222", str(_resolve_timeout(None)))

    # 7c. per-repo [seats.codex].model beats global; global-only applies.
    with crew_config(project="[seats.codex]\nmodel = \"codex-repo\"\n",
                     glob="[seats.codex]\nmodel = \"codex-global\"\n"):
        check("precedence: per-repo [seats.codex].model BEATS global",
              _codex_model() == "codex-repo", "codex-repo", str(_codex_model()))
    with crew_config(glob="[seats.codex]\nmodel = \"codex-global\"\n"):
        check("precedence: global-only [seats.codex].model applies (no per-repo)",
              _codex_model() == "codex-global", "codex-global", str(_codex_model()))

    # 7c2. No config in either layer -> _codex_model falls back to the seat's
    #      built-in CODEX_SEATS pin (per codex seat, not None).
    with crew_config():
        check("no config: _codex_model() -> codex's built-in pin gpt-5.6-sol",
              _codex_model() == "gpt-5.6-sol", "gpt-5.6-sol", str(_codex_model()))
        check("no config: _codex_model('codex-luna') -> its pin gpt-5.6-luna",
              _codex_model("codex-luna") == "gpt-5.6-luna",
              "gpt-5.6-luna", str(_codex_model("codex-luna")))
    # A [seats.codex-luna].model override beats the pin AND stays seat-scoped
    # (the bare codex seat keeps its own pin).
    with crew_config(project="[seats.codex-luna]\nmodel = \"luna-repo\"\n"):
        check("[seats.codex-luna].model BEATS the built-in pin",
              _codex_model("codex-luna") == "luna-repo",
              "luna-repo", str(_codex_model("codex-luna")))
        check("codex-luna config does NOT leak into the bare codex seat",
              _codex_model() == "gpt-5.6-sol", "gpt-5.6-sol", str(_codex_model()))

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
                get_provider("codex").run("PROMPT", timeout=15)
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

    # 8c. Registry codex seats: the pinned --model lands in argv when no explicit
    #     model is given, and [seats.codex-luna].reasoning_effort reaches ONLY the
    #     luna seat (the bare codex seat keeps the xhigh default).
    with project("[seats.codex-luna]\nreasoning_effort = \"low\"\n"):
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
                get_provider("codex-luna").run("PROMPT", timeout=15)
                argv_luna = json.loads(cap.read_text())
                get_provider("codex").run("PROMPT", timeout=15)
                argv_codex = json.loads(cap.read_text())
            finally:
                os.environ["PATH"] = old_path
            check("per-seat: codex-luna argv carries its pinned --model gpt-5.6-luna",
                  "--model" in argv_luna and "gpt-5.6-luna" in argv_luna,
                  "--model gpt-5.6-luna", str(argv_luna))
            check("per-seat: [seats.codex-luna].reasoning_effort -> luna argv effort=low",
                  "model_reasoning_effort=low" in argv_luna, "low in argv", str(argv_luna))
            check("per-seat: bare codex argv carries its pinned --model gpt-5.6-sol",
                  "--model" in argv_codex and "gpt-5.6-sol" in argv_codex,
                  "--model gpt-5.6-sol", str(argv_codex))
            check("per-seat: luna effort does NOT leak into codex (stays xhigh)",
                  "model_reasoning_effort=xhigh" in argv_codex,
                  "xhigh in argv", str(argv_codex))
            check("per-seat: codex hermetic flags identical across codex seats",
                  "--ignore-user-config" in argv_luna and "--ignore-user-config" in argv_codex,
                  "--ignore-user-config in both", f"luna={argv_luna} codex={argv_codex}")

    # 9. cursor seat model from config reaches CursorProvider.run()'s --model;
    #    per-repo BEATS global (env retired).
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
    with tempfile.TemporaryDirectory() as bd:
        d = Path(bd)
        make_fake_bin(d, "agent", fake_agent)
        cap = d / "argv.json"
        old_path = os.environ["PATH"]
        os.environ["PATH"] = str(d) + os.pathsep + old_path
        os.environ["CURSOR_CAPTURE"] = str(cap)
        try:
            with project("[seats.cursor-glm]\nmodel = \"glm-from-config\"\n"):
                r = get_provider("cursor-glm").run("P", timeout=10)
                argv = json.loads(cap.read_text())
                check("per-seat: config cursor model reaches run() --model argv",
                      "glm-from-config" in argv and r.model == "glm-from-config",
                      "glm-from-config", f"argv_model={'glm-from-config' in argv} r.model={r.model}")
            with crew_config(project="[seats.cursor-glm]\nmodel = \"glm-repo\"\n",
                             glob="[seats.cursor-glm]\nmodel = \"glm-global\"\n"):
                r2 = get_provider("cursor-glm").run("P", timeout=10)
                check("per-seat: per-repo cursor model BEATS global ~/.crew-config.toml",
                      r2.model == "glm-repo", "glm-repo", str(r2.model))
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("CURSOR_CAPTURE", None)

    # 10. agy model + print_timeout from config reach the argv; per-repo BEATS global.
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
    with crew_config(project="[seats.agy]\nmodel = \"Agy Repo\"\nprint_timeout = \"4m\"\n",
                     glob="[seats.agy]\nmodel = \"Agy Global\"\nprint_timeout = \"9m\"\n"):
        res2, cap2 = _run_agy_with_fake("""
        import sys, json
        json.dump(sys.argv[1:], open(CAPTURE, "w"))
        sys.stdout.write("review body")
        sys.exit(0)
        """)
        check("per-seat: per-repo agy model/print_timeout BEAT the global file",
              cap2 is not None and "Agy Repo" in cap2 and "4m" in cap2
              and "Agy Global" not in cap2 and "9m" not in cap2,
              "per-repo wins over global", str(cap2))

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
    from multiagent import seats as _seatcat  # noqa: E402
    _cursor_all = sorted(_seatcat.group_tokens()["cursor"])
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
              and obj["subprocess_seats"] == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]
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
              and obj["subprocess_seats"] == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]
              and obj["task_seats"] == ["opus", "sonnet"],
              "full fallback on invalid", f"rc={proc.returncode} obj={obj}")


def test_global_roster_availability():
    log_section("global ~/.crew-config.toml + [panels] roster + seat availability (getters)")
    import io  # noqa: E402
    from contextlib import redirect_stderr  # noqa: E402
    from multiagent import config, seats  # noqa: E402

    # --- Global-only value applies (no per-repo file) -------------------------
    with global_home('default_panel = "lite"\n'):
        check("global-only default_panel applies when no per-repo file",
              config.default_panel() == "lite", "lite", str(config.default_panel()))
    with global_home('[seats.agy]\nmodel = "Global Agy"\n'):
        check("global-only [seats.agy].model applies (via the catalog)",
              seats.seat_spec("agy").model == "Global Agy",
              "Global Agy", str(seats.seat_spec("agy").model))

    # --- panels() roster ------------------------------------------------------
    # Override a built-in preset AND define a custom one.
    with project_config('[panels]\nfull = ["codex", "opus"]\nquick = ["codex"]\n'):
        p = config.panels()
        check("panels(): override builtin full + custom quick",
              p == {"full": ["codex", "opus"], "quick": ["codex"]},
              "{'full': ['codex','opus'], 'quick': ['codex']}", str(p))
        # default_panel="quick" is now ACCEPTED (validates vs PANEL_PRESETS ∪ panels()).
    with project_config('default_panel = "quick"\n[panels]\nquick = ["codex"]\n'):
        check("default_panel='quick' accepted (matches [panels] override, also a builtin name)",
              config.default_panel() == "quick", "quick", str(config.default_panel()))
    # Unknown seat in a roster list is dropped (warn once); the entry survives.
    with project_config('[panels]\nmix = ["codex", "nope"]\n'):
        buf = io.StringIO()
        with redirect_stderr(buf):
            p = config.panels()
        check("panels(): unknown roster seat dropped (entry kept)",
              p == {"mix": ["codex"]}, "{'mix': ['codex']}", str(p))
        check("panels(): unknown roster seat warns to stderr",
              "nope" in buf.getvalue(), "warn mentions 'nope'", repr(buf.getvalue()[:160]))
    # An entry whose EVERY element drops is omitted; all-dropped -> None.
    with project_config('[panels]\nbad = ["nope", "alsobad"]\n'):
        buf = io.StringIO()
        with redirect_stderr(buf):
            p = config.panels()
        check("panels(): an entry with all-unknown seats is omitted -> None",
              p is None, "None", str(p))
    # Non-table [panels] -> None (+ warn).
    with project_config('panels = "not a table"\n'):
        buf = io.StringIO()
        with redirect_stderr(buf):
            p = config.panels()
        check("panels(): non-table -> None (+ warn)",
              p is None and "panels" in buf.getvalue().lower(),
              "None + warn", f"p={p} stderr={buf.getvalue()[:120]!r}")
    # No [panels] anywhere -> None.
    with project_config("", write_file=False):
        check("panels(): absent -> None", config.panels() is None, "None", str(config.panels()))
    # per-repo [panels] entry overrides global PER NAME; global-only names remain.
    with crew_config(project='[panels]\nfull = ["codex"]\n',
                     glob='[panels]\nfull = ["opus"]\nextra = ["sonnet"]\n'):
        p = config.panels()
        check("panels(): per-repo overrides global per name; global-only name remains",
              p == {"full": ["codex"], "extra": ["sonnet"]},
              "{'full': ['codex'], 'extra': ['sonnet']}", str(p))

    # --- seat availability (catalog `available` flag) -------------------------
    def avail(seat):
        return seats.seat_spec(seat).available
    with project_config("", write_file=False):
        check("seat available default True (no config)",
              avail("cursor-glm") is True, "True", str(avail("cursor-glm")))
    with project_config('[seats.cursor-glm]\navailable = false\n'):
        check("seat available explicit False",
              avail("cursor-glm") is False, "False", str(avail("cursor-glm")))
    # per-repo available overrides global.
    with crew_config(project='[seats.cursor-glm]\navailable = true\n',
                     glob='[seats.cursor-glm]\navailable = false\n'):
        check("seat available: per-repo True beats global False",
              avail("cursor-glm") is True, "True", str(avail("cursor-glm")))
    with crew_config(glob='[seats.cursor-glm]\navailable = false\n'):
        check("seat available: global-only False applies",
              avail("cursor-glm") is False, "False", str(avail("cursor-glm")))
    # Non-bool available -> treated as available (never silently HIDE a seat) + warn.
    with project_config('[seats.cursor-glm]\navailable = "yes"\n'):
        buf = io.StringIO()
        with redirect_stderr(buf):
            a = avail("cursor-glm")
        check("seat available: non-bool -> True (opt-out safety) + warn",
              a is True and "available" in buf.getvalue().lower(),
              "True + warn", f"avail={a} stderr={buf.getvalue()[:120]!r}")


def _clean_env(proj: str) -> dict:
    """Subprocess env for a dispatcher panel/availability test: CLAUDE_PROJECT_DIR
    at the temp repo + HOME at an isolated empty dir (so a developer's real
    ~/.crew-config.toml never leaks into the assertion)."""
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = proj
    env["HOME"] = proj  # no ~/.crew-config.toml under the temp project dir
    return env


def test_panel_availability_consistency():
    log_section("[panels] roster + availability consistency across commands (subprocess)")

    def _write_cfg(proj: Path, toml: str) -> None:
        (proj / ".crew").mkdir(parents=True, exist_ok=True)
        (proj / ".crew" / "config.toml").write_text(toml)

    # 1. [panels].full override changes the resolved full set in BOTH ad-hoc
    #    `crew seats` (no --seats) AND review-prep.
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, '[panels]\nfull = ["codex", "opus"]\n')
        _write_plan(proj)
        env = _clean_env(td)
        seats_p = _run_dispatcher(["seats"], cwd=td, env=env, timeout=30)
        lines = [ln for ln in seats_p.stdout.splitlines() if ln.strip()]
        check("ad-hoc `crew seats` honors [panels].full override (codex only; opus is a Task seat)",
              seats_p.returncode == 0 and lines == ["codex"], "['codex']", str(lines))
        rp = _run_dispatcher(["review-prep", "plan.md", "--session-id", "po"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("review-prep honors [panels].full override (subprocess [codex], task [opus])",
              obj["subprocess_seats"] == ["codex"] and obj["task_seats"] == ["opus"],
              "subprocess [codex], task [opus]", str(obj))

    # 2. A custom [panels].quick resolves via --panel quick (choices removed) AND
    #    via default_panel="quick".
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, '[panels]\nquick = ["codex", "opus"]\n')
        _write_plan(proj)
        env = _clean_env(td)
        rp = _run_dispatcher(["review-prep", "plan.md", "--panel", "quick", "--session-id", "pq"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout) if rp.returncode == 0 else None
        check("--panel quick resolves a custom [panels] preset (choices removed)",
              rp.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex"] and obj["task_seats"] == ["opus"],
              "subprocess [codex], task [opus]", f"rc={rp.returncode} obj={obj}")
        # debate path: `crew seats --debate --panel quick` resolves it too.
        ds = _run_dispatcher(["seats", "--debate", "--panel", "quick"],
                             cwd=td, env=env, timeout=30)
        dlines = [ln for ln in ds.stdout.splitlines() if ln.strip()]
        check("`crew seats --debate --panel quick` resolves the custom preset (subprocess + task)",
              ds.returncode == 0 and dlines == ["codex", "opus"], "['codex','opus']", str(dlines))
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, 'default_panel = "quick"\n[panels]\nquick = ["codex"]\n')
        _write_plan(proj)
        env = _clean_env(td)
        rp = _run_dispatcher(["review-prep", "plan.md", "--session-id", "pdq"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("default_panel='quick' resolves with no flags",
              obj["subprocess_seats"] == ["codex"] and obj["task_seats"] == [],
              "subprocess [codex], task []", str(obj))

    # 3. Unknown --panel name falls back to a CONFIGURED [panels].full (not builtin).
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, '[panels]\nfull = ["codex"]\n')
        _write_plan(proj)
        env = _clean_env(td)
        rp = _run_dispatcher(["review-prep", "plan.md", "--panel", "bogus", "--session-id", "pb"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("unknown --panel name falls back to the CONFIGURED [panels].full",
              obj["subprocess_seats"] == ["codex"] and obj["task_seats"] == [],
              "configured full [codex]", str(obj))

    # 4. Availability: available=false drops the seat from a DEFAULT panel silently
    #    (ad-hoc `crew seats` + review-prep both).
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, '[seats.cursor-auto]\navailable = false\n')
        _write_plan(proj)
        env = _clean_env(td)
        seats_p = _run_dispatcher(["seats"], cwd=td, env=env, timeout=30)
        lines = [ln for ln in seats_p.stdout.splitlines() if ln.strip()]
        check("ad-hoc `crew seats` drops an available=false seat from the default panel",
              "cursor-auto" not in lines and "codex" in lines and "agy" in lines,
              "no cursor-auto", str(lines))
        rp = _run_dispatcher(["review-prep", "plan.md", "--session-id", "pa1"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("review-prep drops an available=false seat from the default panel",
              "cursor-auto" not in obj["subprocess_seats"] and "codex" in obj["subprocess_seats"],
              "no cursor-auto", str(obj["subprocess_seats"]))

    # 5. Explicitly-named unavailable seat -> SKIPPED WITH a one-time stderr note.
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, '[seats.cursor-auto]\navailable = false\n')
        env = _clean_env(td)
        seats_p = _run_dispatcher(["seats", "--seats", "codex,cursor-auto"],
                                  cwd=td, env=env, timeout=30)
        lines = [ln for ln in seats_p.stdout.splitlines() if ln.strip()]
        check("explicit --seats <unavailable> skipped, the rest run",
              lines == ["codex"], "['codex']", str(lines))
        check("explicit --seats <unavailable> emits a one-time stderr skip-note",
              "unavailable" in seats_p.stderr.lower() and "cursor-auto" in seats_p.stderr,
              "stderr skip-note", repr(seats_p.stderr[:160]))

    # 6. Empty-after-filter -> warn + fall back to the UNFILTERED panel (run rather
    #    than nothing). An explicitly-named lone unavailable seat still RUNS.
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, '[seats.cursor-auto]\navailable = false\n')
        env = _clean_env(td)
        seats_p = _run_dispatcher(["seats", "--seats", "cursor-auto"],
                                  cwd=td, env=env, timeout=30)
        lines = [ln for ln in seats_p.stdout.splitlines() if ln.strip()]
        check("empty-after-filter falls back to the unfiltered panel (lone unavailable still runs)",
              lines == ["cursor-auto"], "['cursor-auto']", str(lines))
        check("empty-after-filter warns to stderr",
              "unavailable" in seats_p.stderr.lower(), "stderr warn", repr(seats_p.stderr[:160]))

    # 7. A --panel preset CONTAINING an unavailable seat skips it WITH a note
    #    (preset members are explicit) — review-prep.
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, '[seats.cursor-auto]\navailable = false\n')
        _write_plan(proj)
        env = _clean_env(td)
        rp = _run_dispatcher(["review-prep", "plan.md", "--panel", "full", "--session-id", "ppf"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("--panel full drops its unavailable member from subprocess_seats",
              "cursor-auto" not in obj["subprocess_seats"] and "codex" in obj["subprocess_seats"],
              "no cursor-auto", str(obj["subprocess_seats"]))
        check("--panel full unavailable member -> one-time stderr skip-note",
              "unavailable" in rp.stderr.lower() and "cursor-auto" in rp.stderr,
              "stderr skip-note", repr(rp.stderr[:160]))

    # 8. BLOCKING: review-prep availability is WHOLE-PANEL, not per-split.
    #    Disabling BOTH task seats in `full` empties the TASK split only — it must
    #    NOT trip the unfiltered-panel fallback (which would re-add opus/sonnet)
    #    because the five subprocess seats remain. Expected: subprocess intact,
    #    task_seats == [] (deliberately-disabled seat-KIND stays disabled).
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj, '[seats.opus]\navailable = false\n[seats.sonnet]\navailable = false\n')
        _write_plan(proj)
        env = _clean_env(td)
        rp = _run_dispatcher(["review-prep", "plan.md", "--panel", "full", "--session-id", "wp1"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("review-prep disabling BOTH task seats in full -> task_seats == [] (no opus/sonnet restoration)",
              rp.returncode == 0
              and obj["subprocess_seats"] == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]
              and obj["task_seats"] == [],
              "subprocess intact, task []", str(obj))
        # The empty TASK split must NOT fire the all-unavailable whole-panel warn
        # (the panel as a whole is NOT empty).
        check("review-prep disabling both task seats does NOT emit the all-unavailable warn",
              "all seats in the resolved panel" not in rp.stderr,
              "no whole-panel fallback warn", repr(rp.stderr[:200]))

    # 8b. Symmetric: disabling ALL FIVE subprocess seats in `full` empties the
    #     SUBPROCESS split only — task seats remain, no fallback, review runs
    #     task-only (subprocess_seats == []).
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj,
                   '[seats.codex]\navailable = false\n'
                   '[seats.codex-luna]\navailable = false\n'
                   '[seats.agy]\navailable = false\n'
                   '[seats.cursor-auto]\navailable = false\n'
                   '[seats.cursor-composer]\navailable = false\n')
        _write_plan(proj)
        env = _clean_env(td)
        rp = _run_dispatcher(["review-prep", "plan.md", "--panel", "full", "--session-id", "wp2"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("review-prep disabling ALL subprocess seats in full -> subprocess_seats == [] (task seats kept)",
              rp.returncode == 0
              and obj["subprocess_seats"] == []
              and obj["task_seats"] == ["opus", "sonnet"],
              "subprocess [], task [opus,sonnet]", str(obj))

    # 9. Disabling EVERY seat in `full` -> the SINGLE whole-panel fallback fires
    #    ONCE (one warn) and restores the unfiltered panel (subprocess + task),
    #    rather than running nothing.
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj,
                   '[seats.codex]\navailable = false\n'
                   '[seats.codex-luna]\navailable = false\n'
                   '[seats.agy]\navailable = false\n'
                   '[seats.cursor-auto]\navailable = false\n'
                   '[seats.cursor-composer]\navailable = false\n'
                   '[seats.opus]\navailable = false\n'
                   '[seats.sonnet]\navailable = false\n')
        _write_plan(proj)
        env = _clean_env(td)
        rp = _run_dispatcher(["review-prep", "plan.md", "--panel", "full", "--session-id", "wp3"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("review-prep disabling EVERY seat -> whole-panel fallback restores the unfiltered panel",
              rp.returncode == 0
              and obj["subprocess_seats"] == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]
              and obj["task_seats"] == ["opus", "sonnet"],
              "unfiltered full restored", str(obj))
        check("review-prep all-unavailable -> the whole-panel fallback warn fires once",
              "all seats in the resolved panel" in rp.stderr,
              "all-unavailable warn", repr(rp.stderr[:200]))

    # 9b. BLOCKING regression: the opaque --task-seats override counts toward the
    #     final panel being NON-empty. Disable ALL FIVE subprocess seats in `full`
    #     AND pass `--task-seats opus` (which sets task_raw=[] and supplies the
    #     panel via explicit_task_seats). The whole-panel emptiness check must see
    #     the override as the (non-empty) task panel and NOT restore the disabled
    #     subprocess seats. Expected: subprocess_seats == [], task_seats == [opus],
    #     NO all-unavailable warn.
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td)
        _write_cfg(proj,
                   '[seats.codex]\navailable = false\n'
                   '[seats.codex-luna]\navailable = false\n'
                   '[seats.agy]\navailable = false\n'
                   '[seats.cursor-auto]\navailable = false\n'
                   '[seats.cursor-composer]\navailable = false\n')
        _write_plan(proj)
        env = _clean_env(td)
        rp = _run_dispatcher(["review-prep", "plan.md", "--panel", "full",
                              "--task-seats", "opus", "--session-id", "wp4"],
                             cwd=td, env=env, timeout=30)
        obj = json.loads(rp.stdout)
        check("review-prep all-subprocess-unavailable + --task-seats opus -> subprocess [] NOT restored, task [opus]",
              rp.returncode == 0
              and obj["subprocess_seats"] == []
              and obj["task_seats"] == ["opus"],
              "subprocess [], task [opus]", str(obj))
        check("review-prep --task-seats override keeps panel non-empty -> NO all-unavailable warn",
              "all seats in the resolved panel" not in rp.stderr,
              "no whole-panel fallback warn", repr(rp.stderr[:200]))


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
        from multiagent import seats as _seatcat  # noqa: E402
        _cursor = set(_seatcat.group_tokens()["cursor"])
        check("review-prep subprocess_seats are registry seats only (no opus/sonnet)",
              proc.returncode == 0 and "opus" not in subs and "sonnet" not in subs
              and "codex" in subs and any(s in _cursor for s in subs),
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
        from multiagent import seats as _seatcat  # noqa: E402
        expected_cursor = sorted(_seatcat.group_tokens()["cursor"])
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
    from multiagent import seats as _seatcat  # noqa: E402
    _registered_cursor = sorted(_seatcat.group_tokens()["cursor"])

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
              and obj["subprocess_seats"] == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]
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

    # 12. Mixed unified --seats split: subprocess vs TASK_SEAT_NAMES. The REMOVED
    #     opus-4.6 seat (its claude-opus-4-6 pin is rejected by the Task tool's
    #     model validation, so it could never be seated) is dropped like any
    #     unknown name — a removal regression guard.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--seats", "codex,opus,opus-4.6", "--session-id", "ms"], td)
        check("review-prep --seats codex,opus,opus-4.6: the REMOVED opus-4.6 seat is dropped",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex"]
              and obj["task_seats"] == ["opus"]
              and obj["task_seat_models"] == {"opus": "opus"},
              "opus-4.6 dropped from both lists", str(obj))

    # 12b. The opt-in fable Task seat splits as a task seat and pins its OWN
    #      name (a first-class alias — no MODEL_OVERRIDES entry).
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        proc, obj = _prep(["--seats", "codex,fable", "--session-id", "fs"], td)
        check("review-prep --seats codex,fable: fable is a task seat pinned model='fable'",
              proc.returncode == 0 and obj is not None
              and obj["subprocess_seats"] == ["codex"]
              and obj["task_seats"] == ["fable"]
              and obj["task_seat_models"] == {"fable": "fable"},
              "fable task seat, own-name pin", str(obj))

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
              and obj["subprocess_seats"] == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]
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
              and obj["subprocess_seats"] == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]
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

    # 16. Staging<->spawn consistency: the comma-joined task_seats (incl. the
    #     opt-in fable) fed to `render --stage-all` stages one prompt per seat —
    #     the SAME filenames the spawn line reads, so they can never diverge.
    with tempfile.TemporaryDirectory() as td:
        _write_plan(Path(td))
        prep, obj = _prep(["--seats", "opus,sonnet,fable", "--session-id", "sc"], td)
        joined = ",".join(obj["task_seats"]) if obj else ""
        render = _run_dispatcher(
            ["render", "plan.md", "--mode", "review", "--stage-all", joined,
             "--session-id", "sc"], cwd=td, timeout=30)
        fable = Path(td) / ".crew" / "reviews" / "sc" / "prompt-fable.txt"
        opus = Path(td) / ".crew" / "reviews" / "sc" / "prompt-opus.txt"
        sonnet = Path(td) / ".crew" / "reviews" / "sc" / "prompt-sonnet.txt"
        check("review-prep task_seats join feeds --stage-all -> stages one prompt per seat",
              prep.returncode == 0 and render.returncode == 0
              and obj["task_seats"] == ["opus", "sonnet", "fable"]
              and opus.is_file() and sonnet.is_file() and fable.is_file(),
              "prompt-fable.txt staged from joined task_seats",
              f"join={joined!r} rc={render.returncode} fable={fable.is_file()}")


def test_debate_panel_resolver():
    log_section("crew seats --debate (config-aware debate panel resolver)")
    from multiagent import seats as _seatcat  # noqa: E402
    _cursor_all = sorted(_seatcat.group_tokens()["cursor"])

    def resolve(config_toml=None, extra_args=()):
        """Run `crew seats --debate [extra_args]` with CLAUDE_PROJECT_DIR pointing
        at a temp repo holding the given .crew/config.toml (or none). Returns the
        printed seat list (one per line). The dispatcher is a fresh subprocess, so
        config memoization needs no reset here."""
        with tempfile.TemporaryDirectory() as td:
            if config_toml is not None:
                crew = Path(td) / ".crew"
                crew.mkdir(parents=True)
                (crew / "config.toml").write_text(config_toml)
            env = {**_neutral_env(), "CLAUDE_PROJECT_DIR": td}
            proc = _run_dispatcher(["seats", "--debate", *extra_args],
                                   env=env, cwd=td, timeout=30)
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            return proc.returncode, lines

    FULL = ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer", "opus", "sonnet"]

    # 1. [debate].panel="full" BEATS default_panel="lite" -> debate resolves full.
    rc, lines = resolve("default_panel = \"lite\"\n\n[debate]\npanel = \"full\"\n")
    check("seats --debate: [debate].panel='full' BEATS default_panel='lite' -> full",
          rc == 0 and lines == FULL, str(FULL), f"rc={rc} {lines}")

    # 2. default_panel="lite" + no [debate] -> debate falls back to default_panel.
    rc, lines = resolve("default_panel = \"lite\"\n")
    check("seats --debate: default_panel='lite' + no [debate] -> lite (opus,sonnet)",
          rc == 0 and lines == ["opus", "sonnet"], "['opus', 'sonnet']",
          f"rc={rc} {lines}")

    # 3. Neither set -> built-in 'full'.
    rc, lines = resolve(None)
    check("seats --debate: no config -> built-in 'full'",
          rc == 0 and lines == FULL, str(FULL), f"rc={rc} {lines}")
    rc, lines = resolve("")  # empty config file, present but empty
    check("seats --debate: empty config -> built-in 'full'",
          rc == 0 and lines == FULL, str(FULL), f"rc={rc} {lines}")

    # 4. Explicit --panel BEATS [debate].panel (explicit wins).
    rc, lines = resolve("[debate]\npanel = \"full\"\n", ["--panel", "lite"])
    check("seats --debate --panel lite BEATS [debate].panel='full' (explicit wins)",
          rc == 0 and lines == ["opus", "sonnet"], "['opus', 'sonnet']",
          f"rc={rc} {lines}")

    # 4b. Explicit --seats BEATS [debate].panel (explicit wins; keeps Task seats).
    rc, lines = resolve("[debate]\npanel = \"full\"\n", ["--seats", "codex,opus"])
    check("seats --debate --seats codex,opus BEATS [debate].panel (explicit wins)",
          rc == 0 and lines == ["codex", "opus"], "['codex', 'opus']",
          f"rc={rc} {lines}")

    # 5. Resolved seat list for each tier is correct (solo + cursor expansion).
    rc, lines = resolve("[debate]\npanel = \"solo\"\n")
    check("seats --debate: [debate].panel='solo' -> ['opus']",
          rc == 0 and lines == ["opus"], "['opus']", f"rc={rc} {lines}")
    rc, lines = resolve("[debate]\npanel = \"cursor\"\n")
    check("seats --debate: [debate].panel='cursor' -> all cursor-* (group expanded), no task",
          rc == 0 and sorted(lines) == _cursor_all,
          str(_cursor_all), f"rc={rc} {lines}")

    # 6. Invalid [debate].panel -> falls back to default_panel (then full), no crash.
    rc, lines = resolve("default_panel = \"lite\"\n\n[debate]\npanel = \"bogus\"\n")
    check("seats --debate: invalid [debate].panel -> falls back to default_panel='lite'",
          rc == 0 and lines == ["opus", "sonnet"], "['opus', 'sonnet']",
          f"rc={rc} {lines}")

    # 7. BLOCKING regression guard: an explicit --seats with a path-traversal /
    #    unknown name is REJECTED (nonzero, nothing on stdout) so a hostile seat
    #    name can never reach .crew/debates/<dir>/<seat>.json. The valid subset
    #    that KEEPS a task seat (case 4b) must still pass.
    rc, lines = resolve(None, ["--seats", "cursor-../../x"])
    check("seats --debate --seats cursor-../../x -> REJECTED (nonzero, no stdout)",
          rc != 0 and lines == [], "rc!=0 and []", f"rc={rc} {lines}")
    rc, lines = resolve(None, ["--seats", "codex,bogusseat"])
    check("seats --debate --seats codex,bogusseat (unknown name) -> REJECTED",
          rc != 0 and lines == [], "rc!=0 and []", f"rc={rc} {lines}")
    # A valid subset incl. a task seat still succeeds and KEEPS opus.
    rc, lines = resolve(None, ["--seats", "codex,opus"])
    check("seats --debate --seats codex,opus (valid subset) -> KEEPS opus",
          rc == 0 and lines == ["codex", "opus"], "['codex', 'opus']",
          f"rc={rc} {lines}")

    # 7b. The REMOVED opus-4.6 seat (its claude-opus-4-6 pin is rejected by the
    #     Task tool's model validation) is now an UNKNOWN name -> rejected exactly
    #     like bogusseat. The opt-in fable Task seat is a member -> accepted.
    rc, lines = resolve(None, ["--seats", "opus-4.6"])
    check("seats --debate --seats opus-4.6 (REMOVED seat) -> REJECTED",
          rc != 0 and lines == [], "rc!=0 and []", f"rc={rc} {lines}")
    rc, lines = resolve(None, ["--seats", "opus,sonnet,fable"])
    check("seats --debate --seats opus,sonnet,fable -> all three KEPT (task seats)",
          rc == 0 and lines == ["opus", "sonnet", "fable"],
          "['opus', 'sonnet', 'fable']", f"rc={rc} {lines}")

    # 8. MINOR: `seats --panel <preset>` WITHOUT --debate errors (--panel only
    #    steers the debate resolver; it would otherwise be silently ignored).
    with tempfile.TemporaryDirectory() as td:
        env = {**_neutral_env(), "CLAUDE_PROJECT_DIR": td}
        proc = _run_dispatcher(["seats", "--panel", "lite"],
                               env=env, cwd=td, timeout=30)
    out_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    check("seats --panel lite WITHOUT --debate -> errors (nonzero, no stdout)",
          proc.returncode != 0 and out_lines == []
          and "--panel" in proc.stderr,
          "rc!=0, no stdout, stderr mentions --panel",
          f"rc={proc.returncode} stdout={out_lines} stderr={proc.stderr!r}")

    # 9. MINOR: a NAMED --panel's members are EXPLICIT in the debate resolver
    #    (mirror review-prep) — an unavailable member is skipped WITH a one-time
    #    stderr note, not silently dropped.
    with tempfile.TemporaryDirectory() as td:
        crew = Path(td) / ".crew"
        crew.mkdir(parents=True)
        (crew / "config.toml").write_text('[seats.cursor-auto]\navailable = false\n')
        env = {**_neutral_env(), "CLAUDE_PROJECT_DIR": td}
        proc = _run_dispatcher(["seats", "--debate", "--panel", "cursor"],
                               env=env, cwd=td, timeout=30)
    dlines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    check("seats --debate --panel cursor drops the unavailable member from the list",
          proc.returncode == 0 and "cursor-auto" not in dlines and "cursor-composer" in dlines,
          "no cursor-auto, cursor-composer kept", str(dlines))
    check("seats --debate --panel <named> unavailable member -> one-time stderr skip-note",
          "unavailable" in proc.stderr.lower() and "cursor-auto" in proc.stderr,
          "stderr skip-note", repr(proc.stderr[:160]))

    # 9b. WITHOUT a named --panel (default/[debate].panel resolution) the same
    #     unavailable seat is dropped SILENTLY — no user-named seat to annotate.
    with tempfile.TemporaryDirectory() as td:
        crew = Path(td) / ".crew"
        crew.mkdir(parents=True)
        (crew / "config.toml").write_text(
            'default_panel = "cursor"\n[seats.cursor-auto]\navailable = false\n')
        env = {**_neutral_env(), "CLAUDE_PROJECT_DIR": td}
        proc = _run_dispatcher(["seats", "--debate"], env=env, cwd=td, timeout=30)
    dlines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    check("seats --debate (no --panel) drops the unavailable seat SILENTLY",
          proc.returncode == 0 and "cursor-auto" not in dlines
          and "cursor-auto" not in proc.stderr,
          "dropped, no skip-note", f"lines={dlines} stderr={proc.stderr[:120]!r}")


def test_panel_catalog():
    log_section("seat catalog loader (seats.py)")
    from multiagent import seats  # noqa: E402
    from multiagent import cli  # noqa: E402

    with crew_config():  # isolated: assert the SHIPPED roster, no personal config
        panels = seats.merged_panels()
        # Keys present, exactly the five shipped panels.
        check("merged_panels() keys == {full, lite, solo, cursor, quick}",
              set(panels) == {"full", "lite", "solo", "cursor", "quick"},
              "{full, lite, solo, cursor, quick}", str(set(panels)))

        # Task seats: exactly the known Claude seats incl. the opt-in fable. The
        # REMOVED opus-4.6 must NOT reappear (its version-locked pin is rejected by
        # the Task tool's model validation — the seat can't be seated).
        check("task_seats() == [opus, sonnet, fable] (opus-4.6 removed)",
              seats.task_seats() == ["opus", "sonnet", "fable"],
              "[opus, sonnet, fable]", str(seats.task_seats()))

        # A Task seat's model is its catalog pin, which IS its own name (a
        # first-class Task-tool alias) — no separate override table.
        check("seat_spec('fable').model == 'fable' (a Task-tool alias, pins its name)",
              seats.seat_spec("fable").model == "fable",
              "fable", str(seats.seat_spec("fable").model))

        # fable is opt-in — in NO shipped panel (the default panel never silently
        # spends the premium tier).
        check("fable is in NO shipped panel (opt-in only)",
              all("fable" not in v for v in panels.values()),
              "absent from all panels", str(panels))

        # ROSTER-FIDELITY: the subprocess subset of full (the names that are NOT
        # Task seats) equals what _resolve_seats(None) runs — the path that
        # actually fans out when nobody names a panel.
        task_names = set(seats.task_seats())
        subprocess_subset = [n for n in panels["full"] if n not in task_names]
        builtin_five = cli._resolve_seats(None)
        check("full's subprocess subset == _resolve_seats(None) (sequence)",
              subprocess_subset == builtin_five,
              str(builtin_five), str(subprocess_subset))

        # INDEPENDENT-literal drift pins: the built-in panel + premium-off set are
        # DERIVED from the catalog's opt_in flags, so pin both against a
        # hand-literal so a derivation-logic bug (wrong order / wrong filter) fails
        # NAMING it, not just when it drifts in lockstep with the panels literal.
        catalog = seats.merged_catalog()
        check("_resolve_seats(None) == the built-in five (order pinned)",
              builtin_five
              == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"],
              "['codex', 'codex-luna', 'agy', 'cursor-auto', 'cursor-composer']",
              str(builtin_five))
        check("premium_off_seats() == the opt-in SET (no dupes/drops)",
              set(seats.premium_off_seats())
              == {"cursor-glm", "cursor-gpt", "cursor-gemini", "cursor-grok", "codex-terra"}
              and len(seats.premium_off_seats()) == 5,
              "5 opt-in seats", str(seats.premium_off_seats()))
        # opt_in agreement: the derived sets tie back to the one flag.
        _sub_opt = {n for n, s in catalog.items() if s.has_executor and s.opt_in}
        check("premium_off_seats() == {subprocess seats with opt_in=True}",
              set(seats.premium_off_seats()) == _sub_opt,
              "opt_in True set", str(sorted(set(seats.premium_off_seats()))))
        check("the built-in five minus agy == {subprocess seats with opt_in=False}",
              set(builtin_five) - {"agy"}
              == {n for n, s in catalog.items()
                  if s.has_executor and not s.opt_in and n != "agy"},
              "opt_in False set", str(sorted(set(builtin_five) - {"agy"})))

        # The other panels, verbatim. cursor is the literal group TOKEN, NOT an
        # expanded cursor-* list (the token is expanded at resolution in cli.py).
        check("lite == ['opus', 'sonnet']",
              panels["lite"] == ["opus", "sonnet"],
              "['opus', 'sonnet']", str(panels["lite"]))
        check("solo == ['opus']",
              panels["solo"] == ["opus"], "['opus']", str(panels["solo"]))
        check("cursor == ['cursor'] (literal group token, unexpanded)",
              panels["cursor"] == ["cursor"], "['cursor']", str(panels["cursor"]))
        check("quick panel is codex+sonnet",
              panels["quick"] == ["codex", "sonnet"],
              "['codex', 'sonnet']", str(panels["quick"]))

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


def test_catalog_cache_reset():
    log_section("catalog cold-cache build + one reset clears every derived cache")
    from multiagent import config, seats  # noqa: E402
    from multiagent import providers  # noqa: E402

    # --- Cold-cache recursion: the entry point must build from a fully-cleared
    # state without re-entering itself. merged_catalog -> reserved_tokens ->
    # group/panel tokens reads seats.toml ALONE (pre-merge), so a cold call cannot
    # cycle back through config.panels -> known_seat_names -> merged_catalog.
    with crew_config():
        config._reset_cache_for_tests()  # every cache cold
        try:
            catalog = seats.merged_catalog()   # THE cold entry point
            built = "codex" in catalog and "opus" in catalog
        except RecursionError:
            built = False
        check("merged_catalog() builds on a fully-cold cache (no recursion)",
              built, "catalog built", "RecursionError or missing seats")
        # The same cold path through cli._resolve_seats (which reaches the panels).
        from multiagent import cli  # noqa: E402
        config._reset_cache_for_tests()
        try:
            resolved = cli._resolve_seats(None)
            ok = resolved == ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]
        except RecursionError:
            ok = False
        check("_resolve_seats(None) resolves on a cold cache (no recursion)",
              ok, "built-in five", str(locals().get("resolved", "RecursionError")))

    # --- One reset clears ALL SIX caches (AC 24). Warm every cache, then reset,
    # and assert each is back to its unloaded/None sentinel.
    with crew_config():
        seats.merged_catalog()          # warms catalog + premium-off + registry
        seats.premium_off_seats()
        providers.get_provider("codex")
        seats._warn_once("probe", "warm the loader warn guard")
        config.default_panel()          # warms config caches + config._warned via nothing
        # Confirm they are warm before the reset.
        warm = (seats._catalog is not None and seats._premium_off is not None
                and providers._REGISTRY is not None and "probe" in seats._warned)
        check("caches warm before reset", warm, "all warm",
              f"catalog={seats._catalog is not None} premium={seats._premium_off is not None} "
              f"registry={providers._REGISTRY is not None} warned={'probe' in seats._warned}")
        config._reset_cache_for_tests()
        cleared = (
            config._cache is config._UNLOADED
            and config._global_cache is config._UNLOADED
            and seats._catalog is None
            and seats._premium_off is None
            and providers._REGISTRY is None
            and seats._warned == set()
        )
        check("one reset clears all six: config caches, catalog, premium-off, registry, loader warn guard",
              cleared, "all six cleared",
              f"config._cache={config._cache!r} catalog={seats._catalog!r} "
              f"premium={seats._premium_off!r} registry={providers._REGISTRY!r} "
              f"warned={seats._warned!r}")


def test_catalog_registry_disjoint():
    log_section("Task seats ⟂ subprocess registry (the safety the catalog buys)")
    from multiagent import seats  # noqa: E402
    from multiagent.providers import known_seat_names, get_provider  # noqa: E402

    task_names = set(seats.task_seats())

    # The invariant: no Task-seat NAME is a registered subprocess seat.
    check("task_seats() ∩ known_seat_names() == ∅ (disjoint)",
          task_names & set(known_seat_names()) == set(),
          "empty intersection",
          str(task_names & set(known_seat_names())))

    # Each Task-seat name fails to resolve through the executor registry.
    for n in task_names:
        raised = False
        try:
            get_provider(n)
        except ValueError:
            raised = True
        check(f"get_provider({n!r}) raises ValueError (no executor)",
              raised, "ValueError raised", "no exception")

    # CONVERSE guard: the SUBPROCESS entries of a preset (codex + cursor-*, i.e.
    # the non-Task-seat names) ARE registry seats and DO resolve. This proves the
    # disjointness assertion is scoped to the Task seats ONLY, not over-broad.
    subprocess_entries = [n for n in seats.merged_panels()["full"]
                          if n not in task_names]
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
    # NOT scan a bare `opus-4.6` (historical test literals and removal notes
    # legitimately mention the retired seat). Only the dot-kept FILENAME
    # spelling is forbidden — the guard outlives the seat because the dot-strip
    # convention applies to ANY future dotted role.
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


def test_persist_seat_doc_sync():
    log_section("persist-seat: the three review-bearing docs use the engine call")
    # DOC-SYNC guard: the review/build/measure-twice command markdown must route
    # Task-seat persistence through `crew persist-seat` (engine-owned slug +
    # six-field shape), NOT the retired hand-rolled Write-tool six-field block.
    # SCRIPT_DIR is plugins/crew/scripts/; its parent is plugins/crew/ — the same
    # `SCRIPT_DIR.parent` repo root the dot-kept FILENAME scan above resolves.
    docs = ["commands/review.md", "commands/build.md", "commands/measure-twice.md"]

    # SENTINEL_PHRASES: load-bearing orchestration invariants that must read
    # IDENTICALLY (verbatim substring) across all three review-bearing docs.
    # Duplication is unavoidable (Claude Code command markdown has no include
    # mechanism), so this table is the sync guard — a phrase reworded in only
    # ONE doc fails here instead of silently drifting.
    SENTINEL_PHRASES = [
        'crew" persist-seat',
        "The Task RESULT is the only completion signal",
        "NEVER judge a seat by a proxy",
        "Wait-for-BOTH barrier.",
        "`repair-seat` is **non-destructive**",
        "Never choke — synthesize from whatever succeeded.",
        "Some registered seats are opt-in and not in the built-in default panel",
    ]

    for rel in docs:
        text = (SCRIPT_DIR.parent / rel).read_text(encoding="utf-8")
        for phrase in SENTINEL_PHRASES:
            check(f"{rel} contains sentinel: {phrase!r}",
                  phrase in text, "present", "MISSING")
        check(f"{rel} has NO hand-rolled six-field Write persistence",
              "the exact six fields" not in text, "absent", "STILL PRESENT")

    # REFERENCE-SPAWN guard: the three review-bearing docs must
    # spawn Task seats by REFERENCE to the staged prompt file — the seat Reads it
    # itself — NOT by pasting the staged prompt's contents inline. A drift back to
    # `prompt="<contents of …prompt-…>"` re-inflates every spawn and re-couples the
    # orchestrator to the prompt body; this catches that regression.
    REF_SPAWN = "Read .crew/reviews/<session-id>/prompt-"
    for rel in docs:
        text = (SCRIPT_DIR.parent / rel).read_text(encoding="utf-8")
        check(f"{rel} spawns Task seats BY REFERENCE: {REF_SPAWN!r}",
              REF_SPAWN in text, "present", "MISSING (drifted back to inline paste?)")
        # CONVERSE: no inline-paste spawn survives.
        check(f"{rel} has NO inline-paste seat spawn",
              'prompt="<contents of' not in text, "absent", "STILL INLINE-PASTING")

    # The reviewer-writes-own-return-file variant was REVERTED: an unvalidated
    # model-returned RETURN-FILE path (traversal/injection) + stale-file risk
    # outweighed the token saving. The reviewer returns its review block as the
    # Task RESULT (the pre-revert behavior); the orchestrator Writes that to a temp file
    # and `persist-seat -f`s it. This CONVERSE guard fails if the reverted happy-path
    # flow creeps back into any of the three review-bearing docs.
    RETURN_FILE_MARKERS = [
        "RETURN-FILE:",                              # the pinned RESULT line
        "-f .crew/reviews/<session-id>/return-",     # persist-seat by path
        "Read the verdict from the",                 # the VERDICT: parse rule
    ]
    for rel in docs:
        text = (SCRIPT_DIR.parent / rel).read_text(encoding="utf-8")
        for marker in RETURN_FILE_MARKERS:
            check(f"{rel} has NO reverted RETURN-FILE flow: {marker!r}",
                  marker not in text, "absent", "STILL PRESENT (return-file flow crept back?)")

    # Grant/doc sync: BOTH panel Task seats are read-only by convention — neither
    # reviewer.md nor panelist.md may grant Write on its frontmatter tools line
    # (reviewer's return-file Write grant was reverted; panelist never had one).
    reviewer_line6 = (SCRIPT_DIR.parent / "agents/reviewer.md").read_text(
        encoding="utf-8").splitlines()[5]
    check("reviewer.md:6 tools line does NOT grant Write (read-only by convention)",
          reviewer_line6.startswith("tools:") and "Write" not in reviewer_line6,
          "tools line without Write", repr(reviewer_line6))
    panelist_line6 = (SCRIPT_DIR.parent / "agents/panelist.md").read_text(
        encoding="utf-8").splitlines()[5]
    check("panelist.md:6 tools line does NOT grant Write (read-only by convention)",
          panelist_line6.startswith("tools:") and "Write" not in panelist_line6,
          "tools line without Write", repr(panelist_line6))


# =============================================================================
# Roster doc-sync — every surviving seat enumeration equals registry truth
# =============================================================================
#
# ONE seat-name charset, defined ONCE, serves ALL roster matching (parse,
# ratchet, run-tokenizer). There is no other matching mechanism. The lookaround
# form (below) is used for counting: NEVER \b, because regex \b treats "-" as a
# boundary, so \bcodex\b matches inside "codex-luna" and a 3-name line would
# count as 4 (the repo's earlier BSD-grep hyphen lesson). re.escape keeps a
# future dotted seat name literal.
import re as _roster_re  # noqa: E402

_SEAT_CHARS = "A-Za-z0-9._-"
_ROSTER_TOKEN_RE = _roster_re.compile(rf"^[{_SEAT_CHARS}]+$")
_ROSTER_MARKER_RE = _roster_re.compile(r"^<!-- seat-roster:(\S+) -->$")

# The pinned prose spans (LOCATORS + SENTINELs are the only deliberate substring
# mechanisms; roster CONTENT is always token-parsed and compared exactly).
_ROSTER_BUILTIN_SPAN = "These lines document the BUILT-IN roster"
_ROSTER_OPT_SENTINEL = "Some registered seats are opt-in and not in the built-in default panel"
_ROSTER_REVIEW_DOCS = [
    "plugins/crew/commands/review.md",
    "plugins/crew/commands/build.md",
    "plugins/crew/commands/measure-twice.md",
]
# Canonical docs that carry the built-in-vs-runtime explanatory sentence. init.md
# is the narrow anchored exception (premium-add-back only); it carries no sentence.
_ROSTER_SENTENCE_DOCS = ["README.md", "plugins/crew/scripts/CLAUDE.md"]

# Expected (file, kind) anchor pairs. Any seat-roster: marker in the scan set
# that is NOT one of these (unknown kind, duplicate, or a marker in a
# non-canonical file) is a failure — no marker can mint an unintended exemption.
_ROSTER_EXPECTED = {
    "README.md": {"default", "preset:lite", "preset:solo", "preset:quick",
                  "opt-in", "task-opt-in", "all"},
    "plugins/crew/scripts/CLAUDE.md": {"default", "opt-in", "task-opt-in"},
    "plugins/crew/commands/init.md": {"premium-add-back"},
}


def _roster_repo_root() -> Path:
    # SCRIPT_DIR is plugins/crew/scripts/; three parents up is the repo root
    # (where README.md lives). Same convention as test_persist_seat_doc_sync,
    # which reads relative to SCRIPT_DIR.parent (plugins/crew/).
    return SCRIPT_DIR.parent.parent.parent


def _roster_unwrap(tok: str) -> str:
    """Repeatedly strip BALANCED wrapper pairs — `` `x` `` and ``**x**`` ONLY,
    the two the grammar permits. An UNBALANCED/mismatched wrapper is left in
    place, so the downstream charset gate rejects it (never silently cleaned)."""
    while True:
        if len(tok) >= 2 and tok[0] == "`" and tok[-1] == "`":
            tok = tok[1:-1]
            continue
        if len(tok) >= 4 and tok.startswith("**") and tok.endswith("**"):
            tok = tok[2:-2]
            continue
        return tok


def _roster_parse_line(line: str):
    """Implement the grammar's `roster` production EXACTLY. Returns
    ``(tokens, None)`` on success (empty list for the bare ``(none)`` literal),
    or ``(None, err)`` on any grammar violation."""
    if ":" not in line:
        return None, "no colon on roster line"
    remainder = line.split(":", 1)[1].strip()
    # Compare the BARE literal BEFORE any unwrapping: a wrapped `(none)` is a
    # grammar violation (the drill has a wrapped-(none) rejection case).
    if remainder == "(none)":
        return [], None
    if not remainder:
        return None, "empty roster after colon"
    tokens = []
    for piece in remainder.split(", "):  # the EXACT separator; a bare "," fails
        unwrapped = _roster_unwrap(piece)
        if not _ROSTER_TOKEN_RE.match(unwrapped):
            return None, f"bad roster token {piece!r}"
        tokens.append(unwrapped)
    return tokens, None


def _roster_max_run(line: str) -> int:
    """Longest run of consecutive seat-name-charset tokens, WRAPPER- and
    BOUNDARY-aware (the stale-mix backstop). Split on the run separators, strip
    one leading label prefix + trailing punctuation per piece, reuse the shared
    unwrap; a piece that is not a charset token ENDS the current run (prose words
    break runs, so sentences never accumulate)."""
    best = cur = 0
    for piece in _roster_re.split(r", | \+ |/", line):
        p = piece
        if ":" in p:                       # strip one leading <label>: prefix
            p = p.split(":", 1)[1]
        p = p.strip().rstrip(".,;:!?)").strip()
        if _ROSTER_TOKEN_RE.match(_roster_unwrap(p)):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _roster_scan_files(root: Path):
    """Ratchet scan set: README.md + EVERY .md under plugins/crew/ (glob-derived,
    so a NEW doc location is covered the day it appears) + the multiagent package
    docstring file. Non-doc .py files stay out (they legitimately hold code-truth
    seat literals)."""
    files = []
    readme = root / "README.md"
    if readme.exists():
        files.append(readme)
    crew = root / "plugins" / "crew"
    files.extend(sorted(crew.rglob("*.md")))
    init = crew / "scripts" / "multiagent" / "__init__.py"
    if init.exists():
        files.append(init)
    return files


def _roster_compare(kind, tokens, truths):
    """Compare parsed tokens to code truth per the kind table. Sequence-kinds
    (default/preset:*) demand exact order; set-kinds demand set equality AND
    equal length (so a duplicate or a stale/missing name both fail)."""
    got = "[" + ", ".join(tokens) + "]"
    FULL, OPT_SUB, OPT_TASK, PREMIUM, ALL_SEATS, presets = truths
    if kind == "default":
        want = list(FULL)
        return tokens == want, f"sequence {want}", got
    if kind.startswith("preset:"):
        want = list(presets[kind.split(":", 1)[1]])
        return tokens == want, f"sequence {want}", got
    if kind == "opt-in":
        want = OPT_SUB
    elif kind == "task-opt-in":
        want = OPT_TASK
    elif kind == "premium-add-back":
        want = list(PREMIUM)
    elif kind == "all":
        want = sorted(ALL_SEATS)
    else:
        return False, "a known kind", f"unknown kind {kind}"
    ok = set(tokens) == set(want) and len(tokens) == len(want)
    return ok, f"set+length {sorted(want)}", got


def _roster_scan(root: Path, report):
    """Parts 1 + 3 against docs under ``root``. ``report(label, cond, want, got)``
    receives every assertion (``check`` for the live run; a collector for the
    drill), and EVERY label embeds the file (and line for the ratchet), so a
    drift fails NAMING THE FILE. No roster-CONTENT comparison here is a substring
    check: rosters are token-parsed and compared exactly. The two deliberate
    substring uses are LOCATORS (finding markers) and the pinned prose spans."""
    from multiagent import seats
    from multiagent.providers import known_seat_names

    # Provider membership (the single truth): a line enumerating 2+ seats of the
    # SAME provider is a stale-enumeration hazard the >=4 / stale-mix checks miss.
    catalog = seats.merged_catalog()
    CODEX_KEYS = {n for n, s in catalog.items() if s.provider == "codex"}
    CURSOR_KEYS = {n for n, s in catalog.items() if s.provider == "cursor"}

    task_names = set(seats.task_seats())
    FULL = seats.merged_panels()["full"]
    known = known_seat_names()
    OPT_SUB = [n for n, s in catalog.items() if s.has_executor and s.opt_in]
    OPT_TASK = sorted(task_names - set(FULL))
    ALL_SEATS = set(known) | task_names
    UNIVERSE = ALL_SEATS
    truths = (FULL, OPT_SUB, OPT_TASK, seats.premium_off_seats(), ALL_SEATS,
              seats.merged_panels())

    scan_files = _roster_scan_files(root)
    file_lines = {}
    for path in scan_files:
        relf = str(path.relative_to(root))
        file_lines[relf] = path.read_text(encoding="utf-8").splitlines()

    # --- Part 1: marker integrity (runs FIRST, before any comparison) --------
    found = {}          # (relf, kind) -> count
    anchor_at = {}      # (relf, kind) -> marker line index (first occurrence)
    for relf, lines in file_lines.items():
        for i, ln in enumerate(lines):
            if "seat-roster:" not in ln:        # LOCATOR (deliberate substring)
                continue
            m = _ROSTER_MARKER_RE.match(ln)
            if not m:
                report(f"{relf}:{i + 1} malformed seat-roster marker", False,
                       "exact '<!-- seat-roster:<kind> -->' alone on its line", repr(ln))
                continue
            kind = m.group(1)
            if kind not in _ROSTER_EXPECTED.get(relf, set()):
                report(f"{relf}:{kind} unexpected seat-roster marker", False,
                       "an expected (file, kind) pair", f"{relf}:{kind} at line {i + 1}")
                continue
            key = (relf, kind)
            found[key] = found.get(key, 0) + 1
            if key not in anchor_at:
                anchor_at[key] = i

    for relf, kinds in _ROSTER_EXPECTED.items():
        for kind in sorted(kinds):
            cnt = found.get((relf, kind), 0)
            report(f"{relf}:{kind} marker present exactly once",
                   cnt == 1, "exactly 1 marker", f"{cnt}")

    # --- Part 1: per-anchor parse + comparison -------------------------------
    for relf, kinds in _ROSTER_EXPECTED.items():
        for kind in sorted(kinds):
            key = (relf, kind)
            if found.get(key, 0) != 1:
                continue
            lines = file_lines[relf]
            midx = anchor_at[key]
            if midx + 1 >= len(lines):
                report(f"{relf}:{kind} roster line present after marker",
                       False, "a roster line immediately after the marker", "EOF")
                continue
            tokens, err = _roster_parse_line(lines[midx + 1])
            if err:
                report(f"{relf}:{kind} roster line grammar", False,
                       "valid roster grammar", f"{err} | {lines[midx + 1]!r}")
                continue
            ok, want, got = _roster_compare(kind, tokens, truths)
            report(f"{relf}:{kind} roster == code truth", ok, want, got)

    # --- Part 1: built-in-vs-runtime explanatory sentence (pinned span) ------
    for relf in _ROSTER_SENTENCE_DOCS:
        text = "\n".join(file_lines.get(relf, []))
        report(f"{relf} carries built-in-vs-runtime sentence span",
               _ROSTER_BUILTIN_SPAN in text, f"contains {_ROSTER_BUILTIN_SPAN!r}", "MISSING")

    # --- Part 3: enumeration ratchet -----------------------------------------
    # Exempt = marker lines + the ONE roster line after each VALID marker; nothing
    # else. Intervening rationale prose IS scanned (forcing it under threshold).
    exempt = set()
    for (relf, kind), midx in anchor_at.items():
        exempt.add((relf, midx))
        exempt.add((relf, midx + 1))
    name_pats = [
        (n, _roster_re.compile(
            rf"(?<![{_SEAT_CHARS}]){_roster_re.escape(n)}(?![{_SEAT_CHARS}])"))
        for n in UNIVERSE
    ]
    for relf, lines in file_lines.items():
        for i, ln in enumerate(lines):
            if (relf, i) in exempt:
                continue
            present = {n for n, pat in name_pats if pat.search(ln)}
            if len(present) >= 4:
                report(f"{relf}:{i + 1} enumerates >= 4 seats", False,
                       "< 4 distinct seat names", f"{relf}:{i + 1}: {ln.strip()}")
            elif len(present) >= 3 and _roster_max_run(ln) >= 4:
                # Stale-mix: a stale/unknown name riding a roster of current ones.
                report(f"{relf}:{i + 1} stale-mix token run >= 4", False,
                       "no >= 4 token run on a 3+ current-name line",
                       f"{relf}:{i + 1}: {ln.strip()}")
            # Provider-family rule (fires at 2, below every threshold above): a
            # non-exempt prose line naming 2+ seats of the SAME provider is a
            # stale-enumeration hazard (name_pats is separator-agnostic, so " + ",
            # "/", ", " all trip). Cross-provider (1 codex + 1 cursor) does NOT.
            if len(present & CODEX_KEYS) >= 2 or len(present & CURSOR_KEYS) >= 2:
                report(f"{relf}:{i + 1} enumerates 2+ same-provider seats", False,
                       "generic phrasing (e.g. 'the codex seats')",
                       f"{relf}:{i + 1}: {ln.strip()}")


def _roster_sentinel_scan(root: Path, report):
    """The pinned opt-in sentinel across the three review-bearing docs (the same
    phrase test_persist_seat_doc_sync's SENTINEL_PHRASES enforces on the live
    tree; reused HERE only so the drill's rewording case is reproducible)."""
    for relf in _ROSTER_REVIEW_DOCS:
        p = root / relf
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        report(f"{relf} carries pinned opt-in sentinel",
               _ROSTER_OPT_SENTINEL in text, "sentinel present", "MISSING")


def _roster_check_scaffold(FULL):
    """Part 2: token-parse the GENERATED scaffold roster, not the cli.py source
    line (which carries the Python string suffix). Run the generator exactly as
    test_scaffold_config does, assert EXACTLY ONE 'Cost-safe built-in full' line,
    and compare its token SEQUENCE+LENGTH to PANEL_PRESETS['full']."""
    from contextlib import redirect_stdout, redirect_stderr
    from io import StringIO
    from multiagent import cli, seats as _seats
    from multiagent.providers import known_seat_names

    with crew_config() as proj:
        det = Path(proj) / "doctor.json"
        det.write_text(json.dumps({
            "subprocess": {n: {"available": True, "diag": ""} for n in known_seat_names()},
            "task": {n: {"available": True, "diag": ""}
                     for n in sorted(_seats.task_seats())},
        }))
        args = cli.build_parser().parse_args(
            ["scaffold-config", "--out", "-", "--detection", str(det)])
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            args.func(args)
        gen = out.getvalue()

    matches = [ln for ln in gen.splitlines() if "Cost-safe built-in full" in ln]
    check("Part 2: scaffold emits exactly ONE 'Cost-safe built-in full' line",
          len(matches) == 1, "exactly 1", f"{len(matches)} lines")
    if len(matches) != 1:
        return
    rhs = matches[0].split("=", 1)[1].strip().rstrip(".").strip()
    tokens = [t.strip() for t in rhs.split("+")]
    check("Part 2: scaffold roster tokens pass the seat charset",
          all(_ROSTER_TOKEN_RE.match(t) for t in tokens), "all charset tokens", str(tokens))
    check("Part 2: scaffold roster == PANEL_PRESETS['full'] (sequence + length)",
          tokens == list(FULL), f"sequence {list(FULL)}", str(tokens))


def _roster_drill(root: Path):
    """Reproducible mutation drill (env-gated on CREW_ROSTER_DRILL=1; a no-op skip
    otherwise). Copies the scanned docs into a temp tree, applies each mutation
    case, reruns the parse/ratchet/sentinel logic against the copies, and asserts
    every case fails NAMING THE RIGHT FILE. The case list is the one tuple below
    (count-free everywhere else: adding a case needs no prose update)."""
    if os.environ.get("CREW_ROSTER_DRILL") != "1":
        print("  (roster mutation drill skipped; set CREW_ROSTER_DRILL=1 to run)")
        return
    log_section("roster mutation drill (each case must fail naming its file)")

    def mutate_roster_line(text, kind, fn):
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip() == f"<!-- seat-roster:{kind} -->":
                lines[i + 1] = fn(lines[i + 1])
                break
        out = "\n".join(lines)
        return out + "\n" if text.endswith("\n") else out

    def append_line(text, extra):
        sep = "" if text.endswith("\n") else "\n"
        return text + sep + extra + "\n"

    CASES = [
        ("seat swap on README default line", "README.md",
         lambda t: mutate_roster_line(t, "default",
             lambda l: l.replace("`codex-luna`", "`cursor-zzz`", 1))),
        ("stale name on scripts/CLAUDE.md opt-in line",
         "plugins/crew/scripts/CLAUDE.md",
         lambda t: mutate_roster_line(t, "opt-in",
             lambda l: l.rstrip() + ", `cursor-stale`")),
        ("fake 4-name line in build.md", "plugins/crew/commands/build.md",
         lambda t: append_line(t, "Panel seats: codex, codex-luna, agy, cursor-auto")),
        ("unanchored wrapped 3-current+1-stale run", "plugins/crew/commands/review.md",
         lambda t: append_line(t, "`codex`, `agy`, `sonnet`, `stale-seat`")),
        ("labeled+punctuated 3-current+1-stale run",
         "plugins/crew/commands/measure-twice.md",
         lambda t: append_line(t, "Seats: codex, agy, sonnet, stale-seat.")),
        ("wrapped (none) on an anchored roster line", "README.md",
         lambda t: mutate_roster_line(t, "opt-in",
             lambda l: l.split(":", 1)[0] + ": `(none)`")),
        ("reworded opt-in sentinel in build.md", "plugins/crew/commands/build.md",
         lambda t: t.replace(_ROSTER_OPT_SENTINEL, "Some seats are optional")),
        ("bare-comma separator on README default line", "README.md",
         lambda t: mutate_roster_line(t, "default",
             lambda l: l.replace("`, `", "`,`", 1))),
        ("unbalanced-backtick wrapper on README default line", "README.md",
         lambda t: mutate_roster_line(t, "default",
             lambda l: l.replace("`codex`", "`codex", 1))),
        # Provider-family rule: 2 same-provider seats on a NON-anchored prose line,
        # one per separator, each BELOW every old-ratchet threshold so ONLY the
        # family rule catches it (a 2-name line never reaches the >=4 / len>=3 gates).
        ("family rule ' + ' separator (2 codex)", "plugins/crew/commands/review.md",
         lambda t: append_line(t, "The codex + codex-luna seats agree here.")),
        ("family rule '/' separator (2 cursor)", "plugins/crew/commands/build.md",
         lambda t: append_line(t, "cursor-auto/cursor-composer routing note.")),
        ("family rule ', ' separator (2 codex)", "plugins/crew/commands/measure-twice.md",
         lambda t: append_line(t, "codex, codex-luna together on this line.")),
    ]

    scan_files = _roster_scan_files(root)
    passed = 0
    for label, target_rel, fn in CASES:
        with tempfile.TemporaryDirectory() as td:
            troot = Path(td)
            for p in scan_files:
                dst = troot / p.relative_to(root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
            tgt = troot / target_rel
            tgt.write_text(fn(tgt.read_text(encoding="utf-8")), encoding="utf-8")

            failures = []

            def collect(lbl, cond, want="", got=""):
                if not cond:
                    failures.append(lbl)

            _roster_scan(troot, collect)
            _roster_sentinel_scan(troot, collect)

            named = [f for f in failures if target_rel in f]
            check(f"drill: {label} -> failure naming {target_rel}",
                  bool(named), f"a failure naming {target_rel}", f"failures={failures}")
            if named:
                passed += 1
    print(f"  roster drill: {passed}/{len(CASES)} cases named-file failures")


def test_roster_doc_sync():
    log_section("roster doc-sync (anchored rosters == registry truth; enumeration ratchet)")
    from multiagent import seats
    from multiagent.providers import known_seat_names

    root = _roster_repo_root()
    FULL = seats.merged_panels()["full"]
    known = known_seat_names()
    OPT_SUB = [n for n, s in seats.merged_catalog().items()
               if s.has_executor and s.opt_in]
    ALL_SEATS = set(known) | set(seats.task_seats())

    # --- Part 0: derivation self-checks (a wrong truth can't agree with a wrong
    # doc). The opt-in subprocess set (has_executor ∧ opt_in) must equal the one
    # derived from FULL (re-proving full's subprocess subset covers the builtin
    # fallback), and FULL must live inside the universe.
    check("Part 0: OPT_SUB (opt-in subprocess seats) == known - FULL",
          set(OPT_SUB) == set(known) - set(FULL),
          str(sorted(set(known) - set(FULL))), str(sorted(set(OPT_SUB))))
    check("Part 0: set(FULL) <= ALL_SEATS",
          set(FULL) <= ALL_SEATS, "FULL subset of ALL_SEATS",
          f"{sorted(set(FULL) - ALL_SEATS)} outside ALL_SEATS")

    # --- Parts 1 + 3: anchored rosters + enumeration ratchet (file-naming) ----
    _roster_scan(root, check)

    # --- Part 2: generated-scaffold roster (token-parsed, not substring) ------
    _roster_check_scaffold(FULL)

    # --- Reproducible mutation drill (env-gated) ------------------------------
    _roster_drill(root)


# =============================================================================
# /crew:dispatch — write-mode single-seat delegation
# =============================================================================

def _fake_codex(action_lines: str = "") -> str:
    """A fake `codex` bin: read the -o file from argv, optionally run a git
    ACTION in its (provider-pinned) cwd, then write a non-empty seat output to -o.

    ``action_lines`` is Python source executed AFTER stdin is read and BEFORE the
    -o file is written, with the subprocess cwd already pinned by the provider.
    """
    return (
        "import sys, os, json, subprocess\n"
        "a = sys.argv[1:]\n"
        "out = None\n"
        "for i, x in enumerate(a):\n"
        "    if x == '-o':\n"
        "        out = a[i + 1]\n"
        "sys.stdin.read()\n"
        + action_lines +
        "open(out, 'w').write('CODEX DID WORK')\n"
        "sys.exit(0)\n"
    )


def _run_dispatch(repo, bins, extra_args, *, session=None, project_dir=None,
                  home=None, timeout=30):
    """Run `cli.py dispatch …` as a subprocess and return (proc, envelope|None).

    With no session the envelope JSON is the whole stdout; with a session it is
    written to the derived file and stdout's last clean line is the path.
    """
    env = path_with(bins)
    env["CLAUDE_WORKING_DIRECTORY"] = str(repo)
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if home is not None:
        env["HOME"] = home
    args = ["dispatch", *extra_args, "--json"]
    if session:
        args += ["--session-id", session]
    proc = _run_cli(args, env=env, cwd=str(repo), timeout=timeout)
    env_obj = None
    if proc.returncode == 0:
        try:
            if session:
                last = [l for l in proc.stdout.splitlines() if l.strip()][-1]
                env_obj = json.loads((Path(repo) / last).read_text())
            else:
                env_obj = json.loads(proc.stdout)
        except Exception:
            env_obj = None
    return proc, env_obj


def _dispatch_ns(**kw):
    import argparse as _ap
    base = dict(seat=None, task=None, file=None, model=None, timeout=None,
                json=False, out=None, session_id=None)
    base.update(kw)
    return _ap.Namespace(**base)


def test_dispatch():
    log_section("dispatch — prompt builder + capability")
    from multiagent import cli, config
    from multiagent.providers import Provider
    import io, contextlib
    import unittest.mock as mock

    # --- Task 1: prompts.dispatch framing + BEGIN/END markers ---
    dp = prompts.dispatch("MAKE A WIDGET")
    check("dispatch prompt wraps task between literal BEGIN TASK / END TASK lines",
          "BEGIN TASK\nMAKE A WIDGET\nEND TASK" in dp,
          "BEGIN TASK\\n<task>\\nEND TASK", dp)
    low = dp.lower()
    check("dispatch prompt forbids commit / git add (stage) / push / branch",
          "do not run `git commit`" in low and "do not run `git add`" in low
          and "do not run `git push`" in low and "do not create or switch branches" in low,
          "no-commit/no-stage/no-push/no-branch clauses", dp)
    check("dispatch prompt is distinct from council/build_prompt",
          dp != prompts.council("MAKE A WIDGET")
          and "multi-model council" not in dp and "review panel" not in dp,
          "distinct from council/review", dp[:120])

    # --- Task 2: supports_workspace_write fail-closed default + explicit True ---
    check("Provider ABC default supports_workspace_write is False (fail-CLOSED)",
          Provider.supports_workspace_write is False, "False", str(Provider.supports_workspace_write))
    for s in ("codex", "agy", "cursor-auto"):
        check(f"{s} provider opts into supports_workspace_write=True",
              get_provider(s).supports_workspace_write is True,
              "True", str(get_provider(s).supports_workspace_write))

    # --- Task 2: codex --ignore-user-config conditional on sandbox ---
    log_section("dispatch — codex argv (--ignore-user-config gating) + cwd pin")
    from multiagent.providers.codex import CodexProvider
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        cap = d / "cap.json"
        make_fake_bin(d, "codex",
            f"import sys, os, json\n"
            f"a = sys.argv[1:]\n"
            f"out = None\n"
            f"for i, x in enumerate(a):\n"
            f"    if x == '-o':\n"
            f"        out = a[i + 1]\n"
            f"sys.stdin.read()\n"
            f"json.dump({{'args': a, 'cwd': os.getcwd()}}, open({str(cap)!r}, 'w'))\n"
            f"open(out, 'w').write('OK')\n")
        old_path = os.environ["PATH"]
        os.environ["PATH"] = str(d) + os.pathsep + old_path
        try:
            CodexProvider().run("p", sandbox="read-only", timeout=30)
            ro = json.loads(cap.read_text())
            CodexProvider().run("p", sandbox="workspace-write", timeout=30)
            ww = json.loads(cap.read_text())
        finally:
            os.environ["PATH"] = old_path
        check("codex read-only argv CONTAINS --ignore-user-config (review hermetic, unchanged)",
              "--ignore-user-config" in ro["args"]
              and any(x.startswith("model_reasoning_effort=") for x in ro["args"]),
              "--ignore-user-config present + effort pin", str(ro["args"]))
        check("codex workspace-write argv OMITS --ignore-user-config (project context loads)",
              "--ignore-user-config" not in ww["args"]
              and any(x.startswith("model_reasoning_effort=") for x in ww["args"]),
              "no --ignore-user-config + effort pin still present", str(ww["args"]))

    # --- Task 2: cwd pin — codex/agy pin cwd to CLAUDE_WORKING_DIRECTORY in WW only ---
    for binname, prov_factory in (("codex", lambda: CodexProvider()),
                                   ("agy", lambda: __import__(
                                       "multiagent.providers.agy", fromlist=["AgyProvider"]
                                   ).AgyProvider())):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as cwdt:
            d = Path(td)
            wdir = Path(cwdt)  # the CLAUDE_WORKING_DIRECTORY, distinct from process cwd
            cap = d / f"cap_{binname}.json"
            if binname == "codex":
                make_fake_bin(d, "codex",
                    f"import sys, os, json\n"
                    f"a = sys.argv[1:]\n"
                    f"out = None\n"
                    f"for i, x in enumerate(a):\n"
                    f"    if x == '-o':\n"
                    f"        out = a[i + 1]\n"
                    f"sys.stdin.read()\n"
                    f"json.dump({{'cwd': os.getcwd()}}, open({str(cap)!r}, 'w'))\n"
                    f"open(out, 'w').write('OK')\n")
            else:
                make_fake_bin(d, "agy",
                    f"import sys, os, json\n"
                    f"json.dump({{'cwd': os.getcwd()}}, open({str(cap)!r}, 'w'))\n"
                    f"print('AGY OK')\n")
            old_path = os.environ["PATH"]
            os.environ["PATH"] = str(d) + os.pathsep + old_path
            saved_wd = os.environ.get("CLAUDE_WORKING_DIRECTORY")
            os.environ["CLAUDE_WORKING_DIRECTORY"] = str(wdir)
            try:
                prov_factory().run("p", sandbox="workspace-write", timeout=30)
                ww_cwd = json.loads(cap.read_text())["cwd"]
                prov_factory().run("p", sandbox="read-only", timeout=30)
                ro_cwd = json.loads(cap.read_text())["cwd"]
            finally:
                os.environ["PATH"] = old_path
                if saved_wd is None:
                    os.environ.pop("CLAUDE_WORKING_DIRECTORY", None)
                else:
                    os.environ["CLAUDE_WORKING_DIRECTORY"] = saved_wd
            check(f"{binname} workspace-write pins subprocess cwd to CLAUDE_WORKING_DIRECTORY",
                  Path(ww_cwd).resolve() == wdir.resolve(),
                  str(wdir.resolve()), ww_cwd)
            check(f"{binname} read-only sets NO cwd override (inherits process cwd, review unchanged)",
                  Path(ro_cwd).resolve() == Path(os.getcwd()).resolve(),
                  str(Path(os.getcwd()).resolve()), ro_cwd)

    # --- Task 3: config.dispatch_seat validation ---
    log_section("dispatch — config.dispatch_seat() validation")
    with project_config('[dispatch]\nseat = "agy"\n'):
        check("[dispatch].seat=agy -> 'agy'", config.dispatch_seat() == "agy",
              "agy", repr(config.dispatch_seat()))
    for bad in ("opus", "bogus", "cursor"):
        with project_config(f'[dispatch]\nseat = "{bad}"\n'):
            check(f"[dispatch].seat={bad} (task/unknown/group) -> None (warn once)",
                  config.dispatch_seat() is None, "None", repr(config.dispatch_seat()))
    with crew_config(project='[dispatch]\nseat = "agy"\n', glob='[dispatch]\nseat = "codex"\n'):
        check("[dispatch].seat per-repo (agy) wins over global (codex)",
              config.dispatch_seat() == "agy", "agy", repr(config.dispatch_seat()))

    # --- Task 4: git tri-state helpers + full envelope scenarios ---
    log_section("dispatch — git guard helpers + envelope scenarios")
    # _git_head tri-state direct
    with tempfile.TemporaryDirectory() as td:
        born = Path(td) / "born"; born.mkdir(); _init_repo(str(born), with_commit=True)
        unborn = Path(td) / "unborn"; unborn.mkdir(); _init_repo(str(unborn), with_commit=False)
        notrepo = Path(td) / "plain"; notrepo.mkdir()
        h_born = cli._git_head(str(born))
        check("_git_head born repo -> a sha", bool(h_born) and h_born not in (None, "<unborn>")
              and len(h_born) >= 7, "40-hex sha", repr(h_born))
        check("_git_head unborn repo -> '<unborn>' sentinel",
              cli._git_head(str(unborn)) == "<unborn>", "<unborn>", repr(cli._git_head(str(unborn))))
        check("_git_head not-a-repo -> None", cli._git_head(str(notrepo)) is None,
              "None", repr(cli._git_head(str(notrepo))))
        # _git_staged is now an index-TREE-HASH snapshot (git write-tree), not a
        # clean/dirty bool: a 40-hex tree sha for a repo, None for a non-repo,
        # and the sha CHANGES when the index content changes (the True->True
        # already-staged case a bool would miss).
        s_clean = cli._git_staged(str(born))
        check("_git_staged born repo -> a 40-hex index tree sha (not a bool)",
              isinstance(s_clean, str) and len(s_clean) == 40 and s_clean not in (None, "<unborn>"),
              "40-hex tree sha", repr(s_clean))
        (born / "staged_me.txt").write_text("x\n")
        _git(["add", "staged_me.txt"], str(born))
        s_staged = cli._git_staged(str(born))
        check("_git_staged tree sha CHANGES when the index gains a staged file (True->True caught)",
              isinstance(s_staged, str) and s_staged != s_clean,
              "different tree sha", f"{s_clean} -> {s_staged}")
        _git(["reset", "-q"], str(born))  # restore a clean index for later checks
        check("_git_staged not-a-repo -> None", cli._git_staged(str(notrepo)) is None,
              "None", repr(cli._git_staged(str(notrepo))))
        check("_git_branch born -> 'main'", cli._git_branch(str(born)) == "main",
              "main", repr(cli._git_branch(str(born))))
        check("_git_branch not-a-repo -> None", cli._git_branch(str(notrepo)) is None,
              "None", repr(cli._git_branch(str(notrepo))))
        # detached HEAD -> '<detached HEAD>' sentinel (NOT 'HEAD', NOT None) so a
        # checkout <sha> fires branch_changed=true.
        _git(["checkout", "-q", "--detach", "HEAD"], str(born))
        check("_git_branch detached HEAD -> '<detached HEAD>' sentinel (not 'HEAD', not None)",
              cli._git_branch(str(born)) == "<detached HEAD>", "<detached HEAD>",
              repr(cli._git_branch(str(born))))
        # BLOCKING-1: the sentinel must be an IMPOSSIBLE refname so no real branch
        # can ever equal it. The OLD spelling '<detached>' IS a valid branch name —
        # a repo on a branch literally named '<detached>' must compare as ITSELF
        # (NOT collide with / get masked by the detached sentinel), and an actual
        # detach must still be caught as the sentinel.
        _git(["checkout", "-q", "main"], str(born))  # re-attach
        _git(["checkout", "-q", "-b", "<detached>"], str(born))  # a branch named like the OLD sentinel
        check("_git_branch on a branch literally named '<detached>' returns its OWN name (no collision)",
              cli._git_branch(str(born)) == "<detached>", "<detached>", repr(cli._git_branch(str(born))))
        check("BLOCKING-1: branch named '<detached>' != the detached sentinel (no masking)",
              cli._git_branch(str(born)) != cli._DETACHED, "distinct from _DETACHED",
              f"branch={cli._git_branch(str(born))!r} sentinel={cli._DETACHED!r}")
        _git(["checkout", "-q", "--detach", "HEAD"], str(born))  # actual detach still caught
        check("BLOCKING-1: an actual detach still resolves to the sentinel (still caught)",
              cli._git_branch(str(born)) == cli._DETACHED, repr(cli._DETACHED),
              repr(cli._git_branch(str(born))))
        # The sentinel itself is an IMPOSSIBLE branch name (the space): git
        # check-ref-format --branch rejects it, proving no real branch can equal it.
        crf = subprocess.run(["git", "check-ref-format", "--branch", cli._DETACHED],
                             capture_output=True, text=True)
        check("BLOCKING-1: _DETACHED is an impossible refname (check-ref-format exits nonzero)",
              crf.returncode != 0, "nonzero exit", f"rc={crf.returncode}")
        # MINOR-3: _git_staged on an UNBORN repo returns the well-known empty-tree
        # SHA (git write-tree on an empty index), NOT None — so an unborn-repo
        # stage is still detectable by the before/after compare.
        s_unborn = cli._git_staged(str(unborn))
        check("_git_staged unborn repo -> empty-tree SHA (real SHA, not None — unborn stage detectable)",
              s_unborn == "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
              "4b825dc642cb6eb9a060e54bf8d69288fbee4904", repr(s_unborn))

    # Full-scenario integration via subprocess dispatch with a fake codex.
    with tempfile.TemporaryDirectory() as td:
        bins = Path(td) / "bin"; bins.mkdir()

        # (1) NOOP success: no git change -> all guards false, ok=true.
        make_fake_bin(bins, "codex", _fake_codex())
        repo = Path(td) / "r1"; repo.mkdir(); _init_repo(str(repo))
        proc, env = _run_dispatch(repo, bins, ["make a file"])
        check("dispatch noop: exit 0, ok=true, seat=codex, all *_changed typed false",
              proc.returncode == 0 and env and env["ok"] is True and env["seat"] == "codex"
              and env["head_moved"] is False and env["staged_changed"] is False
              and env["branch_changed"] is False
              and isinstance(env["head_moved"], bool) and isinstance(env["staged_changed"], bool)
              and isinstance(env["branch_changed"], bool),
              "exit0 ok=true all-false typed", f"{proc.returncode}: {env}")
        check("dispatch envelope has the 15 fields",
              env and set(env.keys()) == {
                  "seat", "model", "ok", "output", "error", "elapsed",
                  "head_before", "head_after", "head_moved",
                  "staged_before", "staged_after", "staged_changed",
                  "branch_before", "branch_after", "branch_changed"},
              "15 fields", str(sorted(env.keys()) if env else None))

        # (2) COMMIT -> head_moved=true with both shas, exit STILL 0 (advisory).
        # Staging-then-committing also changes the index TREE (it now reflects the
        # new commit's tree), so the tree-hash guard ALSO trips staged_changed=true
        # here — redundant-but-not-wrong noise alongside the load-bearing
        # head_moved warning (a pure commit still violates "leave uncommitted").
        make_fake_bin(bins, "codex", _fake_codex(
            "open('seat.txt','w').write('x')\n"
            "subprocess.run(['git','add','.'])\n"
            "subprocess.run(['git','commit','-q','-m','seat commit'])\n"))
        repo = Path(td) / "r2"; repo.mkdir(); _init_repo(str(repo))
        proc, env = _run_dispatch(repo, bins, ["do it"])
        check("dispatch seat COMMIT -> head_moved=true (both shas), exit 0 (advisory)",
              proc.returncode == 0 and env and env["head_moved"] is True
              and env["head_before"] != env["head_after"],
              "head_moved=true exit0", f"{proc.returncode}: {env}")

        # (3) STAGE (git add, no commit) -> staged_changed=true, head_moved=false.
        make_fake_bin(bins, "codex", _fake_codex(
            "open('seat.txt','w').write('x')\n"
            "subprocess.run(['git','add','.'])\n"))
        repo = Path(td) / "r3"; repo.mkdir(); _init_repo(str(repo))
        proc, env = _run_dispatch(repo, bins, ["do it"])
        check("dispatch seat STAGE -> staged_changed=true, head_moved=false",
              env and env["staged_changed"] is True and env["head_moved"] is False,
              "staged_changed=true head_moved=false", str(env))

        # (4) BRANCH (checkout -b tmp, same sha) -> branch_changed=true, head_moved=false.
        make_fake_bin(bins, "codex", _fake_codex(
            "subprocess.run(['git','checkout','-q','-b','tmp'])\n"))
        repo = Path(td) / "r4"; repo.mkdir(); _init_repo(str(repo))
        proc, env = _run_dispatch(repo, bins, ["do it"])
        check("dispatch seat checkout -b tmp -> branch_changed=true, head_moved=false",
              env and env["branch_changed"] is True and env["head_moved"] is False
              and env["branch_before"] == "main" and env["branch_after"] == "tmp",
              "branch_changed=true head_moved=false", str(env))

        # (5) UNBORN first commit -> head_moved=true, head_before='<unborn>'.
        make_fake_bin(bins, "codex", _fake_codex(
            "open('seat.txt','w').write('x')\n"
            "subprocess.run(['git','add','.'])\n"
            "subprocess.run(['git','commit','-q','-m','first'])\n"))
        repo = Path(td) / "r5"; repo.mkdir(); _init_repo(str(repo), with_commit=False)
        proc, env = _run_dispatch(repo, bins, ["bootstrap"])
        check("dispatch UNBORN first commit -> head_moved=true, head_before='<unborn>' (BLOCKING-2)",
              env and env["head_moved"] is True and env["head_before"] == "<unborn>"
              and env["head_after"] not in (None, "<unborn>"),
              "head_moved=true head_before=<unborn>", str(env))

        # (6) NOT-A-REPO -> all guards typed false, never null.
        make_fake_bin(bins, "codex", _fake_codex())
        plain = Path(td) / "plain"; plain.mkdir()
        proc, env = _run_dispatch(plain, bins, ["do it"])
        check("dispatch not-a-repo -> *_changed all typed false (never null)",
              env and env["head_moved"] is False and env["staged_changed"] is False
              and env["branch_changed"] is False and env["head_before"] is None,
              "all typed false, head_before null", str(env))

        # (7) Divergent CLAUDE_WORKING_DIRECTORY: a seat that commits in its OWN
        #     cwd lands the commit in CLAUDE_WORKING_DIRECTORY (provider cwd pin)
        #     AND the guard (pinned to repo_dir) detects head_moved=true.
        make_fake_bin(bins, "codex", _fake_codex(
            "open('seat.txt','w').write('x')\n"
            "subprocess.run(['git','add','.'])\n"
            "subprocess.run(['git','commit','-q','-m','seat'])\n"))
        repo = Path(td) / "r7"; repo.mkdir(); _init_repo(str(repo))
        before_sha = _git(["rev-parse", "HEAD"], str(repo)).stdout.strip()
        # process cwd for cli.py is a DISTINCT empty dir, NOT the repo.
        elsewhere = Path(td) / "elsewhere"; elsewhere.mkdir()
        env_d = path_with(bins)
        env_d["CLAUDE_WORKING_DIRECTORY"] = str(repo)
        proc = _run_cli(["dispatch", "do it", "--json"], env=env_d, cwd=str(elsewhere), timeout=30)
        envv = json.loads(proc.stdout) if proc.returncode == 0 else None
        after_sha = _git(["rev-parse", "HEAD"], str(repo)).stdout.strip()
        check("dispatch divergent CWD: commit lands in CLAUDE_WORKING_DIRECTORY (provider cwd pin)",
              after_sha != before_sha, "repo HEAD moved", f"{before_sha} -> {after_sha}")
        check("dispatch divergent CWD: guard (pinned repo_dir) detects head_moved=true (BLOCKING-1/R13)",
              envv and envv["head_moved"] is True, "head_moved=true", str(envv))

        # (8) Engine-printed path == written file (with --session-id, no -o).
        make_fake_bin(bins, "codex", _fake_codex())
        repo = Path(td) / "r8"; repo.mkdir(); _init_repo(str(repo))
        env_d = path_with(bins); env_d["CLAUDE_WORKING_DIRECTORY"] = str(repo)
        proc = _run_cli(["dispatch", "x", "--session-id", "sess1", "--json"],
                        env=env_d, cwd=str(repo), timeout=30)
        last = [l for l in proc.stdout.splitlines() if l.strip()][-1]
        check("dispatch --session-id derives + PRINTS dispatch-<seat>.json path (== written file)",
              proc.returncode == 0 and last.endswith("dispatch-codex.json")
              and (repo / last).exists()
              and json.loads((repo / last).read_text())["seat"] == "codex",
              "printed path == written dispatch-codex.json", f"{proc.returncode}: {last!r}")

        # (9) Default-seat routing via [dispatch].seat config -> agy.
        make_fake_bin(bins, "agy",
            "import sys\nsys.stdin.read() if False else None\nprint('AGY OK WORK')\n")
        repo = Path(td) / "r9"; repo.mkdir(); _init_repo(str(repo))
        proj = Path(td) / "proj"; (proj / ".crew").mkdir(parents=True)
        (proj / ".crew" / "config.toml").write_text('[dispatch]\nseat = "agy"\n')
        home = Path(td) / "home"; home.mkdir()
        proc, env = _run_dispatch(repo, bins, ["x"], project_dir=proj, home=str(home))
        check("dispatch [dispatch].seat=agy routes (no --seat) -> envelope seat=agy",
              env and env["seat"] == "agy", "seat=agy", str(env))

        # (9b) HERMETICITY REGRESSION (billable-leak incident): a REAL
        #      ~/.crew-config.toml in the parent process's HOME must NEVER leak
        #      into a dispatch subprocess that doesn't pass home=. Before the
        #      path_with/_run_cli HOME neutralization, a developer's global
        #      [dispatch].seat = "cursor-glm" rerouted this test's dispatch to
        #      the REAL (billable) Cursor CLI instead of the fake codex bin.
        poisoned = Path(td) / "poisoned-home"; poisoned.mkdir()
        (poisoned / ".crew-config.toml").write_text('[dispatch]\nseat = "cursor-glm"\n')
        repo = Path(td) / "r9b"; repo.mkdir(); _init_repo(str(repo))
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = str(poisoned)
        try:
            proc, env = _run_dispatch(repo, bins, ["x"])  # no home= — the leaky shape
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
        check("dispatch ignores the PARENT process's real ~/.crew-config.toml "
              "(no home= -> neutral HOME -> builtin codex, NOT cursor-glm)",
              env and env["seat"] == "codex" and env["ok"] is True,
              "seat=codex ok=true (fake bin ran)", str(env))

        # (10) DETACH (checkout <sha>, same commit) -> branch_changed=true via the
        #      '<detached>' sentinel, head_moved=false (BLOCKING-1: a None branch
        #      would have masked this to false).
        make_fake_bin(bins, "codex", _fake_codex(
            "subprocess.run(['git','checkout','-q','--detach','HEAD'])\n"))
        repo = Path(td) / "r10"; repo.mkdir(); _init_repo(str(repo))
        proc, env = _run_dispatch(repo, bins, ["do it"])
        check("dispatch seat DETACH (main -> checkout sha) -> branch_changed=true, head_moved=false",
              env and env["branch_changed"] is True and env["head_moved"] is False
              and env["branch_before"] == "main" and env["branch_after"] == "<detached HEAD>",
              "branch_changed=true head_moved=false main->'<detached HEAD>'", str(env))

        # (11) RE-ATTACH from a pre-detached HEAD (checkout main) -> branch_changed=true.
        make_fake_bin(bins, "codex", _fake_codex(
            "subprocess.run(['git','checkout','-q','main'])\n"))
        repo = Path(td) / "r11"; repo.mkdir(); _init_repo(str(repo))
        _git(["checkout", "-q", "--detach", "HEAD"], str(repo))  # detached BEFORE dispatch
        proc, env = _run_dispatch(repo, bins, ["do it"])
        check("dispatch seat RE-ATTACH ('<detached HEAD>' -> main) -> branch_changed=true, head_moved=false",
              env and env["branch_changed"] is True and env["head_moved"] is False
              and env["branch_before"] == "<detached HEAD>" and env["branch_after"] == "main",
              "branch_changed=true '<detached HEAD>'->main", str(env))

        # (12) ALREADY-STAGED index, seat stages MORE (True->True case a bool
        #      would miss) -> staged_changed=true via the index-tree-hash compare.
        make_fake_bin(bins, "codex", _fake_codex(
            "open('seat_more.txt','w').write('y')\n"
            "subprocess.run(['git','add','seat_more.txt'])\n"))
        repo = Path(td) / "r12"; repo.mkdir(); _init_repo(str(repo))
        (repo / "pre_staged.txt").write_text("z\n")
        _git(["add", "pre_staged.txt"], str(repo))  # index ALREADY dirty BEFORE dispatch
        proc, env = _run_dispatch(repo, bins, ["do it"])
        check("dispatch already-staged -> seat stages MORE -> staged_changed=true (True->True caught)",
              env and env["staged_changed"] is True and env["head_moved"] is False
              and env["staged_before"] != env["staged_after"]
              and isinstance(env["staged_before"], str) and isinstance(env["staged_after"], str),
              "staged_changed=true, tree sha differs", str(env))

    # --- pre-run rejections (exit 2, NO envelope, NO path) ---
    log_section("dispatch — pre-run rejections + arg XOR + exit/advisory")
    with tempfile.TemporaryDirectory() as td:
        bins = Path(td) / "bin"; bins.mkdir()
        make_fake_bin(bins, "codex", _fake_codex())
        repo = Path(td) / "r"; repo.mkdir(); _init_repo(str(repo))
        env_d = path_with(bins); env_d["CLAUDE_WORKING_DIRECTORY"] = str(repo)

        proc = _run_cli(["dispatch", "x", "--seat", "opus", "--json"], env=env_d, cwd=str(repo), timeout=30)
        check("dispatch --seat opus (Task) -> exit 2, no path printed",
              proc.returncode == 2 and "Task seat" in proc.stderr and not proc.stdout.strip(),
              "exit2 Task seat, empty stdout", f"{proc.returncode}: {proc.stdout!r} {proc.stderr!r}")

        proc = _run_cli(["dispatch", "x", "--seat", "bogus", "--json"], env=env_d, cwd=str(repo), timeout=30)
        check("dispatch --seat bogus (unknown) -> exit 2, no path printed",
              proc.returncode == 2 and "subprocess seat" in proc.stderr and not proc.stdout.strip(),
              "exit2 unknown, empty stdout", f"{proc.returncode}: {proc.stdout!r} {proc.stderr!r}")

        proc = _run_cli(["dispatch", "x", "--session-id", "<SID>", "--json"], env=env_d, cwd=str(repo), timeout=30)
        check("dispatch unsubstituted <…> session-id placeholder -> exit 2, no path",
              proc.returncode == 2 and "placeholder" in proc.stderr and not proc.stdout.strip(),
              "exit2 placeholder, empty stdout", f"{proc.returncode}: {proc.stdout!r} {proc.stderr!r}")

        # -f XOR positional
        tf = Path(td) / "task.txt"; tf.write_text("FROM FILE TASK")
        proc, env = _run_dispatch(repo, bins, ["-f", str(tf)])
        check("dispatch -f reads task file and runs (ok=true)",
              env and env["ok"] is True, "ok=true via -f", str(env))
        proc = _run_cli(["dispatch", "x", "-f", str(tf), "--json"], env=env_d, cwd=str(repo), timeout=30)
        check("dispatch BOTH -f and positional -> exit 2 'not both'",
              proc.returncode == 2 and "not both" in proc.stderr,
              "exit2 not both", f"{proc.returncode}: {proc.stderr!r}")
        proc = _run_cli(["dispatch", "--json"], env=env_d, cwd=str(repo), timeout=30)
        check("dispatch NEITHER -f nor positional -> exit 2 'no task'",
              proc.returncode == 2 and "no task" in proc.stderr,
              "exit2 no task", f"{proc.returncode}: {proc.stderr!r}")

        # MINOR-2: a non-UTF-8 -f task file raises UnicodeDecodeError (a ValueError,
        # NOT an OSError) — it must be caught so dispatch prints the graceful error +
        # exits 2 cleanly, never a traceback.
        bad = Path(td) / "bad-task.bin"; bad.write_bytes(b"\xff\xfe\x00bad")
        proc = _run_cli(["dispatch", "-f", str(bad), "--json"], env=env_d, cwd=str(repo), timeout=30)
        check("dispatch -f non-UTF-8 file -> exit 2 graceful 'cannot read task file' (no traceback) (MINOR-2)",
              proc.returncode == 2 and "cannot read task file" in proc.stderr
              and "Traceback" not in proc.stderr,
              "exit2 graceful, no traceback", f"{proc.returncode}: {proc.stderr!r}")

    # --- in-process: unavailable-seat skip, non-writable fail-fast, null model header ---
    log_section("dispatch — unavailable skip / non-writable fail-fast / null-model header")
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "r"; repo.mkdir(); _init_repo(str(repo))
        saved_wd = os.environ.get("CLAUDE_WORKING_DIRECTORY")
        os.environ["CLAUDE_WORKING_DIRECTORY"] = str(repo)
        # cmd_dispatch derives a RELATIVE -o path (.crew/reviews/<sid>/…) written
        # by _emit relative to the process cwd — chdir into repo so it lands there
        # (NOT in the real project repo running the test).
        saved_cwd = os.getcwd()
        os.chdir(str(repo))
        try:
            # Unavailable-but-known-writable seat -> skipped ok=false envelope, exit 0, path printed.
            class _Unavail(Provider):
                name = "codex"
                supports_workspace_write = True
                def is_available(self): return (False, "codex not found on PATH")
                def run(self, *a, **k): raise AssertionError("seat ran despite unavailable")
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_Unavail()):
                with contextlib.redirect_stdout(out):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=True,
                                                        session_id="usess"))
            lines = [l for l in out.getvalue().splitlines() if l.strip()]
            envj = json.loads(Path(lines[-1]).read_text())
            check("dispatch unavailable seat -> exit 0, ok=false skipped envelope, path printed",
                  rc == 0 and envj["ok"] is False and "skipped" in (envj["error"] or "")
                  and envj["head_moved"] is False and envj["staged_changed"] is False
                  and envj["branch_changed"] is False
                  and lines[-1].endswith("dispatch-codex.json"),
                  "exit0 ok=false skipped path-printed", f"rc={rc} {envj}")

            # Non-writable provider -> fail fast exit 2, seat never probed/run.
            class _NoWrite(Provider):
                name = "codex"
                supports_workspace_write = False
                def is_available(self): raise AssertionError("is_available probed on non-writable seat")
                def run(self, *a, **k): raise AssertionError("non-writable seat ran")
            err = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_NoWrite()):
                with contextlib.redirect_stderr(err):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=True))
            check("dispatch non-writable seat -> exit 2, fails fast (no is_available/run)",
                  rc == 2 and "workspace-write" in err.getvalue(),
                  "exit2 fail-fast", f"rc={rc} {err.getvalue()!r}")

            # Null-model human header omits the (…) parens.
            class _OkNullModel(Provider):
                name = "codex"
                supports_workspace_write = True
                def is_available(self): return (True, "")
                def run(self, *a, **k):
                    return ProviderResult(name="codex", model=None, ok=True,
                                          output="BODY", error=None, elapsed=0.1)
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_OkNullModel()):
                with contextlib.redirect_stdout(out):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False))
            head = out.getvalue().splitlines()[0]
            check("dispatch human header null model -> 'dispatch: codex' (no parens)",
                  rc == 0 and head == "dispatch: codex", "dispatch: codex", repr(head))

            class _OkModel(_OkNullModel):
                def run(self, *a, **k):
                    return ProviderResult(name="codex", model="gpt-x", ok=True,
                                          output="BODY", error=None, elapsed=0.1)
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_OkModel()):
                with contextlib.redirect_stdout(out):
                    cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False))
            head = out.getvalue().splitlines()[0]
            check("dispatch human header with model -> 'dispatch: codex (gpt-x)'",
                  head == "dispatch: codex (gpt-x)", "dispatch: codex (gpt-x)", repr(head))

            # BLOCKING-1: human-mode warnings appear LAST and in the fixed D4
            # relative order HEAD -> staged -> branch. The staged warning ALWAYS
            # fires when staged_changed is true (a guard must never silently drop a
            # real staged violation — incl. the commit-then-additional-`git add`
            # case), worded DESCRIPTIVELY ("staged/index content changed … inspect
            # with git status") so it is non-misleading on a pure commit too.
            STAGED_MARK = "staged/index content changed"
            class _OkBody(Provider):
                name = "codex"
                supports_workspace_write = True
                def is_available(self): return (True, "")
                def run(self, *a, **k):
                    return ProviderResult(name="codex", model=None, ok=True,
                                          output="BODY", error=None, elapsed=0.1)
            # Slice A: head_moved + staged + branch ALL change (the
            # commit-then-additional-stage shape) -> ALL THREE warn, in the fixed
            # HEAD -> staged -> branch order, last, after the body.
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_OkBody()), \
                 mock.patch("multiagent.cli._git_head", side_effect=["AAA", "BBB"]), \
                 mock.patch("multiagent.cli._git_staged", side_effect=["s1", "s2"]), \
                 mock.patch("multiagent.cli._git_branch", side_effect=["main", "tmp"]):
                with contextlib.redirect_stdout(out):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False))
            wlines = out.getvalue().splitlines()
            warn_idx = [i for i, l in enumerate(wlines) if l.startswith("WARNING:")]
            body_idx = next(i for i, l in enumerate(wlines) if l == "BODY")
            check("dispatch human warnings: head+staged+branch ALL warn, HEAD->staged->branch last (BLOCKING-1)",
                  rc == 0 and len(warn_idx) == 3
                  and "moved HEAD" in wlines[warn_idx[0]]
                  and STAGED_MARK in wlines[warn_idx[1]]
                  and "changed branch" in wlines[warn_idx[2]]
                  and warn_idx == [len(wlines) - 3, len(wlines) - 2, len(wlines) - 1]
                  and min(warn_idx) > body_idx,
                  "3 warnings (HEAD->staged->branch), last", str(wlines))
            # Slice B: independent STAGE-only (head NOT moved) + branch_changed ->
            # staged warning fires, in the fixed staged->branch order.
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_OkBody()), \
                 mock.patch("multiagent.cli._git_head", side_effect=["AAA", "AAA"]), \
                 mock.patch("multiagent.cli._git_staged", side_effect=["s1", "s2"]), \
                 mock.patch("multiagent.cli._git_branch", side_effect=["main", "tmp"]):
                with contextlib.redirect_stdout(out):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False))
            wlines = out.getvalue().splitlines()
            warn_idx = [i for i, l in enumerate(wlines) if l.startswith("WARNING:")]
            check("dispatch human warnings: independent stage-only DOES warn, staged->branch order",
                  rc == 0 and len(warn_idx) == 2
                  and STAGED_MARK in wlines[warn_idx[0]]
                  and "changed branch" in wlines[warn_idx[1]],
                  "2 warnings (staged->branch)", str(wlines))
            # BLOCKING-1 (a): a pure COMMIT (head_moved + staged consequence, same
            # branch) STILL surfaces the staged warning — but worded
            # non-misleadingly (descriptive "staged/index content changed", NOT a
            # "this is the fix / git reset unstages it" no-op promise).
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_OkBody()), \
                 mock.patch("multiagent.cli._git_head", side_effect=["AAA", "BBB"]), \
                 mock.patch("multiagent.cli._git_staged", side_effect=["s1", "s2"]), \
                 mock.patch("multiagent.cli._git_branch", side_effect=["main", "main"]):
                with contextlib.redirect_stdout(out):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False))
            wlines = out.getvalue().splitlines()
            staged_line = next((l for l in wlines if STAGED_MARK in l), "")
            check("dispatch human: pure commit warns HEAD AND surfaces staged (worded non-misleadingly) (BLOCKING-1a)",
                  rc == 0 and any("moved HEAD" in l for l in wlines)
                  and bool(staged_line)
                  and "inspect with `git status`" in staged_line
                  and "STAGED changes against instruction" not in staged_line,
                  "HEAD warning + descriptive staged line", str(wlines))
            # BLOCKING-1 (b): commit-then-additional-`git add` — at the guard-data
            # level this is head_moved=true AND staged_changed=true (same shape as a
            # pure commit; the guard CANNOT distinguish extra staged content from
            # the commit's own tree, which is exactly WHY it must always warn). The
            # staged warning MUST be present so the codex-flagged case is not hidden.
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_OkBody()), \
                 mock.patch("multiagent.cli._git_head", side_effect=["AAA", "BBB"]), \
                 mock.patch("multiagent.cli._git_staged", side_effect=["s1", "s3"]), \
                 mock.patch("multiagent.cli._git_branch", side_effect=["main", "main"]):
                with contextlib.redirect_stdout(out):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False))
            wlines = out.getvalue().splitlines()
            check("dispatch human: commit-then-extra-stage surfaces the staged warning (BLOCKING-1b)",
                  rc == 0 and any("moved HEAD" in l for l in wlines)
                  and any(STAGED_MARK in l for l in wlines),
                  "HEAD warning + staged warning both present", str(wlines))
            # BLOCKING-2: dispatch STARTED detached (branch_before == sentinel) and
            # the seat re-attached to a branch -> recovery hint references
            # head_before via `git checkout --detach <head_before>`, NOT the
            # nonsense `git checkout <sentinel>`.
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_OkBody()), \
                 mock.patch("multiagent.cli._git_head", side_effect=["origsha", "origsha"]), \
                 mock.patch("multiagent.cli._git_staged", side_effect=["s1", "s1"]), \
                 mock.patch("multiagent.cli._git_branch", side_effect=[cli._DETACHED, "main"]):
                with contextlib.redirect_stdout(out):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False))
            wlines = out.getvalue().splitlines()
            bwarn = next((l for l in wlines if "changed branch" in l), "")
            check("dispatch human: detached-before recovery uses 'git checkout --detach <head_before>' (BLOCKING-2)",
                  rc == 0 and "git checkout --detach origsha" in bwarn
                  and f"git checkout {cli._DETACHED}" not in bwarn,
                  "git checkout --detach origsha (not the sentinel)", repr(bwarn))

            # MINOR-1: detached-before recovery with head_before=None (the _git_head
            # probe errored while _git_branch returned the detached sentinel) must
            # NOT emit the literal `--detach None` — it falls back to a safe generic
            # hint instead.
            out = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_OkBody()), \
                 mock.patch("multiagent.cli._git_head", side_effect=[None, None]), \
                 mock.patch("multiagent.cli._git_staged", side_effect=["s1", "s1"]), \
                 mock.patch("multiagent.cli._git_branch", side_effect=[cli._DETACHED, "main"]):
                with contextlib.redirect_stdout(out):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False))
            wlines = out.getvalue().splitlines()
            bwarn = next((l for l in wlines if "changed branch" in l), "")
            check("dispatch human: detached-before recovery null-guards head_before (no literal '--detach None') (MINOR-1)",
                  rc == 0 and "--detach None" not in bwarn
                  and "re-detach to your original commit" in bwarn,
                  "safe generic hint, never '--detach None'", repr(bwarn))

            # MINOR-2: human-mode failure with -o still WRITES the envelope file
            # (parity with the JSON path) and keeps exit 1.
            class _Fail(Provider):
                name = "codex"
                supports_workspace_write = True
                def is_available(self): return (True, "")
                def run(self, *a, **k):
                    return ProviderResult(name="codex", model=None, ok=False,
                                          output="", error="seat blew up", elapsed=0.1)
            ofile = str(repo / "fail-envelope.json")
            err = io.StringIO()
            with mock.patch("multiagent.cli.get_provider", return_value=_Fail()):
                with contextlib.redirect_stderr(err):
                    rc = cli.cmd_dispatch(_dispatch_ns(seat="codex", task="x", json=False, out=ofile))
            wrote = Path(ofile).exists()
            envf = json.loads(Path(ofile).read_text()) if wrote else None
            check("dispatch human-mode failure with -o WRITES the envelope file, exit 1 (MINOR-2 parity)",
                  rc == 1 and wrote and envf and envf["ok"] is False
                  and "seat blew up" in err.getvalue(),
                  "exit1 + envelope file written", f"rc={rc} wrote={wrote} {envf}")
        finally:
            os.chdir(saved_cwd)
            if saved_wd is None:
                os.environ.pop("CLAUDE_WORKING_DIRECTORY", None)
            else:
                os.environ["CLAUDE_WORKING_DIRECTORY"] = saved_wd

    # --- §10 dispatch.md static fence check ---
    log_section("dispatch.md static fence check (§10)")
    import re as _re
    md_path = SCRIPT_DIR.parent / "commands" / "dispatch.md"
    text = md_path.read_text(encoding="utf-8")
    # Frontmatter allowed-tools includes Write.
    fm = text.split("---", 2)
    front = fm[1] if len(fm) >= 3 else ""
    at_line = next((l for l in front.splitlines() if l.strip().startswith("allowed-tools")), "")
    check("dispatch.md allowed-tools includes Write (spill mechanism)",
          "Write" in at_line, "Write in allowed-tools", at_line)
    # Extract bash code-fence command lines only (NOT prose): collect lines
    # between a ```bash fence open and the next ``` close.
    fence_lines = []
    collecting = False
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("```bash"):
            collecting = True
            continue
        if st.startswith("```"):
            collecting = False
            continue
        if collecting and st and not st.startswith("#"):
            fence_lines.append(st)
    joined = "\n".join(fence_lines)
    tokens_per_line = [l.split() for l in fence_lines]
    check("dispatch.md: no executed line carries -o/--out (engine derives + prints the path)",
          all("-o" not in t and "--out" not in t for t in tokens_per_line),
          "no -o/--out in command lines", joined)
    check("dispatch.md: no executed git add/commit/push/checkout -b",
          not _re.search(r"\bgit\s+add\b", joined)
          and not _re.search(r"\bgit\s+commit\b", joined)
          and not _re.search(r"\bgit\s+push\b", joined)
          and not _re.search(r"\bgit\s+checkout\s+-b\b", joined),
          "no index/commit/push/branch mutation in fences", joined)
    check("dispatch.md: --seat appears in NO executed command line (passed only when user names one)",
          "--seat" not in joined, "no --seat in fences", joined)
    check("dispatch.md: no executed line constructs a literal dispatch-<seat>.json (only the glob)",
          all(("dispatch-" not in l) or ("dispatch-*.json" in l) for l in fence_lines),
          "only dispatch-*.json glob, never dispatch-<seat>.json", joined)
    check("dispatch.md: engine reached via the bare crew dispatcher (dispatch subcommand)",
          '"${CLAUDE_PLUGIN_ROOT}/crew" dispatch' in joined,
          "bare crew dispatch invocation", joined)
    # BLOCKING-2: the task is ALWAYS spilled to a -f file (Write tool writes exact
    # bytes, no shell), NEVER passed positionally — a positional task with $(…)/
    # $VAR/backticks/" would be expanded or mangled by the Bash-tool shell.
    dispatch_cmd_lines = [
        l for l in fence_lines if '"${CLAUDE_PLUGIN_ROOT}/crew" dispatch' in l
    ]
    check("dispatch.md: every crew dispatch invocation passes -f, never a positional task (BLOCKING-2)",
          bool(dispatch_cmd_lines)
          and all("-f" in l.split() for l in dispatch_cmd_lines),
          "every dispatch line carries -f", str(dispatch_cmd_lines))
    check("dispatch.md: no crew dispatch invocation passes a positional \"<TASK>\"/\"$ARGUMENTS\" (BLOCKING-2)",
          all('"<TASK>"' not in l and '"$ARGUMENTS"' not in l for l in dispatch_cmd_lines),
          "no positional task token in any dispatch line", str(dispatch_cmd_lines))
    # /crew: prefix on command references. Only flag a SLASH-COMMAND token
    # (preceded by whitespace or a backtick) — NOT a path component like
    # `.crew/dispatch/` or a slash-joined word pair like `review/debate`.
    bare = _re.findall(
        r"(?<=[\s`])/(review|debate|dispatch|execute|build|measure-twice)\b", text)
    check("dispatch.md: all command references use the /crew: prefix",
          not bare and "/crew:" in text, "no bare /command refs", str(bare))


def test_doctor():
    log_section("doctor probe (registry-derived, non-billable, cursor probed once)")
    from multiagent import seats as _seats  # noqa: E402

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"; bins.mkdir()
        home = d / "home"; home.mkdir()
        proj = d / "proj"; proj.mkdir()
        agent_calls = d / "agent_calls.txt"
        codex_calls = d / "codex_calls.txt"
        # codex present — is_available() is pure shutil.which (NEVER exec'd); the
        # fake records any exec so we can assert doctor made NO billable run.
        make_fake_bin(bins, "codex", f"""
        open({str(codex_calls)!r}, 'a').write('x')
        """)
        # agent present — the cursor identity probe DOES exec `agent --version`.
        # Record each call so we can assert it runs AT MOST ONCE (fanned to all
        # cursor-* seats). Print a date-stamp first line so the identity check passes.
        make_fake_bin(bins, "agent", f"""
        open({str(agent_calls)!r}, 'a').write('x')
        print('2026.06.24-00-00-00-abcdef0')
        """)
        # agy is NOT faked -> absent on the isolated PATH.
        # Isolated PATH: fake bins + the python3/env resolution dirs, EXCLUDING the
        # real homebrew/.local dirs (so the real codex/agy/agent never leak in).
        def isol_env(home_p=None, proj_p=None):
            env = dict(os.environ)
            env["PATH"] = os.pathsep.join(
                [str(bins), os.path.dirname(sys.executable), "/usr/bin", "/bin"]
            )
            if home_p is not None:
                env["HOME"] = str(home_p)
            if proj_p is not None:
                env["CLAUDE_PROJECT_DIR"] = str(proj_p)
            env.pop("CLAUDE_SESSION_ID", None)
            return env

        env = isol_env(home, proj)
        proc = _run_cli(["doctor", "--session-id", "SES1"], env=env, cwd=str(proj), timeout=30)
        check("doctor exits 0", proc.returncode == 0, "0", f"{proc.returncode}: {proc.stderr[:200]}")
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:
            payload = None
            check("doctor stdout parses as JSON", False, "valid json", f"{exc}: {proc.stdout[:200]}")
        if payload is not None:
            sub = payload.get("subprocess", {})
            check("doctor: codex detected available (present on PATH)",
                  sub.get("codex", {}).get("available") is True, "codex true", str(sub.get("codex")))
            check("doctor: codex-luna tracks the shared codex binary (available)",
                  sub.get("codex-luna", {}).get("available") is True,
                  "codex-luna true", str(sub.get("codex-luna")))
            check("doctor: agy detected ABSENT with a PATH diag",
                  sub.get("agy", {}).get("available") is False
                  and "PATH" in (sub.get("agy", {}).get("diag") or ""),
                  "agy false + diag", str(sub.get("agy")))
            _cursor = set(_seats.group_tokens()["cursor"])
            check("doctor: every cursor-* seat present (probe fanned)",
                  all(sub.get(s, {}).get("available") is True
                      for s in sub if s in _cursor),
                  "all cursor true", str({k: v for k, v in sub.items() if k in _cursor}))
            task = payload.get("task", {})
            check("doctor: task block in sorted task-seat order (deterministic)",
                  list(task.keys()) == sorted(_seats.task_seats()),
                  str(sorted(_seats.task_seats())), str(list(task.keys())))
        # stdout carries ONLY JSON (no stray non-JSON line).
        check("doctor stdout is JSON-only (no extra lines)",
              proc.stdout.strip().startswith("{") and proc.stdout.strip().endswith("}"),
              "pure JSON", proc.stdout[:120])
        # the cursor binary was probed AT MOST ONCE (not once per cursor-* seat).
        n_agent = agent_calls.read_text().count("x") if agent_calls.exists() else 0
        check("doctor probes the cursor binary AT MOST ONCE (fanned to all cursor-* seats)",
              n_agent <= 1, "<=1 probe", str(n_agent))
        # codex was NEVER exec'd (is_available is which-only -> no billable run).
        check("doctor makes NO billable run (codex never exec'd; pure shutil.which)",
              not codex_calls.exists(), "codex not exec'd", "exec'd" if codex_calls.exists() else "ok")
        # session-scoped file written AND matches stdout.
        sess_file = proj / ".crew" / "reviews" / "SES1" / "doctor.json"
        check("doctor --session-id writes .crew/reviews/<id>/doctor.json",
              sess_file.is_file(), "session file present",
              "present" if sess_file.is_file() else "missing")
        if sess_file.is_file() and payload is not None:
            check("doctor session file == stdout JSON",
                  json.loads(sess_file.read_text()) == payload, "equal", "differs")

        # -o wins over the session path (session file NOT also written).
        proj2 = d / "proj2"; proj2.mkdir()
        out_path = d / "explicit-doctor.json"
        env2 = isol_env(home, proj2)
        proc = _run_cli(["doctor", "--session-id", "SES2", "-o", str(out_path)],
                        env=env2, cwd=str(proj2), timeout=30)
        check("doctor -o writes the explicit path", out_path.is_file(),
              "explicit file", "present" if out_path.is_file() else "missing")
        check("doctor -o wins: the session path is NOT also written",
              not (proj2 / ".crew" / "reviews" / "SES2" / "doctor.json").exists(),
              "no session file", "session file present")
        check("doctor -o still prints JSON to stdout",
              proc.stdout.strip().startswith("{"), "JSON on stdout", proc.stdout[:120])

        # NEITHER session nor -o -> NO file written, stdout still JSON.
        proj3 = d / "proj3"; proj3.mkdir()
        env3 = isol_env(home, proj3)
        proc = _run_cli(["doctor"], env=env3, cwd=str(proj3), timeout=30)
        check("doctor with NEITHER --session-id nor -o writes NO file",
              not (proj3 / ".crew").exists(), "no .crew dir", "created .crew")
        check("doctor (no session/-o) still prints JSON to stdout",
              proc.stdout.strip().startswith("{"), "JSON on stdout", proc.stdout[:120])


def test_probe():
    log_section("crew probe (live seat smoke test, opt-in billable)")

    def isol_env(bins):
        # Isolated PATH — REPLACES PATH entirely (not path_with's prepend) so a
        # real codex on the dev machine can never leak in and mask an "absent
        # CLI" case (mirrors test_doctor's isol_env).
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join(
            [str(bins), os.path.dirname(sys.executable), "/usr/bin", "/bin"]
        )
        env.pop("CLAUDE_SESSION_ID", None)
        return env

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        # 1. Unknown seat name -> exit 2, error on stderr, no JSON on stdout.
        bins_empty = d / "bin-empty"; bins_empty.mkdir()
        env_empty = isol_env(bins_empty)
        proc = _run_cli(["probe", "not-a-real-seat"], env=env_empty, timeout=30)
        check("probe unknown seat -> exit 2",
              proc.returncode == 2, "2", str(proc.returncode))
        check("probe unknown seat -> error on stderr",
              proc.stderr.strip() != "", "stderr message", proc.stderr[:120])
        check("probe unknown seat -> no JSON on stdout",
              proc.stdout.strip() == "", "empty stdout", proc.stdout[:120])

        # 2. Absent CLI (codex not on the isolated PATH) -> status skipped, but
        # since EVERY probed seat was skipped, exit 2 (all-skipped must NOT read
        # as a healthy green). The JSON still records the skipped status.
        proc = _run_cli(["probe", "codex"], env=env_empty, timeout=30)
        check("probe all-skipped -> exit 2", proc.returncode == 2, "2",
              f"{proc.returncode}: {proc.stderr[:200]}")
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:
            payload = None
            check("probe absent CLI stdout parses as JSON", False, "valid json",
                  f"{exc}: {proc.stdout[:200]}")
        if payload is not None:
            check("probe absent CLI -> codex status skipped",
                  payload.get("codex", {}).get("status") == "skipped",
                  "skipped", str(payload.get("codex")))

        # 3. Fake codex that prints PROBE-OK -> status pass, exit 0.
        bins_pass = d / "bin-pass"; bins_pass.mkdir()
        make_fake_bin(bins_pass, "codex", """
        import sys
        a = sys.argv[1:]
        out = None
        for i, x in enumerate(a):
            if x == "-o":
                out = a[i + 1]
        sys.stdin.read()
        with open(out, "w") as f:
            f.write("PROBE-OK\\n")
        sys.exit(0)
        """)
        env_pass = isol_env(bins_pass)
        proc = _run_cli(["probe", "codex"], env=env_pass, timeout=30)
        check("probe pass -> exit 0", proc.returncode == 0, "0",
              f"{proc.returncode}: {proc.stderr[:200]}")
        # 6. stderr contains "BILLABLE" before any provider ran.
        check("probe pass -> BILLABLE warning on stderr",
              "BILLABLE" in proc.stderr, "BILLABLE", proc.stderr[:200])
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:
            payload = None
            check("probe pass stdout parses as JSON", False, "valid json",
                  f"{exc}: {proc.stdout[:200]}")
        if payload is not None:
            check("probe pass -> codex status pass",
                  payload.get("codex", {}).get("status") == "pass",
                  "pass", str(payload.get("codex")))
        # 7. stdout parses as JSON and contains ONLY the JSON (doctor's discipline).
        check("probe pass -> stdout is JSON-only (no extra lines)",
              proc.stdout.strip().startswith("{") and proc.stdout.strip().endswith("}"),
              "pure JSON", proc.stdout[:120])

        # 7b. Mixed pass + skipped (codex present via env_pass, agy absent on the
        # isolated PATH) -> exit 0. A skipped seat alongside a real pass is still
        # healthy; only ALL-skipped (test 2) or any fail/degraded flips nonzero.
        proc = _run_cli(["probe", "codex", "agy"], env=env_pass, timeout=30)
        check("probe pass+skipped -> exit 0", proc.returncode == 0, "0",
              f"{proc.returncode}: {proc.stderr[:200]}")
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:
            payload = None
            check("probe pass+skipped stdout parses as JSON", False, "valid json",
                  f"{exc}: {proc.stdout[:200]}")
        if payload is not None:
            check("probe pass+skipped -> codex pass, agy skipped",
                  payload.get("codex", {}).get("status") == "pass"
                  and payload.get("agy", {}).get("status") == "skipped",
                  "codex=pass agy=skipped",
                  f"codex={payload.get('codex')} agy={payload.get('agy')}")

        # 4. Fake codex that prints unrelated prose -> status degraded, exit 1.
        bins_degraded = d / "bin-degraded"; bins_degraded.mkdir()
        make_fake_bin(bins_degraded, "codex", """
        import sys
        a = sys.argv[1:]
        out = None
        for i, x in enumerate(a):
            if x == "-o":
                out = a[i + 1]
        sys.stdin.read()
        with open(out, "w") as f:
            f.write("Sure, here is a completely unrelated essay about cats.")
        sys.exit(0)
        """)
        env_degraded = isol_env(bins_degraded)
        proc = _run_cli(["probe", "codex"], env=env_degraded, timeout=30)
        check("probe degraded -> exit 1", proc.returncode == 1, "1",
              f"{proc.returncode}: {proc.stderr[:200]}")
        check("probe degraded -> BILLABLE warning on stderr",
              "BILLABLE" in proc.stderr, "BILLABLE", proc.stderr[:200])
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:
            payload = None
            check("probe degraded stdout parses as JSON", False, "valid json",
                  f"{exc}: {proc.stdout[:200]}")
        if payload is not None:
            check("probe degraded -> codex status degraded",
                  payload.get("codex", {}).get("status") == "degraded",
                  "degraded", str(payload.get("codex")))

        # 5. Fake codex that exits nonzero with no output -> status fail, exit 1.
        bins_fail = d / "bin-fail"; bins_fail.mkdir()
        make_fake_bin(bins_fail, "codex", """
        import sys
        sys.stdin.read()
        sys.stderr.write("boom: simulated codex crash")
        sys.exit(1)
        """)
        env_fail = isol_env(bins_fail)
        proc = _run_cli(["probe", "codex"], env=env_fail, timeout=30)
        check("probe fail -> exit 1", proc.returncode == 1, "1",
              f"{proc.returncode}: {proc.stderr[:200]}")
        check("probe fail -> BILLABLE warning on stderr",
              "BILLABLE" in proc.stderr, "BILLABLE", proc.stderr[:200])
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:
            payload = None
            check("probe fail stdout parses as JSON", False, "valid json",
                  f"{exc}: {proc.stdout[:200]}")
        if payload is not None:
            check("probe fail -> codex status fail",
                  payload.get("codex", {}).get("status") == "fail",
                  "fail", str(payload.get("codex")))

        # 10. Fake codex that echoes the probe prompt verbatim (CONTAINS
        # "single line: PROBE-OK" but no line strips to exactly "PROBE-OK")
        # -> status degraded, exit 1. Regression for the false-pass where the
        # probe prompt itself contains the substring "PROBE-OK".
        bins_echo = d / "bin-echo"; bins_echo.mkdir()
        make_fake_bin(bins_echo, "codex", """
        import sys
        a = sys.argv[1:]
        out = None
        for i, x in enumerate(a):
            if x == "-o":
                out = a[i + 1]
        prompt = sys.stdin.read()
        with open(out, "w") as f:
            f.write(prompt)
        sys.exit(0)
        """)
        env_echo = isol_env(bins_echo)
        proc = _run_cli(["probe", "codex"], env=env_echo, timeout=30)
        check("probe echoed-prompt -> exit 1 (no exact PROBE-OK line)",
              proc.returncode == 1, "1", f"{proc.returncode}: {proc.stderr[:200]}")
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:
            payload = None
            check("probe echoed-prompt stdout parses as JSON", False, "valid json",
                  f"{exc}: {proc.stdout[:200]}")
        if payload is not None:
            check("probe echoed-prompt -> codex status degraded",
                  payload.get("codex", {}).get("status") == "degraded",
                  "degraded", str(payload.get("codex")))

        # 11. -o write-branch coverage (mirrors test_doctor's -o branch): the
        # passing fake -> the file exists, parses as JSON, codex status pass;
        # stdout still carries the JSON per the existing contract.
        out_path_probe = d / "explicit-probe.json"
        proc = _run_cli(["probe", "codex", "-o", str(out_path_probe)],
                         env=env_pass, timeout=30)
        check("probe -o -> exit 0", proc.returncode == 0, "0",
              f"{proc.returncode}: {proc.stderr[:200]}")
        check("probe -o writes the explicit path", out_path_probe.is_file(),
              "explicit file", "present" if out_path_probe.is_file() else "missing")
        if out_path_probe.is_file():
            file_payload = json.loads(out_path_probe.read_text(encoding="utf-8"))
            check("probe -o file parses as JSON with codex status pass",
                  file_payload.get("codex", {}).get("status") == "pass",
                  "pass", str(file_payload.get("codex")))
        try:
            stdout_payload = json.loads(proc.stdout)
        except Exception as exc:
            stdout_payload = None
            check("probe -o stdout still parses as JSON", False, "valid json",
                  f"{exc}: {proc.stdout[:200]}")
        if stdout_payload is not None:
            check("probe -o stdout carries codex status pass",
                  stdout_payload.get("codex", {}).get("status") == "pass",
                  "pass", str(stdout_payload.get("codex")))

        # 8. Task seats (opus/sonnet/fable) named explicitly -> exit 2 with a
        # message saying Task seats are orchestrator-dispatched and cannot be
        # probed by the engine (mirrors cmd_run's Task-seat error branch).
        for task_seat in ("opus", "sonnet", "fable"):
            proc = _run_cli(["probe", task_seat], env=env_empty, timeout=30)
            check(f"probe {task_seat} (Task seat) -> exit 2 + 'Task seat'/'orchestrator' message",
                  proc.returncode == 2
                  and "Task seat" in proc.stderr
                  and "orchestrator" in proc.stderr
                  and task_seat in proc.stderr,
                  "exit2 + Task seat/orchestrator", f"{proc.returncode}: {proc.stderr!r}")

        # 9. --session-id "<session-id>" (literal unsubstituted placeholder) ->
        # exit 2 with the fake seat CLI NEVER invoked. Usage-error validation
        # (the placeholder check) must run BEFORE the billable per-seat loop,
        # mirroring cmd_run's session-id guard ahead of provider.run.
        marker = d / "invoked.marker"
        bins_marker = d / "bin-marker"; bins_marker.mkdir()
        make_fake_bin(bins_marker, "codex", f"""
        import sys
        from pathlib import Path
        Path({str(marker)!r}).write_text("invoked")
        a = sys.argv[1:]
        out = None
        for i, x in enumerate(a):
            if x == "-o":
                out = a[i + 1]
        sys.stdin.read()
        with open(out, "w") as f:
            f.write("PROBE-OK\\n")
        sys.exit(0)
        """)
        env_marker = isol_env(bins_marker)
        proc = _run_cli(
            ["probe", "codex", "--session-id", "<session-id>"],
            env=env_marker, timeout=30,
        )
        check("probe placeholder session-id -> exit 2",
              proc.returncode == 2, "2", str(proc.returncode))
        check("probe placeholder session-id -> error mentions placeholder",
              "placeholder" in proc.stderr, "placeholder", proc.stderr[:200])
        check("probe placeholder session-id -> fake seat CLI NOT executed",
              not marker.exists(), "marker absent", f"exists={marker.exists()}")


def test_scaffold_config():
    log_section("scaffold-config (template + no-clobber + conservative --repo overrides)")
    from contextlib import redirect_stdout, redirect_stderr  # noqa: E402
    from io import StringIO  # noqa: E402
    from multiagent import cli, config  # noqa: E402
    import tomllib as _toml  # noqa: E402  (tests run on 3.11+)

    def run(argv):
        args = cli.build_parser().parse_args(argv)
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = args.func(args)
        return rc, out.getvalue(), err.getvalue()

    def write_det(path, *, absent=None, present=None):
        sub = {}
        for s in (absent or []):
            sub[s] = {"available": False, "diag": f"{s} not found on PATH"}
        for s in (present or []):
            sub[s] = {"available": True, "diag": ""}
        path.write_text(json.dumps({
            "subprocess": sub,
            "task": {"opus": {"available": True, "diag": "x"},
                     "sonnet": {"available": True, "diag": "y"},
                     "fable": {"available": True, "diag": "z"}},
        }))

    # --- Task 1: public path wrappers equal the private ones ------------------
    with crew_config() as proj:
        check("config.repo_config_path() == _config_path()",
              config.repo_config_path() == config._config_path(),
              str(config._config_path()), str(config.repo_config_path()))
        check("config.global_config_path() == _global_config_path()",
              config.global_config_path() == config._global_config_path(),
              str(config._global_config_path()), str(config.global_config_path()))

    # --- fresh global template: --out - is PURE TOML, note on stderr ----------
    with crew_config() as proj:
        rc, out, err = run(["scaffold-config", "--out", "-",
                            "--default-panel", "lite", "--dispatch-seat", "agy"])
        check("scaffold --out - exits 0", rc == 0, "0", str(rc))
        parsed = None
        try:
            parsed = _toml.loads(out)
            toml_ok = True
        except Exception as exc:
            toml_ok = False
            check("scaffold --out - stdout is valid TOML", False, "valid TOML", f"{exc}: {out[:200]}")
        check("scaffold --out - stdout is PURE TOML (no JSON envelope)",
              toml_ok and "diverted" not in out, "no envelope", out[:120])
        check("bare (no --detection): 'detection skipped' note on STDERR not stdout",
              "detection skipped" in err and "detection skipped" not in out,
              "note on stderr only", f"err={err[:80]} out-has-note={'detection skipped' in out}")
        # The template emits every LOADER-READ key, and `[tuning].deadline_minutes`
        # (the persistence loops' wall clock, read by `crew state init`) is one: a
        # knob absent from the generated config is a knob nobody discovers.
        from models import DEFAULT_DEADLINE_MINUTES as _DDM, MAX_DEADLINE_MINUTES as _MDM
        check("scaffold [tuning] emits deadline_minutes at the enforced default",
              f"# deadline_minutes = {_DDM}" in out and f"1-{_MDM}" in out,
              f"commented `deadline_minutes = {_DDM}` + the 1-{_MDM} range",
              out[out.find("[tuning]"):][:200] if "[tuning]" in out else out[:120])
        if parsed is not None:
            seats_tbl = parsed.get("seats", {})
            check("bare (no --detection) OMITS every per-seat available line",
                  all("available" not in tbl for tbl in seats_tbl.values()),
                  "no available keys", str({k: v for k, v in seats_tbl.items() if 'available' in v}))
            check("fresh template carries a [seats.codex-luna] block (default seat)",
                  "codex-luna" in seats_tbl, "codex-luna table present", str(sorted(seats_tbl)))

        # round-trip through the real loader: getters read the values, ZERO warnings.
        config.global_config_path().write_text(out)
        config._reset_cache_for_tests()
        check("round-trip: default_panel() reads the baked value",
              config.default_panel() == "lite", "lite", str(config.default_panel()))
        check("round-trip: dispatch_seat() reads the baked value",
              config.dispatch_seat() == "agy", "agy", str(config.dispatch_seat()))
        check("round-trip: ZERO config warnings on the emitted template",
              not config._warned, "no warnings", str(config._warned))

    # --- detection present: detected-absent + premium -> available=false ------
    with crew_config() as proj:
        det = Path(proj) / "doctor.json"
        write_det(det, absent=["agy"],
                  present=["codex", "codex-luna", "codex-terra",
                           "cursor-auto", "cursor-composer",
                           "cursor-glm", "cursor-gpt", "cursor-gemini",
                           "cursor-grok"])
        rc, out, err = run(["scaffold-config", "--out", "-", "--detection", str(det),
                            "--disable-seat", "opus"])
        parsed = _toml.loads(out)
        seats_tbl = parsed.get("seats", {})
        check("detection: detected-absent agy -> available=false",
              seats_tbl.get("agy", {}).get("available") is False, "agy false", str(seats_tbl.get("agy")))
        check("detection: detected-present codex -> available OMITTED",
              "available" not in seats_tbl.get("codex", {}), "codex no available", str(seats_tbl.get("codex")))
        check("detection: detected-present codex-luna -> available OMITTED (default seat, not opt-in)",
              "available" not in seats_tbl.get("codex-luna", {}),
              "codex-luna no available", str(seats_tbl.get("codex-luna")))
        check("detection: premium cursor-glm -> available=false (cost-safe)",
              seats_tbl.get("cursor-glm", {}).get("available") is False,
              "glm false", str(seats_tbl.get("cursor-glm")))
        check("detection: premium cursor-grok -> available=false (cost-safe)",
              seats_tbl.get("cursor-grok", {}).get("available") is False,
              "grok false", str(seats_tbl.get("cursor-grok")))
        check("detection: opt-in codex-terra -> available=false (even when CLI present)",
              seats_tbl.get("codex-terra", {}).get("available") is False,
              "codex-terra false", str(seats_tbl.get("codex-terra")))
        check("detection: --disable-seat opus (TASK seat) -> [seats.opus].available=false",
              seats_tbl.get("opus", {}).get("available") is False, "opus false", str(seats_tbl.get("opus")))
        # the opus-disabled output loads clean via the real loader.
        from multiagent import seats as _seats  # noqa: E402
        config.global_config_path().write_text(out)
        config._reset_cache_for_tests()
        _opus_avail = _seats.seat_spec("opus").available
        check("detection: --disable-seat opus output loads + opus seat available False",
              _opus_avail is False and not config._warned,
              "opus unavailable, no warnings", f"{_opus_avail} warned={config._warned}")

    # --- input validation rejects bad values (nonzero + stderr, no file) ------
    with crew_config() as proj:
        target = Path(proj) / "v.toml"
        for argv, label in [
            (["scaffold-config", "--out", str(target), "--default-panel", "nope"], "bad --default-panel"),
            (["scaffold-config", "--out", str(target), "--disable-seat", "notaseat"], "bogus --disable-seat"),
            (["scaffold-config", "--out", str(target), "--add-seat", "notaseat"], "bogus --add-seat"),
            (["scaffold-config", "--out", str(target), "--disable-seat", "cursor"], "group token --disable-seat"),
            (["scaffold-config", "--out", str(target), "--dispatch-seat", "opus"], "task --dispatch-seat (subprocess-only)"),
        ]:
            rc, out, err = run(argv)
            check(f"reject {label}: nonzero + stderr + no file",
                  rc != 0 and err.strip() and not target.exists(),
                  "nonzero, stderr, no file", f"rc={rc} err={err[:60]} exists={target.exists()}")

    # --- fail-fast: GIVEN-but-bad --detection (distinct from absent-flag) ------
    with crew_config() as proj:
        target = Path(proj) / "ff.toml"
        missing = Path(proj) / "nope.json"
        empty = Path(proj) / "empty.json"; empty.write_text("")
        malformed = Path(proj) / "bad.json"; malformed.write_text("{not json")
        for det_path, label in [(missing, "missing"), (empty, "empty"), (malformed, "malformed")]:
            rc, out, err = run(["scaffold-config", "--out", str(target), "--detection", str(det_path)])
            check(f"--detection {label} file -> nonzero + stderr, no file (NOT the omit path)",
                  rc != 0 and "error" in err.lower() and not target.exists()
                  and "detection skipped" not in err,
                  "fail-fast", f"rc={rc} err={err[:80]} exists={target.exists()}")

    # --- fail-fast: UNPARSEABLE global on --repo ------------------------------
    with crew_config(glob="this = = not [[[ toml\n") as proj:
        target = Path(proj) / ".crew" / "config.toml"
        rc, out, err = run(["scaffold-config", "--repo", "--out", str(target)])
        check("--repo with UNPARSEABLE global -> nonzero + stderr, nothing written",
              rc != 0 and err.strip() and not target.exists(),
              "fail-fast", f"rc={rc} exists={target.exists()}")

    # --- no-clobber 4 paths ---------------------------------------------------
    with crew_config() as proj:
        target = Path(proj) / "cfg.toml"
        rc, out, err = run(["scaffold-config", "--out", str(target), "--default-panel", "full"])
        env_json = json.loads(out)
        check("no-clobber: absent target -> wrote target, diverted false",
              env_json["diverted"] is False and env_json["wrote"] == str(target) and target.is_file(),
              "diverted false", str(env_json))
        live_before = target.read_text()
        rc, out, err = run(["scaffold-config", "--out", str(target), "--default-panel", "full"])
        env_json = json.loads(out)
        newp = Path(str(target) + ".new")
        check("no-clobber: present target -> wrote <target>.new, diverted true",
              env_json["diverted"] is True and env_json["wrote"] == str(newp) and newp.is_file(),
              "diverted true + .new", str(env_json))
        check("no-clobber: live target untouched while diverted",
              target.read_text() == live_before, "unchanged", "changed")
        rc, out, err = run(["scaffold-config", "--out", str(target), "--default-panel", "lite"])
        check("no-clobber: existing <target>.new OVERWRITTEN + stderr note",
              "replaced an existing" in err and 'default_panel = "lite"' in newp.read_text(),
              "overwrite + note", f"err={err[:80]}")
        check("no-clobber: stdout stays the clean envelope (note on stderr)",
              "diverted" in out and "replaced an existing" not in out,
              "clean stdout", out[:120])
        rc, out, err = run(["scaffold-config", "--out", str(target), "--force", "--default-panel", "lite"])
        env_json = json.loads(out)
        check("no-clobber: --force overwrites target directly (diverted false)",
              env_json["diverted"] is False and 'default_panel = "lite"' in target.read_text(),
              "force overwrite", str(env_json))

    # --- D3 conservative override table ---------------------------------------
    # multi-[seats.X] section scoping: edit codex.available, leave agy.model alone.
    multi_global = (
        '# tuned global\n'
        'default_panel = "lite"\n\n'
        '[seats.codex]\n'
        'model = "gpt-x"\n\n'
        '[seats.agy]\n'
        'model = "Gemini 3.1 Pro (High)"\n'
    )
    with crew_config(glob=multi_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-",
                            "--default-panel", "full", "--dispatch-seat", "codex",
                            "--disable-seat", "codex"])
        parsed = _toml.loads(out)
        check("D3 case1: default_panel replaced in place (lite -> full)",
              parsed.get("default_panel") == "full", "full", str(parsed.get("default_panel")))
        check("D3 case2: codex.available inserted into existing [seats.codex] header",
              parsed["seats"]["codex"].get("available") is False
              and parsed["seats"]["codex"].get("model") == "gpt-x",
              "codex false + model kept", str(parsed["seats"].get("codex")))
        check("D3 section-scoping: [seats.agy].model UNTOUCHED (not hit by codex edit)",
              parsed["seats"]["agy"].get("model") == "Gemini 3.1 Pro (High)"
              and "available" not in parsed["seats"]["agy"],
              "agy model intact, no available", str(parsed["seats"].get("agy")))
        check("D3: comment '# tuned global' preserved verbatim",
              "# tuned global" in out, "comment present", "missing")
        check("D3 case3: [dispatch] appended (absent in base) -> seat lands",
              parsed.get("dispatch", {}).get("seat") == "codex", "codex", str(parsed.get("dispatch")))

    # absent-key-in-existing-header (--add-seat lands available=true) +
    # absent-in-every-form (--add-seat cursor-composer appends a new table).
    glm_global = (
        'default_panel = "full"\n\n'
        '[seats.cursor-glm]\n'
        'model = "glm-5.2-max"\n'
    )
    with crew_config(glob=glm_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-",
                            "--add-seat", "cursor-glm", "--add-seat", "cursor-composer"])
        parsed = _toml.loads(out)
        check("D3 case2: --add-seat cursor-glm inserts available=true in existing header",
              parsed["seats"]["cursor-glm"].get("available") is True
              and parsed["seats"]["cursor-glm"].get("model") == "glm-5.2-max",
              "glm true + model kept", str(parsed["seats"].get("cursor-glm")))
        check("D3 case3: --add-seat cursor-composer (absent every form) appends a new table",
              parsed["seats"].get("cursor-composer", {}).get("available") is True,
              "composer true", str(parsed["seats"].get("cursor-composer")))
        check("D3 case3: no duplicate [seats.cursor-glm] header (single definition)",
              out.count("[seats.cursor-glm]") == 1, "1 header", str(out.count("[seats.cursor-glm]")))

    # absent top-level default_panel -> inserted after leading comment block.
    nopanel_global = "# header comment\n# more\n\n[dispatch]\nseat = \"codex\"\n"
    with crew_config(glob=nopanel_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--default-panel", "lite"])
        parsed = _toml.loads(out)
        lines = out.split("\n")
        dp_idx = next(i for i, l in enumerate(lines) if l.startswith("default_panel"))
        disp_idx = next(i for i, l in enumerate(lines) if l.strip() == "[dispatch]")
        check("D3 case4: absent default_panel inserted after comments, before first header",
              parsed.get("default_panel") == "lite" and dp_idx < disp_idx
              and lines[0].startswith("#"),
              "inserted in position", f"dp@{dp_idx} disp@{disp_idx}")

    # Case-4 position-0 edge: base starts DIRECTLY with a header, no leading comment.
    pos0_global = "[seats.codex]\nmodel = \"x\"\n"
    with crew_config(glob=pos0_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--default-panel", "lite"])
        parsed = _toml.loads(out)
        check("D3 case4 edge: no leading comment -> default_panel inserted at position 0",
              parsed.get("default_panel") == "lite"
              and out.split("\n")[0].startswith("default_panel"),
              "position 0", out.split("\n")[0])

    # multiline-string base -> whole-seed pre-gate leaves it VERBATIM (byte-identical).
    multiline_global = 'default_panel = "lite"\n\n[seats.codex]\nmodel = """multi\nline"""\n'
    with crew_config(glob=multiline_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-",
                            "--default-panel", "full", "--disable-seat", "codex"])
        check("D3 pre-gate: multiline-string base left VERBATIM (no line edits)",
              out.rstrip("\n") == multiline_global.rstrip("\n")
              and "could not safely apply override" in err,
              "verbatim + note", f"changed={out.rstrip()!=multiline_global.rstrip()}")
        # still loads (it was loadable to begin with).
        _toml.loads(out)

    # inline-table base -> leave codex verbatim + note, NO duplicate, still loads.
    inline_global = '[seats]\ncodex = { reasoning_effort = "xhigh" }\n'
    with crew_config(glob=inline_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--disable-seat", "codex"])
        check("D3 case5: inline-table codex -> stderr note, no duplicate [seats.codex], loads",
              "could not safely apply override 'codex'" in err
              and out.count("[seats.codex]") == 0,
              "leave + note, no dup", f"err={err[:60]} dup={out.count('[seats.codex]')}")
        loaded = _toml.loads(out)  # must NOT raise TOMLDecodeError
        check("D3 case5: inline-table output keeps codex inline (override NOT applied)",
              loaded["seats"]["codex"] == {"reasoning_effort": "xhigh"},
              "codex inline intact", str(loaded["seats"].get("codex")))

    # dotted-key base -> leave + note, still loads, no duplicate.
    dotted_global = 'seats.agy.print_timeout = "30s"\n'
    with crew_config(glob=dotted_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--disable-seat", "agy"])
        check("D3 case5: dotted-key agy -> stderr note + loads (no duplicate)",
              "could not safely apply override 'agy'" in err,
              "leave + note", f"err={err[:60]}")
        _toml.loads(out)  # must NOT raise

    # commented-out dotted key must NOT block a real correction (case-3 scan skips comments).
    commented_global = (
        '# seats.cursor-glm.available = false\n'
        'default_panel = "full"\n'
    )
    with crew_config(glob=commented_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--disable-seat", "cursor-glm"])
        parsed = _toml.loads(out)
        check("D3: commented-out dotted key does NOT trigger false Case-5 (correction lands)",
              parsed["seats"]["cursor-glm"].get("available") is False
              and "could not safely apply override 'cursor-glm'" not in err,
              "appended, no false note", f"err={err[:60]}")

    # idempotency: two runs, identical inputs + unchanged global -> byte-identical.
    with crew_config(glob=multi_global) as proj:
        argv = ["scaffold-config", "--repo", "--out", "-", "--default-panel", "full",
                "--dispatch-seat", "codex", "--disable-seat", "codex"]
        _, out1, _ = run(argv)
        _, out2, _ = run(argv)
        check("D3 idempotency: identical inputs -> byte-identical output",
              out1 == out2, "byte-identical", "differs")

    # --- header-recognition: commented / quoted / whitespace headers ----------
    # A loader probe through the REAL config code path (not just _toml.loads).
    def loads_clean(text):
        """True iff `text` parses via the real loader's _load_uncached with NO
        TOMLDecodeError surfaced (config._load_uncached swallows malformed into
        {} + a warning, so probe tomllib directly for the hard assert too)."""
        config._reset_cache_for_tests()
        config.global_config_path().write_text(text)
        loaded = config._load_uncached(config.global_config_path())
        _toml.loads(text)  # hard assert: must NOT raise TOMLDecodeError
        return loaded

    # codex's exact repro: trailing inline comment on the header.
    commented_header_global = (
        'default_panel = "full"\n\n'
        '[seats.codex] # my note\n'
        'model = "gpt-x"\n'
    )
    with crew_config(glob=commented_header_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--disable-seat", "codex"])
        check("header-recog: '[seats.codex] # note' -> NO duplicate [seats.codex]",
              out.count("[seats.codex]") == 1, "1 header", str(out.count("[seats.codex]")))
        loaded = loads_clean(out)
        check("header-recog: commented-header output LOADS (no TOMLDecodeError)",
              loaded.get("seats", {}).get("codex", {}).get("available") is False
              and loaded["seats"]["codex"].get("model") == "gpt-x",
              "in-place disable + model kept", str(loaded.get("seats", {}).get("codex")))

    # quoted dotted component: [seats."cursor-glm"].
    quoted_header_global = (
        'default_panel = "full"\n\n'
        '[seats."cursor-glm"]\n'
        'model = "glm-5.2-max"\n'
    )
    with crew_config(glob=quoted_header_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--add-seat", "cursor-glm"])
        check("header-recog: '[seats.\"cursor-glm\"]' recognized -> no duplicate header",
              out.count("[seats.") == 1, "1 seats header", str(out.count("[seats.")))
        loaded = loads_clean(out)
        check("header-recog: quoted-header output LOADS + available inserted in place",
              loaded["seats"]["cursor-glm"].get("available") is True
              and loaded["seats"]["cursor-glm"].get("model") == "glm-5.2-max",
              "glm true + model kept", str(loaded["seats"].get("cursor-glm")))

    # internal-whitespace header: [ seats.agy ].
    ws_header_global = (
        'default_panel = "full"\n\n'
        '[ seats.agy ]\n'
        'model = "Gemini 3.1 Pro (High)"\n'
    )
    with crew_config(glob=ws_header_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--disable-seat", "agy"])
        check("header-recog: '[ seats.agy ]' internal-whitespace header recognized",
              out.count("seats.agy") == 1, "1 occurrence", str(out.count("seats.agy")))
        loaded = loads_clean(out)
        check("header-recog: whitespace-header output LOADS + disable applied in place",
              loaded["seats"]["agy"].get("available") is False
              and loaded["seats"]["agy"].get("model") == "Gemini 3.1 Pro (High)",
              "agy false + model kept", str(loaded["seats"].get("agy")))

    # --- parse-guard BACKSTOP: an edit that would break the base is discarded --
    # Monkeypatch the recognizer to MISS a header (simulating any form the regex
    # still slips) so a disable would APPEND a duplicate [seats.codex] -> invalid.
    # The backstop must catch it and hand back the VERBATIM base + a note.
    guard_global = (
        'default_panel = "full"\n\n'
        '[seats.codex]\n'
        'model = "gpt-x"\n'
    )
    with crew_config(glob=guard_global) as proj:
        orig_header_name = cli._header_name
        cli._header_name = lambda line: None  # blind the recognizer entirely
        try:
            rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--disable-seat", "codex"])
        finally:
            cli._header_name = orig_header_name
        check("parse-guard: blinded recognizer would dup -> output FALLS BACK to verbatim base",
              out.rstrip("\n") == guard_global.rstrip("\n")
              and "left it unchanged" in err,
              "verbatim + note", f"changed={out.rstrip()!=guard_global.rstrip()} err={err[:60]}")
        loaded = loads_clean(out)  # the verbatim fallback ALWAYS loads
        check("parse-guard: fallback output LOADS (no TOMLDecodeError)",
              "codex" in loaded.get("seats", {}), "loads", str(loaded.get("seats")))

    # --- MINOR: contradictory --add-seat X --disable-seat X -> validation error
    with crew_config() as proj:
        target = Path(proj) / "c.toml"
        rc, out, err = run(["scaffold-config", "--out", str(target),
                            "--add-seat", "codex", "--disable-seat", "codex"])
        check("contradictory --add-seat/--disable-seat codex -> nonzero + stderr, no file",
              rc != 0 and "codex" in err and not target.exists(),
              "nonzero, stderr, no file", f"rc={rc} err={err[:60]} exists={target.exists()}")

    # --- MINOR: --repo + existing global + NO --detection -> skipped note ------
    with crew_config(glob=multi_global) as proj:
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--default-panel", "full"])
        check("--repo + existing global + no --detection: 'detection skipped' note on stderr",
              "detection skipped" in err and "detection skipped" not in out,
              "note on stderr", f"err={err[:80]}")
        check("--repo + existing global + no --detection: NO 'available' lines auto-emitted",
              "available" not in out, "no available", out[:120])

    # --- MINOR: --repo + global + VALID --detection -> parsed, NOT auto-applied
    with crew_config(glob=multi_global) as proj:
        det = Path(proj) / "doctor.json"
        write_det(det, absent=["agy"], present=["codex"])
        rc, out, err = run(["scaffold-config", "--repo", "--out", "-", "--detection", str(det),
                            "--disable-seat", "codex"])
        parsed = _toml.loads(out)  # loads clean
        check("--repo + global + valid --detection: detected-absent agy NOT auto-applied",
              "available" not in parsed.get("seats", {}).get("agy", {"available": "x"}),
              "agy untouched by detection", str(parsed.get("seats", {}).get("agy")))
        check("--repo + global + valid --detection: only explicit --disable-seat codex lands",
              parsed["seats"]["codex"].get("available") is False,
              "codex false (explicit)", str(parsed["seats"].get("codex")))
        check("--repo + global + valid --detection: no 'detection skipped' note (flag present)",
              "detection skipped" not in err, "no skip note", err[:80])


# =============================================================================
# findings.py — parser, grouping predicate, digest renderer (Tasks 3/4/8)
# =============================================================================

def _fixture_result(kind: str, seat: str) -> ProviderResult:
    """Load a committed review-panel fixture seat as a ProviderResult."""
    d = json.loads((FIXTURES_DIR / kind / f"{seat}.json").read_text(encoding="utf-8"))
    return ProviderResult.from_dict(d)


def _mk(sev, path, line, desc, seat="s", text=None):
    return findings.Finding(severity=sev, raw_path=path, line=line,
                            description=desc, seat=seat,
                            text=text if text is not None else desc)


def test_findings_parser():
    log_section("findings.parse_seat (structured parser, pure, never raises)")

    # COMPLIANT block [compliant fixture] -> exact findings/criteria/verdict +
    # findings_parsed=True.
    codex = _fixture_result("compliant", "codex")
    p = findings.parse_seat(codex)
    check("compliant codex: findings_parsed=True, verdict APPROVED, confidence",
          p.findings_parsed and p.verdict == "APPROVED" and p.confidence == "medium",
          "parsed + APPROVED + medium",
          f"parsed={p.findings_parsed} v={p.verdict} c={p.confidence}")
    check("compliant codex: 4 criteria parsed (Correctness..Safety all PASS)",
          p.criteria.get("Correctness") == "PASS" and len(p.criteria) == 4,
          "4 criteria PASS", str(p.criteria))
    check("compliant codex: 1 finding, MINOR, line 250, path extracted",
          len(p.findings) == 1 and p.findings[0].severity == "MINOR"
          and p.findings[0].line == 250 and p.findings[0].raw_path is not None,
          "1 MINOR :250 with path", str(p.findings))

    # EMPTY ## FINDINGS section -> findings_parsed=True, zero findings.
    empty = _fixture_result("compliant", "empty")
    pe = findings.parse_seat(empty)
    check("empty-FINDINGS seat: findings_parsed=True, zero findings (counts in N)",
          pe.findings_parsed and pe.findings == [],
          "parsed, no findings", f"parsed={pe.findings_parsed} n={len(pe.findings)}")

    # PARTIAL compliance: VERDICT+CRITERIA headers, PROSE findings ->
    # findings_parsed=False, but verdict/criteria still extracted (metadata additive).
    partial = _fixture_result("compliant", "partial")
    pp = findings.parse_seat(partial)
    check("partial seat: findings_parsed=False (prose findings) — BLOCKING-1",
          pp.findings_parsed is False, "not parsed", str(pp.findings_parsed))
    check("partial seat: verdict + criteria STILL extracted (metadata additive)",
          pp.verdict == "REVISE" and pp.criteria.get("Correctness") == "FAIL",
          "REVISE + Correctness FAIL", f"v={pp.verdict} c={pp.criteria}")

    # REAL-PROSE block [real-prose fixture] -> findings_parsed=False, no metadata.
    rcodex = _fixture_result("real-prose", "codex")
    pr = findings.parse_seat(rcodex)
    check("real-prose codex: findings_parsed=False, NO metadata (no ## headers)",
          pr.findings_parsed is False and pr.verdict is None
          and pr.confidence is None and pr.criteria == {},
          "not parsed, no metadata",
          f"parsed={pr.findings_parsed} v={pr.verdict} c={pr.criteria}")
    # never raises on a failed/empty seat.
    ragy = _fixture_result("real-prose", "agy")
    pa = findings.parse_seat(ragy)
    check("real-prose agy (empty/failed): not parsed, no findings, no raise",
          pa.findings_parsed is False and pa.findings == [],
          "graceful", str(pa))

    # CRITERIA parse rule across formats (bullet / bold / table cell).
    fmt = ProviderResult(name="x", model=None, ok=True, error=None, elapsed=0.0,
        output="## CRITERIA\n- Correctness: FAIL — bad\n"
               "**Completeness:** PASS\n"
               "| **Quality** | **PASS** |\n"
               "## FINDINGS\nnone\n")
    pf = findings.parse_seat(fmt)
    check("CRITERIA parse: bullet `-`, bold `**:**`, AND table-cell `| ** |`",
          pf.criteria.get("Correctness") == "FAIL"
          and pf.criteria.get("Completeness") == "PASS"
          and pf.criteria.get("Quality") == "PASS",
          "all three formats parse", str(pf.criteria))

    # VERDICT parse: an ANCHORED own-line verdict beats an incidental mention in
    # surrounding prose — "not APPROVED" on an earlier line must not win over a
    # compliant own-line REVISE.
    vmix = ProviderResult(name="x", model=None, ok=True, error=None, elapsed=0.0,
        output="## VERDICT\nWe are not APPROVED here.\nREVISE\n\n## FINDINGS\nnone\n")
    check("VERDICT parse: own-line REVISE beats earlier prose 'not APPROVED'",
          findings.parse_seat(vmix).verdict == "REVISE",
          "REVISE", str(findings.parse_seat(vmix).verdict))
    vbold = ProviderResult(name="x", model=None, ok=True, error=None, elapsed=0.0,
        output="## VERDICT\n**APPROVED**\n\n## FINDINGS\nnone\n")
    check("VERDICT parse: bolded own-line **APPROVED** matches the anchored form",
          findings.parse_seat(vbold).verdict == "APPROVED",
          "APPROVED", str(findings.parse_seat(vbold).verdict))
    vloose = ProviderResult(name="x", model=None, ok=True, error=None, elapsed=0.0,
        output="## VERDICT\n**Verdict: REVISE** — two blockers.\n\n## FINDINGS\nnone\n")
    check("VERDICT parse: loose fallback still reads `**Verdict: REVISE**` prose",
          findings.parse_seat(vloose).verdict == "REVISE",
          "REVISE", str(findings.parse_seat(vloose).verdict))

    # Two-step path/line extraction — all forms incl. the {1,8}-ext bare-path.
    def one_finding(line):
        r = ProviderResult(name="x", model=None, ok=True, error=None, elapsed=0.0,
            output=f"## FINDINGS\n{line}\n")
        fs = findings.parse_seat(r).findings
        return fs[0] if fs else None
    a = one_finding("- [MINOR] [the label](src/app.py:42) — link form")
    check("path form (a) markdown-link [label](path:line)",
          a and a.raw_path == "src/app.py" and a.line == 42, "src/app.py:42",
          str(a))
    b = one_finding("- [BLOCKING] pkg/mod.py:17 — bare path:line")
    check("path form (b) bare path:line (path has '.'/'/', line digits)",
          b and b.raw_path == "pkg/mod.py" and b.line == 17, "pkg/mod.py:17",
          str(b))
    c = one_finding("- [MINOR] build.gradle.kts — bare path, {1,8}-ext, no line")
    check("path form (c) bare path w/ known ext, no line",
          c and c.raw_path == "build.gradle.kts" and c.line is None,
          "build.gradle.kts, None", str(c))
    d = one_finding("- [MINOR] (no file) — terse drops a skill")
    check("path form (d) (no file) -> raw_path None",
          d and d.raw_path is None and d.line is None, "None path",
          str(d))

    # path form (c2): a bare, well-known EXTENSIONLESS filename keeps a path (so it
    # groups path-aware) instead of parsing as a no-file finding.
    df = one_finding("- [BLOCKING] Dockerfile — runs as root, no USER directive")
    check("path form (c2) bare extensionless Dockerfile keeps a path",
          df and df.raw_path == "Dockerfile" and df.line is None
          and df.description == "runs as root, no USER directive",
          "Dockerfile path-aware", str(df))
    mk = one_finding("- [MINOR] Makefile:12 — clean target missing")
    check("path form (c2) Makefile:line honors the trailing line",
          mk and mk.raw_path == "Makefile" and mk.line == 12,
          "Makefile:12", str(mk))
    # a normal capitalized prose word must NOT be mistaken for a filename.
    nf = one_finding("- [MINOR] Correctness could be better here")
    check("path form (c2) does NOT misfire on ordinary Capitalized prose",
          nf and nf.raw_path is None, "no false path", str(nf))

    # path:line:COL -> the column does NOT leak into the description.
    e = one_finding("- [BLOCKING] cli.py:1696:12 — col must not leak into desc")
    check("path form (b'): path:line:col strips :col (no column leak in desc)",
          e and e.raw_path == "cli.py" and e.line == 1696
          and e.description == "col must not leak into desc"
          and "12" not in e.description, "cli.py:1696, clean desc", str(e))

    # BUG-CLASS: a `### Sub-section` INSIDE the ## FINDINGS body must NOT terminate
    # the section — tagged lines below the sub-heading are still parsed/grouped.
    sub_hdr = ProviderResult(name="x", model=None, ok=True, error=None, elapsed=0.0,
        output="## FINDINGS\n"
               "- [BLOCKING] a.py:1 — above the sub-heading\n"
               "### Details\n"
               "- [MINOR] b.py:2 — BELOW the sub-heading, must still parse\n"
               "## CONFIDENCE\nhigh\n")
    ph = findings.parse_seat(sub_hdr)
    check("BUG-CLASS ### inside FINDINGS: deeper sub-heading does NOT end section",
          ph.findings_parsed is True and len(ph.findings) == 2
          and {f.raw_path for f in ph.findings} == {"a.py", "b.py"}
          and ph.confidence == "high",
          "both findings parsed across the ### sub-heading",
          f"parsed={ph.findings_parsed} n={len(ph.findings)} c={ph.confidence}")

    # BUG-CLASS: MIXED tagged + untagged-prose FINDINGS -> nothing lost. The
    # untagged prose finding can't be silently dropped, so the seat is marked NOT
    # findings-parsed (its whole body surfaces raw — nothing vanishes).
    mixed = ProviderResult(name="x", model=None, ok=True, error=None, elapsed=0.0,
        output="## FINDINGS\n"
               "- [BLOCKING] a.py:1 — a tagged finding\n"
               "the error handling in bar.py is also weak and untagged\n"
               "## CONFIDENCE\nlow\n")
    pm = findings.parse_seat(mixed)
    check("BUG-CLASS mixed tagged+prose: marked NOT parsed so prose is not dropped",
          pm.findings_parsed is False,
          "mixed -> not parsed (surfaces raw)", str(pm.findings_parsed))

    # CRITERIA name with a hyphen (`Error-handling`) parses.
    crit = ProviderResult(name="x", model=None, ok=True, error=None, elapsed=0.0,
        output="## CRITERIA\n- Error-handling: FAIL — needs a guard\n"
               "## FINDINGS\nnone\n")
    pc = findings.parse_seat(crit)
    check("CRITERIA name with hyphen: `Error-handling: FAIL` parses",
          pc.criteria.get("Error-handling") == "FAIL",
          "Error-handling FAIL", str(pc.criteria))


def test_findings_grouping():
    log_section("findings grouping predicate + complete-linkage clustering")

    # merge invariant: same (path,severity)+line, SIMILAR -> one 2/2; DIVERGENT -> two.
    f1 = _mk("MINOR", "session-start.py", 250, "lsp loaded emitted before hook loads lsp prefer required", "a")
    f2 = _mk("MINOR", "session-start.py", 252, "lsp loaded emitted before hook loads lsp prefer required wording", "b")
    g = findings.group([f1, f2])
    check("merge invariant: same file+sev+line, SIMILAR desc -> one group (2 seats)",
          len(g) == 1 and g[0].count == 2, "1 group of 2", str([gr.count for gr in g]))
    f3 = _mk("MINOR", "session-start.py", 250, "toolsearch select undocumented host mismatch risk", "b")
    g2 = findings.group([f1, f3])
    check("merge invariant: same file+sev, DIVERGENT desc (Jaccard<0.5) -> two groups",
          len(g2) == 2, "2 groups", str([gr.count for gr in g2]))

    # complete-linkage: A~B, B~C, A~C<0.5 -> A and C NOT collapsed into one 3/3.
    A = _mk("MINOR", "f.py", 10, "alpha beta gamma delta", "a")
    B = _mk("MINOR", "f.py", 10, "alpha beta gamma delta epsilon zeta eta theta", "b")
    C = _mk("MINOR", "f.py", 10, "epsilon zeta eta theta", "c")
    check("desc Jaccard wiring: A~B>=0.5, B~C>=0.5, A~C<0.5",
          findings.jaccard(A.description, B.description) >= 0.5
          and findings.jaccard(B.description, C.description) >= 0.5
          and findings.jaccard(A.description, C.description) < 0.5,
          "chain thresholds hold",
          f"AB={findings.jaccard(A.description,B.description):.2f} "
          f"BC={findings.jaccard(B.description,C.description):.2f} "
          f"AC={findings.jaccard(A.description,C.description):.2f}")
    g3 = findings.group([A, B, C])
    check("complete-linkage: transitive chain A~B~C, A≁C -> NOT one 3/3 group",
          not any(gr.count == 3 for gr in g3), "no 3-member cluster",
          str([gr.count for gr in g3]))

    # path-suffix: a/config.py vs b/config.py (similar desc) -> NOT merged.
    pa = _mk("MINOR", "a/config.py", 5, "config value wrong should be fixed here", "a")
    pb = _mk("MINOR", "b/config.py", 5, "config value wrong should be fixed here", "b")
    check("path-suffix: a/config.py vs b/config.py (similar desc) -> distinct groups",
          len(findings.group([pa, pb])) == 2, "2 groups", "?")
    # config.py vs dir/config.py -> MAY merge (suffix); abs vs rel same file -> merge.
    rel = _mk("MINOR", "config.py", 5, "config value wrong should be fixed here", "a")
    sub = _mk("MINOR", "dir/config.py", 5, "config value wrong should be fixed here", "b")
    check("path-suffix: config.py vs dir/config.py (suffix) -> one group",
          len(findings.group([rel, sub])) == 1, "1 group", "?")
    absf = _mk("MINOR", "/Volumes/x/repo/config.py", 5, "config value wrong should be fixed here", "a")
    check("path-suffix: abs vs repo-relative SAME file -> merge",
          findings.path_compatible(findings.normalize_path(absf.raw_path, "/none"),
                                   findings.normalize_path(rel.raw_path, "/none")),
          "abs ~ rel", "?")

    # line-window (MINOR): 14/15 merge; 14/30 don't; None vs concrete merges.
    l1 = _mk("MINOR", "f.py", 14, "same shared description for the window test here", "a")
    l2 = _mk("MINOR", "f.py", 15, "same shared description for the window test here", "b")
    l3 = _mk("MINOR", "f.py", 30, "same shared description for the window test here", "c")
    l0 = _mk("MINOR", "f.py", None, "same shared description for the window test here", "d")
    check("line-window: 14 & 15 (|Δ|=1) -> merge",
          len(findings.group([l1, l2])) == 1, "1 group", "?")
    check("line-window: 14 & 30 (|Δ|=16>10) -> do NOT merge",
          len(findings.group([l1, l3])) == 2, "2 groups", "?")
    check("line-window: None-line vs concrete (wildcard) -> merge",
          len(findings.group([l1, l0])) == 1, "1 group", "?")

    # cross-severity NEVER merges (severity is the only hard partition).
    sb = _mk("BLOCKING", "f.py", 10, "identical wording across both severities here now", "a")
    sm = _mk("MINOR", "f.py", 10, "identical wording across both severities here now", "b")
    gs = findings.group([sb, sm])
    check("severity HARD partition: same file+line+desc, diff severity -> 2 groups",
          len(gs) == 2, "2 groups", str([(gr.severity, gr.count) for gr in gs]))

    # representative text = the longest complete line in the cluster.
    short = _mk("MINOR", "f.py", 10, "shared issue paraphrase one here", "a", text="- short repr")
    longr = _mk("MINOR", "f.py", 10, "shared issue paraphrase one here too", "b",
                text="- a much much much longer representative line wins")
    gr = findings.group([short, longr])
    check("representative = longest complete line in the cluster",
          len(gr) == 1 and gr[0].representative == "- a much much much longer representative line wins",
          "longest line", gr[0].representative if gr else "?")


def test_collect_grouped():
    log_section("crew collect --group / --full / --report-unparsed + repair-seat")

    def stage(td, seats, kind="compliant"):
        rd = Path(td) / ".crew" / "reviews" / "sess"
        rd.mkdir(parents=True, exist_ok=True)
        for s in seats:
            src = FIXTURES_DIR / kind / f"{s}.json"
            (rd / f"{s}.json").write_text(src.read_text(encoding="utf-8"),
                                          encoding="utf-8")
        return rd

    six = ["codex", "agy", "cursor-auto", "cursor-composer", "opus", "sonnet"]

    # --group + --full: writes both; --full byte-equals render_panel; grouped smaller.
    with tempfile.TemporaryDirectory() as td:
        stage(td, six)
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", ",".join(six),
             "--group", "-o", ".crew/reviews/sess/panel.md",
             "--full", ".crew/reviews/sess/panel-full.md"], cwd=td, timeout=30)
        grouped = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        full = (Path(td) / ".crew/reviews/sess/panel-full.md").read_text()
        results = [_fixture_result("compliant", s) for s in six]
        expect_full = render.render_panel(results) + "\n"
        check("collect --full byte-equals render_panel + '\\n'",
              proc.returncode == 0 and full == expect_full,
              "byte-faithful sibling", f"rc={proc.returncode}")
        check("collect --group digest is materially smaller than --full",
              len(grouped) < len(full), f"{len(grouped)} < {len(full)}",
              f"grouped={len(grouped)} full={len(full)}")
        import re as _re
        check("collect --group --full: stderr reports the digest-vs-full size stat",
              _re.search(r"digest \d+B vs full \d+B \(\d+%\)", proc.stderr) is not None,
              "digest NB vs full MB (K%) on stderr", proc.stderr)
        check("collect --group --full: stdout is STILL only the -o path",
              proc.stdout == ".crew/reviews/sess/panel.md\n",
              "path-only stdout", repr(proc.stdout))
        check("grouped digest: shared finding present ONCE as 6/6 with all seats",
              grouped.count("6/6 [MINOR]") == 1
              and "codex, agy, cursor-auto, cursor-composer, opus, sonnet" in grouped,
              "one 6/6 row", grouped[:80])
        check("grouped digest: a Task-seat-only finding surfaces as ⚠ SINGLETON",
              "⚠ SINGLETON 1/6 [BLOCKING]" in grouped and "(opus)" in grouped,
              "opus BLOCKING singleton", "?")
        check("grouped digest: VERDICTS roster + CRITERIA MATRIX + GROUPED FINDINGS",
              "## VERDICTS" in grouped and "## CRITERIA MATRIX" in grouped
              and "## GROUPED FINDINGS" in grouped, "all sections", "?")
        # header says "seats:" (the NAMED list, skipped included) — not "seats
        # ran:"; the roster below is what distinguishes ran from skipped.
        check("grouped digest: header labels the list 'seats:' (not 'seats ran:')",
              grouped.startswith("# PANEL DIGEST — seats: ")
              and "seats ran:" not in grouped,
              "'seats:' header", grouped[:80])

    # denominator vs roster: 6 ran, one partial -> header "6 ran, 5 findings-parsed",
    # partial in roster + RAW, groups /5, excluded from denominator.
    with tempfile.TemporaryDirectory() as td:
        seats = ["codex", "agy", "cursor-auto", "cursor-composer", "opus", "partial"]
        stage(td, seats)
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", ",".join(seats),
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        check("denominator vs roster: header shows '6 ran, 5 findings-parsed'",
              "(6 ran, 5 findings-parsed)" in dig, "6 ran 5 parsed", dig.split(chr(10))[0])
        check("partial seat: in VERDICTS roster (verdict REVISE counted)",
              "- partial  REVISE" in dig, "partial REVISE in roster", "?")
        check("partial seat: prose body rendered VERBATIM under RAW (no finding dropped)",
              "## RAW / UNPARSED SEATS" in dig and "seat: partial" in dig
              and "line window also looks" in dig, "partial RAW verbatim", "?")
        check("finding denominator excludes the partial seat (groups are /5 not /6)",
              "/5 [MINOR]" in dig and "/6 [MINOR]" not in dig,
              "M/5 not M/6", "?")

    # zero-compliant [real-prose fixture]: grouped == render_panel + "\n".
    with tempfile.TemporaryDirectory() as td:
        rseats = ["codex", "agy"]
        stage(td, rseats, kind="real-prose")
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", ",".join(rseats),
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        results = [_fixture_result("real-prose", s) for s in rseats]
        check("zero-compliant (real-prose): grouped panel.md == render_panel + '\\n'",
              dig == render.render_panel(results) + "\n",
              "byte-faithful fallback", dig[:80])

    # Claude-only branch: --seats opus,sonnet -> valid 2-seat digest.
    with tempfile.TemporaryDirectory() as td:
        stage(td, ["opus", "sonnet"])
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "opus,sonnet",
             "--group", "-o", ".crew/reviews/sess/panel.md",
             "--full", ".crew/reviews/sess/panel-full.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        check("Claude-only branch: --seats opus,sonnet -> valid 2-seat grouped digest",
              proc.returncode == 0 and "(2 ran, 2 findings-parsed)" in dig
              and "## GROUPED FINDINGS" in dig, "2-seat digest", dig.split(chr(10))[0])
        check("Claude-only branch: panel-full.md sibling written too",
              (Path(td) / ".crew/reviews/sess/panel-full.md").exists(),
              "full sibling exists", "?")

    # seat order: VERDICTS roster follows --seats order, full names.
    with tempfile.TemporaryDirectory() as td:
        order = ["sonnet", "codex", "opus"]
        stage(td, order)
        _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", ",".join(order),
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        roster = [ln for ln in dig.splitlines() if ln.startswith("- sonnet")
                  or ln.startswith("- codex") or ln.startswith("- opus")]
        check("seat order: VERDICTS roster follows --seats order (sonnet, codex, opus)",
              roster[0].startswith("- sonnet") and roster[1].startswith("- codex")
              and roster[2].startswith("- opus"), "roster order", str(roster))

    # dotted Task seat (BLOCKING-B): opus-46 passes charset guard + labels opus-46;
    # raw dot opus-4.6 is REJECTED.
    with tempfile.TemporaryDirectory() as td:
        rd = stage(td, ["codex", "opus"])
        (rd / "opus-46.json").write_text((rd / "opus.json").read_text(), encoding="utf-8")
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,opus-46",
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        check("dotted seat: collect --seats codex,opus-46 succeeds + labels opus-46",
              proc.returncode == 0 and "- opus-46  " in dig, "opus-46 labeled",
              f"rc={proc.returncode}")
        proc2 = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,opus-4.6",
             "--group", "-o", "x.md"], cwd=td, timeout=30)
        check("dotted seat: a RAW dot (opus-4.6) is rejected by the charset guard",
              proc2.returncode == 2, "exit 2", f"rc={proc2.returncode}")

    # mixed seat-kinds: subprocess + opus/sonnet fold into shared groups.
    with tempfile.TemporaryDirectory() as td:
        stage(td, six)
        _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", ",".join(six),
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        check("mixed seat-kinds: subprocess + Task seats share the 6/6 group",
              "6/6 [MINOR]" in dig and "opus" in dig and "codex" in dig,
              "shared group across kinds", "?")

    # Task seat with no usable block -> SKIPPED, excluded from denominator.
    with tempfile.TemporaryDirectory() as td:
        rd = stage(td, ["codex", "opus"])
        skipped = ProviderResult(name="sonnet", model=None, ok=False, output="",
                                 error="skipped: no result", elapsed=0.0)
        (rd / "sonnet.json").write_text(json.dumps(skipped.to_dict()), encoding="utf-8")
        _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,opus,sonnet",
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        # A SKIPPED seat never RAN -> excluded from the ran-count R (and from RAW),
        # but still shown as "(skipped)" in the VERDICTS roster.
        check("no-block Task seat: SKIPPED, header '2 ran, 2 findings-parsed'",
              "(2 ran, 2 findings-parsed)" in dig and "(skipped)" in dig,
              "skipped excluded from ran-count + N", dig.split(chr(10))[0])
        # the skipped seat must NOT appear under RAW / UNPARSED (it never ran).
        raw_section = dig.split("## RAW / UNPARSED SEATS", 1)[1]
        check("no-block Task seat: SKIPPED seat is NOT listed under RAW / UNPARSED",
              "seat: sonnet" not in raw_section,
              "skipped not in RAW", raw_section[:80])

    # singleton-not-buried (seat-kind-blind): lone BLOCKING from a SUBPROCESS seat
    # AND from a Task seat both surface as ⚠ SINGLETON.
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "sess"
        rd.mkdir(parents=True)
        sub_block = ProviderResult(name="codex", model=None, ok=True, error=None, elapsed=0.0,
            output="## VERDICT\nREVISE\n## FINDINGS\n- [BLOCKING] x.py:1 — subprocess lone dissent\n")
        task_block = ProviderResult(name="opus", model=None, ok=True, error=None, elapsed=0.0,
            output="## VERDICT\nAPPROVED\n## FINDINGS\n- [MINOR] y.py:1 — task lone dissent\n")
        (rd / "codex.json").write_text(json.dumps(sub_block.to_dict()), encoding="utf-8")
        (rd / "opus.json").write_text(json.dumps(task_block.to_dict()), encoding="utf-8")
        _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,opus",
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        check("singleton seat-kind-blind: subprocess BLOCKING + Task MINOR both ⚠ SINGLETON",
              dig.count("⚠ SINGLETON") == 2 and "(codex)" in dig and "(opus)" in dig,
              "two singletons", "?")

    # BUG-CLASS end-to-end: a MIXED tagged+prose seat loses NOTHING — both the
    # tagged finding AND the untagged prose surface verbatim under RAW.
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "sess"
        rd.mkdir(parents=True)
        mixed = ProviderResult(name="codex", model=None, ok=True, error=None, elapsed=0.0,
            output="## FINDINGS\n- [BLOCKING] a.py:1 — tagged finding here\n"
                   "the error handling in bar.py is ALSO weak and untagged\n")
        compliant = ProviderResult(name="opus", model=None, ok=True, error=None, elapsed=0.0,
            output="## VERDICT\nAPPROVED\n## FINDINGS\n- [MINOR] z.py:1 — minor nit\n")
        (rd / "codex.json").write_text(json.dumps(mixed.to_dict()), encoding="utf-8")
        (rd / "opus.json").write_text(json.dumps(compliant.to_dict()), encoding="utf-8")
        _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,opus",
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        raw_section = dig.split("## RAW / UNPARSED SEATS", 1)[1]
        check("BUG-CLASS mixed seat: tagged + untagged prose BOTH appear in RAW (nothing lost)",
              "tagged finding here" in raw_section
              and "ALSO weak and untagged" in raw_section,
              "both lines verbatim in RAW", raw_section[:120])

    # --group with --full sibling AND a --full WITHOUT --group warning.
    with tempfile.TemporaryDirectory() as td:
        stage(td, ["codex", "opus"])
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,opus",
             "--full", ".crew/reviews/sess/panel-full.md"], cwd=td, timeout=30)
        check("collect --full WITHOUT --group: warns on stderr, writes no sibling",
              proc.returncode == 0 and "warning" in proc.stderr.lower()
              and "--full" in proc.stderr
              and not (Path(td) / ".crew/reviews/sess/panel-full.md").exists(),
              "stderr warning + no sibling", repr(proc.stderr[:120]))

    # --report-unparsed: identifies the partial (non-compliant) seat only.
    with tempfile.TemporaryDirectory() as td:
        stage(td, ["codex", "partial", "opus"])
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,partial,opus",
             "--report-unparsed"], cwd=td, timeout=30)
        check("--report-unparsed: lists ONLY the non-compliant seat (partial)",
              proc.returncode == 0 and proc.stdout.strip() == "partial",
              "partial", repr(proc.stdout))
        check("--report-unparsed alone: no ignored-flags warning on stderr",
              "ignored with --report-unparsed" not in proc.stderr,
              "no warning", repr(proc.stderr[:120]))
        # combining query mode with digest-shaping flags warns loudly on stderr
        # (stdout still carries ONLY the seat names — nothing is written).
        wproc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,partial,opus",
             "--report-unparsed", "--group",
             "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        check("--report-unparsed + --group/-o: stderr warns, stdout names only, "
              "no digest written",
              wproc.returncode == 0 and wproc.stdout.strip() == "partial"
              and "ignored with --report-unparsed" in wproc.stderr
              and "--group" in wproc.stderr and "-o/--out" in wproc.stderr
              and not (Path(td) / ".crew/reviews/sess/panel.md").exists(),
              "warned + query-only", repr(wproc.stderr[:160]))

    # repair-seat: repaired text lands in repaired_output (grouping-only); the
    # ORIGINAL output is PRESERVED byte-intact; a repaired (now-compliant) seat
    # groups normally.
    with tempfile.TemporaryDirectory() as td:
        rd = stage(td, ["codex", "partial"])
        repl = ("## VERDICT\nREVISE\n## FINDINGS\n"
                "- [BLOCKING] cli.py:1600 — collect drops prose. WHY: no fallback. FIX: raw.\n"
                "## CONFIDENCE\nmedium\n")
        repl_path = Path(td) / "repl.txt"
        repl_path.write_text(repl, encoding="utf-8")
        before = json.loads((rd / "partial.json").read_text())
        proc = _run_dispatcher(
            ["repair-seat", "--seat", str(rd / "partial.json"), "-f", str(repl_path)],
            cwd=td, timeout=30)
        after = json.loads((rd / "partial.json").read_text())
        check("repair-seat: ORIGINAL output UNTOUCHED; repaired text goes to "
              "repaired_output (six fields preserved + the one new key)",
              proc.returncode == 0
              and after["output"] == before["output"]      # original byte-intact
              and after["repaired_output"] == repl          # repair recorded separately
              and after["name"] == before["name"] and after["ok"] == before["ok"]
              and set(after) == set(before) | {"repaired_output"},
              "original kept, repaired_output added",
              f"keys={sorted(after)}")
        check("repair-seat: rewritten JSON round-trips through from_dict/to_dict",
              ProviderResult.from_dict(after).repaired_output == repl
              and ProviderResult.from_dict(after).output == before["output"],
              "round-trip", "?")
        # the repaired seat now parses + groups (grouping reads repaired_output).
        chk = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,partial",
             "--report-unparsed"], cwd=td, timeout=30)
        check("repair-seat: repaired seat is now findings-parsed (report-unparsed empty)",
              chk.stdout.strip() == "", "no unparsed left", repr(chk.stdout))

    # repair-seat DATA-LOSS PATH (the crux): a repair that PARSES but silently
    # DROPPED a finding must NOT destroy the original. The grouped digest uses the
    # (lossy) repaired text, but --full AND the seat file's original output stay
    # byte-intact, so the dropped finding is STILL recoverable from --full.
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "sess"
        rd.mkdir(parents=True)
        # original: TWO tagged findings, but wrapped in mixed prose so it does NOT
        # parse as-is (findings_parsed=False -> a repair candidate).
        original_out = (
            "## VERDICT\nREVISE\n## FINDINGS\n"
            "- [BLOCKING] auth.py:10 — token is logged in plaintext\n"
            "- [BLOCKING] db.py:20 — SQL built by string concat (injection)\n"
            "and separately the retry loop never backs off, which is bad\n")
        seat = ProviderResult(name="partial", model="gpt-5", ok=True,
                              output=original_out, error=None, elapsed=1.0)
        (rd / "partial.json").write_text(json.dumps(seat.to_dict()), encoding="utf-8")
        codex_seat = _fixture_result("compliant", "codex")
        (rd / "codex.json").write_text(json.dumps(codex_seat.to_dict()), encoding="utf-8")
        # a LOSSY-but-VALID repair: parses, but keeps only ONE of the two findings.
        lossy = ("## VERDICT\nREVISE\n## FINDINGS\n"
                 "- [BLOCKING] auth.py:10 — token is logged in plaintext\n"
                 "## CONFIDENCE\nhigh\n")
        lossy_path = Path(td) / "lossy.txt"
        lossy_path.write_text(lossy, encoding="utf-8")
        rproc = _run_dispatcher(
            ["repair-seat", "--seat", str(rd / "partial.json"), "-f", str(lossy_path)],
            cwd=td, timeout=30)
        after = json.loads((rd / "partial.json").read_text())
        check("repair-seat (lossy): exit 0 + ORIGINAL output byte-intact on disk",
              rproc.returncode == 0 and after["output"] == original_out
              and after["repaired_output"] == lossy,
              "original preserved, repaired stored", f"rc={rproc.returncode}")
        # grouped digest uses the repaired (lossy) text; --full uses the original.
        _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,partial",
             "--group", "-o", ".crew/reviews/sess/panel.md",
             "--full", ".crew/reviews/sess/panel-full.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        full = (Path(td) / ".crew/reviews/sess/panel-full.md").read_text()
        # the grouped digest reflects the repair: partial now parses, so it is NOT
        # in RAW, and its (only surviving) finding groups.
        check("repair-seat (lossy): grouped digest uses repaired text (partial "
              "findings-parsed, its surviving finding present)",
              "token is logged in plaintext" in dig
              and "seat: partial" not in dig.split("## RAW / UNPARSED SEATS", 1)[1],
              "grouped uses repaired", dig[:80])
        # the DROPPED finding is GONE from the grouped digest but RECOVERABLE from
        # --full (which renders the untouched original).
        check("repair-seat (lossy): dropped finding is RECOVERABLE from --full "
              "(original rendered faithfully)",
              "SQL built by string concat" in full
              and "retry loop never backs off" in full,
              "dropped finding survives in --full", "?")
        # and --full renders the ORIGINAL, not the repaired text.
        results = [_fixture_result("compliant", "codex"),
                   ProviderResult.from_dict(after)]
        check("repair-seat (lossy): --full byte-equals render_panel over ORIGINAL output",
              full == render.render_panel(results) + "\n",
              "faithful original in --full", "?")

    # repair-seat FAITHFUL repair (parses, same findings): grouped uses it, --full
    # still shows the original, the new field round-trips.
    with tempfile.TemporaryDirectory() as td:
        rd = stage(td, ["codex", "partial"])
        original_out = json.loads((rd / "partial.json").read_text())["output"]
        faithful = ("## VERDICT\nREVISE\n## CRITERIA\n- Correctness: FAIL\n"
                    "## FINDINGS\n- [BLOCKING] cli.py:1600 — collect can drop "
                    "prose findings. WHY: no raw fallback. FIX: surface raw.\n"
                    "## CONFIDENCE\nmedium\n")
        fpath = Path(td) / "faithful.txt"
        fpath.write_text(faithful, encoding="utf-8")
        fproc = _run_dispatcher(
            ["repair-seat", "--seat", str(rd / "partial.json"), "-f", str(fpath)],
            cwd=td, timeout=30)
        after = json.loads((rd / "partial.json").read_text())
        check("repair-seat (faithful): exit 0, original preserved, repaired_output set",
              fproc.returncode == 0 and after["output"] == original_out
              and after["repaired_output"] == faithful, "faithful stored", "?")
        _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,partial",
             "--group", "-o", ".crew/reviews/sess/panel.md",
             "--full", ".crew/reviews/sess/panel-full.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        full = (Path(td) / ".crew/reviews/sess/panel-full.md").read_text()
        check("repair-seat (faithful): grouped digest uses the repaired finding",
              "collect can drop prose findings" in dig
              and "seat: partial" not in dig.split("## RAW / UNPARSED SEATS", 1)[1],
              "grouped uses repaired", "?")
        check("repair-seat (faithful): --full STILL shows the seat's ORIGINAL prose",
              "line window also looks" in full
              and "collect can drop prose findings" not in full,
              "original in --full, not repaired", "?")

    # repair-seat: still-non-compliant after repair -> NON-DESTRUCTIVE no-op.
    # The reformat doesn't parse, so the ORIGINAL output is PRESERVED (never
    # overwritten by a degraded haiku rewrite). RAW/full shows the seat's GENUINE
    # review verbatim; the bad replacement text must NOT appear anywhere.
    with tempfile.TemporaryDirectory() as td:
        rd = stage(td, ["codex", "partial"])
        original = json.loads((rd / "partial.json").read_text())["output"]
        bad = Path(td) / "bad.txt"
        bad.write_text("still just prose, no schema headers at all\n", encoding="utf-8")
        rproc = _run_dispatcher(
            ["repair-seat", "--seat", str(rd / "partial.json"), "-f", str(bad)],
            cwd=td, timeout=30)
        # a kept-original no-op is SUCCESS (exit 0) with a stderr note — not an
        # error the orchestrator should react to (exit 2 = real usage/read error).
        check("repair-seat: non-parsing reformat -> exit 0 + stderr kept-original note",
              rproc.returncode == 0 and "leaving the seat UNCHANGED" in rproc.stderr,
              "exit 0 + note", f"rc={rproc.returncode} err={rproc.stderr[:120]!r}")
        # the file's output is UNCHANGED — the original is recoverable.
        after = json.loads((rd / "partial.json").read_text())["output"]
        check("repair-seat: failed repair leaves ORIGINAL output intact (recoverable)",
              after == original and "still just prose" not in after,
              "original preserved", repr(after[:60]))
        proc = _run_dispatcher(
            ["collect", "--session-id", "sess", "--seats", "codex,partial",
             "--group", "-o", ".crew/reviews/sess/panel.md"], cwd=td, timeout=30)
        dig = (Path(td) / ".crew/reviews/sess/panel.md").read_text()
        check("repair-seat: STILL-non-compliant after repair -> RAW shows ORIGINAL, "
              "never the degraded rewrite",
              "## RAW / UNPARSED SEATS" in dig
              and "line window also looks" in dig
              and "still just prose" not in dig,
              "original in RAW, bad text absent", "?")

    # repair-seat: missing file / non-object -> exit 2.
    with tempfile.TemporaryDirectory() as td:
        proc = _run_dispatcher(
            ["repair-seat", "--seat", str(Path(td) / "nope.json"), "-f", "/dev/null"],
            cwd=td, timeout=30)
        check("repair-seat: missing seat file -> exit 2", proc.returncode == 2,
              "exit 2", f"rc={proc.returncode}")


def test_persist_seat():
    log_section("crew persist-seat (engine-owned Task-seat persistence)")

    SIX = {"name", "model", "ok", "output", "error", "elapsed"}

    # 1. Happy path: -f file with review text -> .crew/reviews/S/fable.json with
    #    EXACTLY the six fields (no repaired_output); name/model=="fable"; ok True;
    #    output round-trips byte-identical; stdout.strip() == written path.
    with tempfile.TemporaryDirectory() as td:
        text = "## VERDICT\nAPPROVED\n\n## FINDINGS\n- [MINOR] a.py:1 — nit.\n"
        src = Path(td) / "review.txt"
        src.write_text(text, encoding="utf-8")
        proc = _run_dispatcher(
            ["persist-seat", "fable", "--session-id", "S", "--model", "fable",
             "-f", str(src)], cwd=td, timeout=30)
        out_path = Path(td) / ".crew" / "reviews" / "S" / "fable.json"
        check("persist happy: exit 0", proc.returncode == 0, "0",
              f"rc={proc.returncode} err={proc.stderr}")
        check("persist happy: fable.json written", out_path.exists(),
              "file exists", "missing")
        data = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
        check("persist happy: EXACTLY six fields (no repaired_output)",
              set(data.keys()) == SIX, str(sorted(SIX)), str(sorted(data.keys())))
        check("persist happy: name == 'fable'", data.get("name") == "fable",
              "fable", str(data.get("name")))
        check("persist happy: model == 'fable'", data.get("model") == "fable",
              "fable", str(data.get("model")))
        check("persist happy: ok is True", data.get("ok") is True, "True",
              str(data.get("ok")))
        check("persist happy: output round-trips byte-identical",
              data.get("output") == text, repr(text[:20]), repr(str(data.get("output"))[:20]))
        check("persist happy: stdout.strip() == written path",
              proc.stdout.strip() == str(Path(".crew") / "reviews" / "S" / "fable.json"),
              ".crew/reviews/S/fable.json", proc.stdout.strip())

    # 2. Dotted seat "x.9" -> file x9.json AND "name": "x9" (slug used for BOTH).
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "r.txt"
        src.write_text("hi", encoding="utf-8")
        proc = _run_dispatcher(
            ["persist-seat", "x.9", "--session-id", "S", "--model", "sonnet",
             "-f", str(src)], cwd=td, timeout=30)
        dotted = Path(td) / ".crew" / "reviews" / "S" / "x9.json"
        data = json.loads(dotted.read_text(encoding="utf-8")) if dotted.exists() else {}
        check("dotted seat: file is x9.json (dot-stripped slug)",
              proc.returncode == 0 and dotted.exists(), "x9.json exists",
              f"rc={proc.returncode}")
        check("dotted seat: name field == 'x9' (slug used for BOTH)",
              data.get("name") == "x9", "x9", str(data.get("name")))

    # 3. stdin path (no -f): text piped in -> same result as -f.
    with tempfile.TemporaryDirectory() as td:
        try:
            os.chmod(CREW_DISPATCHER, 0o755)
        except OSError:
            pass
        proc = subprocess.run(
            [str(CREW_DISPATCHER), "persist-seat", "opus", "--session-id", "S",
             "--model", "opus"],
            input="piped review body", capture_output=True, text=True,
            cwd=td, timeout=30)
        opus_path = Path(td) / ".crew" / "reviews" / "S" / "opus.json"
        data = json.loads(opus_path.read_text(encoding="utf-8")) if opus_path.exists() else {}
        check("stdin: no -f reads output from stdin",
              proc.returncode == 0 and data.get("output") == "piped review body",
              "piped review body", str(data.get("output")))

    # 4. --failed --error "spawn timeout" -> ok false, error set, exit 0.
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "r.txt"
        src.write_text("", encoding="utf-8")
        proc = _run_dispatcher(
            ["persist-seat", "sonnet", "--session-id", "S", "--model", "sonnet",
             "-f", str(src), "--failed", "--error", "spawn timeout"],
            cwd=td, timeout=30)
        p = Path(td) / ".crew" / "reviews" / "S" / "sonnet.json"
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        check("failed+error: ok False, error set, exit 0",
              proc.returncode == 0 and data.get("ok") is False
              and data.get("error") == "spawn timeout",
              "ok=False error='spawn timeout'",
              f"rc={proc.returncode} ok={data.get('ok')} error={data.get('error')}")

    # 5. --failed with no --error -> ok false, error is the documented default.
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            [str(CREW_DISPATCHER), "persist-seat", "sonnet", "--session-id", "S",
             "--model", "sonnet", "--failed"],
            input="", capture_output=True, text=True, cwd=td, timeout=30)
        p = Path(td) / ".crew" / "reviews" / "S" / "sonnet.json"
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        check("failed no --error: default diagnostic",
              proc.returncode == 0 and data.get("ok") is False
              and data.get("error") == "Task seat failed (no diagnostic provided)",
              "default diagnostic", str(data.get("error")))

    # 6. Overwrite: persist twice with different text -> file holds SECOND text.
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a.txt"; a.write_text("FIRST", encoding="utf-8")
        b = Path(td) / "b.txt"; b.write_text("SECOND", encoding="utf-8")
        _run_dispatcher(["persist-seat", "opus", "--session-id", "S",
                         "--model", "opus", "-f", str(a)], cwd=td, timeout=30)
        _run_dispatcher(["persist-seat", "opus", "--session-id", "S",
                         "--model", "opus", "-f", str(b)], cwd=td, timeout=30)
        p = Path(td) / ".crew" / "reviews" / "S" / "opus.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        check("overwrite: second persist replaces stale text",
              data.get("output") == "SECOND", "SECOND", str(data.get("output")))

    # 7. Integration: persist "sonnet" + a fixture codex.json, then grouped collect
    #    -> exit 0 and "- sonnet" in the VERDICTS roster (persisted seat flows
    #    through the SAME grouped collect).
    with tempfile.TemporaryDirectory() as td:
        rd = Path(td) / ".crew" / "reviews" / "S"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "codex.json").write_text(
            (FIXTURES_DIR / "compliant" / "codex.json").read_text(encoding="utf-8"),
            encoding="utf-8")
        sonnet_text = _fixture_result("compliant", "sonnet").output
        src = Path(td) / "sonnet.txt"
        src.write_text(sonnet_text, encoding="utf-8")
        pp = _run_dispatcher(
            ["persist-seat", "sonnet", "--session-id", "S", "--model", "sonnet",
             "-f", str(src)], cwd=td, timeout=30)
        cp = _run_dispatcher(
            ["collect", "--session-id", "S", "--seats", "codex,sonnet", "--group",
             "-o", ".crew/reviews/S/panel.md"], cwd=td, timeout=30)
        dig = (rd / "panel.md").read_text(encoding="utf-8") if (rd / "panel.md").exists() else ""
        check("integration: persist + grouped collect exit 0",
              pp.returncode == 0 and cp.returncode == 0, "both 0",
              f"persist={pp.returncode} collect={cp.returncode} err={cp.stderr}")
        check("integration: persisted 'sonnet' appears in VERDICTS roster",
              "- sonnet" in dig, "- sonnet in roster", dig[:120])

    # 8. Placeholder session id "<session-id>" -> exit 2 + stderr, no file written.
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "r.txt"; src.write_text("x", encoding="utf-8")
        proc = _run_dispatcher(
            ["persist-seat", "sonnet", "--session-id", "<session-id>",
             "--model", "sonnet", "-f", str(src)], cwd=td, timeout=30)
        check("placeholder session id: exit nonzero (2) + stderr",
              proc.returncode == 2 and proc.stderr.strip() != "",
              "rc=2 + stderr", f"rc={proc.returncode} err={proc.stderr!r}")
        check("placeholder session id: no .crew/ directory created",
              not (Path(td) / ".crew").exists(), ".crew absent",
              f"exists={(Path(td) / '.crew').exists()}")


def test_process_group_reaping():
    """A timed-out codex/cursor/agy seat reaps its WHOLE process group so a
    billable grandchild is not orphaned. One case per provider proves the shared
    _proc.run_reaped helper at all three call sites (and exercises the extracted
    agy _terminate escalation)."""
    log_section("provider process-group reaping")
    import signal as _signal
    from multiagent.providers.cursor import CursorProvider

    # Fake CLI: spawns a sleeping grandchild in the SAME process group (no
    # start_new_session), records both PIDs, then hangs. Only a process-GROUP
    # kill reaps the grandchild; killing the direct child alone would orphan it.
    fake_hang = """
    import os, sys, subprocess, time, json
    PIDFILE = {pidfile!r}
    gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(PIDFILE, "w") as f:
        json.dump({{"parent": os.getpid(), "grandchild": gc.pid}}, f)
        f.flush()
    time.sleep(120)
    """

    def _pid_alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _run_case(seat, binname, invoke, *, timeout, config_toml=None):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            pidfile = d / "pids.json"
            make_fake_bin(d, binname, fake_hang.format(pidfile=str(pidfile)))
            env_path = os.environ["PATH"]
            os.environ["PATH"] = str(d) + os.pathsep + env_path
            ctx = project_config(config_toml) if config_toml else None
            try:
                if ctx is not None:
                    ctx.__enter__()
                res = invoke(timeout)
            finally:
                os.environ["PATH"] = env_path
                if ctx is not None:
                    ctx.__exit__(None, None, None)

            gc_pid = None
            if pidfile.exists():
                try:
                    gc_pid = json.loads(pidfile.read_text()).get("grandchild")
                except (OSError, json.JSONDecodeError):
                    gc_pid = None

            check(f"{seat}: timed-out run -> ok=False with timeout error",
                  res.ok is False and bool(res.error) and "timed out" in res.error.lower(),
                  "ok=False 'timed out'", f"ok={res.ok} error={res.error!r}")

            reaped = False
            if gc_pid is not None:
                for _ in range(80):  # up to ~8s (SIGTERM kills promptly)
                    if not _pid_alive(gc_pid):
                        reaped = True
                        break
                    time.sleep(0.1)
                if not reaped:
                    try:
                        os.kill(gc_pid, _signal.SIGKILL)  # avoid leaking the leak
                    except OSError:
                        pass
            check(f"{seat}: grandchild reaped on timeout (no orphaned process)",
                  gc_pid is not None and reaped,
                  "grandchild PID dead", f"gc_pid={gc_pid} reaped={reaped}")

    # codex / cursor pass the caller timeout straight through.
    _run_case("codex", "codex",
              lambda t: CodexProvider().run("PROMPT", timeout=t), timeout=2)
    _run_case("cursor", "agent",
              lambda t: CursorProvider("cursor-test", "some-model").run("PROMPT", timeout=t),
              timeout=2)
    # agy's wall-clock floor = print_timeout + grace; pin print_timeout low so the
    # kill fires fast (mirrors the existing agy hang test). Built via the catalog
    # so the config's print_timeout reaches the seat (a bare AgyProvider() no
    # longer reads config).
    from multiagent.providers import get_provider as _get_provider  # noqa: E402
    _run_case("agy", "agy",
              lambda t: _get_provider("agy").run("PROMPT", timeout=t),
              timeout=1, config_toml='[seats.agy]\nprint_timeout = "1s"\n')


# =============================================================================
# Seat-roster drift guard: the shipped roster + panel resolution, pinned.
# =============================================================================

_ROSTER_FIXTURES = TESTS_DIR / "fixtures" / "seat-roster"

# The built-in subprocess panel: what a bare `crew seats` must print, and what
# _resolve_seats(None) must return, on a machine with no crew config at all.
_BUILTIN_FIVE = ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer"]

# Fixed in BOTH the committed capture and the verify below: review-prep's
# prompt_path embeds the session segment, so a run under a different id could
# never be byte-identical to the fixture.
_ROSTER_SESSION_ID = "FIXED-CAPTURE-ID"

# scaffold-config stamps the CURRENT DATE into its header, so a byte-exact
# fixture would go red the day after it was captured. Mask that one field on
# both sides; every other byte is compared verbatim.
_GENERATED_ON_RE = re.compile(r"(generated by /crew:init on )\d{4}-\d{2}-\d{2}")


def _mask_generated_on(text: str) -> str:
    return _GENERATED_ON_RE.sub(r"\1<DATE>", text)


def _isolated_capture(args, cwd, timeout=60):
    """Run the dispatcher with HOME and CLAUDE_PROJECT_DIR pinned at a config-free
    dir, so what comes back is the BUILT-IN roster.

    Load-bearing, not hygiene. A developer's own ~/.crew-config.toml may set
    default_panel, and every panel-resolving path then answers with THAT panel
    (a different seat list, possibly including an opt-in premium seat). A capture
    taken without this pins one machine's personal roster, and the guard goes on
    to certify a roster nobody ships.

    Invoked through sys.executable rather than the dispatcher's shebang: an
    emptied HOME also strips a version-manager's interpreter config, so
    `env python3` resolves no interpreter at all and the run dies at exit 126.
    """
    env = dict(os.environ)
    env["HOME"] = cwd
    env["CLAUDE_PROJECT_DIR"] = cwd
    return subprocess.run(
        [sys.executable, str(CREW_DISPATCHER), *args],
        capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout,
    )


def test_seat_roster_drift_guard():
    log_section("seat-roster drift guard (roster + panels pinned, isolated config)")
    from multiagent import cli, seats  # noqa: E402
    from multiagent.providers import get_provider  # noqa: E402

    # --- 1. The subprocess roster, as a hand-written literal ------------------
    # name / provider class / model / opt_in, in fan-out ORDER. Hand-written
    # rather than derived from the same dicts it checks, so a change to how the
    # roster is assembled fails HERE, naming the seat that moved.
    expected_subprocess = [
        ("codex",           "CodexProvider",  "gpt-5.6-sol",           False),
        ("codex-luna",      "CodexProvider",  "gpt-5.6-luna",          False),
        ("codex-terra",     "CodexProvider",  "gpt-5.6-terra",         True),
        ("agy",             "AgyProvider",    "Gemini 3.1 Pro (High)", False),
        ("cursor-gpt",      "CursorProvider", "gpt-5.5-extra-high",    True),
        ("cursor-gemini",   "CursorProvider", "gemini-3.1-pro",        True),
        ("cursor-glm",      "CursorProvider", "glm-5.2-max",           True),
        ("cursor-grok",     "CursorProvider", "grok-4.5-xhigh",        True),
        ("cursor-auto",     "CursorProvider", "auto",                  False),
        ("cursor-composer", "CursorProvider", "composer-2.5",          False),
    ]
    # The actual is read from the catalog loader (the deleted CODEX_SEATS /
    # CURSOR_SEATS dicts are gone); the EXPECTED above stays a hand-written literal
    # so a seat added to seats.toml without updating it fails HERE, naming the seat.
    def _row(name):
        provider = type(get_provider(name)).__name__
        spec = seats.seat_spec(name)
        return (name, provider, spec.model, spec.opt_in)

    with crew_config():
        subprocess_names = [n for n, s in seats.shipped_catalog().items()
                            if s.has_executor]
        actual_subprocess = [_row(n) for n in subprocess_names]
    check("subprocess roster pinned (name/provider/model/opt_in/order)",
          actual_subprocess == expected_subprocess,
          str(expected_subprocess), str(actual_subprocess))

    # --- 2. The Task (Claude) seats -------------------------------------------
    # A Task seat's model is its catalog pin (its own name, a Task-tool alias), and
    # "opt-in" means "in no preset" (fable is the premium tier, so no default panel
    # can spend it without being named). Expected stays a hand-written literal.
    expected_task = [("opus", "opus", False), ("sonnet", "sonnet", False),
                     ("fable", "fable", True)]
    with crew_config():
        panel_lists = list(seats.merged_panels().values())
        actual_task = [
            (n, seats.seat_spec(n).model,
             all(n not in preset for preset in panel_lists))
            for n in ("opus", "sonnet", "fable")
        ]
        task_names = set(seats.task_seats())
    check("Task-seat roster pinned (name/model/opt-in)",
          actual_task == expected_task and task_names == {"opus", "sonnet", "fable"},
          str(expected_task), f"{actual_task} names={sorted(task_names)}")

    # --- 3. All five presets, verbatim ---------------------------------------
    expected_presets = {
        "full": ["codex", "codex-luna", "agy", "cursor-auto", "cursor-composer",
                 "opus", "sonnet"],
        "quick": ["codex", "sonnet"],
        "lite": ["opus", "sonnet"],
        "solo": ["opus"],
        "cursor": ["cursor"],  # the literal group token, unexpanded
    }
    with crew_config():
        actual_presets = seats.merged_panels()
    check("shipped panels == the five pinned presets (verbatim)",
          actual_presets == expected_presets,
          str(expected_presets), str(actual_presets))

    # --- 4. The billing gate: what actually runs when nobody names a panel ----
    # _resolve_seats(None) is the path every ad-hoc panel takes, so it is the one
    # that decides what gets SPENT. Pinned here (rather than the constant it
    # falls back to) because the constant is an implementation detail; this call
    # is the behavior.
    with crew_config():
        resolved = cli._resolve_seats(None)
    check("_resolve_seats(None) == the built-in five (isolated config)",
          resolved == _BUILTIN_FIVE, str(_BUILTIN_FIVE), str(resolved))
    check("no opt-in premium seat rides the default panel",
          not ({"codex-terra", "cursor-gpt", "cursor-gemini", "cursor-glm",
                "cursor-grok", "fable"} & set(resolved)) and "agy" in resolved,
          "no opt-in seat, agy present", str(resolved))

    # The isolation above is what makes those two assertions mean anything: with
    # a config present, the SAME call answers with the configured roster instead.
    # Proving that here keeps the isolation from being mistaken for boilerplate
    # and quietly dropped.
    with crew_config(glob='default_panel = "personal"\n'
                          '[panels]\npersonal = ["codex", "codex-terra", "opus"]\n'):
        configured = cli._resolve_seats(None)
    check("a global config REDEFINES that same resolution (isolation is load-bearing)",
          configured == ["codex", "codex-terra"] and configured != _BUILTIN_FIVE,
          "['codex', 'codex-terra']", str(configured))

    # --- 4b. The cursor GROUP token expands to the SORTED cursor seats --------
    # The fan-out order for `--seats cursor` is stable by name (not by catalog
    # position), so a reordered seats.toml can never quietly reshuffle it.
    with crew_config():
        expanded = cli._expand_seat_groups(["cursor"])
        cursor_members = set(seats.group_tokens()["cursor"])
    check("_expand_seat_groups(['cursor']) returns the sorted cursor seats",
          expanded == sorted(cursor_members) and set(expanded) == cursor_members,
          str(sorted(cursor_members)), str(expanded))

    # --- 5. Byte-identical CLI captures --------------------------------------
    # The fixtures are the ORACLE: they are a photograph of today's behavior, and
    # a change to how the roster is built must reproduce them byte for byte. They
    # are never regenerated to make a failing run pass.
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "plan.md").write_text("# Plan\n\nStub target for the roster capture.\n")

        p = _isolated_capture(["seats"], td)
        check("`crew seats` byte-identical to the pinned capture",
              p.returncode == 0
              and p.stdout == (_ROSTER_FIXTURES / "seats.txt").read_text(),
              (_ROSTER_FIXTURES / "seats.txt").read_text(),
              f"rc={p.returncode} out={p.stdout!r}")

        p = _isolated_capture(
            ["review-prep", "plan.md", "--session-id", _ROSTER_SESSION_ID], td)
        check("`crew review-prep` byte-identical to the pinned capture",
              p.returncode == 0
              and p.stdout == (_ROSTER_FIXTURES / "review-prep.json").read_text(),
              (_ROSTER_FIXTURES / "review-prep.json").read_text(),
              f"rc={p.returncode} out={p.stdout!r}")

        # scaffold-config on all THREE surfaces: the bare call plus the two flag
        # paths that emit roster-derived lines a bare call never reaches
        # (--detection is the only path emitting `available`, --disable-seat opus
        # the only one emitting a Task-seat block).
        detection = str(_ROSTER_FIXTURES / "detection-input.json")
        for label, args, fixture in (
            ("bare", ["scaffold-config", "--out", "-"], "scaffold-config.toml"),
            ("--detection", ["scaffold-config", "--detection", detection, "--out", "-"],
             "scaffold-config-detection.toml"),
            ("--disable-seat opus", ["scaffold-config", "--disable-seat", "opus", "--out", "-"],
             "scaffold-config-disable-seat.toml"),
        ):
            p = _isolated_capture(args, td)
            want = (_ROSTER_FIXTURES / fixture).read_text()
            got = _mask_generated_on(p.stdout)
            check(f"`crew scaffold-config` ({label}) matches the pinned capture",
                  p.returncode == 0 and got == want,
                  f"{fixture} ({len(want)} chars)",
                  f"rc={p.returncode} got={len(got)} chars, first diff at "
                  f"{next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), 'n/a')}")

    # The detection input is HAND-WRITTEN, not a capture of `crew doctor` on this
    # machine: doctor reports which CLIs happen to be installed here, which would
    # make the fixture red on any box with a different set. One seat is marked
    # unavailable so the `available = false` emission is actually exercised.
    det = json.loads((_ROSTER_FIXTURES / "detection-input.json").read_text())
    _shipped = seats.shipped_catalog()  # config-independent (seats.toml alone)
    _shipped_sub = {n for n, s in _shipped.items() if s.has_executor}
    _shipped_task = {n for n, s in _shipped.items() if not s.has_executor}
    check("detection fixture covers every seat and exercises an unavailable one",
          set(det["subprocess"]) == _shipped_sub
          and set(det["task"]) == _shipped_task
          and any(not v["available"] for v in det["subprocess"].values()),
          "all seats, one unavailable",
          f"{sorted(det['subprocess'])} / {sorted(det['task'])}")


def main():
    print(f"{YELLOW}Running multiagent engine tests...{NC}")
    test_result_contract()
    test_default_seats()
    test_resolve_timeout()
    test_deadline_minutes()
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
    test_process_group_reaping()
    test_render()
    test_prompts()
    test_targets()
    test_production_invocation_and_fanout()
    test_run_subcommand()
    test_dispatch()
    test_council_subcommand()
    test_debate_subcommand()
    test_rounds()
    test_discuss_and_modes()
    test_render_subcommand()
    test_cursor()
    test_from_dict_and_escaping()
    test_collect()
    test_findings_parser()
    test_findings_grouping()
    test_collect_grouped()
    test_config()
    test_global_roster_availability()
    test_panel_availability_consistency()
    test_review_prep()
    test_debate_panel_resolver()
    test_panel_catalog()
    test_catalog_cache_reset()
    test_seat_roster_drift_guard()
    test_catalog_registry_disjoint()
    test_no_dotkept_staged_filename()
    test_persist_seat_doc_sync()
    test_roster_doc_sync()
    test_doctor()
    test_probe()
    test_scaffold_config()
    test_persist_seat()

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
