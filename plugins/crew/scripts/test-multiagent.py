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

from multiagent import prompts, render, targets  # noqa: E402
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
    check("agy: --dangerously-skip-permissions present",
          "--dangerously-skip-permissions" in args, "present", str(args))
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
        check("default subprocess seats == codex,agy (agy is a default seat)",
              seats == ["codex", "agy"], "['codex', 'agy']", str(seats))
        # opus/sonnet in CREW_MA_SEATS are ignored (orchestrator owns Task seats)
        os.environ["CREW_MA_SEATS"] = "codex,agy,opus,sonnet"
        seats = _default_subprocess_seats()
        check("CREW_MA_SEATS filters to subprocess seats only",
              seats == ["codex", "agy"], "['codex', 'agy']", str(seats))
    finally:
        if saved is None:
            os.environ.pop("CREW_MA_SEATS", None)
        else:
            os.environ["CREW_MA_SEATS"] = saved


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
    check("prompts has NO debate template",
          not hasattr(prompts, "debate") and not hasattr(prompts, "debate_round"),
          "no debate fn", "has debate fn")
    check("prompts has NO SEAT/ROUND header text",
          "SEAT:" not in code and "ROUND:" not in code, "no SEAT/ROUND", "present")


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

    # working-tree includes staged + unstaged; untracked surfaced
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        (Path(td) / "a.txt").write_text("hello\nstaged\n")
        _git(["add", "a.txt"], td)
        (Path(td) / "b.txt").write_text("new untracked\n")  # untracked
        t = targets.resolve("working-tree", cwd=td)
        check("working-tree includes staged change",
              "staged" in t.content, "staged content", t.content[:80])
        check("working-tree surfaces untracked file by path",
              any("b.txt" in n for n in t.notes), "untracked note b.txt", str(t.notes))

    # clean repo -> last commit vs base (auto)
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td)
        _git(["checkout", "-q", "-b", "feature"], td)
        (Path(td) / "a.txt").write_text("hello\nfeature\n")
        _git(["commit", "-aqm", "feat"], td)
        t = targets.resolve("auto", base="main", cwd=td)
        check("auto clean repo -> branch diff vs base",
              t.kind == "code" and "feature" in t.content, "feature diff", f"scope={t.scope}")

    # no commits yet
    with tempfile.TemporaryDirectory() as td:
        _init_repo(td, with_commit=False)
        (Path(td) / "a.txt").write_text("new\n")
        t = targets.resolve("auto", cwd=td)
        check("no-commits-yet -> clear note, no traceback",
              t.kind == "code" and any("no commits" in n for n in t.notes),
              "no-commits note", str(t.notes))

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


def main():
    print(f"{YELLOW}Running multiagent engine tests...{NC}")
    test_result_contract()
    test_default_seats()
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
