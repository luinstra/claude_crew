"""Continuation-chain records and the provider continuation state machine.

This module is deliberately a pure filesystem leaf.  It knows how to validate
and persist a narrowly bound provider conversation, but it does not import a
provider or the command-line dispatcher.  The latter keeps the state machine
unit-testable and prevents provider transcript details from leaking into the
store.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from multiagent import review_runs
from multiagent.continuation_ids import valid_conversation_id
from models import atomic_write_json
from state_discovery import crew_base

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux provide fcntl
    fcntl = None


CHAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RECORD_SCHEMA = 1
RECORD_NAME = "record.json"
DEFAULT_MODEL_SENTINEL = "<provider-default>"
DEFAULT_CHAIN_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.02

BINDING_DIMENSIONS = (
    "chain", "loop_instance_id", "seat", "provider", "model", "workspace",
    "head", "branch", "index_tree",
)
CAPABILITY_VALUES = frozenset({"supported", "unsupported"})
PHASE_VALUES = frozenset({"fresh", "resume"})
FAILURE_VALUES = frozenset({"none", "timeout", "error"})

CONTINUATION_STATUSES = frozenset({
    "created",
    "resumed",
    "unsupported",
    "unavailable",
    "capture_failed",
    "resume_failed",
    "invalidated",
    "skipped",
})

# The action is intentionally separate from the public status.  Dispatch can
# use this small vocabulary without having to infer whether a status should
# create, update, remove, or leave a record alone.
CHAIN_ACTION_PERSIST = "persist"
CHAIN_ACTION_UPDATE = "update"
CHAIN_ACTION_CLEAR = "clear"
CHAIN_ACTION_KEEP = "keep"
CHAIN_ACTIONS = frozenset({
    CHAIN_ACTION_PERSIST,
    CHAIN_ACTION_UPDATE,
    CHAIN_ACTION_CLEAR,
    CHAIN_ACTION_KEEP,
})


class ContinuationError(Exception):
    """Base error for invalid continuation names, records, or bindings."""


class ContinuationRecordError(ContinuationError):
    """A continuation record is corrupt, unsupported, or internally invalid."""


class ContinuationBindingError(ContinuationError):
    """A continuation record does not belong to the requested execution."""


class ContinuationLockError(ContinuationError):
    """The chain-specific sibling lock could not be acquired."""


@dataclass(frozen=True)
class ContinuationBinding:
    """The immutable dimensions that must match before an ID can be resumed."""

    chain: str
    loop_instance_id: str
    seat: str
    provider: str
    model: str
    workspace: str
    head: str
    branch: str
    index_tree: str

    def normalized(self) -> "ContinuationBinding":
        """Return a binding with its workspace canonicalized."""
        return ContinuationBinding(
            chain=self.chain,
            loop_instance_id=self.loop_instance_id,
            seat=self.seat,
            provider=self.provider,
            model=self.model,
            workspace=canonical_workspace(self.workspace),
            head=self.head,
            branch=self.branch,
            index_tree=self.index_tree,
        )


@dataclass(frozen=True)
class ContinuationRecord:
    """The persisted provider conversation identity and guard-clean snapshot."""

    schema: int = RECORD_SCHEMA
    chain: str = ""
    loop_instance_id: str = ""
    seat: str = ""
    provider: str = ""
    model: str = DEFAULT_MODEL_SENTINEL
    workspace: str = ""
    head: str = ""
    branch: str = ""
    index_tree: str = ""
    conversation_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Validate and serialize the record, retaining unknown-field safety."""
        _validate_record_values(self)
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "ContinuationRecord":
        """Coerce a decoded JSON object or raise a safe record error."""
        if not isinstance(data, dict):
            raise ContinuationRecordError("continuation record is not a JSON object")

        schema = data.get("schema")
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise ContinuationRecordError("continuation record has no integer schema")
        if schema > RECORD_SCHEMA:
            raise ContinuationRecordError(
                f"continuation record schema {schema} is newer than {RECORD_SCHEMA}"
            )
        if schema != RECORD_SCHEMA:
            raise ContinuationRecordError(
                f"unsupported continuation record schema {schema}"
            )

        required = (
            "chain", "loop_instance_id", "seat", "provider", "model",
            "workspace", "head", "branch", "index_tree", "conversation_id",
            "created_at", "updated_at",
        )
        missing = [key for key in required if not isinstance(data.get(key), str)]
        if missing:
            raise ContinuationRecordError(
                "continuation record has malformed fields: " + ", ".join(missing)
            )

        record = cls(
            schema=schema,
            **{key: data[key] for key in required},
        )
        _validate_record_values(record)
        return record


