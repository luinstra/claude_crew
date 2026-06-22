"""Target resolution: a plan ``.md`` file or a git diff.

A *target* is what the panel reviews. ``resolve()`` returns a ``Target`` with
the text content plus a small descriptor (kind / scope) for the prompt header.

Diff target kinds:
  * ``working-tree`` — uncommitted changes (staged + unstaged) to tracked
    files; untracked files are surfaced by PATH in the descriptor.
  * ``branch``       — diff vs the merge-base with ``--base`` (default ``main``).
  * ``commit``       — a single SHA (``<sha>^..<sha>``).
  * ``auto``         — working-tree if dirty, else last commit vs base.

Git edge cases (detached HEAD, no commits yet, shallow clone, base-ahead) each
return a clear message rather than a stack trace.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


class TargetError(Exception):
    """Raised with a clear, user-facing message (never a raw traceback)."""


@dataclass
class Target:
    kind: str            # "plan" | "code"
    scope: str           # human-readable scope, e.g. "working-tree", path
    content: str         # the text to review (plan body or diff)
    descriptor: str      # one-line header for the prompt
    notes: list[str] = field(default_factory=list)  # untracked files, warnings
    # How a seat reproduces this target ITSELF (reference mode — the default):
    #   * code targets — the exact git command that prints the diff, with SHAs
    #     resolved so it is deterministic (e.g. ``git diff <mergebase>..HEAD``).
    #   * plan targets — the file path to open (``ref_path``).
    # ``content`` is still computed (for ``--inline-diff`` and for empty-diff /
    # error detection), but in reference mode it is NOT shipped to the seats —
    # they run ``diff_cmd`` / read ``ref_path`` in the repo they already sit in.
    diff_cmd: str | None = None
    ref_path: str | None = None


# =============================================================================
# git helpers
# =============================================================================

def _git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TargetError(
            "git executable not found on PATH; a code review requires git"
        ) from exc


def _in_git_repo(cwd: str | None = None) -> bool:
    cp = _git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return cp.returncode == 0 and cp.stdout.strip() == "true"


def _has_commits(cwd: str | None = None) -> bool:
    cp = _git(["rev-parse", "--verify", "HEAD"], cwd=cwd)
    return cp.returncode == 0


def _is_dirty(cwd: str | None = None) -> bool:
    cp = _git(["status", "--porcelain"], cwd=cwd)
    return cp.returncode == 0 and bool(cp.stdout.strip())


def _untracked(cwd: str | None = None) -> list[str]:
    cp = _git(
        ["ls-files", "--others", "--exclude-standard"], cwd=cwd
    )
    if cp.returncode != 0:
        return []
    return [line for line in cp.stdout.splitlines() if line.strip()]


def _merge_base(base: str, cwd: str | None = None) -> str | None:
    cp = _git(["merge-base", base, "HEAD"], cwd=cwd)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


# =============================================================================
# plan target
# =============================================================================

def _resolve_plan(path: str) -> Target:
    p = Path(path)
    if not p.exists():
        raise TargetError(f"plan file not found: {path}")
    if not p.is_file():
        raise TargetError(f"plan target is not a file: {path}")
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TargetError(f"could not read plan file {path}: {exc}") from exc
    return Target(
        kind="plan",
        scope=str(p),
        content=content,
        descriptor=f"plan: {p}",
        ref_path=str(p),
    )


# =============================================================================
# diff targets
# =============================================================================

def _untracked_note(cwd: str | None) -> list[str]:
    untracked = _untracked(cwd)
    if untracked:
        listed = ", ".join(untracked[:20])
        more = "" if len(untracked) <= 20 else f" (+{len(untracked) - 20} more)"
        return [f"untracked files present (not diffed): {listed}{more}"]
    return []


def _resolve_working_tree(cwd: str | None) -> Target:
    # No commits yet: there is no HEAD to diff against; everything is new.
    if not _has_commits(cwd):
        notes = ["no commits yet (unborn HEAD); all tracked content is new"]
        # Show staged content as a diff against the empty tree if possible.
        empty_tree = _git(["hash-object", "-t", "tree", "/dev/null"], cwd=cwd)
        content = ""
        if empty_tree.returncode == 0:
            tree = empty_tree.stdout.strip()
            dcp = _git(["diff", tree], cwd=cwd)
            if dcp.returncode == 0:
                content = dcp.stdout
        notes += _untracked_note(cwd)
        return Target(
            kind="code",
            scope="working-tree (no commits yet)",
            content=content,
            descriptor="code: working-tree (no commits yet)",
            notes=notes,
            # No HEAD to diff against — instruct the seat to inspect the tree.
            diff_cmd=None,
        )

    # Full uncommitted diff (staged + unstaged) to tracked files.
    cp = _git(["diff", "HEAD"], cwd=cwd)
    if cp.returncode != 0:
        raise TargetError(f"git diff failed: {cp.stderr.strip()}")

    notes = _untracked_note(cwd)
    content = cp.stdout
    if not content.strip():
        notes.append("working tree has no tracked changes (empty diff)")
    return Target(
        kind="code",
        scope="working-tree",
        content=content,
        descriptor="code: working-tree (uncommitted, staged + unstaged)",
        notes=notes,
        # --no-pager: a seat reproducing this in a TTY shell would otherwise
        # page a large diff through `less` and hang to its timeout.
        diff_cmd="git --no-pager diff HEAD",
    )


def _resolve_branch(base: str, cwd: str | None) -> Target:
    if not _has_commits(cwd):
        raise TargetError(
            "no commits yet (unborn HEAD); cannot diff a branch — review the "
            "working tree instead"
        )
    mb = _merge_base(base, cwd)
    if mb is None:
        raise TargetError(
            f"could not resolve merge-base with {base!r}; the base ref may be "
            f"missing (shallow clone?) — try a deeper fetch or pass a valid "
            f"--base, or review the working tree"
        )
    cp = _git(["diff", f"{mb}..HEAD"], cwd=cwd)
    if cp.returncode != 0:
        raise TargetError(f"git diff {mb}..HEAD failed: {cp.stderr.strip()}")
    notes = []
    if not cp.stdout.strip():
        notes.append(
            f"empty diff vs merge-base with {base!r} (HEAD may be at or behind "
            f"the base) — zero-change target"
        )
    return Target(
        kind="code",
        scope=f"branch vs {base}",
        content=cp.stdout,
        descriptor=f"code: branch diff vs merge-base({base})",
        notes=notes,
        # Merge-base SHA is pinned, so this stays deterministic even as the base
        # branch advances. --no-pager guards against a TTY pager hang.
        diff_cmd=f"git --no-pager diff {mb}..HEAD",
    )


def _resolve_commit(sha: str, cwd: str | None) -> Target:
    verify = _git(["rev-parse", "--verify", f"{sha}^{{commit}}"], cwd=cwd)
    if verify.returncode != 0:
        raise TargetError(f"could not resolve commit {sha!r}: {verify.stderr.strip()}")
    resolved = verify.stdout.strip()
    # Diff the commit against its first parent; falls back to empty-tree for the
    # root commit.
    diff_cmd = f"git --no-pager diff {resolved}^!"
    cp = _git(["diff", f"{resolved}^!"], cwd=cwd)
    if cp.returncode != 0:
        # Root commit (no parent): diff against the empty tree.
        empty = _git(["hash-object", "-t", "tree", "/dev/null"], cwd=cwd)
        if empty.returncode == 0:
            tree = empty.stdout.strip()
            cp = _git(["diff", tree, resolved], cwd=cwd)
            diff_cmd = f"git --no-pager diff {tree} {resolved}"
        if cp.returncode != 0:
            raise TargetError(f"git diff for commit {sha!r} failed: {cp.stderr.strip()}")
    return Target(
        kind="code",
        scope=f"commit {resolved[:12]}",
        content=cp.stdout,
        descriptor=f"code: commit {resolved[:12]}",
        # Full SHA so the seat reproduces exactly this commit's diff.
        diff_cmd=diff_cmd,
    )


def _resolve_auto(base: str, cwd: str | None) -> Target:
    if _is_dirty(cwd):
        return _resolve_working_tree(cwd)
    if not _has_commits(cwd):
        # Clean and no commits — nothing committed, fall back to working-tree
        # (which will report the empty/new state clearly).
        return _resolve_working_tree(cwd)
    # Clean: last commit vs base merge-base.
    mb = _merge_base(base, cwd)
    if mb is None:
        # No resolvable base (e.g. detached HEAD with no base, shallow clone):
        # fall back to last commit vs its parent.
        return _resolve_commit("HEAD", cwd)
    return _resolve_branch(base, cwd)


# =============================================================================
# public entry point
# =============================================================================

def resolve(
    spec: str = "auto",
    *,
    base: str = "main",
    cwd: str | None = None,
) -> Target:
    """Resolve a target spec to a ``Target``.

    ``spec`` is one of: a path to a ``.md`` plan, ``working-tree``, ``branch``,
    ``commit:<sha>`` (or a bare SHA), or ``auto``.
    """
    # Plan target: an explicit .md path.
    if spec.endswith(".md"):
        return _resolve_plan(spec)

    # Everything else is a code/diff target and requires a git repo.
    if not _in_git_repo(cwd):
        raise TargetError(
            "code review requires a git repository, but the current directory "
            "is not inside one"
        )

    if spec in ("auto", ""):
        return _resolve_auto(base, cwd)
    if spec == "working-tree":
        return _resolve_working_tree(cwd)
    if spec == "branch":
        return _resolve_branch(base, cwd)
    if spec.startswith("commit:"):
        return _resolve_commit(spec.split(":", 1)[1], cwd)
    # A bare value that isn't a known kind: treat as a commit-ish SHA.
    return _resolve_commit(spec, cwd)
