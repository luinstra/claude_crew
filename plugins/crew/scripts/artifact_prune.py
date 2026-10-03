"""Enumerate stale crew artifacts safe to prune: ONE definition, two callers.

The `crew swab` command and the session-start reporter both need to answer the
same question ("which review-run, debate, and probe artifacts are stale?") under the
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

import contextlib

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from state_discovery import is_active_value, is_loop_state_file
from build_state import writer_fence
from models import state_lock
from multiagent.review_runs import (
    POINTER_NAME,
    RUN_JSON_NAME,
    ReviewRunError,
    read_run_json,
    read_pointer_run_id,
    session_segment,
    verify_run_record,
)

# This MUST track what review_runs mints, NOT the permissive shared RUN_ID_RE.
# `mint_identity` returns "run-" + sha256(spec)[:12]: the prefix plus EXACTLY 12
# lowercase hex chars. The engine-wide `rounds.RUN_ID_RE` (`run-[A-Za-z0-9_-]+`)
# is a TRAVERSAL guard shared with debate rounds, not an identity: it also admits
# `run-scratch` and `run-my_notes`, names a user can park under .crew/reviews/ and
# the engine never mints. A destructive sweep validates against the mint, not a
# guard. If review_runs ever changes the truncation length or charset, change this.
MINTED_RUN_DIR_RE = re.compile(r"^run-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STANDALONE_POINTER_NAMES = (
    "current-standalone-review.json",
    "current-standalone-debate.json",
)

# Coarse debate-dir ownership heuristic: a `run-` or `YYYYMMDD-HHMMSS` PREFIX. This
# is a LOOSE prefix match, not proof the engine minted the name (it also admits a
# user-parked `run-notes` or `20250101-000000-scratch`). Tightening the grammar was
# deliberately declined: for debates the real safety is elsewhere, not this regex.
# The layout is legacy; the engine writes no `.crew/debates/` dir any more and
# this finder only sweeps stale legacy debate dirs left by older runs.
# The destructive step is the ATTENDED `crew swab` dry-run (the human reads the list
# before `--yes`), and enumeration further requires the 1-day staleness threshold
# plus NO synthesis.md (a completed debate is a decision record, kept at any age).
DEBATE_DIR_RE = re.compile(r"^(run-|\d{8}-\d{6})")

# Debate-staleness threshold (1 day): a live debate mid-write is never a candidate.
# Review runs carry NO age (the human reads the orphan list); debates keep this
# 1-day age contract.
DEBATE_STALE_SECONDS = 86400

# Grace window (1 day) for a standalone workflow record that FAILS VALIDATION.
# Review runs otherwise carry NO age, but this one path needs it: a half-written
# `workflow.json` can be a LIVE run caught mid-write, so a young one stays
# protected. Past the window the record is permanently unvalidatable, and
# protecting it forever would hide it from both `crew swab` and the session-start
# orphan count with no way to ever reclaim it. Scope is deliberately narrow: only a
# WorkflowError ages out. A transient I/O or import failure says nothing about the
# record and stays protected at every age, and a live run is protected by its
# pointer regardless of age (see pointer_protected_keys).
UNVALIDATABLE_WORKFLOW_GRACE_SECONDS = 86400

REVIEWS_SUBDIR = "reviews"
DEBATES_SUBDIR = "debates"
PROBES_SUBDIR = "probes"
PROBE_FILE_RE = re.compile(r"^cursor-hook-env-\d{4}-\d{2}-\d{2}\.txt$")
PROBE_STALE_SECONDS = 7 * 86400


@dataclass
class Prunable:
    """One artifact dir the caller may remove, with its on-disk size when measured.

    ``kind`` is "review-run", "debate", or "probe"; ``name`` is the dir's own name (the run
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
            if json_file.name.startswith("build-state"):
                keys.add((session_segment(json_file.stem.removeprefix("build-state-") if json_file.stem != "build-state" else ""), "*"))
            continue
        if not isinstance(data, dict):
            if json_file.name.startswith("build-state"):
                keys.add((session_segment(json_file.stem.removeprefix("build-state-") if json_file.stem != "build-state" else ""), "*"))
            continue
        fenced = writer_fence(data)
        if fenced:
            if json_file.name.startswith("build-state"):
                owner = json_file.stem.removeprefix("build-state-") if json_file.stem != "build-state" else ""
                keys.add((session_segment(owner), "*"))
            recorded_owner = data.get("session_id")
            if isinstance(recorded_owner, str):
                keys.add((session_segment(recorded_owner), "*"))
        if not is_active_value(data.get("active", False)) and not fenced:
            continue
        run_id = data.get("run_id")
        if isinstance(run_id, str) and MINTED_RUN_DIR_RE.fullmatch(run_id):
            keys.add((session_segment(data.get("session_id") or ""), run_id))
        journal = data.get("bl_workflow") or data.get("mt_workflow")
        if not isinstance(journal, dict):
            continue
        pending = journal.get("pending_review_inputs")
        refs = [journal.get("review_ref"), pending.get("ref") if isinstance(pending, dict) else None]
        for ref in refs:
            if (isinstance(ref, dict) and set(ref) == {"schema", "session_segment", "run_id", "attempt_id", "target_sha256"}
                    and type(ref.get("schema")) is int and ref["schema"] == 1
                    and isinstance(ref.get("session_segment"), str)
                    and re.fullmatch(r"[A-Za-z0-9_-]+", ref["session_segment"])
                    and isinstance(ref.get("run_id"), str) and MINTED_RUN_DIR_RE.fullmatch(ref["run_id"])
                    and isinstance(ref.get("attempt_id"), str) and re.fullmatch(r"attempt-[0-9]{4}", ref["attempt_id"])
                    and isinstance(ref.get("target_sha256"), str) and SHA256_RE.fullmatch(ref["target_sha256"])):
                keys.add((ref["session_segment"], ref["run_id"]))
    return keys