def validate_chain_name(chain: str) -> str:
    """Validate the explicit safe-name grammar before any path construction."""
    if not isinstance(chain, str) or CHAIN_NAME_RE.fullmatch(chain) is None:
        raise ContinuationError(
            f"invalid continuation chain name {chain!r}; expected "
            f"{CHAIN_NAME_RE.pattern}"
        )
    return chain


def canonical_workspace(workspace: str | os.PathLike[str]) -> str:
    """Canonicalize aliases such as ``/Users`` and ``/Volumes`` via realpath."""
    return os.path.realpath(os.fspath(workspace))


def resolve_model_identity(
    explicit: str | None,
    configured: str | None,
) -> str:
    """Resolve model identity using explicit/config/default order."""
    if isinstance(explicit, str) and explicit:
        return explicit
    if isinstance(configured, str) and configured:
        return configured
    return DEFAULT_MODEL_SENTINEL


def reviews_dir(session_id: str, *, base: str | Path | None = None) -> Path:
    """Resolve the session review directory under the shared project anchor.

    ``base`` is a review-root override for focused tests and callers that have
    already resolved ``.crew/reviews``.  The default always uses ``crew_base``.
    """
    return review_runs.reviews_dir(
        session_id,
        base=os.fspath(base) if base is not None else None,
    )


def continuations_dir(
    session_id: str,
    *,
    base: str | Path | None = None,
) -> Path:
    return reviews_dir(session_id, base=base) / "continuations"


def chain_dir(
    session_id: str,
    chain: str,
    *,
    base: str | Path | None = None,
) -> Path:
    validate_chain_name(chain)
    return continuations_dir(session_id, base=base) / chain


def record_path(
    session_id: str,
    chain: str,
    *,
    base: str | Path | None = None,
) -> Path:
    return chain_dir(session_id, chain, base=base) / RECORD_NAME


def lock_path(
    session_id: str,
    chain: str,
    *,
    base: str | Path | None = None,
) -> Path:
    """Return the stable sibling lock, outside the prunable record directory."""
    validate_chain_name(chain)
    return continuations_dir(session_id, base=base) / f"{chain}.lock"


def _absolute_path(path: Path) -> Path:
    """Return an absolute lexical path without resolving symlinks."""
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_components(path: Path, *, start: Path | None = None) -> None:
    """Reject symlink components at or below an optional trusted anchor."""
    absolute = _absolute_path(path)
    if start is None:
        current = Path(absolute.anchor)
        components = absolute.parts[1:]
    else:
        anchor = _absolute_path(start)
        try:
            components = absolute.relative_to(anchor).parts
        except ValueError:
            return
        current = anchor
    for component in components:
        current /= component
        if current.is_symlink():
            raise ContinuationError(
                f"continuation path contains a symlink component: {current}"
            )


def _validate_storage_anchor(anchor: Path) -> None:
    """Reject symlinked storage ancestors and anchors outside the project."""
    anchor_absolute = _absolute_path(anchor)
    project_absolute = _absolute_path(crew_base())
    try:
        anchor_absolute.relative_to(project_absolute)
    except ValueError as exc:
        raise ContinuationError(
            f"continuation storage anchor is outside the project: {anchor}"
        ) from exc

    anchor_real = Path(os.path.realpath(anchor_absolute))
    project_real = Path(os.path.realpath(project_absolute))
    try:
        anchor_real.relative_to(project_real)
    except ValueError as exc:
        raise ContinuationError(
            f"continuation storage anchor resolves outside the project: {anchor}"
        ) from exc

    current = anchor_absolute
    while True:
        if current.is_symlink():
            raise ContinuationError(
                f"continuation storage path contains a symlink ancestor: {current}"
            )
        if current == project_absolute:
            break
        parent = current.parent
        if parent == current:
            raise ContinuationError(
                f"continuation storage anchor is not below the project: {anchor}"
            )
        current = parent


def _assert_contained(target: Path, root: Path) -> None:
    """Require a target's realpath to remain below a trusted project path."""
    _validate_storage_anchor(root)
    target_real = Path(os.path.realpath(target))
    root_real = Path(os.path.realpath(root))
    try:
        target_real.relative_to(root_real)
    except ValueError as exc:
        raise ContinuationError(
            f"continuation path escapes its trusted root: {target}"
        ) from exc


