"""Importable loop transactions and the shared review completion policy."""
from __future__ import annotations

from collections.abc import Callable
import contextlib
from build_state import writer_fence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiagent.review_workflow import LoopReviewBinding, ReviewRef
from pathlib import Path
import re
import uuid

from models import (LoopState, StateLockError, SCHEMA_VERSION, LOAD_OK, LOAD_MISSING, LOAD_FUTURE_SCHEMA,
    effective_count, loop_bound_reason,
    DEFAULT_DEADLINE_MINUTES, NO_DEADLINE, FORCE_EXIT_KEY, read_state_json,
    update_state_json, utc_now_iso, state_lock, atomic_write_json)
from state_discovery import crew_base, find_session_state_file, find_adoptable_legacy, is_active_value
from multiagent import config, review_runs, targets

VERDICTS = ("APPROVED", "REVISE", "REJECT", "FAILED")
MAX_CONSECUTIVE_REVIEW_FAILURES = 2
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

class LoopStateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

class _Refusal(LoopStateError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_transition", message)

@dataclass(frozen=True, slots=True)
class CompletionAuthorization:
    diagnostic: str
    conditions: tuple[str, ...]

class _Advisory(LoopStateError):
    def __init__(self, diagnostic: str, conditions: tuple[str, ...] = ()) -> None:
        super().__init__("completion_advisory", diagnostic)
        self.diagnostic = diagnostic
        self.conditions = conditions

class BindingStatus(StrEnum):
    MATCH = "match"
    DIVERGED = "diverged"
    MISSING = "missing"

@dataclass(frozen=True, slots=True)
class LegacyReviewSource:
    run_id: str
    manifest_identity_digest: str | None
    observed_pointer_run_id: str | None

@dataclass(frozen=True, slots=True)
class LoopReviewSource:
    binding: LoopReviewBinding
    ref: ReviewRef
    accepted_outcome_sha256: str | None

@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    run_id: str
    target_sha256: str
    target_spec: str
    target_base: str
    expected_seats: tuple[str, ...]
    usable_seats: tuple[str, ...]
    binding_status: BindingStatus
    source: LegacyReviewSource | LoopReviewSource

@dataclass(frozen=True, slots=True)
class RecordedVerdict:
    verdict: str
    phase: str
    overrides: tuple[str, ...] = ()

def _refuse(message: str) -> None:
    raise _Refusal(message)

def _distinct_seats(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(s for s in value if isinstance(s, str) and s))

def validate_evidence(evidence: ReviewEvidence, *, pending: bool = False) -> None:
    review_runs.validate_run_id(evidence.run_id)
    digests = [evidence.target_sha256]
    if isinstance(evidence.source, LegacyReviewSource):
        if evidence.source.manifest_identity_digest is not None:
            digests.append(evidence.source.manifest_identity_digest)
        elif evidence.usable_seats:
            _refuse("usable legacy evidence requires a verified manifest")
        if evidence.source.run_id != evidence.run_id:
            _refuse("legacy evidence run identity differs")
    elif isinstance(evidence.source, LoopReviewSource):
        if not pending or evidence.source.accepted_outcome_sha256 is not None:
            digests.append(evidence.source.accepted_outcome_sha256)
        from multiagent.review_workflow import parse_loop_binding, loop_binding_to_dict, parse_review_ref, review_ref_to_dict
        binding = parse_loop_binding(loop_binding_to_dict(evidence.source.binding))
        ref = parse_review_ref(review_ref_to_dict(evidence.source.ref))
        if (binding.session_segment != ref.session_segment or ref.run_id != evidence.run_id
                or ref.target_sha256 != evidence.target_sha256 or evidence.binding_status != BindingStatus.MATCH):
            _refuse("loop evidence source does not match its frozen binding")
    else:
        _refuse("review evidence source is not supported")
    if any(not isinstance(d, str) or not SHA_RE.fullmatch(d) for d in digests):
        _refuse("review evidence digests must be full SHA-256 strings")
    if (any(not isinstance(seat, str) or not seat for seat in (*evidence.expected_seats, *evidence.usable_seats))
            or len(set(evidence.expected_seats)) != len(evidence.expected_seats)
            or len(set(evidence.usable_seats)) != len(evidence.usable_seats)
            or not set(evidence.usable_seats) <= set(evidence.expected_seats)):
        _refuse("review evidence roster is invalid")

def resolve(loop: str, session_id: str) -> Path:
    prefix = {"bl": "build-state", "mt": "measure-twice-state"}[loop]
    directory = crew_base() / ".crew"
    return (find_session_state_file(directory, prefix, session_id)
            or directory / f"{prefix}{'-' + session_id if session_id else ''}.json")

def read(path: Path) -> dict[str, object]:
    data, status = read_state_json(path)
    if status != LOAD_OK:
        raise LoopStateError(status, f"cannot read loop state {path}: {status}")
    return data

def mutate(path: Path, operation: Callable[[dict[str, object]], dict[str, object]],
           default: dict[str, object] | None = None) -> dict[str, object]:
    try:
        data, status = update_state_json(path, operation, default=default)
    except StateLockError as exc:
        raise LoopStateError("state_lock_timeout", str(exc)) from exc
    if status not in (LOAD_OK, LOAD_MISSING):
        raise LoopStateError(status, f"refusing to modify {path}: {status}")
    return data

def admission_lock():
    return state_lock(crew_base() / ".crew" / "loop-admission")


@contextlib.contextmanager
def admission_states(session_id: str):
    directory = crew_base() / ".crew"
    candidates = set()
    for prefix in ("build-state", "measure-twice-state"):
        candidates.add(directory / f"{prefix}{'-' + session_id if session_id else ''}.json")
        legacy = find_adoptable_legacy(directory, prefix, session_id) if session_id else None
        if legacy is not None:
            candidates.add(legacy)
    with contextlib.ExitStack() as stack:
        for path in sorted(candidates, key=lambda p: str(p.absolute())):
            stack.enter_context(state_lock(path))
        yield {p: read_state_json(p) for p in candidates}


def check_writer_fences(states: dict, *, continuing: Path | None = None) -> None:
    for path, (data, status) in states.items():
        if path.name.startswith("build-state") and status not in (LOAD_OK, LOAD_MISSING):
            raise LoopStateError(status, f"cannot admit work over unreadable build writer state: {path}; retain evidence for inspection")
        if data is None:
            continue
        fence = writer_fence(data)
        if fence and path != continuing:
            raise LoopStateError("outstanding_writer", fence)


def check_admission(states: dict, *, replacing: Path | None = None, continuing: Path | None = None) -> None:
    for path, (_data, status) in states.items():
        if status not in (LOAD_OK, LOAD_MISSING):
            raise LoopStateError(status, f"cannot admit work over unreadable state: {path}")
    check_writer_fences(states, continuing=continuing)
    for path, (data, _status) in states.items():
        if data is None:
            continue
        if (replacing is None or path == replacing) and path.name.startswith("measure-twice-state") and (data.get(FORCE_EXIT_KEY) or data.get("exit_kind") == "force_exit"):
            raise LoopStateError("active_request_conflict", f"safety-exited lifetime cannot be replaced by start: {path}")
        if path != replacing and is_active_value(data.get("active")):
            raise LoopStateError("active_request_conflict", f"another loop is active in this session: {path}")


def initialize(task: str, session_id: str, *, journal: dict[str, object],
               previous: dict[str, object] | None = None) -> dict[str, object]:
    from multiagent import continuations
    path = resolve("mt", session_id)
    deadline = config.deadline_minutes()
    deadline = DEFAULT_DEADLINE_MINUTES if deadline is None else deadline
    fresh = asdict(LoopState(active=True, loop="mt", task=task, session_id=session_id,
        loop_instance_id=str(uuid.uuid4()), started_at=utc_now_iso(),
        deadline_minutes=deadline, no_deadline=deadline == NO_DEADLINE))
    fresh.update(schema=SCHEMA_VERSION, mt_workflow=journal)
    with admission_lock(), admission_states(session_id) as observed:
        check_admission(observed)
        replacing_chain = any(data and data.get("bl_workflow") is not None for data, _ in observed.values())
    chain = continuations.continuation_lock(session_id, "build-executor") if replacing_chain else contextlib.nullcontext()
    try:
        with chain, admission_lock(), admission_states(session_id) as states:
            check_admission(states)
            if states != observed:
                raise LoopStateError("active_request_conflict", "admission changed; retry the same request")
            data, status = states[path]
            if (data != previous and data != (previous or {}) and status != LOAD_MISSING
                    or data and (is_active_value(data.get("active")) or data.get(FORCE_EXIT_KEY)
                                 or data.get("exit_kind") == "force_exit")):
                raise LoopStateError("active_request_conflict", "existing lifetime changed or cannot be replaced by start")
            if replacing_chain:
                continuations.invalidate(session_id, "build-executor")
            atomic_write_json(path, fresh)
    except continuations.ContinuationLockError as exc:
        raise LoopStateError("continuation_busy", "Continuation chain is busy; retry after the owned writer finishes") from exc
    return fresh


def initialize_compatibility(path: Path, state: LoopState) -> dict[str, object]:
    """Replace a fresh state after the compatibility CLI's admission checks."""
    fresh = asdict(state)
    fresh["schema"] = SCHEMA_VERSION
    return mutate(path, lambda _data: fresh)

def await_input(path: Path, waiting: bool = True) -> dict[str, object]:
    def update(data: dict[str, object]) -> dict[str, object]:
        if is_active_value(data.get("active")):
            data.update(awaiting_input=waiting, schema=SCHEMA_VERSION)
        return data
    return mutate(path, update)

def bind_review(data: dict[str, object], evidence: ReviewEvidence) -> None:
    validate_evidence(evidence, pending=True)
    if not is_active_value(data.get("active")) or data.get("phase", "drafting") not in {"drafting", "reviewing"}:
        _refuse("loop cannot bind a review from its current phase")
    data.update(schema=SCHEMA_VERSION, phase="reviewing", run_id=evidence.run_id,
        target_sha256=evidence.target_sha256, target_spec=evidence.target_spec,
        target_base=evidence.target_base, expected_seats=list(evidence.expected_seats), awaiting_input=False)

def legacy_evidence(data: dict[str, object], session_id: str) -> ReviewEvidence:
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        _refuse("this loop froze no run identity; re-run begin-review after review-prep")
    review_runs.validate_run_id(run_id)
    reviews = review_runs.reviews_dir(session_id, base=str(crew_base() / ".crew" / "reviews"))
    run = reviews / run_id
    try:
        record = review_runs.read_run_json(run)
    except review_runs.ReviewRunError:
        # Missing/unreadable manifests cannot certify surviving flat results.
        # Retain zero-result recovery; a readable identity must still verify.
        record = None
    if record is not None:
        review_runs.verify_run_record(record, expected_run_id=run_id, source=run / "run.json")
    expected = tuple(_distinct_seats(data.get("expected_seats")))
    sha = str(data.get("target_sha256") or "")
    pointer = review_runs.read_pointer_run_id(reviews)
    return ReviewEvidence(run_id, sha, str(data.get("target_spec") or ""),
        str(data.get("target_base") or ""), expected,
        tuple(s for s in expected if review_runs.seat_landed_valid(run, s, run_id, sha)) if record is not None else (),
        BindingStatus.MATCH if pointer == run_id else BindingStatus.DIVERGED if pointer else BindingStatus.MISSING,
        LegacyReviewSource(run_id, record["identity_digest"] if record else None, pointer))

def bound_reason(data: dict[str, object]) -> str | None:
    if data.get("phase") == "done":
        return None
    return loop_bound_reason(data)

def deactivate(data: dict[str, object], *, cancel: bool = False, reason: str = "") -> None:
    if not is_active_value(data.get("active")):
        return
    if not cancel and data.get("phase") != "done":
        _refuse("deactivation requires a completing verdict or explicit cancellation")
    data.update(schema=SCHEMA_VERSION, active=False, completed_at=utc_now_iso(),
                exit_kind="cancelled" if cancel else "approved")
    if reason:
        data["reason"] = reason


def apply_verdict(data: dict[str, object], evidence: ReviewEvidence, verdict: str, *,
              minor_only: bool = False, force: bool = False, loop: str = "mt",
              session_id: str = "", force_authorization: CompletionAuthorization | None = None) -> RecordedVerdict:
    if verdict not in VERDICTS or (minor_only and verdict != "REVISE"):
        _refuse("minor_only qualifies REVISE only; verdict must be a supported value")
    completing = verdict == "APPROVED" or (verdict == "REVISE" and minor_only)
    overrode: list[tuple[str, str]] = []
    loop_owned = isinstance(evidence.source, LoopReviewSource)
    next_command, owner_ref = ("build-next", "BuildRef") if loop == "bl" else ("measure-twice-next", "MeasureRef")
    begin_hint = (f"Use {next_command} with the issued {owner_ref}; repair or cancel the invalid binding."
                  if loop_owned else
                  f"Run `crew review-prep`, then `crew state begin-review {loop} --session-id {session_id or '<id>'}`.")
    # is_active_value, never `is True` (see begin-review).
    if not is_active_value(data.get("active", False)):
        _refuse(
            f"loop '{loop}' is not active: a verdict describes a panel a "
            f"LIVE loop launched. Nothing was recorded."
        )
    phase = data.get("phase") or "drafting"
    if phase != "reviewing":
        _refuse(
            f"cannot record a verdict from phase '{phase}': this loop has no "
            f"panel in flight. {begin_hint}"
        )
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        _refuse(
            f"this loop froze no run identity, so there is nothing to judge "
            f"the results against. {begin_hint}"
        )
    try:
        review_runs.validate_run_id(run_id)
    except review_runs.ReviewRunError as exc:
        _refuse(f"the frozen run identity is unusable ({exc}); re-run begin-review.")

    validate_evidence(evidence)
    if (evidence.run_id != run_id or evidence.target_sha256 != data.get("target_sha256")
            or evidence.target_spec != (data.get("target_spec") or "")
            or evidence.target_base != (data.get("target_base") or "")
            or evidence.expected_seats != tuple(_distinct_seats(data.get("expected_seats")))):
        _refuse("review evidence does not match the frozen loop identity")
    target_sha256 = evidence.target_sha256
    expected = evidence.expected_seats
    usable = evidence.usable_seats
    decide_command = "build-decide" if loop == "bl" else "measure-twice-decide"
    repanel = (f"use an exact bound human retry decision through {decide_command}, or cancel"
               if isinstance(evidence.source, LoopReviewSource) else
               "re-run `crew review-prep` + begin-review + the panel")

    if verdict == "FAILED":
        if usable:
            _refuse(
                f"FAILED says the panel returned nothing, but run {run_id} "
                f"holds {len(usable)} usable result(s) "
                f"({', '.join(usable)}): record REVISE/APPROVED/REJECT "
                f"instead. Nothing was recorded."
            )
        failures = effective_count(
            data.get("consecutive_review_failures"), 0) + 1
        data["schema"] = SCHEMA_VERSION
        data["last_verdict"] = verdict
        # The override stamp pairs with last_verdict; FAILED overrides nothing.
        data.pop("last_verdict_overrides", None)
        data["consecutive_review_failures"] = failures
        data["phase"] = "drafting"
        # A recorded verdict means the loop advanced, so it is no longer
        # waiting on the human: clear the pause (re-arms the nudge).
        data["awaiting_input"] = False
        if failures >= MAX_CONSECUTIVE_REVIEW_FAILURES:
            # In THIS transaction, never a follow-up deactivate call: an
            # unlocked gap between recording the failure and turning the loop
            # off is a window where a reader sees a half-transitioned loop.
            data["active"] = False
            data["exit_kind"] = "review_failed"
            data["completed_at"] = utc_now_iso()
            data["reason"] = (
                f"{failures} consecutive review runs produced no usable seat "
                f"results (most recent: {run_id}); ending the loop rather "
                f"than re-prepping a panel that is not returning."
            )
        return RecordedVerdict(verdict, str(data.get("phase")), tuple(n for n, _ in overrode))

    # A completing verdict with no replayable spec is a malformed freeze, not
    # a drifted world: there is nothing to re-hash against, so this stays a
    # HARD refusal (exit 2) checked before the advisory gathering below.
    spec = data.get("target_spec") or ""
    if completing and not spec:
        _refuse(
            "this loop froze no replayable target spec, so the reviewed "
            f"content cannot be proven unchanged. {begin_hint}"
        )

    # ADVISORY gathering: every remaining check describes a world that
    # drifted from what the panel saw, which is a human's call. Collect ALL
    # that trip (never stop at the first) so the combined diagnostic names
    # every one; without --force this raises exit 3, with --force it records
    # and stamps the list. Each entry is (short-name, existing fix text).
    advisory: list[tuple[str, str]] = []
    current_target = ""
    if not usable:
        # The symmetric half of FAILED's refusal: FAILED requires zero usable
        # seats, so every other verdict requires at least one. Recorded as
        # progress an all-failed panel would reset consecutive_review_failures
        # and the returning-nothing bound would never trip: a --force here is
        # a human choosing to record over a panel they read themselves.
        advisory.append((
            "zero-usable",
            f"a {verdict} verdict describes what the panel found, but 0 of "
            f"{len(expected)} launched seats returned a usable result in run "
            f"{run_id}, so the panel found nothing. Record FAILED instead "
            f"(the verdict for a panel that produced nothing)."
        ))
    if completing:
        needed = len(expected) // 2 + 1
        if len(usable) < needed:
            quorum_fix = ("retry missing seats through the exact bound human decision; "
                          "a partial panel may demand more work, never sign off"
                          if loop_owned else
                          "re-run the missing seats against this run, or record REVISE/REJECT "
                          "(a partial panel may demand more work, never sign off)")
            advisory.append((
                "quorum-not-met",
                f"quorum not met: {len(usable)} of {len(expected)} launched "
                f"seats returned a usable result in run {run_id}, {needed} "
                f"needed. Fix: {quorum_fix}."
            ))
        pointer_run_id = (evidence.source.observed_pointer_run_id
                          if isinstance(evidence.source, LegacyReviewSource) else evidence.run_id)
        if evidence.binding_status != BindingStatus.MATCH:
            advisory.append((
                "pointer-divergence",
                f"pointer divergence: the run pointer names "
                f"{pointer_run_id or 'nothing'} but this loop froze {run_id}, "
                f"so the panel was re-prepped (or the pointer swept) after the "
                f"freeze. Fix: `crew state begin-review {loop} "
                f"--session-id {session_id or '<id>'}` so the frozen identity "
                f"matches the latest prep, then run the panel over it."
            ))
        # Re-resolved with the base VERBATIM as frozen (empty for every kind
        # but branch, where the flag is what the diff was derived from):
        # substituting a default here would re-hash something the panel never
        # read.
        # This resolve shells out to git while the state lock is HELD, so a
        # concurrent Stop hook can time out on it. Accepted: that path fails
        # open loudly without writing, and it costs seconds at most. Resolving
        # outside the lock would trade it for a re-hash of bytes the gate never
        # judged, which is the drift this check exists to catch.
        # This drift re-hash resolves the target through `targets.resolve`,
        # which anchors to `crew_base()` (the project root), the SAME source
        # `_reviews_dir` roots at (see there), so the re-hash and the panel
        # read one `.crew` tree even when the process cwd differs. A target
        # that genuinely no longer resolves yields a drift advisory (clearable
        # with `--force`), never a false clean PASS.
        try:
            fresh = review_runs.sha256_text(
                targets.resolve(spec, base=data.get("target_base") or "").content
            )
        except targets.TargetError as exc:
            current_target = f"unresolved: {exc}"
            advisory.append((
                "target-no-longer-resolves",
                f"the reviewed target {spec!r} no longer resolves ({exc}), so "
                f"it cannot be shown to be what the panel read. Fix: {repanel}."
            ))
        else:
            current_target = f"SHA-256: {fresh}"
            if fresh != target_sha256:
                advisory.append((
                    "target-drift",
                    f"target drift: {spec!r} now hashes to {fresh}, but the "
                    f"panel reviewed {target_sha256 or '(nothing)'}. The thing "
                    f"on disk is not the thing that was reviewed. Fix: {repanel} over the current content."
                ))

    if advisory or force_authorization is not None:
        names = ", ".join(n for n, _ in advisory)
        body = "\n".join(f"  - {text}" for _, text in advisory)
        force_hint = ("answer the exact bound question with kind=force and confirmation=force "
                      if loop_owned else "record with --force ")
        diagnostic = (f"Advisory: recording {verdict} names a world changed from what "
            f"the panel reviewed ({len(advisory)} condition(s): {names}). "
            f"These are advisories, not errors: surface them to the user, and "
            f"{force_hint}ONLY on the user's explicit say-so "
            f"(otherwise fix and re-panel). Nothing was recorded.\n{body}")
        if not advisory:
            diagnostic = f"Completion conditions changed; current checks for {verdict} are clear. Confirm the current target or retry review."
        if loop_owned and completing:
            diagnostic += (f"\nCurrent target: {spec!r}; base: {evidence.target_base!r}; "
                           f"plan file: {data.get('plan_file')!r}; {current_target}")
        conditions = tuple(name for name, _text in advisory)
        # Compare the disclosed world in the same guard that would record it.
        if (advisory and not force) or (force_authorization is not None
                and force_authorization != CompletionAuthorization(diagnostic, conditions)):
            raise _Advisory(diagnostic, conditions)

    data["schema"] = SCHEMA_VERSION
    data["last_verdict"] = verdict
    # A recorded verdict means the loop advanced (a completing verdict or a new
    # revision round), so it is no longer waiting on the human: clear the pause.
    data["awaiting_input"] = False
    # Any verdict that is not FAILED is evidence the panel CAN return.
    data["consecutive_review_failures"] = 0
    if completing:
        data["phase"] = "done"
    else:
        # The one place a revision round advances.
        data["revision_round"] = effective_count(
            data.get("revision_round"), 0) + 1
        data["phase"] = "drafting"
    if advisory:
        # --force reached here: stamp the audit trail with the same names the
        # warning prints, and carry them out for the post-write warning.
        overrode.extend(advisory)
        data["last_verdict_overrides"] = [n for n, _ in advisory]
    else:
        # Keep the stamp paired with last_verdict: a clean record clears any
        # override left by a prior round.
        data.pop("last_verdict_overrides", None)
    return RecordedVerdict(verdict, str(data.get("phase")), tuple(n for n, _ in overrode))
