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

from state_discovery import crew_base  # the ONE `.crew`/project-root resolver


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
    # The BARE, replayable ``resolve()`` input that reproduces THIS resolution:
    # the plan path itself, ``working-tree``, ``branch``, ``commit:<full-sha>``,
    # or ``<shaA>..<shaB>`` (endpoints pinned). Each resolver populates it with
    # its pinned canonical form as it resolves, so ``auto``, short SHAs, and
    # branch-name ranges are normalized BEFORE anything persists the spec
    # (never a labeled/descriptor form, never ``auto``).
    replay_spec: str = ""
    # Set by review-prep ONLY (never by a resolver): the run dir's frozen
    # snapshot of ``content``. When set, the prompt builder makes the snapshot
    # the reviewed content and demotes ``ref_path``/``diff_cmd`` (still the
    # LIVE target) to supplementary context.
    snapshot_path: str | None = None


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


def _is_engine_artifact(path: str) -> bool:
    # The engine writes its own state, run dirs, and signals under ``.crew/``.
    # A working-tree review that included them would re-hash on every run (the
    # panel's own artifacts land mid-review), so completion would warn on the
    # engine's own bookkeeping. Excluded here so drift reflects reviewed content.
    return path == ".crew" or path.startswith(".crew/")


def _untracked_partition(
    cwd: str | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """The SINGLE enumeration of the working-tree target's untracked entries.

    Every consumer of "what is untracked here" derives from THIS one call, so the
    three cannot disagree on what the snapshot holds: ``_is_dirty`` (does ``auto``
    pick the working tree), the content/hash the panel reviews, and the shell
    command a reference-mode seat replays (``_worktree_diff_cmd`` mirrors the same
    exclusions). Extend HERE, never add a fourth view.

    Returns ``(included, excluded)``: ``included`` are renderable new-file paths;
    ``excluded`` are ``(path, reason)`` pairs kept OUT of the content and NAMED in
    the notes, never silently claimed as reviewed. ``-z`` keeps paths with spaces
    or newlines intact (NUL-separated, never shell-quoted).

    Two kinds never reach the content:
      * engine artifacts under ``.crew/`` are dropped SILENTLY (they land
        mid-review, so naming or hashing them would churn on the panel's own
        bookkeeping: documented policy, not a surprise worth a note).
      * an untracked DIRECTORY / nested repo / linked worktree, which ``git
        ls-files --others`` reports as one trailing-slash ``dir/`` entry, is
        NAMED as not-included: ``git diff --no-index /dev/null <dir>`` fails on it
        and renders no bytes, so a consumer must not claim it is in the snapshot.
    """
    cp = _git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd)
    if cp.returncode != 0:
        return [], []
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    for f in cp.stdout.split("\0"):
        if not f or _is_engine_artifact(f):
            continue
        if f.endswith("/"):
            excluded.append((f, "untracked directory / nested repo"))
        else:
            included.append(f)
    return included, excluded


def _has_tracked_changes(cwd: str | None = None) -> bool:
    """Quiet dirtiness predicate for tracked files (staged + unstaged): ``git diff
    --quiet`` reports via EXIT CODE (1 = differences) and captures no patch, so
    ``auto`` can test dirtiness without the heavy diff capture ``_resolve_working_tree``
    runs again during resolution. Working-tree semantics (no ``--cached``), same
    unborn-repo empty-tree base. Only exit 1 counts as dirty: exit 0 is clean and any
    other code is a git error, matching the old capture-then-``strip()`` predicate that
    treated a failed diff as not-dirty.

    No index refresh: `--quiet` skips it, so a STAT-dirty file (touched, bytes
    identical) can false-report as a difference here. That is harmless and left
    unguarded on purpose: `_resolve_auto` falls through to the committed diff
    whenever the resolved working tree carries no content, so a stat-dirty
    false-positive never becomes an empty auto target. Refreshing would WRITE
    index metadata during read-only target discovery, which this predicate must
    not do."""
    if _has_commits(cwd):
        cp = _git(["diff", "--quiet", "HEAD"], cwd=cwd)
        return cp.returncode == 1
    empty = _git(["hash-object", "-t", "tree", "/dev/null"], cwd=cwd)
    if empty.returncode != 0:
        return False
    cp = _git(["diff", "--quiet", empty.stdout.strip()], cwd=cwd)
    return cp.returncode == 1