def _continuation_paths(
    session_id: str,
    chain: str,
    *,
    base: str | Path | None = None,
) -> tuple[Path, Path, Path, Path]:
    """Return trusted root, chain, record, and lock paths after validation."""
    validate_chain_name(chain)
    root = _absolute_path(continuations_dir(session_id, base=base))
    trusted_anchor = _absolute_path(crew_base())
    _validate_storage_anchor(root)
    directory = root / chain
    record = directory / RECORD_NAME
    lock = root / f"{chain}.lock"

    for path in (root, directory, record, lock):
        _reject_symlink_components(path, start=trusted_anchor)
    for target in (directory, record, lock):
        _assert_contained(target, root)
    return root, directory, record, lock


def _ensure_directory(path: Path, *, trusted_anchor: Path | None = None) -> None:
    """Create a directory tree one component at a time without following links."""
    absolute = _absolute_path(path)
    _reject_symlink_components(absolute, start=trusted_anchor)
    missing: list[Path] = []
    current = absolute
    while not current.exists():
        if current.is_symlink():
            raise ContinuationError(
                f"continuation path contains a symlink component: {current}"
            )
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent

    _reject_symlink_components(current, start=trusted_anchor)
    if not current.is_dir():
        raise ContinuationError(f"continuation path is not a directory: {current}")

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        _reject_symlink_components(directory, start=trusted_anchor)
        if not directory.is_dir():
            raise ContinuationError(
                f"continuation path is not a directory: {directory}"
            )


def _validate_record_values(record: ContinuationRecord) -> None:
    if record.schema != RECORD_SCHEMA:
        raise ContinuationRecordError(
            f"unsupported continuation record schema {record.schema}"
        )
    validate_chain_name(record.chain)
    for field_name in (
        "loop_instance_id", "seat", "provider", "model", "workspace", "head",
        "branch", "index_tree", "conversation_id", "created_at", "updated_at",
    ):
        value = getattr(record, field_name)
        valid = (
            valid_conversation_id(value)
            if field_name == "conversation_id"
            else isinstance(value, str) and bool(value)
        )
        if not valid:
            raise ContinuationRecordError(
                f"continuation record field {field_name!r} is invalid"
            )


def _record_from_any(record: ContinuationRecord | Mapping[str, Any]) -> ContinuationRecord:
    if isinstance(record, ContinuationRecord):
        _validate_record_values(record)
        return record
    if isinstance(record, Mapping):
        return ContinuationRecord.from_dict(dict(record))
    raise ContinuationRecordError("continuation record has an unsupported type")


def load_record(
    session_id: str,
    chain: str,
    *,
    base: str | Path | None = None,
) -> ContinuationRecord | None:
    """Load a record; missing/corrupt/future records fail closed as unusable."""
    try:
        _, _, path, _ = _continuation_paths(session_id, chain, base=base)
        data = json.loads(path.read_text(encoding="utf-8"))
        record = ContinuationRecord.from_dict(data)
        if record.chain != chain:
            raise ContinuationRecordError(
                f"record chain {record.chain!r} does not match {chain!r}"
            )
        return record
    except (OSError, json.JSONDecodeError, ContinuationError, TypeError, ValueError):
        return None


def save_record(
    session_id: str,
    chain: str,
    record: ContinuationRecord | Mapping[str, Any],
    *,
    base: str | Path | None = None,
) -> Path:
    """Atomically write a validated record with the shared 0600 discipline."""
    validate_chain_name(chain)
    checked = _record_from_any(record)
    if checked.chain != chain:
        raise ContinuationBindingError(
            f"record chain {checked.chain!r} does not match {chain!r}"
        )
    root, directory, path, _ = _continuation_paths(session_id, chain, base=base)
    trusted_anchor = _absolute_path(crew_base())
    _ensure_directory(root, trusted_anchor=trusted_anchor)
    _ensure_directory(directory, trusted_anchor=trusted_anchor)
    _, _, path, _ = _continuation_paths(session_id, chain, base=base)
    atomic_write_json(path, checked.to_dict())
    return path


