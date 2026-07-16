"""Run-scoped review result dirs keyed by run identity: a pure filesystem leaf.

A *review run* is a directory holding one review round's frozen inputs and its
seat results:

    .crew/reviews/<session>/<run_id>/
        run.json           immutable identity record (written ONCE at mint)
        target.md | .diff  the frozen content under review (the snapshot)
        prompt-*.txt       staged seat prompts (subprocess + Task seats)
        <seat>.json        per-seat results, stamped with the run identity

``run_id = "run-" + sha256(canonical review spec)[:12]``. The spec covers the
FULL content hash AND the review inputs (replayable target spec, base, and a
per-seat execution signature: ``{name: {kind, provider, model}}`` for
subprocess seats, ``{name: {kind, model}}`` for Task seats), so the same bytes
reviewed with a different plan path, base, panel, provider, or model pin is a
DIFFERENT run by construction, while a re-prep of an identical spec lands on
the same dir and resumes. The truncated id is
only a directory name: everything that COUNTS a result validates the full
``target_sha256`` stamp, never the id alone.

Like ``rounds.py`` this module never spawns a model or builds a prompt. It owns
the identity math, the on-disk layout, the reviews-subdir session sanitization
(one mechanism for writers and gate alike), and the write rules (write-once
``run.json``, self-healing snapshot, preserve-valid seat writes), so the digest
and the gate can never disagree on what a valid landed seat is.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

# One run-id grammar for debates AND review runs: the `run-` prefix marks a run
# dir for cleanup targeting, and the charset admits no `/` or `.`, so a hostile
# --run-id can never escape the reviews dir (path-traversal guard).
from multiagent.rounds import RUN_ID_RE

_DEFAULT_BASE = ".crew/reviews"
POINTER_NAME = "current-run.json"
RUN_JSON_NAME = "run.json"

# Seat names double as run-dir filename stems (<seat>.json, prompt-<seat>.txt),
# so a seat named after a control file would let an ordinary seat write DESTROY
# the identity record or shadow the pointer, and a Task seat named "seat" would
# stage prompt-seat.txt ONTO the shared subprocess prompt. Only review-prep may
# create or modify a run dir's control files; every seat-name intake and every
# DERIVED seat write checks this set through is_reserved_stem, which
# case-folds: on a case-insensitive filesystem (macOS APFS, Windows) `Run.json`
# IS `run.json`, so a mixed-case stem would alias a control file.
RESERVED_STEMS = frozenset({"run", "current-run", "seat"})


def is_reserved_stem(name: str) -> bool:
    """Case-folded reserved-stem membership: the ONE comparison every intake
    uses, so a mixed-case name can never alias a control file on a
    case-insensitive filesystem."""
    return (name or "").lower() in RESERVED_STEMS


class ReviewRunError(Exception):
    """Raised with a clear, user-facing message (never a raw traceback)."""


# ---------------------------------------------------------------------------
# Session dir + run-id resolution
# ---------------------------------------------------------------------------

def session_segment(session_id: str) -> str:
    """Charset-guard a session id to ``[A-Za-z0-9_-]`` for use as a dir segment.

    Containment is by SANITIZATION, not by raising: a hostile ``../../x``
    COLLAPSES (every ``/`` and ``.`` stripped) to the single child segment
    ``x``. An empty result means the flat (sessionless) layout.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")


def reviews_dir(session_id: str, *, base: str = _DEFAULT_BASE) -> Path:
    """Resolve ``.crew/reviews/<sanitized-session-id>`` (flat when no session)."""
    seg = session_segment(session_id)
    return Path(base) / seg if seg else Path(base)


def validate_run_id(run_id: str) -> str:
    """Return ``run_id`` unchanged if valid; raise ``ReviewRunError`` otherwise.

    A valid run-id has no path separators, so it can only ever name a single
    child directory of the reviews dir.
    """
    if not run_id or not RUN_ID_RE.match(run_id):
        raise ReviewRunError(
            f"invalid run-id {run_id!r}; must match {RUN_ID_RE.pattern} "
            "(no path separators, the traversal guard)"
        )
    return run_id


def run_dir(session_id: str, run_id: str, *, base: str = _DEFAULT_BASE) -> Path:
    """Resolve the run directory (validated; not created)."""
    return reviews_dir(session_id, base=base) / validate_run_id(run_id)


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------

