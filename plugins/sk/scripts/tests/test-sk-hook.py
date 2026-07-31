#!/usr/bin/env python3
"""
Test script for the sk SessionStart hook
Run from project root: python3 plugins/sk/scripts/tests/test-sk-hook.py
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
NC = "\033[0m"

PASS_COUNT = 0
FAIL_COUNT = 0

# This test lives in scripts/tests/; the hook it loads sits in scripts/.
SCRIPT_DIR = Path(__file__).resolve().parent.parent
HOOK = SCRIPT_DIR / "session-start.py"


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


def load_hook() -> ModuleType:
    """Import the hyphen-named hook module by path."""
    spec = importlib.util.spec_from_file_location("sk_session_start", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(directory: Path) -> dict:
    """Run the hook as the harness does and return its parsed stdout."""
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env={"CLAUDE_PROJECT_DIR": str(directory), "PATH": "/usr/bin:/bin"},
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"__unparsed__": result.stdout, "__stderr__": result.stderr}


def context_of(payload: dict) -> str:
    return payload.get("hookSpecificOutput", {}).get("additionalContext", "")


def main() -> int:
    sk = load_hook()
    detect = sk.detect_project_stack

    with tempfile.TemporaryDirectory() as test_dir:
        test_path = Path(test_dir)

        # =====================================================================
        # BOUNDED STACK DETECTION
        # =====================================================================
        log_section("stack detection (pruned bounded walk)")

        # Prune proven: a .kt ONLY under a pruned dir is NOT detected.
        prune = test_path / "prune"
        (prune / "node_modules" / "pkg" / "src").mkdir(parents=True)
        (prune / "node_modules" / "pkg" / "src" / "Buried.kt").write_text("class X")
        if "Kotlin" not in detect(prune):
            log_pass("stack detect: .kt only under a pruned dir (node_modules) is NOT detected")
        else:
            log_fail("stack detect: .kt only under a pruned dir is NOT detected", "no Kotlin", "Kotlin detected")

        # Hit: a .kt in a real source dir IS detected.
        hit = test_path / "hit"
        (hit / "src" / "main").mkdir(parents=True)
        (hit / "src" / "main" / "App.kt").write_text("class A")
        if "Kotlin" in detect(hit):
            log_pass("stack detect: .kt in a real source dir IS detected")
        else:
            log_fail("stack detect: .kt in a real source dir IS detected", "Kotlin", "not detected")

        # A .gradle.kts yields BOTH Kotlin and Gradle (it is also a .kts).
        gradle = test_path / "gradle"
        gradle.mkdir()
        (gradle / "settings.gradle.kts").write_text('rootProject.name = "x"')
        gradle_hints = detect(gradle)
        if "Kotlin" in gradle_hints and "Gradle" in gradle_hints:
            log_pass("stack detect: a .gradle.kts yields both Kotlin and Gradle")
        else:
            log_fail("stack detect: a .gradle.kts yields both Kotlin and Gradle", "Kotlin+Gradle", str(gradle_hints))

        # Python is detected by extension: a tree with real Python but none of
        # the usual root markers (this marketplace is one) must still hit.
        py = test_path / "py"
        (py / "scripts").mkdir(parents=True)
        (py / "scripts" / "hook.py").write_text("print('x')")
        py_hints = detect(py)
        if "Python" in py_hints and "Kotlin" not in py_hints:
            log_pass("stack detect: a .py with no pyproject.toml IS detected, and alone")
        else:
            log_fail("stack detect: a .py with no pyproject.toml IS detected, and alone",
                     "Python only", str(py_hints))

        # A .py only under a pruned dir is NOT detected (same prune, new marker).
        py_prune = test_path / "py-prune"
        (py_prune / ".venv" / "lib").mkdir(parents=True)
        (py_prune / ".venv" / "lib" / "vendored.py").write_text("x = 1")
        if "Python" not in detect(py_prune):
            log_pass("stack detect: .py only under a pruned dir (.venv) is NOT detected")
        else:
            log_fail("stack detect: .py only under a pruned dir is NOT detected", "no Python", "Python detected")

        # Root build file drives the dependency hints.
        deps = test_path / "deps"
        deps.mkdir()
        (deps / "build.gradle.kts").write_text(
            'implementation("org.jetbrains.exposed:exposed-core")\n'
        )
        dep_hints = detect(deps)
        if "Exposed" in dep_hints:
            log_pass("stack detect: an exposed dep in build.gradle.kts yields Exposed")
        else:
            log_fail("stack detect: an exposed dep in build.gradle.kts yields Exposed", "Exposed", str(dep_hints))

        # Wall-clock bound: a deep/wide decoy under node_modules plus one real
        # .kt returns well under the hook budget because the decoy is pruned.
        wall = test_path / "wall"
        (wall / "src").mkdir(parents=True)
        (wall / "src" / "Main.kt").write_text("class M")
        decoy = wall / "node_modules"
        for i in range(40):
            d = decoy / f"pkg{i}" / "deep" / "deeper"
            d.mkdir(parents=True)
            for j in range(20):
                (d / f"f{j}.js").write_text("x")
        t0 = time.perf_counter()
        wall_hints = detect(wall)
        elapsed = time.perf_counter() - t0
        if "Kotlin" in wall_hints and elapsed < 2.0:
            log_pass(f"stack detect: pruned walk returns fast on a big decoy tree ({elapsed:.3f}s)")
        else:
            log_fail("stack detect: pruned walk returns fast on a big decoy tree",
                     "Kotlin + < 2s", f"hints={wall_hints}, elapsed={elapsed:.3f}s")

        # Entry-cap: a low cap halts the walk before it descends to a deep .kt
        # (root's plain files exhaust the budget first), which proves the cap.
        cap = test_path / "cap"
        cap.mkdir()
        for j in range(10):
            (cap / f"plain{j}.txt").write_text("x")
        sub = cap / "sub"
        sub.mkdir()
        (sub / "Deep.kt").write_text("class D")
        saved_cap = sk.WALK_MAX_ENTRIES
        sk.WALK_MAX_ENTRIES = 5
        try:
            capped = detect(cap)
        finally:
            sk.WALK_MAX_ENTRIES = saved_cap
        if "Kotlin" not in capped:
            log_pass("stack detect: entry cap halts the walk before a deep .kt (bounded)")
        else:
            log_fail("stack detect: entry cap halts the walk before a deep .kt", "no Kotlin (cap hit)", str(capped))
        if "Kotlin" in detect(cap):
            log_pass("stack detect: same tree with default cap finds the deep .kt (cap was the cause)")
        else:
            log_fail("stack detect: same tree with default cap finds the deep .kt", "Kotlin", "not detected")

        # =====================================================================
        # GIT INDEX SOURCE (preferred) vs WALK (fallback)
        # =====================================================================
        log_section("file list source: git index preferred, walk as fallback")

        # Outside a repo there is no index, which is the fallback signal. Every
        # test above this point relies on it: temp dirs are not repos, so they
        # all exercised the walk.
        if sk.index_paths(hit) is None:
            log_pass("source: a non-repo directory yields no index (walk fallback)")
        else:
            log_fail("source: a non-repo directory yields no index", "None", str(sk.index_paths(hit))[:200])

        repo = test_path / "gitrepo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "App.kt").write_text("class A")
        (repo / "generated").mkdir()
        (repo / "generated" / "emitted.py").write_text("x = 1")
        (repo / ".gitignore").write_text("generated/\n")
        init = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, text=True)

        if init.returncode != 0:
            log_fail("source: git init for the index tests", "exit 0", init.stderr[:200])
        else:
            # The index is gitignore-aware for free, so generated output does not
            # register as a stack. This is the whole reason the index is preferred:
            # the walk has no way to know `generated/` is not source.
            via_index = detect(repo)
            if via_index == ["Kotlin"]:
                log_pass("source: index respects .gitignore (generated/ .py is not Python)")
            else:
                log_fail("source: index respects .gitignore", "['Kotlin']", str(via_index))

            # Same tree, walked: the ignored dir now counts. Documents the exact
            # behavioral difference the switch buys, so a regression is visible.
            via_walk = sk.source_markers(sk.walk_filenames(repo))
            if "py" in via_walk and "kt" in via_walk:
                log_pass("source: the walk fallback sees the same ignored dir as source (the difference)")
            else:
                log_fail("source: the walk fallback sees the ignored dir", "kt + py", str(via_walk))

            # Untracked-but-unignored counts: a file written and not yet added is
            # still part of the project. Nothing is committed in this repo at all,
            # so every pass above already depended on --others.
            (repo / "src" / "later.py").write_text("y = 2")
            if "Python" in detect(repo):
                log_pass("source: an untracked, unignored file counts (--others)")
            else:
                log_fail("source: an untracked, unignored file counts", "Python", str(detect(repo)))

            # The index lists ENTRIES, not files. A tracked path deleted from the
            # working tree is still cached, so without the on-disk check the hook
            # reports a stack that is gone.
            subprocess.run(["git", "-C", str(repo), "add", "-A"], capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-qm", "init"], capture_output=True, text=True)
            (repo / "src" / "later.py").unlink()
            cached = subprocess.run(["git", "-C", str(repo), "ls-files", "--deleted"],
                                    capture_output=True, text=True).stdout
            if "later.py" not in cached:
                log_fail("source: a deleted-but-cached .py is not reported",
                         "git still caches the deleted path (test setup)", f"ls-files --deleted={cached!r}")
            elif "Python" not in detect(repo):
                log_pass("source: a tracked .py deleted from the tree is NOT reported")
            else:
                log_fail("source: a tracked .py deleted from the tree is NOT reported",
                         "no Python", str(detect(repo)))

        # =====================================================================
        # ADVERTISEMENT GATE
        # =====================================================================
        log_section("skill advertisement (gated on the skill being shipped)")

        # Every skill named in the map must be one this install actually ships,
        # OR be filtered out. This is the guard against the drift that made crew
        # advertise a skill sk never had.
        for hint, skills in sk.STACK_SKILLS.items():
            for skill in skills:
                shipped = sk.is_installed(skill)
                advertised = f"sk:{skill}" in sk.relevant_skills([hint])
                if shipped == advertised:
                    state = "shipped and advertised" if shipped else "not shipped, not advertised"
                    log_pass(f"advertisement gate: sk:{skill} is {state}")
                else:
                    log_fail(f"advertisement gate: sk:{skill}",
                             f"advertised == shipped ({shipped})", f"advertised={advertised}")

        # An unknown skill name is never advertised.
        if not sk.relevant_skills(["NoSuchStack"]):
            log_pass("advertisement gate: an unmapped stack hint advertises nothing")
        else:
            log_fail("advertisement gate: an unmapped stack hint advertises nothing",
                     "empty list", str(sk.relevant_skills(["NoSuchStack"])))

        # =====================================================================
        # HOOK OUTPUT
        # =====================================================================
        log_section("hook output")

        payload = run_hook(hit)
        context = context_of(payload)
        if "sk:kotlin" in context and "sk:kotlin-testing" in context:
            log_pass("hook output: a Kotlin tree names the Kotlin skills")
        else:
            log_fail("hook output: a Kotlin tree names the Kotlin skills",
                     "sk:kotlin + sk:kotlin-testing", context[:300] or str(payload)[:300])

        if "Project stack detected: Kotlin" in context and "<system-reminder>" in context:
            log_pass("hook output: stack line wrapped in a <system-reminder>")
        else:
            log_fail("hook output: stack line wrapped in a <system-reminder>",
                     "stack line inside <system-reminder>", context[:300])

        if "🔧 [sk]" in context:
            log_pass("hook output: carries the first-reply status line")
        else:
            log_fail("hook output: carries the first-reply status line", "🔧 [sk] …", context[:300])

        py_context = context_of(run_hook(py))
        if "sk:python" in py_context and "sk:kotlin" not in py_context:
            log_pass("hook output: a Python tree names sk:python and no Kotlin skill")
        else:
            log_fail("hook output: a Python tree names sk:python and no Kotlin skill",
                     "sk:python without sk:kotlin", py_context[:300])

        # sk:git is action-triggered, not structural: no tree should surface it.
        if "sk:git" not in context and "sk:git" not in py_context:
            log_pass("hook output: sk:git is never named (left to passive activation)")
        else:
            log_fail("hook output: sk:git is never named", "no sk:git",
                     f"kotlin={context[:150]} python={py_context[:150]}")

        # No stack, nothing to say: an empty object, never an empty reminder.
        empty = test_path / "empty"
        empty.mkdir()
        empty_payload = run_hook(empty)
        if empty_payload == {}:
            log_pass("hook output: a stackless tree emits {} (no empty reminder)")
        else:
            log_fail("hook output: a stackless tree emits {}", "{}", str(empty_payload)[:300])

        # A hint whose skills are ALL unshipped suppresses the WHOLE block, not
        # just the missing names: a reminder that announces a stack and then
        # offers nothing is noise. Exercised with an injected hint, since every
        # real mapping currently ships (and an on-disk route to a dependency hint
        # would drag Kotlin + Gradle in with it via the build.gradle.kts itself).
        sk.STACK_SKILLS["Ghost"] = ("does-not-ship",)
        try:
            ghost = sk.build_guidance(["Ghost"])
        finally:
            del sk.STACK_SKILLS["Ghost"]
        if ghost == "":
            log_pass("hook output: a hint whose skills are all unshipped emits nothing")
        else:
            log_fail("hook output: a hint whose skills are all unshipped emits nothing",
                     "empty guidance", ghost[:200])

    # =========================================================================
    # INTERPRETER FLOOR
    # =========================================================================
    log_section("interpreter floor (3.11)")

    # Static: the guard must precede the real imports. A `X | None` annotation
    # below it is a def-time TypeError on an old interpreter, so a guard placed
    # after the imports would never get to speak.
    src = HOOK.read_text()
    guard_at = src.find("if sys.version_info < MIN_PYTHON:")
    first_project_import = src.find("import subprocess")
    if guard_at != -1 and first_project_import > guard_at:
        log_pass("session-start.py: version guard precedes the imports it protects")
    else:
        log_fail("session-start.py: version guard precedes the imports it protects",
                 "guard before `import subprocess`", f"guard@{guard_at} import@{first_project_import}")

    # Real interpreter, when one is around: below the floor the hook must be a
    # VISIBLE no-op, not a traceback. 3.9 is the one that shows up in practice,
    # since macOS ships it as /usr/bin/python3.
    old_python = None
    for candidate in ("python3.9", "python3.10", "/usr/bin/python3"):
        path = shutil.which(candidate) if not candidate.startswith("/") else candidate
        if path and Path(path).exists():
            ver = subprocess.run([path, "-c", "import sys; print(sys.version_info[:2])"],
                                 capture_output=True, text=True)
            if ver.stdout.strip() in ("(3, 9)", "(3, 10)"):
                old_python = path
                break
    if old_python is None:
        print(f"{YELLOW}~ SKIP{NC}: no sub-3.11 interpreter found (real-interpreter check)")
    else:
        with tempfile.TemporaryDirectory() as old_dir:
            Path(old_dir, "App.kt").write_text("class A")
            run = subprocess.run(
                [old_python, str(HOOK)], input="{}", capture_output=True, text=True,
                env={"CLAUDE_PROJECT_DIR": old_dir, "PATH": "/usr/bin:/bin"},
            )
        try:
            payload = json.loads(run.stdout)
        except json.JSONDecodeError:
            payload = {}
        diag = context_of(payload)
        if run.returncode == 0 and "below this marketplace's 3.11 floor" in diag and "Traceback" not in run.stderr:
            log_pass("sub-3.11 interpreter: visible no-op (exit 0 + floor diagnostic, no traceback)")
        else:
            log_fail("sub-3.11 interpreter: visible no-op (exit 0 + floor diagnostic, no traceback)",
                     "exit 0, floor diagnostic on the context channel",
                     f"code={run.returncode} out={run.stdout[:160]} err={run.stderr[:160]}")

    print()
    print(f"{GREEN}Passed: {PASS_COUNT}{NC}  {RED}Failed: {FAIL_COUNT}{NC}")
    return 1 if FAIL_COUNT else 0


if __name__ == "__main__":
    sys.exit(main())