def invalidate(
    session_id: str,
    chain: str,
    *,
    base: str | Path | None = None,
) -> bool:
    """Remove the owned chain directory and leave its sibling lock untouched."""
    root, directory, _, _ = _continuation_paths(session_id, chain, base=base)
    if not directory.exists():
        return False
    if directory.is_symlink() or not directory.is_dir():
        raise ContinuationError(f"continuation path is not an owned directory: {directory}")
    _reject_symlink_components(directory, start=_absolute_path(crew_base()))
    _assert_contained(directory, root)
    shutil.rmtree(directory)
    return True


@contextlib.contextmanager
def continuation_lock(
    session_id: str,
    chain: str,
    *,
    base: str | Path | None = None,
    timeout: float = DEFAULT_CHAIN_LOCK_TIMEOUT_SECONDS,
):
    """Hold the chain-specific sibling lock across a provider turn and write."""
    if fcntl is None:  # pragma: no cover - supported execution platforms have fcntl
        raise ContinuationLockError("interprocess continuation locks are unavailable")
    root, _, _, path = _continuation_paths(session_id, chain, base=base)
    _ensure_directory(root, trusted_anchor=_absolute_path(crew_base()))
    _, _, _, path = _continuation_paths(session_id, chain, base=base)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:  # pragma: no cover - supported execution platforms have it
        raise ContinuationLockError("no-follow lock opens are unavailable")
    fd: int | None = None
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | no_follow, 0o600)
        os.fchmod(fd, 0o600)
    except OSError as exc:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        raise ContinuationLockError(f"cannot open {path.name}: {exc}") from exc
    try:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ContinuationLockError(
                        f"timed out after {timeout:g}s waiting for {path.name}: {exc}"
                    ) from exc
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def binding_mismatches(
    record: ContinuationRecord | Mapping[str, Any],
    expected: ContinuationBinding | Mapping[str, Any],
) -> tuple[str, ...]:
    """Return non-secret binding dimensions that differ, in stable order."""
    try:
        checked = _record_from_any(record)
        wanted = _binding_values(expected)
    except ContinuationError as exc:
        raise ContinuationBindingError(str(exc)) from exc

    mismatches: list[str] = []
    for dimension in BINDING_DIMENSIONS:
        if dimension not in wanted:
            mismatches.append(dimension)
            continue
        actual = getattr(checked, dimension)
        expected_value = wanted[dimension]
        if dimension == "workspace":
            try:
                actual = canonical_workspace(actual)
                expected_value = canonical_workspace(expected_value)
            except (TypeError, ValueError, OSError):
                mismatches.append(dimension)
                continue
        if actual != expected_value:
            mismatches.append(dimension)
    mismatches.extend(sorted(set(wanted) - set(BINDING_DIMENSIONS)))
    return tuple(mismatches)


def binding_matches(
    record: ContinuationRecord | Mapping[str, Any],
    expected: ContinuationBinding | Mapping[str, Any],
) -> bool:
    try:
        return not binding_mismatches(record, expected)
    except ContinuationError:
        return False


def validate_binding(
    record: ContinuationRecord | Mapping[str, Any],
    expected: ContinuationBinding | Mapping[str, Any],
) -> bool:
    """Validate all binding dimensions or raise a diagnostic-only error."""
    mismatches = binding_mismatches(record, expected)
    if mismatches:
        raise ContinuationBindingError(
            "continuation binding mismatch: " + ", ".join(mismatches)
        )
    return True


def new_record(
    *,
    binding: ContinuationBinding | Mapping[str, Any],
    conversation_id: str,
    created_at: str | None = None,
    updated_at: str | None = None,
) -> ContinuationRecord:
    """Mint a validated record from a binding and provider conversation ID."""
    if not valid_conversation_id(conversation_id):
        raise ContinuationError(
            "conversation_id must be a printable, non-option token"
        )
    values = _binding_values(binding)
    now = datetime.now(timezone.utc).isoformat()
    values["conversation_id"] = conversation_id
    values["created_at"] = created_at or now
    values["updated_at"] = updated_at or values["created_at"]
    try:
        record = ContinuationRecord(**values)
    except TypeError as exc:
        raise ContinuationError(f"invalid continuation record fields: {exc}") from exc
    _validate_record_values(record)
    return record


