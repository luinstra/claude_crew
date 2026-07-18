"""Enumerate stale crew artifacts safe to prune: ONE definition, two callers.

The `crew swab` command and the session-start reporter both need to answer the
same question ("which review-run and debate dirs are stale orphans?") under the
same safety rules. This module is that single answer, so the deliberate command
and the unattended reporter can never disagree on what is prunable.

It ENUMERATES and validates only; it never deletes. The caller decides whether to
act on the list (swab with --yes) or merely report it. Keeping deletion out of the
enumerator is deliberate: the file class this touches has the worst data-loss
history in the repo, so the validation lives in one place and the destructive step
is the caller's explicit choice.

Sits at the scripts root (beside state_discovery) so BOTH session-start.py (scripts
root) and multiagent.cli (scripts on sys.path) import it as a top-level module.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from state_discovery import is_active_value, is_loop_state_file
from multiagent.review_runs import (
    POINTER_NAME,
    RUN_JSON_NAME,
    read_pointer_run_id,
    session_segment,
)

# This MUST track what review_runs mints, NOT the permissive shared RUN_ID_RE.
# `mint_identity` returns "run-" + sha256(spec)[:12]: the prefix plus EXACTLY 12
# lowercase hex chars. The engine-wide `rounds.RUN_ID_RE` (`run-[A-Za-z0-9_-]+`)
# is a TRAVERSAL guard shared with debate rounds, not an identity: it also admits
# `run-scratch` and `run-my_notes`, names a user can park under .crew/reviews/ and
# the engine never mints. A destructive sweep validates against the mint, not a
# guard. If review_runs ever changes the truncation length or charset, change this.
MINTED_RUN_DIR_RE = re.compile(r"^run-[0-9a-f]{12}$")

# Coarse debate-dir ownership heuristic: a `run-` or `YYYYMMDD-HHMMSS` PREFIX. This
# is a LOOSE prefix match, not proof the engine minted the name (it also admits a
# user-parked `run-notes` or `20250101-000000-scratch`). Tightening the grammar was
# deliberately declined: for debates the real safety is elsewhere, not this regex.
# The destructive step is the ATTENDED `crew swab` dry-run (the human reads the list
# before `--yes`), and enumeration further requires the 1-day staleness threshold
# plus NO synthesis.md (a completed debate is a decision record, kept at any age).
DEBATE_DIR_RE = re.compile(r"^(run-|\d{8}-\d{6})")

# Debate-staleness threshold (1 day): a live debate mid-write is never a candidate.
# Review runs carry NO age (the human reads the orphan list); debates keep this
# 1-day age contract.
DEBATE_STALE_SECONDS = 86400

REVIEWS_SUBDIR = "reviews"
DEBATES_SUBDIR = "debates"


@dataclass
class Prunable:
    """One artifact dir the caller may remove, with its on-disk size when measured.

    ``kind`` is "review-run" or "debate"; ``name`` is the dir's own name (the run
    id for a review run); ``bytes`` is the byte total of the subtree (symlinks NOT
    followed) when the caller asked for sizing, or 0 when enumerated with
    ``with_sizes=False`` (the count-only path that skips the walk).
    ``session_dir`` is the parent under which a stale pointer may need dropping
    when a review run is removed.
    """
    kind: str
    path: Path
    name: str
    bytes: int
    session_dir: Path


def own_dir(path: Path) -> bool:
    """True only for a REAL directory, never a symlink to one.

    A destructive rmtree over ``is_dir()`` (which RESOLVES a symlink) would follow
    a link named like a generated dir to whatever it targets, anywhere on disk. The
    engine mkdirs its dirs and never links one, so a symlink is by definition not
    ours to touch.
    """
    try:
        return path.is_dir() and not path.is_symlink()
    except OSError:
        return False


def _resolved_within(path: Path, root: Path) -> bool:
    """True only if ``path``'s resolved real target stays inside ``root``.

    Belt-and-suspenders past ``own_dir``'s per-segment symlink refusal: verify the
    fully-resolved candidate cannot escape the reviews/debates root even through a
    segment we did not walk ourselves.
    """
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def dir_size(path: Path) -> int:
    """Byte total of a subtree, NEVER following a symlink (lstat on every entry).

    A symlinked file counts as the link's own size, not its target's, so accounting
    can never wander off the subtree it is measuring.
    """
    total = 0
    for root, dirnames, filenames in os.walk(str(path), followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
        for name in list(dirnames):
            # A symlinked subdir is not descended (followlinks=False), so count its
            # own link size here or it would be missed entirely.
            full = os.path.join(root, name)
            try:
                if os.path.islink(full):
                    total += os.lstat(full).st_size
            except OSError:
                continue
    return total


def live_run_keys(crew_dir: Path) -> set:
    """``(session segment, run_id)`` pairs frozen in a still-ACTIVE loop state.

    A live loop's frozen run is never prunable, at any age: every bound is
    evaluated on a Stop fire, so a session parked asleep is not deadline-bounded
    and "old" does not imply "dead". Read raw (not through the schema-validating
    reader): this set only PROTECTS, so a newer-schema state file must still shield
    its run dir. The session id goes through the reviews-dir sanitizer, the segment
    the run dir was actually created under.
    """
    import json

    keys = set()
    for json_file in crew_dir.glob("*.json"):
        if not is_loop_state_file(json_file.name):
            continue
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if not is_active_value(data.get("active", False)):
            continue
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not MINTED_RUN_DIR_RE.match(run_id):
            continue
        keys.add((session_segment(data.get("session_id") or ""), run_id))
    return keys


def pointer_protected_keys(reviews_root: Path) -> set:
    """``(session segment, run_id)`` pairs named by a ``current-run.json`` POINTER.

    The pointer means "this is the run in play", so a run it names is NOT an
    orphan even when no loop state records it: it protects an in-flight standalone
    ``crew review`` (no loop) and a loop run freshly prepped BEFORE begin-review
    freezes it into loop state. Both are named by no loop yet, so without this
    their run dir is immediately prunable and a ``--yes`` mid-review would delete
    the run currently executing. Covers the flat ``.crew/reviews/current-run.json``
    (segment "") and every session ``.crew/reviews/<sid>/current-run.json``. The id
    is read through ``review_runs.read_pointer_run_id`` so its grammar is validated
    the same way every other pointer reader validates it.
    """
    keys = set()
    flat = read_pointer_run_id(reviews_root)
    if flat:
        keys.add(("", flat))
    for session_dir in reviews_root.iterdir():
        if not own_dir(session_dir):
            continue
        rid = read_pointer_run_id(session_dir)
        if rid:
            keys.add((session_dir.name, rid))
    return keys


def _review_runs_under(
    session_dir: Path,
    segment: str,
    protected: set,
    reviews_root: Path,
    with_sizes: bool = True,
) -> list[Prunable]:
    """Prunable review-run dirs directly under ``session_dir``.

    ``segment`` is the sanitized session id these dirs were minted under (the empty
    string for the FLAT sessionless layout, whose run dirs sit at the reviews root).
    A dir is prunable only when ALL hold: its name matches the EXACT mint grammar;
    it is a real dir (not a symlink, and its resolved path stays inside reviews/);
    it holds a present ``run.json`` marker file (presence, not contents: an
    unreadable record still means crew wrote the dir); and its run id is NOT in
    ``protected`` (named by an active loop OR by a current-run pointer).

    ``with_sizes=False`` keeps every validation but SKIPS the ``dir_size`` walk and
    records ``bytes=0``: a count-only caller (the session-start reporter) needs the
    accurate orphan set without paying an unbounded recursive sizing per session
    start. Exact sizing stays the default for the attended swab.
    """
    out: list[Prunable] = []
    for entry in session_dir.glob("run-*"):
        if not own_dir(entry) or not MINTED_RUN_DIR_RE.match(entry.name):
            continue
        if not _resolved_within(entry, reviews_root):
            continue
        run_json = entry / RUN_JSON_NAME
        try:
            if not run_json.is_file():
                continue
        except OSError:
            continue
        if (segment, entry.name) in protected:
            continue
        out.append(
            Prunable(
                kind="review-run",
                path=entry,
                name=entry.name,
                bytes=dir_size(entry) if with_sizes else 0,
                session_dir=session_dir,
            )
        )
    return out


def prunable_review_runs(crew_dir: Path, with_sizes: bool = True) -> list[Prunable]:
    """Every prunable review-run dir under ``.crew/reviews/``.

    Covers BOTH the flat/sessionless ``.crew/reviews/<run-id>/`` and the
    ``.crew/reviews/<session>/<run-id>/`` layouts. There is NO age logic: the human
    reads the orphan list and decides. ``with_sizes`` is passed straight to the
    per-dir enumerator (False skips the sizing walk, keeps every safety check).
    """
    # A symlinked (or non-dir) `.crew` root means this is not the tree crew created.
    # own_dir refuses a symlinked reviews_root BELOW, but not a symlinked `.crew`
    # parent one level up: a real `.crew/reviews` still sits at the link target, so
    # reviews_root would pass and a --yes could rmtree dirs outside the project (the
    # dry-run prints the unresolved `.crew/...` path, so the human would not notice).
    if not own_dir(crew_dir):
        return []
    reviews_root = crew_dir / REVIEWS_SUBDIR
    if not own_dir(reviews_root):
        return []
    # A run is protected when a still-active loop OR a current-run pointer names it.
    protected = live_run_keys(crew_dir) | pointer_protected_keys(reviews_root)

    out = _review_runs_under(reviews_root, "", protected, reviews_root, with_sizes)
    for session_dir in reviews_root.iterdir():
        if not own_dir(session_dir):
            continue
        out.extend(
            _review_runs_under(
                session_dir, session_dir.name, protected, reviews_root, with_sizes
            )
        )
    return out


def prunable_debate_dirs(
    crew_dir: Path, now: float, with_sizes: bool = True
) -> list[Prunable]:
    """Every stale, incomplete debate dir under ``.crew/debates/``.

    The debate contract: a generated dir name, older than the 1-day staleness
    threshold, with NO ``synthesis.md`` (a completed debate is a decision record
    and is KEPT at any age). Same symlink refusal and reviews/ containment as the
    review-run sweep. ``with_sizes=False`` skips the ``dir_size`` walk and records
    ``bytes=0`` while keeping every staleness/symlink check.
    """
    # Reject a symlinked `.crew` root before descending (see prunable_review_runs):
    # a symlinked parent one level up would let debates_root pass at the link target.
    if not own_dir(crew_dir):
        return []
    debates_root = crew_dir / DEBATES_SUBDIR
    if not own_dir(debates_root):
        return []

    out: list[Prunable] = []
    for entry in debates_root.iterdir():
        if not own_dir(entry) or not DEBATE_DIR_RE.match(entry.name):
            continue
        if not _resolved_within(entry, debates_root):
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if (entry / "synthesis.md").exists():
            continue
        if age <= DEBATE_STALE_SECONDS:
            continue
        out.append(
            Prunable(
                kind="debate",
                path=entry,
                name=entry.name,
                bytes=dir_size(entry) if with_sizes else 0,
                session_dir=debates_root,
            )
        )
    return out


def _drop_nested(items: list[Prunable]) -> list[Prunable]:
    """Drop any candidate whose resolved path is nested under another candidate.

    The engine never mints a run dir inside another run dir, but a hand-planted
    ``run-<hex>/run-<hex>`` would be enumerated once by the flat pass (the parent)
    and once by the parent-as-session pass (the child): the child's bytes would be
    counted twice, and a ``--yes`` that rmtree'd the parent would then hit a missing
    child and log a spurious removal error (a false failure now that swab exits
    nonzero on any failed delete). Keeping only the outermost candidate removes the
    subtree in one rmtree.
    """
    resolved: list[tuple[Path, Prunable]] = []
    for it in items:
        try:
            resolved.append((it.path.resolve(), it))
        except OSError:
            resolved.append((it.path, it))
    # Shallowest first so a parent is decided before any child it contains.
    resolved.sort(key=lambda pair: len(pair[0].parts))
    kept: list[Prunable] = []
    kept_paths: list[Path] = []
    for rp, it in resolved:
        if any(rp != anc and rp.is_relative_to(anc) for anc in kept_paths):
            continue
        kept.append(it)
        kept_paths.append(rp)
    return kept


def collect_prunable(
    crew_dir: Path, now: float, with_sizes: bool = True
) -> list[Prunable]:
    """The full prunable set (review runs first, then debates).

    The ONE entry point the swab command and the session-start reporter both
    call, so they enumerate identically. A residual TOCTOU remains for the caller
    that deletes: a path segment swapped for a symlink between this enumeration and
    the caller's rmtree could redirect the removal, negligible for an attended local
    tool and not defended here.

    ``with_sizes`` (default True, so the attended swab is unchanged and still shows
    on-disk sizes) threads to both sub-enumerators. False keeps the FULL orphan set
    and every safety check but records ``bytes=0`` instead of walking each subtree,
    so the count-only session-start reporter does no unbounded per-dir sizing.
    """
    return _drop_nested(
        prunable_review_runs(crew_dir, with_sizes)
    ) + prunable_debate_dirs(crew_dir, now, with_sizes)


def drop_dangling_pointer(session_dir: Path, removed_names: set) -> None:
    """Remove a ``current-run.json`` pointer left naming a just-removed run.

    Hygiene, not correctness (the next review-prep overwrites the pointer before
    anything reads it): a dangling pointer would make launch-time derivation
    resolve to a missing dir.
    """
    if read_pointer_run_id(session_dir) in removed_names:
        try:
            (session_dir / POINTER_NAME).unlink(missing_ok=True)
        except OSError:
            pass