def _standalone_pointer_data(session_dir: Path, name: str) -> dict | None:
    """Parse only the exact schema-1 standalone pointer shape."""
    try:
        data = json.loads((session_dir / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or set(data) != {"schema", "run_id", "identity_digest", "target_sha256"}
        or data.get("schema") != 1
        or not isinstance(data.get("run_id"), str)
        or MINTED_RUN_DIR_RE.fullmatch(data["run_id"]) is None
        or not isinstance(data.get("identity_digest"), str)
        or SHA256_RE.fullmatch(data["identity_digest"]) is None
        or not isinstance(data.get("target_sha256"), str)
        or SHA256_RE.fullmatch(data["target_sha256"]) is None
    ):
        return None
    return data


def _standalone_pointer_run_ids(session_dir: Path) -> list[str]:
    """Return run ids whose standalone pointers agree with immutable records."""
    run_ids: list[str] = []
    for name in STANDALONE_POINTER_NAMES:
        data = _standalone_pointer_data(session_dir, name)
        if data is None:
            continue
        run = session_dir / data["run_id"]
        if not own_dir(run):
            continue
        try:
            record = read_run_json(run)
            verify_run_record(
                record,
                expected_run_id=data["run_id"],
                source=run / RUN_JSON_NAME,
            )
        except ReviewRunError:
            continue
        if (
            data["identity_digest"] != record.get("identity_digest")
            or data["target_sha256"] != record.get("target_sha256")
        ):
            continue
        run_ids.append(data["run_id"])
    return run_ids


def pointer_protected_keys(reviews_root: Path) -> set:
    """Run keys named by the standalone pointers.

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
    for rid in _standalone_pointer_run_ids(reviews_root):
        keys.add(("", rid))
    for session_dir in reviews_root.iterdir():
        if not own_dir(session_dir):
            continue
        rid = read_pointer_run_id(session_dir)
        if rid:
            keys.add((session_dir.name, rid))
        for rid in _standalone_pointer_run_ids(session_dir):
            keys.add((session_dir.name, rid))
    return keys


def _within_grace(stamp: Path, now: float) -> bool:
    """True while ``stamp``'s own mtime is inside the unvalidatable grace window.

    Reads the path's OWN timestamp (``lstat``, no symlink follow): an in-place
    rewrite of a file does not touch its parent dir's mtime, so timing the run dir
    instead would treat a freshly corrupted record inside an old run as expired.
    An unreadable timestamp protects the run.
    """
    try:
        age = now - stamp.lstat().st_mtime
    except OSError:
        return True
    return age <= UNVALIDATABLE_WORKFLOW_GRACE_SECONDS


def _standalone_workflow_terminal(run: Path, segment: str, now: float) -> bool | None:
    """Delegate pruning classification to the canonical standalone engine.

    ``None`` means there is no standalone workflow here at all. A validation error
    (``WorkflowError``) means the record is genuinely malformed or of an obsolete
    schema: protect it while it is young enough to be a live run mid-write, then
    let a permanently unvalidatable one become reclaimable. Any OTHER exception is
    a transient runtime failure (I/O, import, interpreter) that says nothing about
    the record, so it stays fail-closed and protected at every age.
    """
    workflow_path = run / "workflow.json"
    try:
        if workflow_path.is_symlink():
            return False
        if not workflow_path.exists():
            return None
        if not workflow_path.is_file():
            return False
        from multiagent import review_workflow

        try:
            return review_workflow._standalone_run_terminal_for_prune(run, segment)
        except review_workflow.WorkflowError:
            return False if _within_grace(workflow_path, now) else True
    except Exception:
        return False


def standalone_workflow_protected_keys(reviews_root: Path, now: float) -> set:
    """Protect every active or conservatively unreadable standalone workflow."""
    protected = set()

    def inspect(session_dir: Path, segment: str) -> None:
        for run in session_dir.glob("run-*"):
            if not own_dir(run) or MINTED_RUN_DIR_RE.fullmatch(run.name) is None:
                continue
            terminal = _standalone_workflow_terminal(run, segment, now)
            if terminal is False:
                protected.add((segment, run.name))

    inspect(reviews_root, "")
    for session_dir in reviews_root.iterdir():
        if own_dir(session_dir):
            inspect(session_dir, session_dir.name)
    return protected


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
    ``protected`` (named by an active loop/current pointer, or carrying a
    nonterminal or conservatively unreadable standalone workflow).

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
        if (segment, entry.name) in protected or (segment, "*") in protected:
            continue
        if segment == "" and any(
            protected_segment == entry.name
            for protected_segment, _protected_run in protected
        ):
            # A first-level name can itself look like a minted run while serving
            # as the session segment for a protected nested standalone run.
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


def prunable_review_runs(
    crew_dir: Path, with_sizes: bool = True, now: float | None = None
) -> list[Prunable]:
    """Every prunable review-run dir under ``.crew/reviews/``.

    Covers BOTH the flat/sessionless ``.crew/reviews/<run-id>/`` and the
    ``.crew/reviews/<session>/<run-id>/`` layouts. Enumeration carries no age
    threshold: the human reads the orphan list and decides. The one exception is
    the grace window on an unvalidatable standalone workflow record
    (UNVALIDATABLE_WORKFLOW_GRACE_SECONDS), which is why ``now`` is threaded here;
    it defaults to the current clock for a direct caller. ``with_sizes`` is passed
    straight to the per-dir enumerator (False skips the sizing walk, keeps every
    safety check).
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
    # Active loops, either current pointer, and every nonterminal/ambiguous
    # standalone workflow independently protect their run.
    protected = (
        live_run_keys(crew_dir)
        | pointer_protected_keys(reviews_root)
        | standalone_workflow_protected_keys(
            reviews_root, time.time() if now is None else now
        )
    )

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


def prunable_probe_files(
    crew_dir: Path, now: float, with_sizes: bool = True
) -> list[Prunable]:
    """Every stale Cursor environment capture under ``.crew/probes/``."""
    if not own_dir(crew_dir):
        return []
    probes_root = crew_dir / PROBES_SUBDIR
    if not own_dir(probes_root):
        return []
    out: list[Prunable] = []
    for entry in probes_root.iterdir():
        if (
            entry.is_symlink()
            or not entry.is_file()
            or PROBE_FILE_RE.fullmatch(entry.name) is None
            or not _resolved_within(entry, probes_root)
        ):
            continue
        try:
            age = now - entry.stat().st_mtime
            size = entry.stat().st_size if with_sizes else 0
        except OSError:
            continue
        if age <= PROBE_STALE_SECONDS:
            continue
        out.append(
            Prunable(
                kind="probe",
                path=entry,
                name=entry.name,
                bytes=size,
                session_dir=probes_root,
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
    """The full prunable set (review runs, debates, then probe captures).

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
        prunable_review_runs(crew_dir, with_sizes, now)
    ) + prunable_debate_dirs(crew_dir, now, with_sizes) + prunable_probe_files(
        crew_dir, now, with_sizes
    )


def drop_dangling_pointer(session_dir: Path, removed_names: set) -> None:
    """Remove either pointer when it names a just-removed run.

    Hygiene, not correctness (the next review-prep overwrites the pointer before
    anything reads it): a dangling pointer would make launch-time derivation
    resolve to a missing dir.
    """
    if read_pointer_run_id(session_dir) in removed_names:
        try:
            (session_dir / POINTER_NAME).unlink(missing_ok=True)
        except OSError:
            pass
    for name in STANDALONE_POINTER_NAMES:
        standalone = _standalone_pointer_data(session_dir, name)
        if standalone is not None and standalone["run_id"] in removed_names:
            try:
                (session_dir / name).unlink(missing_ok=True)
            except OSError:
                pass


@contextlib.contextmanager
def prune_guard(crew_dir: Path):
    """Freeze pruning eligibility once while all loop owners and admission are locked."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(state_lock(crew_dir / "loop-admission"))
        for state in sorted((p for p in crew_dir.glob("*.json") if is_loop_state_file(p.name)), key=str):
            stack.enter_context(state_lock(state))
        yield {item.path for item in collect_prunable(crew_dir, time.time(), with_sizes=False)}