def _binding_values(
    binding: ContinuationBinding | Mapping[str, Any],
) -> dict[str, Any]:
    """Return a complete, validated binding mapping or raise safely."""
    if isinstance(binding, ContinuationBinding):
        values = asdict(binding)
    elif isinstance(binding, Mapping):
        values = dict(binding)
    else:
        raise ContinuationBindingError("continuation binding has an unsupported type")

    unknown = [key for key in values if key not in BINDING_DIMENSIONS]
    if unknown:
        raise ContinuationError(
            "continuation binding has unknown fields: "
            + ", ".join(repr(key) for key in unknown)
        )
    missing = [dimension for dimension in BINDING_DIMENSIONS if dimension not in values]
    if missing:
        raise ContinuationBindingError(
            "continuation binding is incomplete: " + ", ".join(missing)
        )
    for dimension in BINDING_DIMENSIONS:
        value = values[dimension]
        if not isinstance(value, str) or not value:
            raise ContinuationBindingError(
                f"continuation binding field {dimension!r} must be a non-empty string"
            )
    values["chain"] = validate_chain_name(values["chain"])
    values["workspace"] = canonical_workspace(values["workspace"])
    return values


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _guard_violation(guard: Any) -> bool:
    """Read the existing dispatch guard shape without importing the dispatcher."""
    guard_flags = (
        "head_moved", "staged_changed", "branch_changed",
        "post_run_state_mismatch", "state_mismatch",
    )
    if isinstance(guard, bool):
        return not guard
    if guard is None:
        return True
    if isinstance(guard, Mapping):
        recognized = False
        for key in ("clean",):
            if key in guard:
                recognized = True
                if not isinstance(guard[key], bool) or guard[key] is False:
                    return True
        if "guard_violation" in guard:
            recognized = True
            if not isinstance(guard["guard_violation"], bool) or guard["guard_violation"] is True:
                return True
        if any(key in guard for key in guard_flags):
            recognized = True
        if any(
            key in guard and not isinstance(guard[key], bool)
            for key in guard_flags
        ):
            return True
        if any(
            key in guard
            for key in ("binding_mismatch", "binding_changed", "binding_mismatches")
        ):
            recognized = True
        return not recognized or any(guard.get(key) is True for key in guard_flags)
    recognized = False
    for key in ("clean",):
        value = getattr(guard, key, None)
        if value is not None:
            recognized = True
            if not isinstance(value, bool) or value is False:
                return True
    value = getattr(guard, "guard_violation", None)
    if value is not None:
        recognized = True
        if not isinstance(value, bool) or value is True:
            return True
    for key in guard_flags:
        value = getattr(guard, key, None)
        if value is not None:
            recognized = True
            if not isinstance(value, bool) or value is True:
                return True
    if any(
        hasattr(guard, key)
        for key in ("binding_mismatch", "binding_changed", "binding_mismatches")
    ):
        recognized = True
    return not recognized


def _binding_mismatch(guard: Any) -> bool:
    if isinstance(guard, Mapping):
        has_binding_fields = any(
            key in guard
            for key in ("binding_mismatch", "binding_changed", "binding_mismatches")
        )
    else:
        has_binding_fields = any(
            hasattr(guard, key)
            for key in ("binding_mismatch", "binding_changed", "binding_mismatches")
        )
    if not has_binding_fields:
        return False

    value = _field(guard, "binding_mismatch", None)
    if value is not None and value is not False and value != "":
        return True
    value = _field(guard, "binding_changed", None)
    if value is not None and not isinstance(value, bool):
        return True
    if value is True:
        return True
    mismatches = _field(guard, "binding_mismatches", None)
    if mismatches is None:
        return False
    if not isinstance(mismatches, (list, tuple, set, frozenset)):
        return True
    return bool(mismatches)


def _outcome_field(outcome: Any, name: str, default: Any) -> Any:
    if isinstance(outcome, Mapping):
        return outcome.get(name, default)
    return getattr(outcome, name, default)


def _normalized_outcome(outcome: Any) -> tuple[str, str, str, bool, bool, bool]:
    """Normalize outcome tokens to safe values and report token validity."""
    missing = object()
    raw_capability = _outcome_field(outcome, "capability", missing)
    raw_phase = _outcome_field(outcome, "phase", missing)
    raw_failure = _outcome_field(outcome, "failure", missing)
    capability_valid = (
        isinstance(raw_capability, str) and raw_capability in CAPABILITY_VALUES
    )
    phase_valid = isinstance(raw_phase, str) and raw_phase in PHASE_VALUES
    failure_valid = isinstance(raw_failure, str) and raw_failure in FAILURE_VALUES
    return (
        raw_capability if capability_valid else "unsupported",
        raw_phase if phase_valid else "fresh",
        raw_failure if failure_valid else "error",
        capability_valid,
        phase_valid,
        failure_valid,
    )