def _is_dirty(cwd: str | None = None) -> bool:
    # DERIVED from the SAME enumeration the working-tree target is built from, so
    # `auto` never picks a working tree that resolves to EMPTY: a tree whose only
    # uncommitted content is filtered-out entries (`.crew/` artifacts, an untracked
    # nested repo) is NOT dirty, and `auto` falls through to the branch diff.
    # Dirty == the target would carry content: tracked changes OR at least one
    # INCLUDED (renderable) untracked file. No separate `.crew`-unaware git-status
    # view. The tracked check is the QUIET predicate (exit code, no patch): the full
    # capture happens once, inside `_resolve_working_tree`, when `auto` picks it.
    if _has_tracked_changes(cwd):
        return True
    included, _ = _untracked_partition(cwd)
    return bool(included)


def _untracked_diff(included: list[str], cwd: str | None = None) -> str:
    """Render the INCLUDED untracked files as new-file diffs (read-only).

    ``git diff --no-index -- /dev/null <file>`` produces a full add-diff for each
    without touching the index or working tree. That command exits 1 whenever the
    files differ (always true vs ``/dev/null``), so the return code is
    intentionally ignored and only stdout is collected. ``included`` comes from
    ``_untracked_partition`` (the ONE enumeration), so excluded entries never
    reach here.
    """
    parts: list[str] = []
    for f in included:
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
    or newlines intact (matching the Python-side ``_untracked_partition``). The
    ``--no-index`` calls exit 1 whenever content differs (always, for a new file)
    — expected and harmless. Fully read-only (no index/ref/tree mutation).
    NB: the read loop assumes a bash/zsh-compatible shell (`read -d`).

    The ``case`` skip mirrors ``_untracked_partition``'s exclusions so a seat
    replays the bytes the snapshot froze: engine artifacts under ``.crew/`` and
    untracked directories / nested repos (the trailing-slash entries ``--no-index``
    cannot render).
    """
    return (
        f"git --no-pager diff {base}; "
        "git ls-files --others --exclude-standard -z | "
        'while IFS= read -r -d "" f; do '
        'case "$f" in .crew|.crew/*|*/) continue ;; esac; '
        'git --no-pager diff --no-index -- /dev/null "$f"; done'
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

def _resolve_plan(path: str, cwd: str) -> Target:
    p = Path(path)
    # Anchor the filesystem read of a RELATIVE plan path to the same project root
    # the git targets use (crew_base when the caller passed no cwd), so a legacy
    # relative plan_file (`.crew/plans/x.md`) resolves under the project and the
    # snapshot freezes the RIGHT bytes even when the process cwd is a subdir or a
    # different repo. The emitted path fields keep the caller's original spec (a
    # relative supplementary ref resolves against the seat's anchored cwd), so the
    # normal-case prompt bytes are unchanged. An absolute plan path is untouched.
    resolved = p if p.is_absolute() else Path(cwd) / p
    if not resolved.exists():
        raise TargetError(f"plan file not found: {path}")
    if not resolved.is_file():
        raise TargetError(f"plan target is not a file: {path}")
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise TargetError(f"could not read plan file {path}: {exc}") from exc
    return Target(
        kind="plan",
        scope=str(p),
        content=content,
        descriptor=f"plan: {p}",
        ref_path=str(p),
        replay_spec=str(p),
    )


# =============================================================================
# diff targets
# =============================================================================

def _untracked_note(
    included: list[str], excluded: list[tuple[str, str]]
) -> list[str]:
    notes: list[str] = []
    if included:
        listed = ", ".join(included[:20])
        more = "" if len(included) <= 20 else f" (+{len(included) - 20} more)"
        notes.append(
            f"{len(included)} untracked file(s) included as new-file diffs: "
            f"{listed}{more}"
        )
    # Name what is present but deliberately NOT in the snapshot, so a reader is
    # never told an entry was reviewed when it contributed no bytes.
    for path, reason in excluded:
        notes.append(f"present but not included ({reason}): {path}")
    return notes


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
        included, excluded = _untracked_partition(cwd)
        content += _untracked_diff(included, cwd)
        notes += _untracked_note(included, excluded)
        if not content.strip():
            notes.append("working tree is empty (nothing to review)")
        # No HEAD here — the reproduced command MUST NOT use `git diff HEAD`
        # (that fails in an unborn repo and would omit staged initial files).
        # Diff staged content against the empty tree, plus untracked files.
        if included:
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
            replay_spec="working-tree",
        )

    # Full uncommitted diff (staged + unstaged) to tracked files, plus
    # untracked (non-ignored) files rendered as new-file diffs so brand-new
    # files are reviewed too — not merely listed.
    cp = _git(["diff", "HEAD"], cwd=cwd)
    if cp.returncode != 0:
        raise TargetError(f"git diff failed: {cp.stderr.strip()}")

    included, excluded = _untracked_partition(cwd)
    tracked = cp.stdout
    content = tracked + _untracked_diff(included, cwd)
    notes = []
    if not tracked.strip():
        notes.append("working tree has no tracked changes")
    notes += _untracked_note(included, excluded)
    if not content.strip():
        # Nothing renders, but a set-aside nested repo / linked worktree must still
        # be named: collapsing to a bare "clean" would tell a reviewer of an
        # all-excluded tree nothing was here, hiding what was deliberately left out.
        notes = ["working tree is clean (nothing to review)"] + _untracked_note(
            [], excluded
        )
    return Target(
        kind="code",
        scope="working-tree",
        content=content,
        descriptor="code: working-tree (uncommitted, staged + unstaged)",
        notes=notes,
        # When INCLUDED untracked files are present, plain `git diff HEAD` would
        # miss them: hand the seat the compound command that includes them.
        # --no-pager guards against a TTY pager hang on a large diff.
        diff_cmd=_WORKTREE_DIFF_CMD if included else "git --no-pager diff HEAD",
        replay_spec="working-tree",
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
        # ``branch`` is the replay spec; the base ref travels alongside it (the
        # caller records it separately), so a replay re-derives the merge-base.
        replay_spec="branch",
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
        replay_spec=f"commit:{resolved}",
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
        # Both endpoints pinned to full SHAs (diff_arg is the pinned form).
        replay_spec=diff_arg,
    )


def _resolve_auto(base: str, cwd: str | None) -> Target:
    if _is_dirty(cwd):
        wt = _resolve_working_tree(cwd)
        # The invariant that makes auto correct independent of the dirtiness
        # predicate: a working-tree target that resolves to NO content (e.g. a
        # stat-dirty file the predicate mistook for a real change) must never be
        # what auto returns, so fall through to the committed diff below.
        if wt.content.strip():
            return wt
    if not _has_commits(cwd):
        # Clean and no commits — nothing committed, fall back to working-tree
        # (which will report the empty/new state clearly, excluded entries named).
        return _resolve_working_tree(cwd)
    # Clean: last commit vs base merge-base.
    mb = _merge_base(base, cwd)
    if mb is None:
        # No resolvable base (e.g. detached HEAD with no base, shallow clone):
        # fall back to last commit vs its parent.
        target = _resolve_commit("HEAD", cwd)
    else:
        target = _resolve_branch(base, cwd)
    # Excluded entries (an untracked nested repo / linked worktree) carry no
    # bytes, so when they are the ONLY working-tree dirtiness auto skips the
    # working tree entirely. Name them on the committed-diff target anyway: the
    # promise that omitted directories are always named must hold here too, the
    # same way the working-tree empty-target path names them.
    _, excluded = _untracked_partition(cwd)
    if excluded:
        target.notes = target.notes + _untracked_note([], excluded)
    return target


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
    # Anchor git target resolution to the project root (crew_base: CLAUDE_PROJECT_DIR,
    # else cwd) when the caller passes no cwd. The run dir and loop identity anchor
    # there too, so a divergent process cwd must not resolve the diff against a
    # different repo or miss untracked files outside a subtree. A caller with a real
    # need (a test) still passes an explicit cwd; only the default changes.
    if cwd is None:
        cwd = str(crew_base())

    # Plan target: an explicit .md path (relative paths anchor to cwd above).
    if spec.endswith(".md"):
        return _resolve_plan(spec, cwd)

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