def sha256_text(text: str) -> str:
    """Full 64-hex sha256 of ``text`` (UTF-8): the content identity every seat
    result is stamped with and every counting consumer validates."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mint_identity(
    *,
    target_sha256: str,
    target_spec: str,
    target_base: str,
    seat_signatures: dict,
) -> tuple[str, str]:
    """Mint the content-addressed identity: ``(run_id, identity_digest)``.

    ``seat_signatures`` carries EVERY seat: ``{name: {"kind": "subprocess",
    "provider": <executor kind>, "model": <resolved model>}}`` for subprocess
    seats and ``{name: {"kind": "task", "model": <pin>}}`` for Task seats
    (which have no subprocess provider). Any flip in what actually reviews the
    content (a seat added or dropped, a seat changing execution kind, a
    config-resolved PROVIDER flip, a model change on EITHER kind) mints a new
    run instead of resume-counting output from a different reviewer under the
    old identity.
    The map is keyed by name and the encoding sorts keys, so seat ORDER never
    splits an identity. ``identity_digest`` is the FULL sha256 of the canonical
    spec; ``run_id`` truncates it for the dir name only, and the resume check
    compares the full digest so a truncation collision can never adopt a
    foreign manifest.
    """
    spec = {
        "target_sha256": target_sha256,
        "target_spec": target_spec,
        "target_base": target_base,
        "seats": seat_signatures,
    }
    canonical = json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return "run-" + digest[:12], digest


def recompute_identity(record: dict) -> tuple[str, str]:
    """Recompute ``(run_id, identity_digest)`` from a ``run.json``-shaped
    record's own identity fields.

    A malformed ``seat_signatures`` field (a list, a string, anything that is
    not a name-keyed map) raises ``ReviewRunError`` like every other corrupt
    record, never a bare TypeError/ValueError into the caller.
    """
    try:
        sigs = dict(record.get("seat_signatures") or {})
    except (TypeError, ValueError) as exc:
        raise ReviewRunError(
            f"run record carries a malformed seat_signatures field ({exc}); "
            "treat the record as corrupt"
        ) from exc
    return mint_identity(
        target_sha256=record.get("target_sha256") or "",
        target_spec=record.get("target_spec") or "",
        target_base=record.get("target_base") or "",
        seat_signatures=sigs,
    )


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` (exact bytes: ``newline=""`` disables platform newline
    translation, so the on-disk bytes always equal what a hash of ``text``
    predicts) via a same-dir temp file + ``os.replace`` so a reader never sees
    a torn file."""
    # mkdir/mkstemp are wrapped too: an unwritable dir must surface as the
    # module's clean error type at the CLI, never a bare OSError traceback.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
    except OSError as exc:
        raise ReviewRunError(f"could not write {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ReviewRunError(f"could not write {path}: {exc}") from exc


def write_text_atomic(path: Path, text: str) -> None:
    """Public atomic text write for run-dir artifacts (prompt staging): a
    mid-flight re-prep re-staging an unchanged prompt must never tear a seat's
    concurrent read of that file."""
    _atomic_write_text(path, text)


# ---------------------------------------------------------------------------
# run.json: write-once immutable identity record
# ---------------------------------------------------------------------------

def write_run_json_once(d: Path, record: dict) -> bool:
    """Write ``run.json`` at mint; leave a same-identity file BYTE-UNTOUCHED.

    Returns True when freshly minted, False when an existing record was
    resumed. Resume requires the existing record to match the proposed one on
    THREE independent axes: its stored ``run_id``, its stored FULL
    ``identity_digest``, and the digest recomputed field-level from its own
    identity fields. A tampered ``run_id`` field, tampered identity fields, or
    a truncated-id collision with a different full digest each fail one of the
    three; any mismatch refuses with a diagnostic naming both sides, the dir
    untouched.

    Like the pointer, this file is orchestrator-serial: check-then-write is
    enough because there is one prep at a time per session, and even if two
    same-identity preps did interleave, they would write equivalent bytes
    (identical identity fields, differing only in ``created_at``), so the loser
    of the race cannot corrupt the winner's record.
    """
    p = d / RUN_JSON_NAME
    if p.is_file():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise ValueError("run.json is not a JSON object")
        except (OSError, ValueError) as exc:
            raise ReviewRunError(
                f"run.json at {p} is unreadable or corrupt ({exc}); refusing to "
                "mint over it (remove the run dir to re-prep)"
            ) from exc
        _rec_id, rec_digest = recompute_identity(existing)
        stored_id = existing.get("run_id")
        stored_digest = existing.get("identity_digest")
        if (
            stored_id != record["run_id"]
            or stored_digest != record["identity_digest"]
            or rec_digest != record["identity_digest"]
        ):
            raise ReviewRunError(
                f"run.json at {p} disagrees with the minted identity: stored "
                f"run_id={stored_id!r} digest={str(stored_digest)[:12]}…, "
                f"fields recompute to {rec_digest[:12]}…, but this prep minted "
                f"{record['run_id']!r} digest={record['identity_digest'][:12]}… "
                "(corruption or a truncation collision); refusing to touch it. "
                "Recovery: remove the run dir and re-prep"
            )
        return False
    _atomic_write_text(
        p, json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    return True


def read_run_json(d: Path) -> dict:
    """Read the run dir's identity record; raise ``ReviewRunError`` when it is
    missing or unreadable (writers stamp FROM this, so absence is fatal)."""
    p = d / RUN_JSON_NAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReviewRunError(f"cannot read run record {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewRunError(f"run record {p} is not a JSON object")
    return data


def verify_run_record(record: dict, *, expected_run_id: str, source: Path) -> None:
    """Integrity-check a loaded ``run.json`` before anything TRUSTS it.

    The same three axes ``write_run_json_once`` checks on resume, aimed at a
    reader: the identity fields must be present, the stored ``run_id`` must
    equal the id whose dir was requested, and the field-level recomputed
    identity must match the stored full digest. A record failing any axis
    would validate seat stamps against values that are missing or lie about
    the dir they sit in (e.g. ``result_current`` against ``None`` stamps would
    pass unstamped files), so the caller must refuse rather than fold.
    Raises ``ReviewRunError`` naming the failing axis and the recovery.
    """
    rid = record.get("run_id")
    tsha = record.get("target_sha256")
    digest = record.get("identity_digest")
    problem = None
    if not rid or not tsha or not digest:
        problem = "identity fields missing (run_id/target_sha256/identity_digest)"
    elif rid != expected_run_id:
        problem = f"stored run_id {rid!r} does not match the requested run {expected_run_id!r}"
    else:
        rec_id, recomputed = recompute_identity(record)
        if recomputed != digest:
            problem = (
                f"identity fields recompute to {recomputed[:12]}… but the stored "
                f"digest is {str(digest)[:12]}…"
            )
        elif rec_id != rid:
            # A hand-crafted record can carry SELF-CONSISTENT identity fields
            # and digest while its stored run_id (the dir name) belongs to a
            # different run; the id DERIVED from the recomputed digest is the
            # axis that catches it.
            problem = (
                f"identity fields derive run id {rec_id!r}, not the stored "
                f"{rid!r}"
            )
    if problem:
        raise ReviewRunError(
            f"run record {source} fails its identity check: {problem}. "
            "Recovery: remove the run dir and re-prep"
        )


# ---------------------------------------------------------------------------
# Snapshot: the frozen bytes every seat reviews
# ---------------------------------------------------------------------------

def snapshot_name(kind: str) -> str:
    return "target.md" if kind == "plan" else "target.diff"


def ensure_snapshot(d: Path, name: str, content: str, target_sha256: str) -> str | None:
    """Create the snapshot with the run record; self-heal it on resume.

    Returns a caller-printable note when a present-but-wrong snapshot was
    rewritten (safe: the content is recomputable from the resolved target, and
    seats must never trust tampered or torn bytes), else None.
    """
    p = d / name
    if p.is_file():
        try:
            on_disk = p.read_bytes()
        except OSError:
            on_disk = None
        if on_disk is not None and hashlib.sha256(on_disk).hexdigest() == target_sha256:
            return None
        # Snapshot writes are atomic, so a mismatch here means something
        # OUTSIDE prep mutated the file. The repo's posture is
        # visible-unsanctioned, not un-gameable: the heal restores the frozen
        # bytes and the note tells the orchestrator which results predate it.
        _atomic_write_text(p, content)
        return (
            f"snapshot {p} did not match the run's target_sha256; rewrote the "
            "correct bytes. Seat results landed BEFORE this heal reviewed the "
            "pre-heal bytes: relaunch each LANDED seat with the same --run-id; "
            "pending seats are unaffected"
        )
    _atomic_write_text(p, content)
    return None


# ---------------------------------------------------------------------------
# Belonging predicates, two tiers with one shared core. `result_current` is
# what the DIGEST validates (a stamped, named failure still belongs and must
# render as a failed block); `result_valid` layers `ok is True` on top for the
# consumers that only ever count successes (prep's pending lists, the
# preserve-valid write, the completion gate). Both resolve stamps + name the
# same way, so the digest and the gates can never disagree on whose result a
# file is; they differ only in whether a failure counts.
# ---------------------------------------------------------------------------

def result_current(data, run_id: str, target_sha256: str, *, name: str | None = None) -> bool:
    """A decoded seat result BELONGS to this run (and, when ``name`` is given,
    to this seat): a JSON object whose two identity stamps match the run's own
    record and whose stored ``name`` equals the expected seat slug. Says
    nothing about ``ok``: a stamped, named FAILURE is still this run's result
    (it renders as a failed block, never as stale)."""
    return (
        isinstance(data, dict)
        and data.get("run_id") == run_id
        and data.get("target_sha256") == target_sha256
        and (name is None or data.get("name") == name)
    )


def result_valid(data, run_id: str, target_sha256: str, *, name: str | None = None) -> bool:
    """A decoded seat result COUNTS only when it belongs to this run/seat
    (``result_current``) AND carries a literal ``ok: true`` (a same-run result
    copied to another seat's filename must never satisfy that seat's pending
    or quorum check)."""
    return (
        result_current(data, run_id, target_sha256, name=name)
        and data.get("ok") is True
    )


def seat_landed_valid(d: Path, seat: str, run_id: str, target_sha256: str) -> bool:
    """True when ``<seat>.json`` in the run dir is a landed, valid success FOR
    that seat (the stored ``name`` must match the filename's seat)."""
    p = d / f"{seat}.json"
    if not p.is_file():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return result_valid(data, run_id, target_sha256, name=seat)


# ---------------------------------------------------------------------------
# Preserve-valid seat write
# ---------------------------------------------------------------------------

@contextmanager
def _seat_write_lock(path: Path):
    """Exclusive lock on a sibling ``.<seat>.json.lock`` across the whole
    read-check-write, so two concurrent duplicate launches of one seat cannot
    race the preserve-valid check. Without ``fcntl`` (non-POSIX) the write
    proceeds unlocked: last-writer-wins beats wedging every seat write."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    lock_path = path.parent / f".{path.name}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lf = open(lock_path, "a+")
    except OSError as exc:
        # An unwritable run dir surfaces as the module's clean error type at
        # the CLI (exit 2 with an error line), never a bare OSError traceback.
        raise ReviewRunError(
            f"could not take the seat write lock {lock_path}: {exc}"
        ) from exc
    with lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def preserve_valid_write(
    path: Path, text: str, *, incoming_ok: bool, run_id: str,
    target_sha256: str, name: str | None = None,
) -> tuple[bool, str | None]:
    """Write a seat result into a run dir, never replacing a landed valid
    success with a failure.

    Returns ``(written, note)``. An incoming failure finding an existing valid
    ``ok=true`` result (stamps matching, and ``name`` matching the EXPECTED
    seat when given: another seat's result copied under this filename earns no
    protection) is SKIPPED with a note; a fresh ``ok=true`` result replaces
    anything (a better retry wins). The check and the write happen under one
    per-seat lock, so the rule holds even for concurrent duplicate launches of
    the same seat.
    """
    with _seat_write_lock(path):
        if not incoming_ok and path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = None
            if result_valid(existing, run_id, target_sha256, name=name):
                return False, (
                    f"kept existing valid result at {path}; the incoming "
                    "failure was not written (preserve-valid)"
                )
        _atomic_write_text(path, text)
        return True, None


# ---------------------------------------------------------------------------
# Pointer: the prep-to-begin-review handoff and a launch-time default ONLY.
# current-run.json is orchestrator-serial: the atomic replace protects readers
# from torn bytes, not concurrent writers from each other (there is exactly one
# prep at a time per session). It is NEVER the completion-time authority.
# ---------------------------------------------------------------------------

def write_pointer(
    reviews_d: Path, *, run_id: str, target_sha256: str, created_at: str
) -> Path:
    p = reviews_d / POINTER_NAME
    _atomic_write_text(
        p,
        json.dumps(
            {
                "run_id": run_id,
                "target_sha256": target_sha256,
                "created_at": created_at,
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    return p


def read_pointer_run_id(reviews_d: Path) -> str | None:
    """The pointer's ``run_id`` when present and well-formed; else None.

    A missing, unreadable, or invalid-id pointer reads as None: callers that
    use the pointer treat it as a launch-time convenience and fall back to the
    flat layout rather than choking (the run identity that matters travels as
    an explicit ``--run-id``, never through here).
    """
    p = reviews_d / POINTER_NAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rid = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(rid, str) or not RUN_ID_RE.match(rid):
        return None
    return rid


def pointer_exists(reviews_d: Path) -> bool:
    """Whether a pointer FILE exists at all (even a corrupt one): the signal
    ``persist-seat`` uses to refuse completion-time attribution by pointer."""
    return (reviews_d / POINTER_NAME).is_file()
