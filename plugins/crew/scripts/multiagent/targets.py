"""Target resolution: a plan ``.md`` file or a git diff.

A *target* is what the panel reviews. ``resolve()`` returns a ``Target`` with
the text content plus a small descriptor (kind / scope) for the prompt header.

Diff target kinds:
  * ``working-tree`` — uncommitted changes (staged + unstaged) to tracked
    files, plus untracked (non-ignored) files rendered as new-file diffs so
    brand-new files are reviewed too.
  * ``branch``       — diff vs the merge-base with ``--base`` (default ``main``).
  * ``range``        — an ``A..B`` / ``A...B`` ref range (SHAs pinned).
  * ``commit``       — a single SHA (``<sha>^..<sha>``).
  * ``auto``         — working-tree if dirty, else last commit vs base.

Untracked files are included read-only via ``git ls-files --others
--exclude-standard -z`` + ``git diff --no-index`` — no index/ref/working-tree
mutation. All git commands run through ``--no-pager`` to avoid TTY hangs.

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
        # --no-pager defends against a TTY pager hanging our captured calls.
        return subprocess.run(
            ["git", "--no-pager", *args],
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


def _untracked_files(cwd: str | None = None) -> list[str]:
    """List untracked, non-ignored files (read-only).

    Uses ``-z`` so paths with spaces or unusual characters survive intact
    (NUL-separated, never shell-quoted).
    """
    cp = _git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd)
    if cp.returncode != 0:
        return []
    return [f for f in cp.stdout.split("\0") if f]


def _untracked_diff(cwd: str | None = None) -> str:
    """Render untracked files as new-file diffs (read-only).

    ``git diff --no-index -- /dev/null <file>`` produces a full add-diff for
    each untracked file without touching the index or working tree. That
    command exits 1 whenever the files differ (always true vs ``/dev/null``),
    so the return code is intentionally ignored and only stdout is collected.
    """
    parts: list[str] = []
    for f in _untracked_files(cwd):
        cp = _git(["diff", "--no-index", "--", "/dev/null", f], cwd=cwd)
        if cp.stdout:
            parts.append(cp.stdout)
    return "".join(parts)


# An unborn repo has no HEAD; diff staged content against git's empty tree
# instead. `$(git hash-object -t tree /dev/null)` yields the empty-tree object
# for the repo's hash algorithm (sha1 OR sha256) — correct without hardcoding.
_EMPTY_TREE_BASE = "$(git hash-object -t tree /dev/null)"


def _worktree_diff_cmd(base: str) -> str:
    """A seat-reproducible command for a COMPLETE working-tree diff vs ``base``.

    Tracked changes vs ``base`` PLUS untracked (non-ignored) files rendered as
    new-file diffs. ``-z`` + a NUL-delimited read loop keeps paths with spaces
    or newlines intact (matching the Python-side ``_untracked_files``). The
    ``--no-index`` calls exit 1 whenever content differs (always, for a new file)
    — expected and harmless. Fully read-only (no index/ref/tree mutation).
    NB: the read loop assumes a bash/zsh-compatible shell (`read -d`).
    """
    return (
        f"git --no-pager diff {base}; "
        "git ls-files --others --exclude-standard -z | "
        'while IFS= read -r -d "" f; do git --no-pager diff --no-index -- /dev/null "$f"; done'
    )


# The common case: a committed HEAD is present.
_WORKTREE_DIFF_CMD = _worktree_diff_cmd("HEAD")


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
    untracked = _untracked_files(cwd)
    if untracked:
        listed = ", ".join(untracked[:20])
        more = "" if len(untracked) <= 20 else f" (+{len(untracked) - 20} more)"
        return [f"{len(untracked)} untracked file(s) included as new-file diffs: {listed}{more}"]
    return []


def _resolve_working_tree(cwd: str | None) -> Target:
    # No commits yet: there is no HEAD to diff against; everything is new.
    if not _has_commits(cwd):
        notes = ["no commits yet (unborn HEAD); all tracked content is new"]
        # Diff the empty tree against the WORKING TREE (not the index): `git diff
        # <tree>` is working-tree semantics, deliberately matching the committed
        # `git diff HEAD` path — NO `--cached`. Consequences of that decision: a
        # file staged-then-modified shows its WORKING-TREE version (not the staged
        # one); a file staged-then-deleted is omitted (it isn't in the working
        # tree). Untracked (non-ignored) files are appended as new-file diffs.
        empty_tree = _git(["hash-object", "-t", "tree", "/dev/null"], cwd=cwd)
        content = ""
        if empty_tree.returncode == 0:
            tree = empty_tree.stdout.strip()
            dcp = _git(["diff", tree], cwd=cwd)
            if dcp.returncode == 0:
                content = dcp.stdout
        untracked = _untracked_files(cwd)
        content += _untracked_diff(cwd)
        notes += _untracked_note(cwd)
        if not content.strip():
            notes.append("working tree is empty (nothing to review)")
        # No HEAD here — the reproduced command MUST NOT use `git diff HEAD`
        # (that fails in an unborn repo and would omit staged initial files).
        # Diff staged content against the empty tree, plus untracked files.
        if untracked:
            diff_cmd = _worktree_diff_cmd(_EMPTY_TREE_BASE)
        elif content.strip():
            diff_cmd = f"git --no-pager diff {_EMPTY_TREE_BASE}"
        else:
            diff_cmd = None
        return Target(
            kind="code",
            scope="working-tree (no commits yet)",
            content=content,
            descriptor="code: working-tree (no commits yet)",
            notes=notes,
            diff_cmd=diff_cmd,
        )

    # Full uncommitted diff (staged + unstaged) to tracked files, plus
    # untracked (non-ignored) files rendered as new-file diffs so brand-new
    # files are reviewed too — not merely listed.
    cp = _git(["diff", "HEAD"], cwd=cwd)
    if cp.returncode != 0:
        raise TargetError(f"git diff failed: {cp.stderr.strip()}")

    untracked = _untracked_files(cwd)
    tracked = cp.stdout
    content = tracked + _untracked_diff(cwd)
    notes = []
    if not tracked.strip():
        notes.append("working tree has no tracked changes")
    notes += _untracked_note(cwd)
    if not content.strip():
        notes = ["working tree is clean (nothing to review)"]
    return Target(
        kind="code",
        scope="working-tree",
        content=content,
        descriptor="code: working-tree (uncommitted, staged + unstaged)",
        notes=notes,
        # When untracked files are present, plain `git diff HEAD` would miss
        # them — hand the seat the compound command that includes them.
        # --no-pager guards against a TTY pager hang on a large diff.
        diff_cmd=_WORKTREE_DIFF_CMD if untracked else "git --no-pager diff HEAD",
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


def _resolve_ref_range(spec: str, cwd: str | None) -> Target:
    """Resolve an ``A..B`` or ``A...B`` ref range to a diff.

    Both endpoints are pinned to SHAs so the seat reproduces exactly this range
    deterministically, even as branches advance.
    """
    sep = "..." if "..." in spec else ".."
    parts = spec.split(sep, 1)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise TargetError(f"invalid ref range: {spec!r}")
    left, right = parts[0].strip(), parts[1].strip()

    def _verify(ref: str) -> str:
        cp = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=cwd)
        if cp.returncode != 0:
            raise TargetError(f"could not resolve ref {ref!r}: {cp.stderr.strip()}")
        return cp.stdout.strip()

    diff_arg = f"{_verify(left)}{sep}{_verify(right)}"
    cp = _git(["diff", diff_arg], cwd=cwd)
    if cp.returncode != 0:
        raise TargetError(f"git diff {diff_arg} failed: {cp.stderr.strip()}")
    notes = []
    if not cp.stdout.strip():
        notes.append(f"empty diff for range {spec!r} (zero-change target)")
    return Target(
        kind="code",
        scope=f"range {spec}",
        content=cp.stdout,
        descriptor=f"code: diff {spec}",
        notes=notes,
        diff_cmd=f"git --no-pager diff {diff_arg}",
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
    an ``A..B`` / ``A...B`` ref range, ``commit:<sha>`` (or a bare SHA), or
    ``auto``.
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
    # A ref range (``A..B`` / ``A...B``) — checked before the commit fallback
    # since none of the named kinds above contain "..".
    if ".." in spec:
        return _resolve_ref_range(spec, cwd)
    if spec.startswith("commit:"):
        return _resolve_commit(spec.split(":", 1)[1], cwd)
    # A bare value that isn't a known kind: treat as a commit-ish SHA.
    return _resolve_commit(spec, cwd)