def _fresh_classification(
    outcome: Any,
    *,
    clear_stale: bool,
    failure: str | None = None,
) -> tuple[str, str]:
    failure = failure or _outcome_field(outcome, "failure", "error")
    conversation_id = _outcome_field(outcome, "conversation_id", None)
    if failure == "error":
        return (
            "capture_failed",
            CHAIN_ACTION_CLEAR if clear_stale else CHAIN_ACTION_KEEP,
        )
    if valid_conversation_id(conversation_id):
        return "created", CHAIN_ACTION_PERSIST
    return "unavailable", CHAIN_ACTION_CLEAR if clear_stale else CHAIN_ACTION_KEEP


def classify_continuation(
    record: ContinuationRecord | Mapping[str, Any] | None,
    guard: Any,
    availability: bool,
    outcome: Any | None,
) -> tuple[str, str]:
    """Classify one chained run using one ordered precedence.

    The function is pure: it only reads its four arguments, never parses an
    error string, touches the filesystem, or consults provider capability flags.
    ``guard`` may be the existing dispatch guard mapping/object or a simple bool
    for focused callers (``True`` means clean).

    The dispatcher handles pre-run misuse before this function. The remaining
    precedence is: 1) unavailable provider, 2) timeout, 3) guard violation,
    4) binding/capability change, 5) unsupported capability, 6) exact resume,
    7) fresh capture.
    """
    # 1. Availability is checked before outcome so the valid None outcome cannot
    # be dereferenced. A false availability means no provider ran, so an existing
    # chain remains untouched.
    if availability is False:
        return "skipped", CHAIN_ACTION_KEEP
    if availability is not True or outcome is None:
        return "invalidated", CHAIN_ACTION_CLEAR
    checked_record = None
    if record is not None:
        try:
            checked_record = _record_from_any(record)
        except ContinuationError:
            return "invalidated", CHAIN_ACTION_CLEAR

    capability, phase, failure, capability_valid, phase_valid, failure_valid = (
        _normalized_outcome(outcome)
    )

    # 2. Timeout outranks every provider phase/failure and clears uncertainty.
    if failure == "timeout":
        return "invalidated", CHAIN_ACTION_CLEAR

    # 3. Git guard or post-run build-state mismatch clears uncertainty.
    if _guard_violation(guard):
        return "invalidated", CHAIN_ACTION_CLEAR

    binding_changed = _binding_mismatch(guard)

    # 4-5. Binding/capability changes cannot preserve a stale ID. Unsupported is
    # reported truthfully and always clears, including when there is no record.
    if capability == "unsupported":
        return "unsupported", CHAIN_ACTION_CLEAR
    if binding_changed:
        if not phase_valid:
            return (
                "capture_failed" if failure == "error" else "unavailable",
                CHAIN_ACTION_CLEAR,
            )
        if phase == "resume":
            return "resume_failed", CHAIN_ACTION_CLEAR
        return _fresh_classification(outcome, clear_stale=True, failure=failure)

    if not capability_valid:
        return "unsupported", CHAIN_ACTION_CLEAR

    # Unknown tokens are never allowed to create or preserve a chain. An unknown
    # phase is interpreted as fresh for status purposes, but its ID is not bound.
    if not phase_valid:
        return (
            "capture_failed" if failure == "error" else "unavailable",
            CHAIN_ACTION_CLEAR,
        )

    # 6. A valid prior record plus a resume phase is the only resumed path.
    if phase == "resume":
        if checked_record is None:
            return "resume_failed", CHAIN_ACTION_CLEAR
        conversation_id = _outcome_field(outcome, "conversation_id", None)
        if (
            failure == "error"
            or not valid_conversation_id(conversation_id)
            or conversation_id != checked_record.conversation_id
        ):
            return "resume_failed", CHAIN_ACTION_CLEAR
        return "resumed", CHAIN_ACTION_UPDATE

    # 7. Fresh capture; a record is unexpected here, so a failed or missing-ID
    # capture clears it defensively while a successful capture replaces it.
    return _fresh_classification(
        outcome,
        clear_stale=record is not None or not failure_valid,
        failure=failure,
    )
