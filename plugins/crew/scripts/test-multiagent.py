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
        check("codex argv: exec -, --sandbox, --skip-git-repo-check, -o",
              cap["args"][0] == "exec" and cap["args"][1] == "-"
              and "--sandbox" in cap["args"] and "--skip-git-repo-check" in cap["args"]
              and "-o" in cap["args"],
              "exec - --sandbox ... --skip-git-repo-check -o", str(cap["args"]))
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
        check("default subprocess panel == codex + cursor-gemini/glm/composer",
              seats == ["codex", "cursor-gemini", "cursor-glm", "cursor-composer"],
              "['codex', 'cursor-gemini', 'cursor-glm', 'cursor-composer']", str(seats))
        # opus/sonnet (Task seats) AND unknown names in CREW_MA_SEATS are dropped;
        # agy is registered, so it's honored as an opt-in seat.
        os.environ["CREW_MA_SEATS"] = "codex,agy,opus,sonnet,bogus"
        seats = _default_subprocess_seats()
        check("CREW_MA_SEATS filters to registered subprocess seats only",
              seats == ["codex", "agy"], "['codex', 'agy']", str(seats))
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

    # unknown / non-subprocess seat -> clear error + nonzero exit.
    proc = _run_cli(["run", "opus", "hi"], timeout=30)
    check("run unknown/non-subprocess seat -> nonzero + clear error",
          proc.returncode != 0
          and "opus" in proc.stderr and "subprocess seat" in proc.stderr,
          "nonzero + 'opus'/'subprocess seat'", f"{proc.returncode}: {proc.stderr!r}")

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
    log_section("debate subcommand (scaffold dir + council in one allowlistable call)")

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

    # positional question + --slug: scaffolds <base>/<ts>-<slug>/ with question.md
    # and subprocess.json, prints a JSON summary with the dir.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)
        env = path_with(bins)
        base = d / "debates"
        proc = _run_cli(
            ["debate", "Is X better than Y?", "--seats", "codex",
             "--slug", "x-vs-y", "--base-dir", str(base)],
            env=env, timeout=30,
        )
        check("debate: exit 0",
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
        ok_json = False
        if ddir and (ddir / "subprocess.json").exists():
            try:
                arr = json.loads((ddir / "subprocess.json").read_text())
                ok_json = isinstance(arr, list) and arr and arr[0]["name"] == "codex"
            except Exception:
                ok_json = False
        check("debate: subprocess.json written (six-field array)",
              ok_json, "subprocess.json array", "?")

    # -f reads the question from a file; auto-slug when --slug omitted.
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        bins = d / "bin"
        bins.mkdir()
        make_fake_bin(bins, "codex", codex_echo)
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
        check("debate -f: reads question file and scaffolds the dir",
              proc.returncode == 0 and bool(ddir) and ddir.exists()
              and (ddir / "question.md").exists(),
              "exit0 + dir + question.md", f"{proc.returncode}: {proc.stdout[:160]}")
        check("debate: auto-slug derived from the question when --slug omitted",
              bool(ddir) and "should-we-adopt" in ddir.name,
              "slug from first words", str(ddir.name if ddir else None))

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

    check("cursor model-seats are registered (cursor-gemini/glm/composer)",
          all(s in known_seat_names() for s in CURSOR_SEATS),
          "all present", str(known_seat_names()))
    check("engine resolves a cursor seat (--seats cursor-glm,codex)",
          cli._resolve_seats("cursor-glm,codex") == ["cursor-glm", "codex"],
          "['cursor-glm', 'codex']", str(cli._resolve_seats("cursor-glm,codex")))

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
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("CURSOR_CAPTURE", None)

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


def main():
    print(f"{YELLOW}Running multiagent engine tests...{NC}")
    test_result_contract()
    test_default_seats()
    test_resolve_timeout()
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
