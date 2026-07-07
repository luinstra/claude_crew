#!/usr/bin/env python3
"""
Tests for scripts/post-commit-version-bump.sh (R4 hardening).

Run from project root: python3 plugins/crew/scripts/tests/test-version-bump.py

Stdlib-only, same check/log style as the existing suites. Each case builds a
throwaway git repo in a temp dir, runs the hardened script exactly as the
post-commit hook would (bash <script>, cwd=repo), and asserts the bump behavior,
the git-state guards, and — critically — the snapshot-restore rollback +
format-stability on BOTH synthetic fixtures AND this repo's REAL json bytes.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
NC = "\033[0m"

PASS_COUNT = 0
FAIL_COUNT = 0

# test lives at plugins/crew/scripts/tests/ → repo root is parents[4].
REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "post-commit-version-bump.sh"
REAL_GIT = shutil.which("git")


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
        log_fail(name, expected, got)


# ---------------------------------------------------------------------------
# Repo scaffolding helpers
# ---------------------------------------------------------------------------

MARKETPLACE_JSON = """{
  "$schema": "https://code.claude.com/marketplace.schema.json",
  "name": "claude-crew",
  "description": "test marketplace",
  "metadata": {
    "version": "0.19.2",
    "pluginRoot": "./plugins"
  },
  "plugins": [
    {
      "name": "crew",
      "source": "./plugins/crew"
    }
  ]
}
"""

# crew plugin.json deliberately carries an INLINE keywords array — the exact
# shape a full json.dump(indent=2) would reformat. The surgical writer must
# leave it byte-identical apart from the version value.
CREW_PLUGIN_JSON = """{
  "$schema": "https://code.claude.com/plugin.schema.json",
  "name": "crew",
  "description": "test crew",
  "version": "0.40.0",
  "keywords": ["agents", "persistence", "build-loop", "planning", "session"]
}
"""

SK_PLUGIN_JSON = """{
  "name": "sk",
  "description": "test sk",
  "version": "0.3.0"
}
"""


def _git(repo: Path, *args, env=None):
    return subprocess.run(
        [REAL_GIT, *args], cwd=str(repo), capture_output=True, text=True,
        env=env if env is not None else _repo_env(),
    )


def _repo_env():
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    env["HOME"] = env.get("_CREW_TEST_HOME", env.get("HOME", "/tmp"))
    return env


def make_repo(tmp: Path, marketplace=MARKETPLACE_JSON,
              crew=CREW_PLUGIN_JSON, sk=SK_PLUGIN_JSON) -> Path:
    """Build a repo with the three json files + a README, one root commit."""
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "marketplace.json").write_text(marketplace)
    for name, body in (("crew", crew), ("sk", sk)):
        d = repo / "plugins" / name / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(body)
        (repo / "plugins" / name / "src.txt").write_text("v0\n")
    (repo / "README.md").write_text("# test\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("doc\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: initial")
    return repo


def run_hook(repo: Path, env=None):
    """Run the hardened script exactly as the post-commit hook would."""
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=str(repo), capture_output=True, text=True,
        env=env if env is not None else _repo_env(),
    )


def read_version(path: Path):
    d = json.loads(path.read_text())
    v = d.get("version")
    if v is None and isinstance(d.get("metadata"), dict):
        v = d["metadata"].get("version")
    return v


def make_fake_git_dir(tmp: Path) -> Path:
    """A `git` shim on PATH that FAILS `git commit` but passes everything else
    through to the real git — a deterministic post-`git add` failure injection."""
    d = tmp / "fakebin"
    d.mkdir()
    shim = d / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "commit" ]; then exit 17; fi\n'
        f'exec {REAL_GIT} "$@"\n'
    )
    shim.chmod(0o755)
    return d


def env_with_fake_git(fakebin: Path):
    env = _repo_env()
    env["PATH"] = f"{fakebin}:{env.get('PATH','')}"
    return env


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bump_types():
    log_section("bump-type mapping (feat/fix/breaking)")
    for kind, subject, field, expect in (
        ("fix→patch", "fix: a bug", "0.40.0", "0.40.1"),
        ("feat→minor", "feat: a feature", "0.40.0", "0.41.0"),
        ("feat!→major", "feat!: breaking", "0.40.0", "1.0.0"),
        ("BREAKING→major", "refactor: x\n\nBREAKING CHANGE: y", "0.40.0", "1.0.0"),
    ):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", subject)
            proc = run_hook(repo)
            got = read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json")
            check(f"{kind}: crew {field} -> {expect}", got == expect and proc.returncode == 0,
                  expect, f"{got} rc={proc.returncode} err={proc.stderr[:150]}")


def test_skip_conditions():
    log_section("skip guards (already-bumped, [skip version])")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: change")
        run_hook(repo)  # creates the bump commit
        head_after_first = _git(repo, "rev-parse", "HEAD").stdout.strip()
        subj = _git(repo, "log", "-1", "--format=%s").stdout.strip()
        check("first run created a bump commit", subj.startswith("chore: bump version"),
              "chore: bump version...", subj)
        # Second run: HEAD is already a bump → no new commit.
        run_hook(repo)
        head_after_second = _git(repo, "rev-parse", "HEAD").stdout.strip()
        check("already-a-bump: second run is a no-op (HEAD unchanged)",
              head_after_first == head_after_second, "same HEAD", "HEAD moved")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: change [skip version]")
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        run_hook(repo)
        head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        check("[skip version]: honored (no bump commit)",
              head_before == head_after, "same HEAD", "HEAD moved")


def test_baseline_not_poisoned_by_body():
    log_section("subject-anchored baseline (body mention doesn't poison)")
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        # A real bump commit first.
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: seed")
        run_hook(repo)  # → real 'chore: bump version' commit
        base_ver = read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json")
        # Now a feat whose BODY mentions the bump subject — must NOT become the baseline.
        (repo / "plugins" / "crew" / "src.txt").write_text("v2\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m",
             "feat: real feature\n\nmentions chore: bump version in the body")
        run_hook(repo)
        new_ver = read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json")
        # If the baseline were poisoned to the feat commit, the range would be
        # empty → patch. Subject-anchored baseline sees the feat → minor.
        b = tuple(int(x) for x in base_ver.split("."))
        n = tuple(int(x) for x in new_ver.split("."))
        minor_bump = n[1] == b[1] + 1 and n[2] == 0
        check("body 'chore: bump version' doesn't poison baseline (feat → minor)",
              minor_bump, f"minor over {base_ver}", new_ver)


def test_git_state_guards():
    log_section("git-state guards (detached / rebase / cherry-pick / merge)")

    # Detached HEAD
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: change")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "checkout", "-q", sha)  # detached
        before = (repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json").read_bytes()
        proc = run_hook(repo)
        after = (repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json").read_bytes()
        check("detached HEAD: clean no-op (exit 0, tree byte-identical)",
              proc.returncode == 0 and before == after,
              "rc=0, unchanged", f"rc={proc.returncode} changed={before != after}")

    # Mid-rebase / cherry-pick / merge markers
    for marker, is_dir in (("rebase-merge", True), ("rebase-apply", True),
                           ("CHERRY_PICK_HEAD", False), ("MERGE_HEAD", False)):
        with tempfile.TemporaryDirectory() as td:
            repo = make_repo(Path(td))
            (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "fix: change")
            gp = repo / ".git" / marker
            if is_dir:
                gp.mkdir()
            else:
                gp.write_text("ref\n")
            before = (repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json").read_bytes()
            proc = run_hook(repo)
            after = (repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json").read_bytes()
            check(f"mid-op marker {marker}: clean no-op",
                  proc.returncode == 0 and before == after,
                  "rc=0, unchanged", f"rc={proc.returncode} changed={before != after}")


def test_malformed_version_aborts():
    log_section("malformed/missing version → abort, byte-identical tree")
    bad_crew = CREW_PLUGIN_JSON.replace('  "version": "0.40.0",\n', "")
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), crew=bad_crew)
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: change")
        cj = repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json"
        before = cj.read_bytes()
        head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        proc = run_hook(repo)
        after = cj.read_bytes()
        head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
        check("missing version key: aborts nonzero",
              proc.returncode != 0, "nonzero", str(proc.returncode))
        check("missing version key: tree byte-identical, no bump commit",
              before == after and head_before == head_after,
              "unchanged + no commit",
              f"changed={before != after} head_moved={head_before != head_after}")


def test_snapshot_restore_and_no_staging():
    log_section("snapshot-restore rollback + nothing-staged on failure")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = make_repo(tmp)
        # A real changed commit so a bump is attempted.
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: change")
        cj = repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json"
        # Inject an unrelated, UNSTAGED edit into the same json (description).
        edited = cj.read_text().replace('"description": "test crew"',
                                        '"description": "EDITED unstaged"')
        cj.write_text(edited)
        before_bytes = cj.read_bytes()
        fakebin = make_fake_git_dir(tmp)
        proc = run_hook(repo, env=env_with_fake_git(fakebin))
        after_bytes = cj.read_bytes()
        check("commit-failure: script aborts nonzero", proc.returncode != 0,
              "nonzero", str(proc.returncode))
        check("snapshot-restore: unrelated unstaged edit preserved, version reverted",
              after_bytes == before_bytes,
              "bytes == pre-hook (edit kept, version un-bumped)",
              f"changed={after_bytes != before_bytes}: {cj.read_text()[:200]}")
        # index must be clean over the three json paths (git reset ran)
        staged = _git(repo, "diff", "--cached", "--name-only", "--",
                      ".claude-plugin/marketplace.json",
                      "plugins/crew/.claude-plugin/plugin.json",
                      "plugins/sk/.claude-plugin/plugin.json").stdout.strip()
        check("nothing-staged-on-failure: index clean over the three json paths",
              staged == "", "empty", repr(staged))


def test_only_changed_plugin_bumps():
    log_section("scope: only changed plugin bumps; marketplace gated")
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        # Touch ONLY crew source (no README/docs/marketplace, no sk).
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: crew only")
        proc = run_hook(repo)
        crew_v = read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json")
        sk_v = read_version(repo / "plugins" / "sk" / ".claude-plugin" / "plugin.json")
        mp_v = read_version(repo / ".claude-plugin" / "marketplace.json")
        check("crew-only change: crew bumped", crew_v == "0.40.1", "0.40.1", crew_v)
        check("crew-only change: sk NOT bumped", sk_v == "0.3.0", "0.3.0", sk_v)
        check("crew-only change: marketplace NOT bumped", mp_v == "0.19.2",
              "0.19.2", f"{mp_v} rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        # A README change alone bumps marketplace.
        (repo / "README.md").write_text("# test\nmore\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "docs: readme")
        run_hook(repo)
        mp_v = read_version(repo / ".claude-plugin" / "marketplace.json")
        check("README change: marketplace bumped (patch)", mp_v == "0.19.3",
              "0.19.3", mp_v)


def test_format_stability_synthetic():
    log_section("format stability: only the version line changes")
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: change")
        cj = repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json"
        before = cj.read_text().splitlines()
        run_hook(repo)
        after = cj.read_text().splitlines()
        diffs = [(b, a) for b, a in zip(before, after) if b != a]
        same_len = len(before) == len(after)
        only_version = same_len and len(diffs) == 1 and '"version"' in diffs[0][0]
        check("crew inline-keywords file: ONLY the version line changed (no reformat)",
              only_version, "exactly 1 changed line (version)",
              f"len_ok={same_len} diffs={diffs}")


def test_real_repo_inputs():
    log_section("dry-run on THIS repo's REAL json bytes (format + snapshot-restore)")
    real_mp = (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text()
    real_crew = (REPO_ROOT / "plugins" / "crew" / ".claude-plugin" / "plugin.json").read_text()
    real_sk = (REPO_ROOT / "plugins" / "sk" / ".claude-plugin" / "plugin.json").read_text()

    # (1) successful bump on real bytes → format stable
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), marketplace=real_mp, crew=real_crew, sk=real_sk)
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: real-input change")
        cj = repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json"
        before_v = read_version(cj)
        before_lines = cj.read_text().splitlines()
        proc = run_hook(repo)
        after_v = read_version(cj)
        after_lines = cj.read_text().splitlines()
        diffs = [(b, a) for b, a in zip(before_lines, after_lines) if b != a]
        parts = before_v.split(".")
        expect_v = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
        check("real crew plugin.json: patch bump succeeds",
              after_v == expect_v and proc.returncode == 0,
              expect_v, f"{after_v} rc={proc.returncode} err={proc.stderr[:150]}")
        check("real crew plugin.json: ONLY the version line changed (no reformat)",
              len(before_lines) == len(after_lines) and len(diffs) == 1
              and '"version"' in diffs[0][0],
              "exactly 1 changed line (version)", f"diffs={diffs}")

    # (2) snapshot-restore on real bytes with an injected commit failure
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = make_repo(tmp, marketplace=real_mp, crew=real_crew, sk=real_sk)
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: real-input change")
        cj = repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json"
        before_bytes = cj.read_bytes()
        fakebin = make_fake_git_dir(tmp)
        proc = run_hook(repo, env=env_with_fake_git(fakebin))
        after_bytes = cj.read_bytes()
        check("real inputs: commit-failure aborts nonzero", proc.returncode != 0,
              "nonzero", str(proc.returncode))
        check("real inputs: snapshot-restore leaves the file byte-identical",
              after_bytes == before_bytes, "byte-identical",
              f"changed={after_bytes != before_bytes}")
        staged = _git(repo, "diff", "--cached", "--name-only").stdout.strip()
        check("real inputs: nothing staged after failure", staged == "",
              "empty", repr(staged))


def main():
    if REAL_GIT is None:
        print("git not found on PATH — cannot run version-bump tests")
        sys.exit(1)
    if not SCRIPT.is_file():
        print(f"script not found: {SCRIPT}")
        sys.exit(1)

    test_bump_types()
    test_skip_conditions()
    test_baseline_not_poisoned_by_body()
    test_git_state_guards()
    test_malformed_version_aborts()
    test_snapshot_restore_and_no_staging()
    test_only_changed_plugin_bumps()
    test_format_stability_synthetic()
    test_real_repo_inputs()

    print()
    print("===========================================")
    if FAIL_COUNT:
        print(f"Results: {GREEN}{PASS_COUNT} passed{NC}, {RED}{FAIL_COUNT} failed{NC}")
        print("===========================================")
        sys.exit(1)
    print(f"Results: {GREEN}{PASS_COUNT} passed{NC}, 0 failed")
    print("===========================================")


if __name__ == "__main__":
    main()
