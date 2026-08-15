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

CURSOR_MARKETPLACE_JSON = """{
  "name": "claude-crew",
  "metadata": {
    "description": "test marketplace",
    "version": "0.19.2",
    "pluginRoot": "./plugins"
  },
  "plugins": [
    {
      "name": "crew",
      "source": "crew"
    },
    {
      "name": "sk",
      "source": "sk"
    }
  ]
}
"""

SK_CURSOR_PLUGIN_JSON = """{
  "name": "sk",
  "description": "test cursor sk",
  "version": "0.3.0",
  "skills": "./skills"
}
"""

CURSOR_CREW_PLUGIN_JSON = """{
  "name": "crew",
  "description": "test cursor crew",
  "version": "0.40.0",
  "commands": "./commands",
  "agents": "./agents-cursor",
  "hooks": "./hooks/cursor-hooks.json"
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
              crew=CREW_PLUGIN_JSON, sk=SK_PLUGIN_JSON, twins=False) -> Path:
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
    if twins:
        (repo / ".cursor-plugin").mkdir(parents=True)
        (repo / ".cursor-plugin" / "marketplace.json").write_text(CURSOR_MARKETPLACE_JSON)
        for name, body in (("crew", CURSOR_CREW_PLUGIN_JSON),
                           ("sk", SK_CURSOR_PLUGIN_JSON)):
            cursor_dir = repo / "plugins" / name / ".cursor-plugin"
            cursor_dir.mkdir(parents=True)
            (cursor_dir / "plugin.json").write_text(body)
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


def test_breaking_change_footer_anchoring():
    log_section("BREAKING CHANGE footer anchoring (no false major)")

    # NEGATIVE: a body that merely MENTIONS the phrase without the footer colon
    # must NOT trigger a major bump — it should fall through to the fix→patch arm.
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m",
             "fix: tidy up\n\nNote: this is NOT a BREAKING CHANGE, just a cleanup.")
        run_hook(repo)
        got = read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json")
        check("mention without footer colon → patch (not major)", got == "0.40.1",
              "0.40.1", got)

    # POSITIVE: a real `BREAKING CHANGE:` footer still forces a major bump.
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m",
             "refactor: rework state\n\nBREAKING CHANGE: state file format changed.")
        run_hook(repo)
        got = read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json")
        check("real BREAKING CHANGE: footer → major", got == "1.0.0", "1.0.0", got)

    # POSITIVE: the hyphen spelling `BREAKING-CHANGE:` footer also forces major.
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m",
             "refactor: rework state\n\nBREAKING-CHANGE: state file format changed.")
        run_hook(repo)
        got = read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json")
        check("BREAKING-CHANGE: (hyphen) footer → major", got == "1.0.0", "1.0.0", got)

    # NEGATIVE: a `type!:` marker is a SUBJECT-line form — a BODY line shaped
    # like `foo!: bar` (e.g. a pasted diff/changelog line) under a plain `fix:`
    # subject must NOT false-trigger a major bump; it falls through to patch.
    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m",
             "fix: tidy imports\n\nDropped a pasted line:\nfoo!: bar was here")
        run_hook(repo)
        got = read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json")
        check("body line 'foo!: bar' under fix: subject → patch (not major)",
              got == "0.40.1", "0.40.1", got)


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


def test_cursor_manifest_parity():
    log_section("Cursor manifest parity")
    claude_marketplace = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text()
    )
    cursor_marketplace = json.loads(
        (REPO_ROOT / ".cursor-plugin" / "marketplace.json").read_text()
    )
    claude_crew = json.loads(
        (REPO_ROOT / "plugins" / "crew" / ".claude-plugin" / "plugin.json").read_text()
    )
    cursor_crew = json.loads(
        (REPO_ROOT / "plugins" / "crew" / ".cursor-plugin" / "plugin.json").read_text()
    )
    marketplace_common = (
        cursor_marketplace.get("name") == claude_marketplace.get("name")
        and cursor_marketplace.get("owner") == claude_marketplace.get("owner")
        and cursor_marketplace["metadata"].get("version")
        == claude_marketplace["metadata"].get("version")
        and cursor_marketplace["metadata"].get("pluginRoot")
        == claude_marketplace["metadata"].get("pluginRoot")
    )
    claude_crew_entry = next(
        entry for entry in claude_marketplace["plugins"] if entry["name"] == "crew"
    )
    cursor_crew_entry = next(
        entry for entry in cursor_marketplace["plugins"] if entry["name"] == "crew"
    )
    plugin_common = all(
        cursor_crew.get(key) == claude_crew.get(key)
        for key in ("name", "version", "author", "repository", "keywords")
    )
    cursor_description = cursor_crew.get("description") == (
        "Persistence, specialized agents, and workflow commands"
    )
    # Cursor's marketplace schema has no top-level description (it lives in
    # metadata), and its pluginRoot is a PREFIX on every source, so the cursor
    # twin's source is relative to it while Claude's stays repo-relative.
    cursor_marketplace_description = (
        "description" not in cursor_marketplace
        and cursor_marketplace["metadata"].get("description")
        == "Persistence, specialized agents, workflow commands, and tech-stack skills"
    )
    entry_common = (
        cursor_crew_entry.get("name") == claude_crew_entry.get("name")
        and cursor_crew_entry.get("description")
        == claude_crew_entry.get("description")
        and claude_crew_entry.get("source") == "./plugins/crew"
        and cursor_crew_entry.get("source") == "crew"
    )
    claude_sk_entry = next(
        entry for entry in claude_marketplace["plugins"] if entry["name"] == "sk"
    )
    cursor_sk_entry = next(
        entry for entry in cursor_marketplace["plugins"] if entry["name"] == "sk"
    )
    claude_sk = json.loads(
        (REPO_ROOT / "plugins" / "sk" / ".claude-plugin" / "plugin.json").read_text()
    )
    cursor_sk = json.loads(
        (REPO_ROOT / "plugins" / "sk" / ".cursor-plugin" / "plugin.json").read_text()
    )
    sk_entry_common = (
        claude_sk_entry.get("source") == "./plugins/sk"
        and cursor_sk_entry.get("source") == "sk"
    )
    # The sk cursor twin exposes skills ONLY: no hooks key, so Cursor never
    # loads sk's Claude-shaped hooks.json, and no "Claude Code" wording.
    sk_twin_common = (
        cursor_sk.get("name") == claude_sk.get("name")
        and cursor_sk.get("version") == claude_sk.get("version")
        and cursor_sk.get("skills") == "./skills"
        and "hooks" not in cursor_sk
        and "Claude Code" not in cursor_sk.get("description", "")
    )
    check("Cursor marketplace mirrors shared metadata and lists crew and sk",
          marketplace_common and cursor_marketplace_description
          and len(cursor_marketplace["plugins"]) == 2
          and entry_common and sk_entry_common,
          "shared marketplace metadata, metadata description, crew and sk entries",
          repr(cursor_marketplace))
    check("Cursor crew manifest mirrors shared plugin identity and version",
          plugin_common and cursor_description,
          "shared plugin fields and cursor description", repr(cursor_crew))
    check("Cursor sk manifest mirrors identity, skills-only, no hooks",
          sk_twin_common,
          "sk name/version parity, skills path, hookless", repr(cursor_sk))


def test_cursor_mirror_bump_behavior():
    log_section("Cursor version mirrors")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: crew without cursor mirror")
        proc = run_hook(repo)
        check("missing Cursor twins remain a silent no-op",
              proc.returncode == 0
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.1"
              and not (repo / ".cursor-plugin").exists()
              and not (repo / "plugins" / "crew" / ".cursor-plugin").exists(),
              "original bump only and no Cursor paths", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: crew source")
        proc = run_hook(repo)
        check("crew source change bumps both plugin manifests",
              proc.returncode == 0
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.1"
              and read_version(repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json") == "0.40.1",
              "both crew versions at 0.40.1", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        (repo / "plugins" / "sk" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: sk source")
        proc = run_hook(repo)
        check("sk source change bumps both sk manifests",
              proc.returncode == 0
              and read_version(repo / "plugins" / "sk" / ".claude-plugin" / "plugin.json") == "0.3.1"
              and read_version(repo / "plugins" / "sk" / ".cursor-plugin" / "plugin.json") == "0.3.1",
              "both sk versions at 0.3.1", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        cursor_sk = repo / "plugins" / "sk" / ".cursor-plugin" / "plugin.json"
        cursor_sk.write_text(cursor_sk.read_text().replace(
            '"description": "test cursor sk"',
            '"description": "hand-edited cursor sk"',
        ))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: cursor sk metadata")
        proc = run_hook(repo)
        check("Cursor-only sk twin change bumps both sk manifests",
              proc.returncode == 0
              and read_version(repo / "plugins" / "sk" / ".claude-plugin" / "plugin.json") == "0.3.1"
              and read_version(cursor_sk) == "0.3.1",
              "both sk versions at 0.3.1", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        cursor_plugin = repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json"
        cursor_plugin.write_text(cursor_plugin.read_text().replace(
            '"description": "test cursor crew"',
            '"description": "hand-edited cursor crew"',
        ))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: cursor plugin metadata")
        proc = run_hook(repo)
        check("Cursor-only plugin metadata change bumps both crew manifests",
              proc.returncode == 0
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.1"
              and read_version(cursor_plugin) == "0.40.1",
              "both crew versions at 0.40.1", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        cursor_marketplace = repo / ".cursor-plugin" / "marketplace.json"
        cursor_marketplace.write_text(cursor_marketplace.read_text().replace(
            '"description": "test marketplace"',
            '"description": "hand-edited cursor marketplace"',
        ))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: cursor marketplace metadata")
        proc = run_hook(repo)
        check("Cursor-only marketplace metadata change bumps both marketplace manifests",
              proc.returncode == 0
              and read_version(repo / ".claude-plugin" / "marketplace.json") == "0.19.3"
              and read_version(cursor_marketplace) == "0.19.3",
              "both marketplace versions at 0.19.3", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        cursor_plugin = repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json"
        cursor_plugin.write_text(cursor_plugin.read_text().replace(
            '"version": "0.40.0"', '"version": "0.40.1"'
        ))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: cursor mirror write")
        before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        proc = run_hook(repo)
        after_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        check("version-only Cursor mirror hand-write does not re-bump",
              proc.returncode == 0 and before_head == after_head
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.0"
              and read_version(cursor_plugin) == "0.40.1",
              "no bump commit and original remains 0.40.0", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        cursor_plugin = repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json"
        before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        cursor_plugin.write_text(cursor_plugin.read_text().replace(
            '"description": "test cursor crew"',
            '"description": "dirty cursor crew"',
        ))
        proc = run_hook(repo)
        check("dirty Cursor twin edit does not trigger a bump",
              proc.returncode == 0
              and _git(repo, "rev-parse", "HEAD").stdout.strip() == before_head
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.0"
              and read_version(cursor_plugin) == "0.40.0",
              "dirty tree ignored and versions unchanged", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        (repo / "README.md").write_text("# changed\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: source and readme")
        cursor_marketplace = repo / ".cursor-plugin" / "marketplace.json"
        cursor_plugin = repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json"
        cursor_marketplace.parent.mkdir(parents=True)
        cursor_plugin.parent.mkdir(parents=True)
        cursor_marketplace.write_text(CURSOR_MARKETPLACE_JSON)
        cursor_plugin.write_text(CURSOR_CREW_PLUGIN_JSON)
        marketplace_bytes = cursor_marketplace.read_bytes()
        plugin_bytes = cursor_plugin.read_bytes()
        proc = run_hook(repo)
        staged = _git(repo, "diff", "--cached", "--name-only").stdout.strip()
        check("untracked Cursor twins are not rewritten or staged by a bump",
              proc.returncode == 0
              and cursor_marketplace.read_bytes() == marketplace_bytes
              and cursor_plugin.read_bytes() == plugin_bytes
              and _git(repo, "ls-files", "--error-unmatch", ".cursor-plugin/marketplace.json").returncode != 0
              and _git(repo, "ls-files", "--error-unmatch", "plugins/crew/.cursor-plugin/plugin.json").returncode != 0
              and staged == ""
              and read_version(repo / ".claude-plugin" / "marketplace.json") == "0.19.3"
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.1",
              "originals bumped, untracked twins unchanged and unstaged",
              f"rc={proc.returncode} staged={staged!r}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td))
        cursor_marketplace = repo / ".cursor-plugin" / "marketplace.json"
        cursor_plugin = repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json"
        cursor_marketplace.parent.mkdir(parents=True)
        cursor_plugin.parent.mkdir(parents=True)
        cursor_marketplace.write_text(CURSOR_MARKETPLACE_JSON)
        cursor_plugin.write_text(CURSOR_CREW_PLUGIN_JSON)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: add cursor mirrors")
        proc = run_hook(repo)
        check("new Cursor twins count as substantive changes",
              proc.returncode == 0
              and read_version(repo / ".claude-plugin" / "marketplace.json") == "0.19.3"
              and read_version(cursor_marketplace) == "0.19.3"
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.1"
              and read_version(cursor_plugin) == "0.40.1",
              "new twins and originals bumped", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        (repo / ".cursor-plugin" / "marketplace.json").unlink()
        (repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: remove cursor mirrors")
        proc = run_hook(repo)
        check("deleted Cursor twins count as substantive changes",
              proc.returncode == 0
              and read_version(repo / ".claude-plugin" / "marketplace.json") == "0.19.3"
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.1"
              and not (repo / ".cursor-plugin" / "marketplace.json").exists()
              and not (repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json").exists(),
              "originals bumped after both twin deletions", f"rc={proc.returncode}")

    with tempfile.TemporaryDirectory() as td:
        repo = make_repo(Path(td), twins=True)
        cursor_plugin = repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json"
        cursor_plugin.write_text("{not-json\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: malformed cursor mirror")
        proc = run_hook(repo)
        check("malformed Cursor twin counts as substantive and is named",
              proc.returncode != 0
              and "cannot parse twin" in proc.stderr
              and "plugins/crew/.cursor-plugin/plugin.json" in proc.stderr
              and read_version(repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json") == "0.40.0"
              and cursor_plugin.read_text() == "{not-json\n",
              "parse note, nonzero rollback, and unchanged original", f"rc={proc.returncode}")


def test_cursor_snapshot_rollback():
    log_section("Cursor mirror rollback")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = make_repo(tmp, twins=True)
        (repo / "plugins" / "crew" / "src.txt").write_text("v1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "fix: rollback mirrors")
        paths = (
            repo / ".claude-plugin" / "marketplace.json",
            repo / ".cursor-plugin" / "marketplace.json",
            repo / "plugins" / "crew" / ".claude-plugin" / "plugin.json",
            repo / "plugins" / "crew" / ".cursor-plugin" / "plugin.json",
        )
        before = {path: path.read_bytes() for path in paths}
        proc = run_hook(repo, env=env_with_fake_git(make_fake_git_dir(tmp)))
        after = {path: path.read_bytes() for path in paths}
        staged = _git(repo, "diff", "--cached", "--name-only").stdout.strip()
        check("Cursor mirror commit failure restores all four JSON snapshots",
              proc.returncode != 0 and before == after,
              "nonzero and all four files byte-identical", f"rc={proc.returncode}")
        check("Cursor mirror rollback leaves the index clean", staged == "",
              "empty staged path list", repr(staged))


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
    test_breaking_change_footer_anchoring()
    test_skip_conditions()
    test_baseline_not_poisoned_by_body()
    test_git_state_guards()
    test_malformed_version_aborts()
    test_snapshot_restore_and_no_staging()
    test_only_changed_plugin_bumps()
    test_cursor_manifest_parity()
    test_cursor_mirror_bump_behavior()
    test_cursor_snapshot_rollback()
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
