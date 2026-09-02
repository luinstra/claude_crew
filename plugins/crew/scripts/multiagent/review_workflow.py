"""Python-owned state machine and schema-1 transport for standalone review."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import functools
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from multiagent import channels, config, findings, prompts, render, review_runs, seats, targets
from multiagent.providers import (
    ATTRIBUTION_REQUESTED_ONLY,
    attribution_for,
    ProviderResult,
    get_provider_for_channel,
    stamp_attribution,
)

SCHEMA = 1
HOSTS = frozenset({"claude", "cursor", "codex", "unknown"})
ACTION_RE = re.compile(r"^attempt-[0-9]{4}:(reviewer|formatter|synthesis):[0-9]{4}$")
ATTEMPT_RE = re.compile(r"^attempt-[0-9]{4}$")
RUN_RE = re.compile(r"^run-[0-9a-f]{12}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
STANDALONE_POINTER = "current-standalone-review.json"
STANDALONE_RESERVED_STEMS = review_runs.RESERVED_STEMS | {"workflow"}
NEEDS_INPUT_QUESTION = (
    "Review the newest plan or which code scope? Reply with a .md path, "
    "working-tree, branch, commit:<rev>, or <left>..<right>."
)


def quote_argv(value: str) -> str:
    """Encode one non-NUL scalar as a POSIX shell word."""
    if "\x00" in value:
        raise ValueError("argv values may not contain NUL")
    if _SAFE_ARG.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


class StepType(StrEnum):
    NEEDS_INPUT = "needs_input"
    WORK_BATCH = "work_batch"
    WAITING = "waiting"
    TERMINAL = "terminal"


class ActionKind(StrEnum):
    REVIEWER = "reviewer"
    FORMATTER = "formatter"
    SYNTHESIS = "synthesis"


class ActionDriver(StrEnum):
    EXTERNAL = "external"
    NATIVE = "native"
    PARENT = "parent"


RECOVERY_DIAGNOSTICS = {
    (ActionKind.REVIEWER, ActionDriver.NATIVE): "native_task_lost",
    (ActionKind.REVIEWER, ActionDriver.EXTERNAL): "external_process_lost",
    (ActionKind.FORMATTER, ActionDriver.NATIVE): "formatter_task_lost",
    (ActionKind.FORMATTER, ActionDriver.PARENT): "parent_formatter_lost",
    (ActionKind.SYNTHESIS, ActionDriver.PARENT): "parent_synthesis_lost",
}


class HostStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    target_input: str
    base: str = "main"
    panel: str | None = None
    seats: str | None = None
    session_id: str = ""
    timeout_seconds: int | None = None
    inline: bool = False
    # None means "the caller named no channels", which is what lets the config
    # layers answer; an empty tuple is an explicit "force nothing" that wins.
    force_external_channels: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ReviewRef:
    session_segment: str
    run_id: str
    attempt_id: str
    target_sha256: str


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    ref: ReviewRef
    action_id: str


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    ref: ReviewRef
    action_id: str
    confirmation: str
    diagnostic_code: str


@dataclass(frozen=True, slots=True)
class RetryRequest:
    ref: ReviewRef
    seats: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class WorkItem:
    action_id: str
    kind: str
    driver: str
    seat: str | None
    role: str | None
    model: str | None
    channel: str | None
    access: str
    prompt_path: str
    ingress_path: str | None
    result_path: str | None
    submission_path: str | None
    timeout_seconds: int | None
    return_transport: dict | None
    host_result_template: dict | None


@dataclass(frozen=True, slots=True)
class HostResult:
    ref: ReviewRef
    action_id: str
    status: str
    artifact: dict | None
    judgment: dict | None
    diagnostic: str | None


@dataclass(frozen=True, slots=True)
class SubmissionRequest:
    submission_path: str
    consume: bool
    result: HostResult


@dataclass(frozen=True, slots=True)
class _SubmissionAdmission:
    """Authenticated host evidence after hard transport validation."""

    artifact_path: Path | None
    accepted_ok: bool
    diagnostic: str | None


@dataclass(frozen=True, slots=True)
class _CanonicalPanelBytes:
    """Python-authoritative grouped and full panel representations."""

    grouped: bytes
    full: bytes


@dataclass(frozen=True, slots=True)
class ClaimResponse:
    ref: ReviewRef
    authorization: str
    action_status: str
    work_item: WorkItem | None


@dataclass(frozen=True, slots=True)
class ReviewStep:
    type: str
    ref: ReviewRef | None = None
    resolved_target: dict | None = None
    display: str | None = None
    question: str | None = None
    work_items: tuple[WorkItem, ...] = ()
    in_flight: tuple[str, ...] = ()
    panel: dict | None = None
    outcome: dict | None = None


class WorkflowError(Exception):
    """A user-facing protocol error with a stable code."""

    def __init__(self, code: str, message: str, error: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code
        self.error = error
        self.message = message


def _public_workflow_boundary(function):
    """Translate persistence failures at every public standalone entrypoint."""

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WorkflowError:
            raise
        except (review_runs.ReviewRunError, OSError) as exc:
            raise WorkflowError("persistence_error", str(exc)) from exc

    return wrapped


def workflow_error_dict(exc: WorkflowError) -> dict:
    return {"schema": SCHEMA, "error": exc.error, "code": exc.code, "message": exc.message}


def review_ref_to_dict(ref: ReviewRef) -> dict:
    return {
        "schema": SCHEMA,
        "session_segment": ref.session_segment,
        "run_id": ref.run_id,
        "attempt_id": ref.attempt_id,
        "target_sha256": ref.target_sha256,
    }


def parse_review_ref(value: object) -> ReviewRef:
    required = {"schema", "session_segment", "run_id", "attempt_id", "target_sha256"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value.get("schema")) is not int
        or value.get("schema") != SCHEMA
    ):
        raise WorkflowError("invalid_ref", "review ref must contain exactly the schema-1 fields")
    if any(not isinstance(value[field], str) for field in required - {"schema"}):
        raise WorkflowError("invalid_ref", "review reference fields must be strings")
    ref = ReviewRef(
        value["session_segment"], value["run_id"],
        value["attempt_id"], value["target_sha256"],
    )
    if not SESSION_RE.fullmatch(ref.session_segment):
        raise WorkflowError("invalid_ref", "review reference session segment is invalid")
    if not RUN_RE.fullmatch(ref.run_id):
        raise WorkflowError("invalid_ref", "review reference run id is invalid")
    if not ATTEMPT_RE.fullmatch(ref.attempt_id):
        raise WorkflowError("invalid_ref", "review reference attempt id is invalid")
    if not SHA_RE.fullmatch(ref.target_sha256):
        raise WorkflowError("invalid_ref", "review reference target hash is invalid")
    return ref


def host_result_to_dict(result: HostResult) -> dict:
    return {
        "schema": SCHEMA, "ref": review_ref_to_dict(result.ref),
        "action_id": result.action_id, "status": result.status,
        "artifact": result.artifact, "judgment": result.judgment,
        "diagnostic": result.diagnostic,
    }


def parse_host_result(value: object) -> HostResult:
    required = {"schema", "ref", "action_id", "status", "artifact", "judgment", "diagnostic"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value.get("schema")) is not int
        or value.get("schema") != SCHEMA
    ):
        raise WorkflowError("invalid_submission", "HostResult must contain exactly the schema-1 fields")
    action_id = value.get("action_id")
    if not isinstance(action_id, str) or not ACTION_RE.fullmatch(action_id):
        raise WorkflowError("invalid_submission", "HostResult action id is invalid")
    status = value.get("status")
    if (
        not isinstance(status, str)
        or status not in {member.value for member in HostStatus}
    ):
        raise WorkflowError("invalid_submission", "HostResult status is invalid")
    artifact = value.get("artifact")
    if artifact is not None:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise WorkflowError("invalid_submission", "HostResult artifact must contain exactly path and sha256")
        if not isinstance(artifact.get("path"), str) or not SHA_RE.fullmatch(str(artifact.get("sha256", ""))):
            raise WorkflowError("invalid_submission", "HostResult artifact path or hash is invalid")
    judgment = value.get("judgment")
    if judgment is not None and (not isinstance(judgment, dict) or set(judgment) != {"verdict", "minor_only"}):
        raise WorkflowError("invalid_submission", "HostResult judgment must contain exactly verdict and minor_only")
    diagnostic = value.get("diagnostic")
    if diagnostic is not None and not isinstance(diagnostic, str):
        raise WorkflowError("invalid_submission", "HostResult diagnostic must be a string or null")
    return HostResult(parse_review_ref(value["ref"]), action_id, status, artifact, judgment, diagnostic)


def work_item_to_dict(item: WorkItem) -> dict:
    return {field.name: getattr(item, field.name) for field in dataclasses.fields(WorkItem)}


def parse_work_item(value: object) -> WorkItem:
    names = {field.name for field in dataclasses.fields(WorkItem)}
    if not isinstance(value, dict) or set(value) != names:
        raise WorkflowError("invalid_step", "WorkItem must contain exactly the schema-1 fields")
    if not isinstance(value.get("action_id"), str) or not ACTION_RE.fullmatch(value["action_id"]):
        raise WorkflowError("invalid_step", "WorkItem action id is invalid")
    if value.get("kind") not in {member.value for member in ActionKind}:
        raise WorkflowError("invalid_step", "WorkItem kind is invalid")
    if value.get("driver") not in {member.value for member in ActionDriver}:
        raise WorkflowError("invalid_step", "WorkItem driver is invalid")
    if not isinstance(value.get("prompt_path"), str) or not isinstance(value.get("access"), str):
        raise WorkflowError("invalid_step", "WorkItem paths/access are invalid")
    return WorkItem(**value)


def review_step_to_dict(step: ReviewStep) -> dict:
    return {
        "schema": SCHEMA, "type": str(step.type),
        "ref": review_ref_to_dict(step.ref) if step.ref else None,
        "resolved_target": step.resolved_target, "display": step.display,
        "question": step.question,
        "work_items": [work_item_to_dict(item) for item in step.work_items],
        "in_flight": list(step.in_flight), "panel": step.panel,
        "outcome": step.outcome,
    }


def parse_review_step(value: object) -> ReviewStep:
    required = {"schema", "type", "ref", "resolved_target", "display", "question",
                "work_items", "in_flight", "panel", "outcome"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value.get("schema")) is not int
        or value.get("schema") != SCHEMA
    ):
        raise WorkflowError("invalid_step", "ReviewStep must contain exactly the schema-1 fields")
    if value.get("type") not in {member.value for member in StepType}:
        raise WorkflowError("invalid_step", "ReviewStep type is invalid")
    if not isinstance(value.get("work_items"), list) or not isinstance(value.get("in_flight"), list):
        raise WorkflowError("invalid_step", "ReviewStep work_items/in_flight are invalid")
    return ReviewStep(
        value["type"], parse_review_ref(value["ref"]) if value["ref"] is not None else None,
        value["resolved_target"], value["display"], value["question"],
        tuple(parse_work_item(item) for item in value["work_items"]),
        tuple(str(item) for item in value["in_flight"]), value["panel"], value["outcome"],
    )


def claim_response_to_dict(response: ClaimResponse) -> dict:
    return {
        "schema": SCHEMA, "ref": review_ref_to_dict(response.ref),
        "authorization": response.authorization,
        "action_status": response.action_status,
        "work_item": work_item_to_dict(response.work_item) if response.work_item else None,
    }


def parse_claim_response(value: object) -> ClaimResponse:
    required = {"schema", "ref", "authorization", "action_status", "work_item"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value.get("schema")) is not int
        or value.get("schema") != SCHEMA
    ):
        raise WorkflowError("invalid_claim", "ClaimResponse must contain exactly the schema-1 fields")
    if value.get("authorization") not in {"spawn", "do_not_spawn", "perform", "do_not_perform"}:
        raise WorkflowError("invalid_claim", "ClaimResponse authorization is invalid")
    if value.get("action_status") not in {"claimed", "already_claimed", "settled"}:
        raise WorkflowError("invalid_claim", "ClaimResponse action status is invalid")
    item = value.get("work_item")
    return ClaimResponse(parse_review_ref(value["ref"]), value["authorization"],
                         value["action_status"], parse_work_item(item) if item is not None else None)


def _atomic(path: Path, payload: object) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    review_runs.write_text_atomic(path, text)


def _read_object(path: Path, *, code: str = "corrupt_workflow") -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(code, f"cannot read JSON record {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(code, f"JSON record {path} is not an object")
    return data


def _host() -> str:
    host = channels.current_host()
    if host not in HOSTS:
        raise WorkflowError("invalid_host", f"unsupported detected host {host!r}")
    return host


def _base() -> Path:
    from state_discovery import crew_base

    return Path(os.path.abspath(crew_base())) / ".crew" / "reviews"


def _review_path_error(message: str, exc: OSError | None = None) -> None:
    error = WorkflowError("unsafe_review_path", message)
    if exc is None:
        raise error
    raise error from exc


def _ensure_real_directory(
    path: Path,
    *,
    create: bool,
    allow_missing: bool,
) -> bool:
    """Validate or create one lexical directory component without following links."""
    try:
        if path.is_symlink():
            _review_path_error(f"standalone review path contains a symlink: {path}")
        if not path.exists():
            if not create:
                if allow_missing:
                    return False
                _review_path_error(f"standalone review directory is missing: {path}")
            try:
                path.mkdir()
            except FileExistsError:
                pass
        if path.is_symlink():
            _review_path_error(f"standalone review path contains a symlink: {path}")
        if not path.is_dir():
            _review_path_error(f"standalone review path is not a directory: {path}")
    except WorkflowError:
        raise
    except OSError as exc:
        _review_path_error(f"cannot validate standalone review directory {path}: {exc}", exc)
    return True


def _guard_review_path(
    *,
    session_segment: str | None = None,
    run_id: str | None = None,
    create: bool = False,
    allow_missing: bool = False,
) -> Path:
    """Return a component-checked standalone review directory.

    The project root is trusted only as a lexical anchor: it and every review
    component below it must be a real directory, never a symlink. Missing
    components are created one at a time only when explicitly requested.
    """
    if session_segment is not None and (
        not session_segment or SESSION_RE.fullmatch(session_segment) is None
    ):
        raise WorkflowError(
            "unsafe_review_path",
            "standalone review path requires a safe non-empty session segment",
        )
    if run_id is not None and (session_segment is None or RUN_RE.fullmatch(run_id) is None):
        raise WorkflowError(
            "unsafe_review_path",
            "standalone review path requires a valid run id and session segment",
        )

    base = _base()
    project = base.parent.parent
    if not _ensure_real_directory(project, create=False, allow_missing=False):
        _review_path_error(f"standalone review project root is missing: {project}")
    try:
        project_real = project.resolve(strict=True)
    except OSError as exc:
        _review_path_error(f"cannot resolve standalone review project root {project}: {exc}", exc)

    components = [project / ".crew", base]
    if session_segment is not None:
        components.append(base / session_segment)
    if run_id is not None:
        components.append(base / session_segment / run_id)

    last_existing = project
    for component in components:
        exists = _ensure_real_directory(
            component,
            create=create,
            allow_missing=allow_missing,
        )
        if not exists:
            return components[-1]
        last_existing = component

    try:
        base_real = base.resolve(strict=True)
        base_real.relative_to(project_real)
        last_existing.resolve(strict=True).relative_to(base_real)
    except (OSError, ValueError) as exc:
        _review_path_error(
            f"standalone review path does not remain beneath its real review root: "
            f"{last_existing}",
            exc if isinstance(exc, OSError) else None,
        )
    return components[-1]


def _prepare_run_descendant(
    run: Path,
    path: Path,
    label: str,
    *,
    create_parents: bool,
    required_file: bool = False,
    corrupt: bool = False,
) -> Path:
    """Validate one run-owned file path without following descendant links."""
    code = "corrupt_workflow" if corrupt else "unsafe_review_path"

    def fail(message: str, exc: OSError | None = None) -> None:
        error = WorkflowError(code, message)
        if exc is None:
            raise error
        raise error from exc

    guarded = _guard_review_path(
        session_segment=run.parent.name,
        run_id=run.name,
        create=False,
    )
    lexical_run = Path(os.path.abspath(run))
    lexical_path = Path(os.path.abspath(path))
    if run != lexical_run or guarded != lexical_run or path != lexical_path:
        fail(f"{label} is not an exact absolute path beneath its guarded run")
    try:
        relative = lexical_path.relative_to(lexical_run)
    except ValueError:
        fail(f"{label} is not lexically contained beneath its guarded run")
    if not relative.parts:
        fail(f"{label} names the run directory instead of a file")

    cursor = lexical_run
    try:
        for part in relative.parts[:-1]:
            cursor = cursor / part
            if cursor.is_symlink():
                fail(f"{label} traverses a symlink: {cursor}")
            if not cursor.exists():
                if not create_parents:
                    fail(f"{label} has a missing parent directory: {cursor}")
                try:
                    cursor.mkdir()
                except FileExistsError:
                    pass
            if cursor.is_symlink():
                fail(f"{label} traverses a symlink: {cursor}")
            if not cursor.is_dir():
                fail(f"{label} traverses a non-directory component: {cursor}")

        if lexical_path.is_symlink():
            fail(f"{label} is an existing symlink: {lexical_path}")
        if lexical_path.exists() and not lexical_path.is_file():
            fail(f"{label} is not a regular file: {lexical_path}")
        if required_file and not lexical_path.is_file():
            fail(f"{label} is missing: {lexical_path}")

        run_real = lexical_run.resolve(strict=True)
        parent_real = lexical_path.parent.resolve(strict=True)
        parent_real.relative_to(run_real)
        if lexical_path.exists():
            lexical_path.resolve(strict=True).relative_to(run_real)
    except WorkflowError:
        raise
    except (OSError, ValueError) as exc:
        fail(f"cannot validate {label}: {exc}", exc if isinstance(exc, OSError) else None)
    return lexical_path


def _write_run_text(run: Path, path: Path, text: str, label: str) -> None:
    safe = _prepare_run_descendant(
        run,
        path,
        label,
        create_parents=True,
    )
    try:
        review_runs.write_text_atomic(safe, text)
    except review_runs.ReviewRunError as exc:
        raise WorkflowError("persistence_error", str(exc)) from exc


def _session(request: ReviewRequest) -> str:
    raw_session = request.session_id.strip()
    if not raw_session:
        raise WorkflowError("missing_session_id", "a harness session id is required")
    if "<" in raw_session or ">" in raw_session:
        raise WorkflowError(
            "invalid_session_id",
            "the harness session id looks like an unsubstituted placeholder",
        )
    segment = review_runs.session_segment(raw_session)
    if not SESSION_RE.fullmatch(segment):
        raise WorkflowError("invalid_session_id", "the harness session id cannot form a safe segment")
    return segment


def _timeout(value: int | None, frozen: dict | None = None) -> tuple[int, int | None]:
    if value is not None and value <= 0:
        raise WorkflowError("invalid_timeout", "timeout must be a positive integer")
    if value is None and isinstance((frozen or {}).get("timeout_seconds"), int):
        return int(frozen["timeout_seconds"]), None
    raw = value if value is not None else config.default_timeout()
    raw = 600 if raw is None else int(raw)
    return min(raw, 540), raw if raw > 540 else None


def _provider_timeout(
    seat: str,
    provider_name: str,
    channel: str,
    timeout: int,
) -> int:
    """Resolve the one provider-specific standalone timeout floor."""
    if provider_name != "agy":
        return timeout
    provider = get_provider_for_channel(seat, channel)
    effective = math.ceil(provider.effective_timeout(timeout))
    if effective > 540:
        raise WorkflowError(
            "provider_timeout_exceeds_budget",
            f"seat {seat!r} requires {effective}s but standalone provider budget is 540s",
        )
    return effective


def _target_prompt_metadata_sha256(
    notes: list[str],
    diff_cmd: str | None,
    snapshot_name: str,
    target_descriptor: str,
) -> str:
    canonical = json.dumps(
        {
            "snapshot": snapshot_name,
            "target_descriptor": target_descriptor,
            "target_diff_cmd": diff_cmd,
            "target_notes": notes,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _newest_plan_target() -> targets.Target:
    """Read the newest direct-child plan through no-follow directory handles."""
    project_root = _base().parent.parent
    plans_dir = project_root / ".crew" / "plans"
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        raise WorkflowError(
            "no_plan_found",
            "cannot safely select a plan under .crew/plans on this platform",
        )
    directory_flags = os.O_RDONLY | no_follow | directory
    try:
        project_real = project_root.resolve(strict=True)
        with contextlib.ExitStack() as stack:
            project_fd = os.open(project_real, directory_flags)
            stack.callback(os.close, project_fd)
            crew_fd = os.open(".crew", directory_flags, dir_fd=project_fd)
            stack.callback(os.close, crew_fd)
            plans_fd = os.open("plans", directory_flags, dir_fd=crew_fd)
            stack.callback(os.close, plans_fd)

            crew_stat = os.stat(".crew", dir_fd=project_fd, follow_symlinks=False)
            plans_stat = os.stat("plans", dir_fd=crew_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(crew_stat.st_mode)
                or not stat.S_ISDIR(plans_stat.st_mode)
                or (crew_stat.st_dev, crew_stat.st_ino)
                != (os.fstat(crew_fd).st_dev, os.fstat(crew_fd).st_ino)
                or (plans_stat.st_dev, plans_stat.st_ino)
                != (os.fstat(plans_fd).st_dev, os.fstat(plans_fd).st_ino)
            ):
                raise OSError(".crew/plans changed during safe selection")

            plans_real = plans_dir.resolve(strict=True)
            expected_plans_real = project_real / ".crew" / "plans"
            if plans_real != expected_plans_real:
                raise OSError(".crew/plans does not resolve beneath the project root")
            plans_real.relative_to(project_real)

            candidates: list[tuple[str, os.stat_result]] = []
            for name in os.listdir(plans_fd):
                if not name.endswith(".md"):
                    continue
                candidate_stat = os.stat(
                    name,
                    dir_fd=plans_fd,
                    follow_symlinks=False,
                )
                if stat.S_ISREG(candidate_stat.st_mode):
                    candidates.append((name, candidate_stat))
            if not candidates:
                raise WorkflowError(
                    "no_plan_found",
                    "no plan found under .crew/plans",
                )
            name, selected_stat = max(
                candidates,
                key=lambda item: (
                    item[1].st_mtime_ns,
                    f".crew/plans/{item[0]}",
                ),
            )
            selected = plans_dir / name
            if selected.parent.resolve(strict=True) != plans_real:
                raise OSError("selected plan parent changed during safe selection")
            plan_fd = os.open(name, os.O_RDONLY | no_follow, dir_fd=plans_fd)
            opened_stat = os.fstat(plan_fd)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (selected_stat.st_dev, selected_stat.st_ino)
            ):
                os.close(plan_fd)
                raise OSError("selected plan changed during safe selection")
            with os.fdopen(
                plan_fd,
                "r",
                encoding="utf-8",
                errors="replace",
            ) as handle:
                content = handle.read()
    except WorkflowError:
        raise
    except (OSError, ValueError) as exc:
        raise WorkflowError(
            "no_plan_found",
            f"cannot safely select a plan under .crew/plans: {exc}",
        ) from exc

    spec = selected.relative_to(project_root).as_posix()
    return targets.Target(
        kind="plan",
        scope=spec,
        content=content,
        descriptor=f"plan: {spec}",
        ref_path=spec,
        replay_spec=spec,
    )


def _newest_plan() -> str:
    return _newest_plan_target().replay_spec


def _is_newest_plan_intent(value: str) -> bool:
    collapsed = re.sub(r"[ \t\r\n\f\v]+", " ", value.strip()).lower()
    without_review = re.sub(r"^review(?: the)?(?: |$)", "", collapsed).strip()
    newest = {"", "latest", "plan", "the plan", "latest plan", "the latest plan"}
    return collapsed in newest or without_review in newest


def _intent_spec(value: str, base: str) -> tuple[str, str] | None:
    """Parse the fixed standalone target grammar into (spec, effective base)."""
    original = value.strip()
    collapsed = re.sub(r"[ \t\r\n\f\v]+", " ", original)
    if _is_newest_plan_intent(value):
        return _newest_plan(), base

    if original.lower().endswith(".md"):
        path_match = re.fullmatch(
            r"(?is)(?:review[ \t\r\n\f\v]+(?:the[ \t\r\n\f\v]+)?"
            r"plan(?:[ \t\r\n\f\v]+at)?[ \t\r\n\f\v]+|"
            r"plan[ \t\r\n\f\v]+)?(.+\.md)",
            original,
        )
        if path_match:
            return path_match.group(1), base

    code_text = re.sub(r"(?i)^review(?: the)? ", "", collapsed).strip()
    code_lower = code_text.lower()
    if code_lower in {"code", "diff", "changes"}:
        return "auto", base
    if re.fullmatch(r"working(?:-| )tree(?: changes)?", code_lower):
        return "working-tree", base
    if code_lower == "branch":
        return "branch", base
    branch_match = re.fullmatch(r"(?i)branch vs (\S+)", code_text)
    if branch_match:
        return "branch", branch_match.group(1)
    commit_match = re.fullmatch(r"(?i)commit(?::| )(\S+)", code_text)
    if commit_match:
        return f"commit:{commit_match.group(1)}", base
    if re.fullmatch(r"\S+\.\.\.?\S+", code_text):
        return code_text, base
    if re.fullmatch(r"[0-9a-fA-F]{7,64}", code_text):
        return code_text, base

    scoped = re.fullmatch(r"(?i)(?:scope|code|diff|changes) (\S+(?: \S+)*)", code_text)
    if scoped:
        concrete = scoped.group(1)
        concrete_lower = concrete.lower()
        if re.fullmatch(r"working(?:-| )tree(?: changes)?", concrete_lower):
            return "working-tree", base
        if concrete_lower == "branch":
            return "branch", base
        commit_match = re.fullmatch(r"(?i)commit(?::| )(\S+)", concrete)
        if commit_match:
            return f"commit:{commit_match.group(1)}", base
        if re.fullmatch(r"\S+\.\.\.?\S+", concrete):
            return concrete, base
        if re.fullmatch(r"[0-9a-fA-F]{7,64}", concrete):
            return concrete, base
    return None


def _resolve_target_intent(
    value: str,
    base: str,
) -> tuple[targets.Target, str] | None:
    if _is_newest_plan_intent(value):
        return _newest_plan_target(), base
    parsed = _intent_spec(value, base)
    if parsed is None:
        return None
    spec, effective_base = parsed
    try:
        return targets.resolve(spec, base=effective_base), effective_base
    except targets.TargetError as exc:
        raise WorkflowError("target_error", str(exc)) from exc


def _resolve_route_policy(request: ReviewRequest, host: str) -> RoutePolicy:
    """Resolve this run's route policy: flag > per-repo > global > built-in.

    The escape hatch is deliberately file-readable rather than env-driven: the
    Cursor agent shell every crew command runs in there scrubs operator exports,
    so an env-var opt-out would be unreachable on the one host that needs it
    most. An explicitly named channel is validated hard, because a typo in a flag
    the operator typed should be told rather than silently ignored; the config
    layers keep the never-choke contract and drop unknown names with a warning.
    """
    named = request.force_external_channels
    if named is None:
        named = config.review_force_external_channels() or ()
    else:
        unknown = sorted(
            channel for channel in named
            if channel not in seats.CHANNEL_TO_LEGACY_KIND
        )
        if unknown:
            raise WorkflowError(
                "unknown_channel",
                f"cannot force unknown channel(s) {', '.join(unknown)} external; "
                f"known channels are {', '.join(sorted(seats.CHANNEL_TO_LEGACY_KIND))}",
            )
    return RoutePolicy.resolve(host, named)


def _resolve_seats(
    request: ReviewRequest, policy: RoutePolicy
) -> list[tuple[str, object, object]]:
    from multiagent import cli

    try:
        # The ROSTER resolver still declares the narrow Task-recipe route: it
        # answers which names are in the panel, and every name reaches the
        # per-seat resolution below whichever split it landed in. It reads that
        # route through the policy, so the split and the per-seat resolution
        # cannot answer the same routing question two ways.
        selection = cli.resolve_review_selection(
            panel=request.panel,
            seats_arg=request.seats,
            strict_explicit=True,
            declared_native=policy.task_declared_native(),
        )
    except LookupError as exc:
        raise WorkflowError("unknown_seat", str(exc)) from exc
    except ValueError as exc:
        raise WorkflowError("duplicate_seat", str(exc)) from exc
    names = [*selection.subprocess_seats, *selection.task_seats]
    if not names:
        raise WorkflowError("no_seats", "no review seats resolved")
    for name in names:
        if name != name.lower() or not SESSION_RE.fullmatch(name):
            raise WorkflowError("invalid_seat", f"review seat {name!r} is not filename-safe")
        if name in STANDALONE_RESERVED_STEMS:
            raise WorkflowError("reserved_seat", f"review seat {name!r} is reserved")

    answer: list[tuple[str, object, object]] = []
    capabilities = channels.active_capabilities()
    for name in names:
        spec = seats.seat_spec(name)
        if spec is None:  # panel configuration can contain stale names
            raise WorkflowError("unknown_seat", f"unknown review seat {name!r}")
        resolved = channels.resolve_seat(
            spec,
            capabilities=capabilities,
            declared_native=native_channel_for(spec, policy),
        )
        if resolved is None:
            continue
        if has_no_route_here(resolved, policy):
            # Warn and drop before the freeze, so quorum counts what runs.
            at_model = (
                f" at model {spec.model!r}" if spec.model
                # A seat with no model pins no string for the role table to
                # match, so naming one would invent a model it never carried.
                else " with no model pinned"
            )
            roles = native_roles(policy)
            if roles is not None and roles.native_pin(spec) is None:
                pin_field = roles.native_pin_field
                pin_doc = (
                    "plugins/crew/docs/cursor-host.md"
                    if policy.host == "cursor"
                    else "plugins/crew/docs/engine-notes.md"
                )
                reason = (
                    f"which this host spawns in-session only at {pin_field} "
                    "verified once against the app's subagent badge, and "
                    f"[seats.{name}] sets none (add {pin_field} after the badge "
                    f"check; see {pin_doc})"
                )
            else:
                reason = (
                    "which this host drives in-session rather than opening as a "
                    "subprocess"
                )
            print(
                f"warning: review seat {name!r} resolves to the {resolved.channel} "
                f"channel{at_model}, {reason}; dropping it from this panel",
                file=sys.stderr,
            )
            continue
        if any(existing[0] == name for existing in answer):
            raise WorkflowError("duplicate_seat", f"duplicate review seat {name!r}")
        answer.append((name, spec, resolved))
    roles = native_roles(policy)
    if roles is not None and roles.single_seat_per_native_pin:
        # Resolution order decides which seat survives, not catalog order. The
        # selection puts subprocess seats first, then Task seats; explicit seat
        # requests, groups, and presets preserve their catalog or list order.
        first_by_pin: dict[str, str] = {}
        deduped: list[tuple[str, object, object]] = []
        for name, spec, execution in answer:
            if execution.native:
                pin = roles.native_pin(spec)
                if pin is not None:
                    first = first_by_pin.get(pin)
                    if first is not None:
                        print(
                            f"warning: review seat {name!r} resolves in-session at "
                            f"{roles.native_pin_field} "
                            f"{pin!r}, the same pin as seat {first!r}; dropping it from this "
                            "panel (one pin is one model, and it votes once)",
                            file=sys.stderr,
                        )
                        continue
                    first_by_pin[pin] = name
            deduped.append((name, spec, execution))
        answer = deduped
    if not answer:
        raise WorkflowError("no_seats", "no review seats resolved")
    return answer


def _spent_model(
    name: str,
    spec: seats.SeatSpec,
    execution: channels.ResolvedExecution,
    roles: HostRoles | None,
) -> str:
    """The model string a seat's frozen identity records: the pin it actually spends."""
    if not execution.native:
        return spec.model or name
    pin = roles.native_pin(spec) if roles is not None else None
    if pin is None:
        pin_field = roles.native_pin_field if roles is not None else "model"
        raise WorkflowError(
            "unresolved_native_role",
            f"seat {name!r} resolved in-session with no {pin_field} to spend; "
            "refusing to freeze it",
        )
    return pin


def _verified_foundation(wf: dict, run: Path) -> tuple[dict, Path]:
    """Validate the immutable run/snapshot authority for mutable workflow state."""
    if not isinstance(wf.get("workflow_identity"), dict):
        raise WorkflowError(
            "obsolete_standalone_workflow",
            "standalone review record is missing workflow_identity; remove "
            "current-standalone-review.json and start again",
        )
    if type(wf.get("schema")) is not int or wf.get("schema") != SCHEMA:
        raise WorkflowError("obsolete_standalone_workflow", "standalone workflow has an unsupported schema")
    try:
        ref = parse_review_ref(wf.get("ref"))
        record = review_runs.read_run_json(run)
        review_runs.verify_run_record(
            record,
            expected_run_id=run.name,
            source=run / review_runs.RUN_JSON_NAME,
        )
    except (WorkflowError, review_runs.ReviewRunError) as exc:
        raise WorkflowError("corrupt_workflow", f"workflow identity is unreadable: {exc}") from exc
    signatures = record.get("seat_signatures")
    valid_identity = (
        ref.run_id == run.name
        and record.get("run_id") == ref.run_id
        and record.get("target_sha256") == ref.target_sha256
        and record.get("workflow_identity") == wf.get("workflow_identity")
        and isinstance(signatures, dict)
        and list(signatures) == wf.get("roster")
    )
    if not valid_identity:
        raise WorkflowError("corrupt_workflow", "workflow does not match immutable run identity")
    target_notes = record.get("target_notes")
    target_diff_cmd = record.get("target_diff_cmd")
    target_descriptor = record.get("target_descriptor")
    snapshot_name = record.get("snapshot")
    target_kind = (
        wf.get("target", {}).get("kind")
        if isinstance(wf.get("target"), dict)
        else None
    )
    prompt_metadata_sha = (record.get("workflow_identity") or {}).get(
        "prompt_metadata_sha256"
    )
    if (
        not isinstance(target_notes, list)
        or any(not isinstance(note, str) for note in target_notes)
        or (target_diff_cmd is not None and not isinstance(target_diff_cmd, str))
        or not isinstance(target_descriptor, str)
        or not isinstance(snapshot_name, str)
        or snapshot_name not in {"target.md", "target.diff"}
        or not isinstance(target_kind, str)
        or target_kind not in {"plan", "code"}
        or snapshot_name != review_runs.snapshot_name(target_kind)
        or not isinstance(prompt_metadata_sha, str)
        or SHA_RE.fullmatch(prompt_metadata_sha) is None
        or prompt_metadata_sha
        != _target_prompt_metadata_sha256(
            target_notes,
            target_diff_cmd,
            snapshot_name,
            target_descriptor,
        )
    ):
        raise WorkflowError(
            "corrupt_workflow",
            "run record carries invalid or unbound reviewer prompt metadata",
        )
    snapshot = run / snapshot_name
    try:
        safe_snapshot = snapshot.is_file() and not snapshot.is_symlink()
        snapshot_sha = (
            hashlib.sha256(snapshot.read_bytes()).hexdigest()
            if safe_snapshot
            else ""
        )
    except OSError as exc:
        raise WorkflowError(
            "corrupt_workflow",
            f"cannot validate immutable target snapshot: {exc}",
        ) from exc
    if not safe_snapshot or snapshot_sha != record["target_sha256"] or snapshot_sha != ref.target_sha256:
        raise WorkflowError(
            "corrupt_workflow",
            "immutable target snapshot is missing, unsafe, or does not match its frozen hash",
        )
    return record, snapshot


def _workflow(run: Path) -> dict:
    wf = _read_object(run / "workflow.json")
    record, snapshot = _verified_foundation(wf, run)
    _validate_workflow(wf, run, record, snapshot)
    return wf


@contextlib.contextmanager
def _workflow_lock(run: Path):
    """Required blocking sibling lock for every workflow transition."""
    expected = _guard_review_path(
        session_segment=run.parent.name,
        run_id=run.name,
        create=False,
    )
    if Path(os.path.abspath(run)) != expected:
        raise WorkflowError(
            "unsafe_review_path",
            f"workflow lock path is not the issued standalone run path: {run}",
        )
    lock_path = run / ".workflow.json.lock"
    try:
        if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
            raise WorkflowError(
                "unsafe_review_path",
                f"workflow lock path is not a regular non-symlink file: {lock_path}",
            )
    except OSError as exc:
        _review_path_error(f"cannot validate workflow lock path {lock_path}: {exc}", exc)
    try:
        with review_runs._sibling_write_lock(
            run / "workflow.json",
            required=True,
        ):
            yield
    except review_runs.ReviewRunError as exc:
        raise WorkflowError("lock_unavailable", str(exc)) from exc


def _verify_host(wf: dict) -> None:
    frozen = (wf.get("workflow_identity") or {}).get("host")
    current = _host()
    if frozen and current != frozen:
        raise WorkflowError(
            "host_mismatch",
            f"current host {current!r} does not match standalone run host {frozen!r}",
            "conflict",
        )


def _load_current_locked(ref: ReviewRef, run: Path | None = None) -> tuple[dict, Path]:
    expected = _guard_review_path(
        session_segment=ref.session_segment,
        run_id=ref.run_id,
        create=False,
    )
    if run is not None and Path(os.path.abspath(run)) != expected:
        raise WorkflowError(
            "unsafe_review_path",
            f"workflow load path is not the issued standalone run path: {run}",
        )
    run = expected
    wf = _workflow(run)
    _verify_host(wf)
    if parse_review_ref(wf.get("ref")) != ref:
        raise WorkflowError("stale_ref", "review reference does not match current workflow", "stale_ref")
    return wf, run


def _pointer_path(session: str) -> Path:
    segment = review_runs.session_segment(session)
    if not segment or not SESSION_RE.fullmatch(segment):
        raise WorkflowError(
            "invalid_pointer",
            "standalone pointer requires a safe non-empty harness session",
        )
    session_dir = _guard_review_path(
        session_segment=segment,
        create=False,
        allow_missing=True,
    )
    return session_dir / STANDALONE_POINTER


def read_standalone_pointer(session: str) -> tuple[dict, dict] | None:
    """Read the exact pointer, then derive its current attempt from workflow."""
    segment = review_runs.session_segment(session)
    path = _pointer_path(session)
    try:
        if path.is_symlink():
            raise WorkflowError(
                "unsafe_review_path",
                f"standalone pointer is a symlink: {path}",
            )
        if not path.exists():
            return None
        if not path.is_file():
            raise WorkflowError(
                "unsafe_review_path",
                f"standalone pointer is not a regular file: {path}",
            )
    except OSError as exc:
        _review_path_error(f"cannot validate standalone pointer {path}: {exc}", exc)
    data = _read_object(path, code="invalid_pointer")
    valid_shape = (
        set(data) == {"schema", "run_id", "identity_digest", "target_sha256"}
        and type(data.get("schema")) is int
        and data.get("schema") == SCHEMA
        and isinstance(data.get("run_id"), str)
        and RUN_RE.fullmatch(data["run_id"]) is not None
        and isinstance(data.get("identity_digest"), str)
        and SHA_RE.fullmatch(data["identity_digest"]) is not None
        and isinstance(data.get("target_sha256"), str)
        and SHA_RE.fullmatch(data["target_sha256"]) is not None
    )
    if not valid_shape:
        raise WorkflowError("invalid_pointer", f"invalid standalone pointer {path}")
    try:
        run = _guard_review_path(
            session_segment=segment,
            run_id=data["run_id"],
            create=False,
            allow_missing=True,
        )
        record = review_runs.read_run_json(run)
        review_runs.verify_run_record(
            record,
            expected_run_id=data["run_id"],
            source=run / review_runs.RUN_JSON_NAME,
        )
    except review_runs.ReviewRunError as exc:
        raise WorkflowError(
            "invalid_pointer",
            f"standalone pointer cannot verify its run record: {exc}",
        ) from exc
    if (
        data["identity_digest"] != record.get("identity_digest")
        or data["target_sha256"] != record.get("target_sha256")
    ):
        raise WorkflowError(
            "invalid_pointer",
            "standalone pointer digest or target hash does not match its run record",
        )
    try:
        wf = _workflow(run)
        ref = parse_review_ref(wf.get("ref"))
    except WorkflowError as exc:
        raise WorkflowError(
            "invalid_pointer",
            f"standalone pointer cannot verify its workflow: {exc.message}",
        ) from exc
    if (
        ref.session_segment != segment
        or ref.run_id != data["run_id"]
        or ref.target_sha256 != data["target_sha256"]
    ):
        raise WorkflowError(
            "invalid_pointer",
            "standalone pointer does not match its workflow reference",
        )
    return data, wf


def _write_pointer_locked(wf: dict) -> None:
    ref = parse_review_ref(wf["ref"])
    run = _guard_review_path(
        session_segment=ref.session_segment,
        run_id=ref.run_id,
        create=False,
    )
    try:
        record = review_runs.read_run_json(run)
        review_runs.verify_run_record(
            record,
            expected_run_id=ref.run_id,
            source=run / review_runs.RUN_JSON_NAME,
        )
    except review_runs.ReviewRunError as exc:
        raise WorkflowError(
            "corrupt_workflow",
            f"cannot write standalone pointer from an unverified run: {exc}",
        ) from exc
    if record.get("target_sha256") != ref.target_sha256:
        raise WorkflowError(
            "corrupt_workflow",
            "cannot write standalone pointer from a mismatched workflow reference",
        )
    pointer = _pointer_path(ref.session_segment)
    try:
        if pointer.is_symlink() or (pointer.exists() and not pointer.is_file()):
            raise WorkflowError(
                "unsafe_review_path",
                f"standalone pointer is not a regular non-symlink file: {pointer}",
            )
    except OSError as exc:
        _review_path_error(f"cannot validate standalone pointer {pointer}: {exc}", exc)
    _atomic(
        pointer,
        {
            "schema": SCHEMA,
            "run_id": record["run_id"],
            "identity_digest": record["identity_digest"],
            "target_sha256": record["target_sha256"],
        },
    )


def _action_id(attempt: int, kind: str, ordinal: int) -> str:
    return f"attempt-{attempt:04d}:{kind}:{ordinal:04d}"


def _attempt_number(attempt_id: str) -> int:
    return int(attempt_id.rsplit("-", 1)[-1])


def _action_attempt(action: dict) -> str:
    return str(action["action_id"]).split(":", 1)[0]


def _hash_action(action_id: str) -> str:
    return hashlib.sha256(action_id.encode("utf-8")).hexdigest()


_REVIEWER_ACTION_KEYS = {
    "action_id", "attempt_id", "ordinal", "kind", "driver", "seat",
    "provider", "role", "model", "channel", "access", "prompt_path",
    "ingress_path", "result_path", "submission_path", "timeout_seconds",
    "return_transport", "status", "ok", "diagnostic", "claim_id",
    "accepted_path", "accepted_sha256", "submission_sha256",
    "reported_model", "model_attribution",
}
_FORMATTER_ACTION_KEYS = {
    "action_id", "attempt_id", "ordinal", "kind", "driver",
    "source_action_id", "seat", "role", "model", "channel", "access",
    "prompt_path", "ingress_path", "result_path", "submission_path",
    "timeout_seconds", "return_transport", "status", "ok", "diagnostic",
    "claim_id", "accepted_path", "accepted_sha256", "submission_sha256",
    # The one field recovery may add: a ready action carries no diagnostic or
    # claim, so a reroute has nowhere else to leave its evidence.
    "rerouted_from",
}
_SYNTHESIS_ACTION_KEYS = {
    "action_id", "attempt_id", "ordinal", "kind", "driver", "seat",
    "role", "model", "channel", "access", "prompt_path", "ingress_path",
    "result_path", "submission_path", "timeout_seconds", "return_transport",
    "status", "ok", "diagnostic", "claim_id", "judgment",
    "accepted_path", "accepted_sha256", "submission_sha256",
}
_ACTION_KEYS = {
    ActionKind.REVIEWER: _REVIEWER_ACTION_KEYS,
    ActionKind.FORMATTER: _FORMATTER_ACTION_KEYS,
    ActionKind.SYNTHESIS: _SYNTHESIS_ACTION_KEYS,
}
_ACTION_STATUSES = {"ready", "claimed", "settled"}
_CLAIM_RE = re.compile(r"^[0-9a-f]{16}$")


def _authoritative_reviewer_prompt(
    run: Path,
    record: dict,
    snapshot: Path,
    seat: str,
) -> str:
    """Regenerate reviewer bytes solely from frozen run and snapshot data."""
    try:
        content = snapshot.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(
            "corrupt_workflow",
            f"cannot read immutable target snapshot as UTF-8: {exc}",
        ) from exc
    kind = "plan" if snapshot.name == "target.md" else "code"
    target_spec = record.get("target_spec")
    descriptor = record.get("target_descriptor")
    if not isinstance(target_spec, str) or not isinstance(descriptor, str):
        raise WorkflowError(
            "corrupt_workflow",
            "run record lacks deterministic reviewer prompt metadata",
        )
    target = targets.Target(
        kind=kind,
        scope=target_spec,
        content=content,
        descriptor=descriptor,
        ref_path=target_spec if kind == "plan" else None,
        replay_spec=target_spec,
        snapshot_path=str(snapshot),
        notes=list(record["target_notes"]),
        diff_cmd=record["target_diff_cmd"],
    )
    prompt_mode = record["workflow_identity"].get("prompt_mode")
    return prompts.build_prompt(
        target,
        seat_role=seat,
        inline=prompt_mode == "inline_diff",
    )


# The model the two Cursor support roles run at. First of the catalog's cursor
# models in cost order: Cursor bills composer from the cheap bucket.
CURSOR_SUPPORT_MODEL = "composer-2.5-fast"

# The two read-only tiers an action may be issued under. ENFORCED means
# something mechanical stops the seat writing (a provider sandbox that refuses
# it, a role whose tool list omits every write); ADVISORY means the read-only is
# what the role prose or the adapter's posture asks for and nothing checks. The
# tier is a property of the individual role or adapter, never of "native" or
# "external" as a category: a Claude reviewer role carries unsandboxed Bash and
# the agy adapter permits in-workspace writes, so each side ships both answers.
# The distinction is stamped rather than flattened because the run record is
# where a reader learns what actually constrained the seat that produced an
# answer.
ACCESS_ENFORCED = "read-only"
ACCESS_ADVISORY = "read-only-advisory"
ACCESS_PARENT = "parent-context"


@dataclass(frozen=True)
class HostRoles:
    """The role names and support-role models one host can drive in-session."""

    channel: str
    scribe_role: str
    scribe_model: str
    formatter_role: str
    formatter_model: str
    reviewer_role_name: str
    native_pin_field: str
    single_seat_per_native_pin: bool
    # How each in-session role is actually held to read-only. The run record
    # stamps these verbatim, so a reader can tell a mechanically enforced
    # boundary from one the role prose asks for. They are per ROLE, not per
    # host: what decides the tier is whether the harness can stop that role
    # writing, and one host can ship both answers.
    reviewer_access: str
    formatter_access: str

    def native_pin(self, spec: seats.SeatSpec) -> str | None:
        """The pin an in-session seat spends on this host, or None for no native route."""
        pin = getattr(spec, self.native_pin_field, None)
        return pin if isinstance(pin, str) and pin else None


_HOST_ROLES: dict[str, HostRoles] = {
    "claude": HostRoles(
        channel="claude",
        scribe_role="crew:scribe",
        scribe_model="haiku",
        formatter_role="crew:formatter",
        formatter_model="haiku",
        reviewer_role_name="crew:reviewer",
        native_pin_field="model",
        # Keep Claude behavior neutral. Config can explicitly declare duplicate
        # pins on this host, and admission does not reject them.
        single_seat_per_native_pin=False,
        # `agents/reviewer.md` is `tools: Read, Grep, Glob, Bash`. The Bash is
        # for git inspection and the role is instructed never to mutate, but
        # nothing sandboxes it, so this seat is read-only by CONVENTION and the
        # record must not claim otherwise.
        reviewer_access=ACCESS_ADVISORY,
        # `agents/formatter.md` is `tools: Read`: a formatter that ignored its
        # prose still could not write.
        formatter_access=ACCESS_ENFORCED,
    ),
    "cursor": HostRoles(
        channel="cursor",
        scribe_role="crew-scribe",
        scribe_model=CURSOR_SUPPORT_MODEL,
        formatter_role="crew-formatter",
        formatter_model=CURSOR_SUPPORT_MODEL,
        reviewer_role_name="crew-reviewer",
        native_pin_field="native_model",
        # Cursor's Task vocabulary has one flattened variant per enabled family;
        # one native pin is one model, so a duplicate must not vote twice.
        single_seat_per_native_pin=True,
        # This host has no per-role tool field at all, so every in-session role
        # inherits the launching session's tools. The adapters ship
        # `readonly: true` and carry the discipline in prose, but no app-surface
        # capture has tested that key, so neither role may claim the enforced
        # tier.
        reviewer_access=ACCESS_ADVISORY,
        formatter_access=ACCESS_ADVISORY,
    ),
}

# How each EXTERNAL channel is actually held to read-only, pinned per provider
# the way the role tiers above are pinned to a role file's `tools:` line.
# "External" is not itself a tier: what decides one is the posture the adapter
# argv establishes, and the adapters do not all establish the same one.
_CHANNEL_ACCESS: dict[str, str] = {
    # `providers/codex.py` runs review seats at `--sandbox read-only`.
    "codex": ACCESS_ENFORCED,
    # `providers/cursor.py` runs review seats under `--mode plan`, which applies
    # no edits (its module records that as verified).
    "cursor": ACCESS_ENFORCED,
    # `providers/claude.py` uses `--permission-mode plan` plus an `--allowedTools`
    # list as its sole read-only boundary.
    "claude": ACCESS_ENFORCED,
    # `providers/agy.py` runs at `--sandbox`, which blocks OUT-of-workspace
    # writes only; that module records in-workspace writes as an accepted
    # residual, so nothing stops this seat mutating the repo it reviews.
    "agy": ACCESS_ADVISORY,
}


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    """The routes ONE run may drive in-session: a host plus its opted-out channels.

    Every route question this module asks (which roles exist, whether a seat is
    admitted native, whether an external seat has anywhere to go) is answered
    from this one value, so the answers cannot disagree across call sites. At
    start it is resolved from the request and config; afterwards it is rebuilt
    from the FROZEN identity, never from live config, so editing the config file
    mid-run cannot change how an already-minted run is judged.
    """

    host: str
    force_external: frozenset[str]

    @classmethod
    def resolve(cls, host: str, force_external: Iterable[str]) -> RoutePolicy:
        return cls(host=host, force_external=frozenset(force_external))

    @classmethod
    def from_identity(cls, identity: Mapping) -> RoutePolicy:
        return cls(
            host=identity["host"],
            force_external=frozenset(identity.get("force_external_channels") or ()),
        )

    def identity_value(self) -> list[str]:
        """The JSON-stable form the run identity records (sorted, so the digest
        does not depend on the order the operator listed the channels in)."""
        return sorted(self.force_external)

    def task_declared_native(self) -> str | None:
        """The narrow Task-recipe channel, minus anything forced external.

        The ROSTER split asks ``channels`` which names are native before the
        per-seat resolution runs, so it has to be told about the opt-out too. A
        split that called a forced channel native would be a third view of the
        one routing fact the rest of this module keeps in a single place.
        """
        channel = channels.task_native_channel(self.host)
        if channel is None or channel in self.force_external:
            return None
        return channel


def _valid_force_external_channels(value: object) -> bool:
    """Whether a frozen identity's forced-external list is well formed.

    The shape rule is not restated here: the value is rebuilt through the same
    mint path that wrote it, so one run's choice has exactly one on-disk
    spelling and cannot present as two different identities."""
    if not isinstance(value, list) or not all(
        isinstance(entry, str) and entry in seats.CHANNEL_TO_LEGACY_KIND
        for entry in value
    ):
        return False
    return RoutePolicy(host="", force_external=frozenset(value)).identity_value() == value


def native_roles(policy: RoutePolicy) -> HostRoles | None:
    """Return the roles this workflow can drive under ``policy``, else None.

    A forced-external channel answers None for the same reason a host with no
    row does: this run drives nothing in-session on it, support roles included.
    A formatter minted native against a role surface the operator has already
    declared unusable would need a recovery call every run to reach the
    parent-context route it should have been minted on.

    It takes the POLICY rather than a host name so that no caller can ask this
    question while holding only half the answer.
    """
    roles = _HOST_ROLES.get(policy.host)
    if roles is None or roles.channel in policy.force_external:
        return None
    return roles


def native_channel_for(spec: seats.SeatSpec, policy: RoutePolicy) -> str | None:
    """Return the native channel this workflow can drive FOR ONE SEAT.

    ``channels`` answers whether a channel is native on a host; this answers
    whether the workflow can actually issue a native action for this seat,
    which depends on the pin held in the host role row. Declaring a native route
    for a seat with no native pin would freeze it as a Task action and then issue
    that action with no pin to spend.

    A host absent from ``_HOST_ROLES`` answers None here even when it has a
    channel row, so a host that gained a channel without roles declares nothing
    native instead of declaring native and failing at mint. That makes the two
    tables' agreement hold by construction; the test pinning their key sets
    equal is the second line of defense, not the only one.

    Admission is keyed on the role row's pin field, not on ``spec.model``, and
    does not consult the seat's own ``via``. What keeps a channel mismatch
    harmless is
    ``channels.resolve_seat``, which honors this declaration only for the
    channel it actually SELECTS from ``spec.via``, so a claude-via seat carrying
    a colliding model string still resolves external. Any change to
    ``resolve_seat`` that widens which channel the declaration applies to must
    add the ``via`` check here, or a seat gets frozen native onto a channel it
    was never routable on.
    """
    channel = channels.native_channel(policy.host)
    if channel is None or channel in policy.force_external:
        return None
    roles = native_roles(policy)
    if roles is None or roles.native_pin(spec) is None:
        return None
    return channel


def has_no_route_here(
    execution: channels.ResolvedExecution, policy: RoutePolicy
) -> bool:
    """Return True when ``execution`` names a route this run cannot take.

    A seat that resolved EXTERNAL onto the host's own native channel has
    nowhere to go under review's routing POLICY: a channel this host drives
    in-session is not also opened as a subprocess from resolution. That is a
    deliberate choice, not a missing binary (the CLI for that channel is
    installed and authenticated on the hosts this applies to). Both consumers
    ask THIS question so neither can answer it differently: roster resolution
    drops such a seat before the freeze, and
    drift reconstruction refuses to re-run an action frozen external before the
    native route existed. Without the shared answer, a seat with no native pin at
    drift time is indistinguishable from a host with no native channel at all,
    and the stale action runs.

    A channel the run forced external is exempt, which is what makes the opt-out
    an escape hatch rather than a way to empty the roster: the whole point of
    forcing it is to send those seats out as subprocesses.

    The question is asked of the SELECTED channel only. A seat whose ``via``
    named a second, runnable channel would still be dropped, which no shipped or
    configurable row can reach today (a multi-entry ``via`` is truncated to one
    at catalog load) and which errs toward refusing a seat rather than running
    it somewhere it was not frozen for.
    """
    if execution.native or execution.channel in policy.force_external:
        return False
    return execution.channel == channels.native_channel(policy.host)


def _require_native_role(
    role: str | None,
    *,
    kind: str,
    seat: str | None,
    model: str | None,
    host: str,
) -> str:
    """Return ``role``, refusing to mint a native action that has none.

    A native action carries the role the host spawns; with no role there is
    nothing to spawn and nothing that may stand in for it. Resolution already
    keeps a roleless seat from being frozen native, so reaching here means the
    two disagree, and the honest answer is a loud refusal rather than an action
    the transport would have to improvise a role for.
    """
    if role is None:
        raise WorkflowError(
            "unresolved_native_role",
            f"no native {kind} role resolves for seat {seat!r} at model {model!r} "
            f"on host {host!r}; refusing to issue a native action with no role to spawn",
        )
    return role


def _reviewer_access(roles: HostRoles | None, *, native: bool, channel: str) -> str:
    """The read-only tier a reviewer action is issued under.

    Mint and validation both read THIS, so a record can never be stamped with
    one tier and judged against another literal somewhere else. Neither may
    compute the tier inline, which is the whole reason this is a function.
    """
    if not native:
        # An external seat is held by its own adapter's sandbox or permission
        # mode, and those postures differ per provider. A channel with no row
        # takes the weaker claim: under-claiming a boundary is safe, and a
        # record that overstates one is the failure this table exists to stop.
        return _CHANNEL_ACCESS.get(channel, ACCESS_ADVISORY)
    # A native action with no role row is refused at mint and read as corrupt at
    # validation, so this branch decides nothing. It claims the weaker tier
    # anyway: an unreachable line must not be the one that overstates a boundary.
    return roles.reviewer_access if roles is not None else ACCESS_ADVISORY


def _reviewer_action(run: Path, ref: ReviewRef, *, ordinal: int, seat: str,
                     model: str, channel: str, driver: str, provider: str,
                     policy: RoutePolicy,
                     timeout_seconds: int | None, prompt: str) -> dict:
    native = driver == ActionDriver.NATIVE
    roles = native_roles(policy)
    reviewer_role = None
    if native:
        # Refuse BEFORE any path is prepared or written: a refusal that had
        # already staged prompts would leave a run dir describing an action
        # nobody minted.
        reviewer_role = _require_native_role(
            roles.reviewer_role_name if roles is not None else None,
            kind="reviewer", seat=seat, model=model, host=policy.host,
        )
        _require_native_role(
            roles.scribe_role if roles is not None else None,
            # The SCRIBE's own model: the transport is what would fail to spawn,
            # and naming the reviewer's model here would send the reader looking
            # at the wrong row of the role table.
            kind="scribe", seat=seat,
            model=roles.scribe_model if roles is not None else None,
            host=policy.host,
        )
    action_id = _action_id(_attempt_number(ref.attempt_id), "reviewer", ordinal)
    digest = _hash_action(action_id)
    root = run / "attempts" / ref.attempt_id
    prompt_path = root / "prompts" / f"reviewer-{ordinal:04d}.txt"
    transport_prompt = root / "transport" / f"scribe-{ordinal:04d}.txt"
    primary_ingress = root / "ingress" / "scribe" / f"{digest}.md"
    fallback_ingress = root / "ingress" / "host-write" / f"{digest}.md"
    result_path = root / "results" / f"{ordinal:04d}.json"
    submission_path = root / "submissions" / f"{digest}.json"
    descendants = [(prompt_path, f"reviewer action {action_id} prompt")]
    if native:
        descendants.extend((
            (transport_prompt, f"reviewer action {action_id} scribe prompt"),
            (primary_ingress, f"reviewer action {action_id} scribe ingress"),
            (fallback_ingress, f"reviewer action {action_id} fallback ingress"),
            (submission_path, f"reviewer action {action_id} submission"),
        ))
    else:
        descendants.append((result_path, f"reviewer action {action_id} result"))
    for descendant, label in descendants:
        _prepare_run_descendant(
            run,
            descendant,
            label,
            create_parents=True,
        )
    _write_run_text(run, prompt_path, prompt, f"reviewer action {action_id} prompt")
    if native:
        _write_run_text(
            run,
            transport_prompt,
            prompts.standalone_scribe_transport(str(primary_ingress)),
            f"reviewer action {action_id} scribe prompt",
        )
    return {
        "action_id": action_id, "attempt_id": ref.attempt_id, "ordinal": ordinal,
        "kind": ActionKind.REVIEWER, "driver": driver, "seat": seat,
        "provider": provider,
        "role": reviewer_role,
        "model": model,
        "channel": channel,
        "access": _reviewer_access(roles, native=native, channel=channel),
        "prompt_path": str(prompt_path),
        "ingress_path": None,
        "result_path": None if native else str(result_path),
        "submission_path": str(submission_path) if native else None,
        "timeout_seconds": None if native else timeout_seconds,
        "return_transport": {
            # Reached only when the mint above found a role, which it cannot do
            # without a role table, so there is no roleless case to guard here.
            "primary": {"kind": "scribe",
                        "role": roles.scribe_role,
                        "model": roles.scribe_model,
                        "prompt_template_path": str(transport_prompt),
                        "data_marker": "{{REVIEWER_RETURN_DATA}}",
                        "ingress_path": str(primary_ingress)},
            "fallback": {"kind": "host_write", "ingress_path": str(fallback_ingress)},
        } if native else None,
        "status": "ready", "ok": None, "diagnostic": None, "claim_id": None,
        "accepted_path": None, "accepted_sha256": None,
        "submission_sha256": None,
        "reported_model": None,
        "model_attribution": (
            ATTRIBUTION_REQUESTED_ONLY if native else None
        ),
    }


def _formatter_route(roles: HostRoles | None) -> dict:
    """Return the route fields a formatter action carries for ``roles``.

    ``None`` means no native role is in play: that covers a host with no role
    row and an action rerouted back to the parent after its native spawn was
    lost. Minting, rerouting, and validation all read this one table, so a
    rerouted action cannot end up describing a route no mint could produce.
    """
    if roles is None:
        return {
            "driver": ActionDriver.PARENT, "role": None, "model": None,
            "channel": None, "access": ACCESS_PARENT,
        }
    return {
        "driver": ActionDriver.NATIVE, "role": roles.formatter_role,
        "model": roles.formatter_model, "channel": roles.channel,
        "access": roles.formatter_access,
    }


def _reroute_lost_formatter(action: dict) -> bool:
    """Send a formatter whose native spawn was lost back out to the parent.

    A repair step must never be the one thing that makes the host a hard
    dependency. Every roster on a host with a role row mints a native formatter,
    including one whose seats all run external and need nothing from the host,
    and a support model the host will not spawn would then cost that seat its
    repair for no reason of its own. The parent-context route is the one codex
    and unknown hosts already take, so the fallback is a shipped path rather than
    a new one.

    A formatter is the one action that may change route this way. A reviewer
    answer belongs to the model that gave it, so a lost reviewer settles
    failed rather than moving transports. The formatter only reshapes an answer
    already given, and its output must still satisfy the findings parser before
    it is accepted. Paths and the action id are untouched, so nothing
    re-derives.

    One reroute at most, structurally: the rerouted action is PARENT, and the
    only recovery code a parent formatter matches settles it failed.
    """
    if action.get("kind") != ActionKind.FORMATTER \
            or action.get("driver") != ActionDriver.NATIVE:
        return False
    action.update(
        **_formatter_route(None),
        rerouted_from="native",
        status="ready", ok=None, diagnostic=None, claim_id=None,
    )
    return True


def _formatter_action(wf: dict, run: Path, source: dict, result: ProviderResult) -> dict:
    ref = parse_review_ref(wf["ref"])
    ordinal = int(source["ordinal"])
    policy = RoutePolicy.from_identity(wf["workflow_identity"])
    roles = native_roles(policy)
    if roles is not None:
        # Same refusal point as the reviewer mint: no paths prepared yet.
        # DEFENSIVE SYMMETRY, not a live path: `formatter_role` is non-optional
        # on every shipped row, so this cannot fire today. It is here so a host
        # row added with no formatter refuses instead of minting an action with
        # nothing to spawn, which is what the reviewer mint beside it does.
        _require_native_role(
            roles.formatter_role, kind="formatter", seat=source["seat"],
            model=roles.formatter_model, host=policy.host,
        )
    action_id = _action_id(_attempt_number(ref.attempt_id), "formatter", ordinal)
    digest = _hash_action(action_id)
    root = run / "attempts" / ref.attempt_id
    prompt_path = root / "prompts" / f"formatter-{ordinal:04d}.txt"
    ingress_path = root / "ingress" / "formatter" / f"{digest}.md"
    submission_path = root / "submissions" / f"{digest}.json"
    for descendant, label in (
        (prompt_path, f"formatter action {action_id} prompt"),
        (ingress_path, f"formatter action {action_id} ingress"),
        (submission_path, f"formatter action {action_id} submission"),
    ):
        _prepare_run_descendant(
            run,
            descendant,
            label,
            create_parents=True,
        )
    _write_run_text(
        run,
        prompt_path,
        prompts.standalone_formatter(result.output),
        f"formatter action {action_id} prompt",
    )
    return {
        "action_id": action_id, "attempt_id": ref.attempt_id, "ordinal": ordinal,
        "kind": ActionKind.FORMATTER,
        **_formatter_route(roles),
        "source_action_id": source["action_id"], "seat": source["seat"],
        "prompt_path": str(prompt_path), "ingress_path": str(ingress_path),
        "result_path": None, "submission_path": str(submission_path),
        "timeout_seconds": None, "return_transport": None, "status": "ready", "ok": None,
        "diagnostic": None, "claim_id": None, "accepted_path": None,
        "accepted_sha256": None, "submission_sha256": None,
        # Every mint starts clean: only recovery sets this, and a later attempt
        # mints its own formatter rather than inheriting an earlier reroute.
        "rerouted_from": None,
    }


def _synthesis_action(wf: dict, run: Path) -> dict:
    ref = parse_review_ref(wf["ref"])
    action_id = _action_id(_attempt_number(ref.attempt_id), "synthesis", 0)
    digest = _hash_action(action_id)
    root = run / "attempts" / ref.attempt_id
    prompt_path = root / "prompts" / "synthesis-0000.txt"
    ingress_path = root / "ingress" / "synthesis" / f"{digest}.md"
    submission_path = root / "submissions" / f"{digest}.json"
    for descendant, label in (
        (prompt_path, f"synthesis action {action_id} prompt"),
        (ingress_path, f"synthesis action {action_id} ingress"),
        (submission_path, f"synthesis action {action_id} submission"),
    ):
        _prepare_run_descendant(
            run,
            descendant,
            label,
            create_parents=True,
        )
    _write_run_text(
        run,
        prompt_path,
        prompts.standalone_synthesis(
            str(run / "panel.md"),
            str(run / "panel-full.md"),
            _effective_artifact_manifest(wf, run),
        ),
        f"synthesis action {action_id} prompt",
    )
    return {
        "action_id": action_id, "attempt_id": ref.attempt_id, "ordinal": 0,
        "kind": ActionKind.SYNTHESIS, "driver": ActionDriver.PARENT,
        "seat": None, "role": None, "model": None, "channel": None,
        "access": ACCESS_PARENT, "prompt_path": str(prompt_path),
        "ingress_path": str(ingress_path),
        "result_path": None, "submission_path": str(submission_path),
        "timeout_seconds": None, "return_transport": None, "status": "ready",
        "ok": None, "diagnostic": None, "claim_id": None, "judgment": None,
        "accepted_path": None, "accepted_sha256": None,
        "submission_sha256": None,
    }


def _load_action(wf: dict, action_id: str) -> dict:
    if not ACTION_RE.fullmatch(action_id):
        raise WorkflowError("invalid_action", f"unknown action {action_id!r}")
    action = next((item for item in wf.get("actions", []) if item.get("action_id") == action_id), None)
    if not isinstance(action, dict):
        raise WorkflowError("invalid_action", f"unknown action {action_id!r}")
    return action


def _current_actions(wf: dict, kind: str | None = None) -> list[dict]:
    attempt = parse_review_ref(wf["ref"]).attempt_id
    return [action for action in wf.get("actions", [])
            if _action_attempt(action) == attempt and (kind is None or action.get("kind") == kind)]


def _issued_artifact_paths(action: dict) -> set[str]:
    if action.get("kind") == ActionKind.REVIEWER:
        if action.get("driver") == ActionDriver.EXTERNAL:
            return {str(action.get("result_path"))}
        transports = action.get("return_transport") or {}
        return {
            str(transport.get("ingress_path"))
            for transport in transports.values()
            if isinstance(transport, dict) and transport.get("ingress_path")
        }
    return {str(action.get("ingress_path"))}


def _read_accepted_bytes(action: dict, run: Path) -> tuple[Path, bytes]:
    path_value = action.get("accepted_path")
    expected_sha = action.get("accepted_sha256")
    if not isinstance(path_value, str) or not SHA_RE.fullmatch(str(expected_sha or "")):
        raise WorkflowError(
            "corrupt_result",
            f"settled action {action.get('action_id')!r} lacks accepted evidence",
        )
    if path_value not in _issued_artifact_paths(action):
        raise WorkflowError(
            "corrupt_result",
            f"accepted evidence for {action['action_id']!r} is not its issued path",
        )
    path = Path(path_value)
    try:
        safe_path = _prepare_run_descendant(
            run,
            path,
            f"accepted evidence for {action['action_id']!r}",
            create_parents=False,
            required_file=True,
            corrupt=True,
        )
        body = safe_path.read_bytes()
    except (OSError, WorkflowError) as exc:
        raise WorkflowError(
            "corrupt_result",
            f"cannot validate accepted evidence for {action['action_id']!r}: {exc}",
        ) from exc
    if hashlib.sha256(body).hexdigest() != expected_sha:
        raise WorkflowError(
            "corrupt_result",
            f"accepted evidence for {action['action_id']!r} is missing, unsafe, or changed",
        )
    return path, body


def _validate_external_result(action: dict, ref: ReviewRef, raw: object) -> ProviderResult:
    if not isinstance(raw, dict):
        raise WorkflowError("corrupt_result", "external reviewer result is not a JSON object")
    required = {
        "name", "model", "ok", "output", "error", "elapsed", "run_id",
        "target_sha256", "action_id", "attempt_id", "channel",
    }
    optional = {"reported_model", "model_attribution"}
    if not required.issubset(raw) or set(raw) - required - optional:
        raise WorkflowError(
            "corrupt_result",
            "external reviewer result has missing or unknown fields",
        )
    reported_model = raw.get("reported_model")
    model_attribution = raw.get("model_attribution")
    if reported_model is not None and not isinstance(reported_model, str):
        raise WorkflowError("corrupt_result", "reported_model must be a string or null")
    if model_attribution is not None and not isinstance(model_attribution, str):
        raise WorkflowError(
            "corrupt_result", "model_attribution must be a string or null"
        )
    derived_attribution = attribution_for(reported_model)
    # An explicit null is absent, exactly as the type check above reads it, so
    # only a non-null value has to agree with the derivation.
    if model_attribution is not None and model_attribution != derived_attribution:
        raise WorkflowError(
            "corrupt_result",
            "model_attribution does not match reported_model",
        )
    frozen = {
        "name": action.get("seat"),
        "model": action.get("model"),
        "channel": action.get("channel"),
        "run_id": ref.run_id,
        "target_sha256": ref.target_sha256,
        "action_id": action.get("action_id"),
        "attempt_id": action.get("attempt_id"),
    }
    mismatch = [name for name, expected in frozen.items() if raw.get(name) != expected]
    elapsed = raw.get("elapsed")
    core_valid = (
        isinstance(raw.get("name"), str)
        and isinstance(raw.get("model"), str)
        and isinstance(raw.get("ok"), bool)
        and isinstance(raw.get("output"), str)
        and (raw.get("error") is None or isinstance(raw.get("error"), str))
        and isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool)
        and math.isfinite(float(elapsed))
        and float(elapsed) >= 0
        and (
            (raw["ok"] and raw["error"] is None)
            or (
                not raw["ok"]
                and isinstance(raw["error"], str)
                and bool(raw["error"].strip())
            )
        )
    )
    if mismatch or not core_valid:
        fields = ", ".join(mismatch or ["core fields"])
        raise WorkflowError(
            "corrupt_result",
            f"external reviewer result does not match frozen {fields}",
        )
    if action.get("status") == "settled":
        settled_ok = action.get("ok")
        if not isinstance(settled_ok, bool):
            raise WorkflowError(
                "corrupt_workflow",
                f"settled external action {action.get('action_id')!r} lacks a boolean result",
            )
        if raw["ok"] is not settled_ok:
            raise WorkflowError(
                "corrupt_result",
                f"external reviewer result ok does not match settled action {action.get('action_id')!r}",
            )
        diagnostic = action.get("diagnostic")
        if settled_ok and raw["error"] is not None:
            raise WorkflowError(
                "corrupt_result",
                f"successful external reviewer result carries an error for {action.get('action_id')!r}",
            )
        if not settled_ok and raw["error"] != diagnostic:
            raise WorkflowError(
                "corrupt_result",
                f"external reviewer error does not match settled diagnostic for {action.get('action_id')!r}",
            )
        if (
            action.get("reported_model") != reported_model
            or action.get("model_attribution") != derived_attribution
        ):
            raise WorkflowError(
                "corrupt_result",
                f"external reviewer attribution does not match settled action {action.get('action_id')!r}",
            )
    return _normalize_reviewer_success(ProviderResult(
        name=raw["name"],
        model=raw["model"],
        ok=raw["ok"],
        output=raw["output"],
        error=raw["error"],
        elapsed=float(elapsed),
        run_id=raw["run_id"],
        target_sha256=raw["target_sha256"],
        action_id=raw["action_id"],
        attempt_id=raw["attempt_id"],
        channel=raw["channel"],
        reported_model=reported_model,
        model_attribution=(
            derived_attribution
            if model_attribution is None
            else model_attribution
        ),
    ))


def _reviewer_output_error(output: object) -> str | None:
    if not isinstance(output, str):
        return "reviewer output is not UTF-8 text"
    try:
        output.encode("utf-8")
    except UnicodeEncodeError:
        return "reviewer output is not valid UTF-8"
    if not output.strip():
        return "empty seat output"
    return None


def _normalize_reviewer_success(result: ProviderResult) -> ProviderResult:
    """Apply the one success-admissibility invariant to provider evidence."""
    if not result.ok:
        return result
    problem = _reviewer_output_error(result.output)
    if problem is not None:
        result.ok = False
        result.output = ""
        result.error = problem
    return result


def _accepted_external_result(wf: dict, action: dict, run: Path) -> ProviderResult:
    _path, body = _read_accepted_bytes(action, run)
    try:
        raw = json.loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WorkflowError("corrupt_result", f"external reviewer result is unreadable: {exc}") from exc
    return _validate_external_result(action, parse_review_ref(wf["ref"]), raw)


def _materialized_reviewer_result(wf: dict, action: dict, run: Path) -> ProviderResult:
    if action.get("status") != "settled":
        raise WorkflowError("corrupt_workflow", "only settled reviewer actions may be projected")
    if action.get("driver") == ActionDriver.EXTERNAL and action.get("accepted_path"):
        result = _accepted_external_result(wf, action, run)
    elif action.get("ok"):
        _path, body = _read_accepted_bytes(action, run)
        try:
            output = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowError("corrupt_result", f"native reviewer result is not UTF-8: {exc}") from exc
        result = ProviderResult(
            name=action["seat"], model=action.get("model"), ok=True,
            output=output, error=None, elapsed=0.0,
        )
        result = _normalize_reviewer_success(result)
    else:
        result = ProviderResult(
            name=action["seat"], model=action.get("model"), ok=False,
            output="", error=action.get("diagnostic") or "reviewer failed",
            elapsed=0.0,
        )
    result.run_id = parse_review_ref(wf["ref"]).run_id
    result.target_sha256 = parse_review_ref(wf["ref"]).target_sha256
    result.action_id = action["action_id"]
    result.attempt_id = action["attempt_id"]
    result.channel = action.get("channel")
    result.reported_model = action.get("reported_model")
    result.model_attribution = action.get("model_attribution")

    formatters = [
        candidate
        for candidate in wf.get("actions", [])
        if candidate.get("kind") == ActionKind.FORMATTER
        and candidate.get("source_action_id") == action["action_id"]
        and candidate.get("status") == "settled"
        and candidate.get("ok") is True
    ]
    if len(formatters) > 1:
        raise WorkflowError("corrupt_workflow", "reviewer action has multiple accepted formatters")
    if formatters:
        _formatter_path, repaired = _read_accepted_bytes(formatters[0], run)
        try:
            result.repaired_output = repaired.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowError("corrupt_result", f"formatter result is not UTF-8: {exc}") from exc
        if not findings.parse_seat(result).findings_parsed:
            raise WorkflowError("corrupt_result", "accepted formatter result no longer satisfies the parser")
    return result


def _effective_reviewers(
    wf: dict,
    run: Path,
    *,
    max_attempt_number: int | None = None,
) -> list[dict]:
    """Frozen-roster projection: prefer a success, else latest settled failure."""
    projected: list[dict] = []
    for seat in wf["roster"]:
        settled = [action for action in wf.get("actions", [])
                   if action.get("kind") == ActionKind.REVIEWER and action.get("seat") == seat
                   and action.get("status") == "settled"
                   and (
                       max_attempt_number is None
                       or _attempt_number(action["attempt_id"]) <= max_attempt_number
                   )]
        successes = [action for action in settled if _materialized_reviewer_result(wf, action, run).ok]
        candidates = successes or settled
        if candidates:
            projected.append(max(candidates, key=lambda action: _attempt_number(action["attempt_id"])))
    return projected


def _effective_results(
    wf: dict,
    run: Path,
    *,
    max_attempt_number: int | None = None,
) -> list[tuple[dict, ProviderResult]]:
    by_seat = {
        action["seat"]: action
        for action in _effective_reviewers(
            wf,
            run,
            max_attempt_number=max_attempt_number,
        )
    }
    return [
        (by_seat[seat], _materialized_reviewer_result(wf, by_seat[seat], run))
        for seat in wf["roster"]
        if seat in by_seat
    ]


def _effective_artifact_manifest(
    wf: dict,
    run: Path,
    *,
    max_attempt_number: int | None = None,
) -> list[tuple[str, str]]:
    manifest: list[tuple[str, str]] = []
    for action, result in _effective_results(
        wf,
        run,
        max_attempt_number=max_attempt_number,
    ):
        if not result.ok:
            continue
        formatter = next(
            (
                candidate
                for candidate in wf.get("actions", [])
                if candidate.get("kind") == ActionKind.FORMATTER
                and candidate.get("source_action_id") == action["action_id"]
                and candidate.get("status") == "settled"
                and candidate.get("ok") is True
                and (
                    max_attempt_number is None
                    or _attempt_number(candidate["attempt_id"]) <= max_attempt_number
                )
            ),
            None,
        )
        evidence = formatter or action
        _read_accepted_bytes(evidence, run)
        manifest.append((action["seat"], evidence["accepted_path"]))
    return manifest


def _usable_seats(
    effective_results: list[tuple[dict, ProviderResult]],
) -> set[str]:
    return {action["seat"] for action, result in effective_results if result.ok}


def _panel(wf: dict, run: Path) -> dict:
    effective_results = _effective_results(wf, run)
    effective = {action["seat"]: result for action, result in effective_results}
    successes = _usable_seats(effective_results)
    pending = [seat for seat in wf["roster"] if seat not in successes]
    failed = [seat for seat in wf["roster"] if seat in effective and seat not in successes]
    expected, usable = len(wf["roster"]), len(successes)
    return {"expected": expected, "usable": usable, "quorum_met": usable > expected / 2,
            "pending": pending, "failed": failed}


def _reviewer_barrier(wf: dict) -> bool:
    reviewers = _current_actions(wf, ActionKind.REVIEWER)
    if not reviewers:
        return bool(_current_actions(wf, ActionKind.SYNTHESIS))
    return all(action.get("status") == "settled" for action in reviewers)


def _canonical_panel_bytes(wf: dict, run: Path) -> _CanonicalPanelBytes:
    effective_results = _effective_results(wf, run)
    results = [result for _action, result in effective_results]
    usable_seats = _usable_seats(effective_results)
    panel = _panel(wf, run)
    return _CanonicalPanelBytes(
        grouped=findings.render_digest(
            results,
            quorum=(panel["expected"], panel["usable"]),
            usable_seats=usable_seats,
        ).encode("utf-8"),
        full=render.render_panel(results).encode("utf-8"),
    )


def _render_effective_panel_locked(wf: dict, run: Path) -> None:
    canonical = _canonical_panel_bytes(wf, run)
    panel_path = run / "panel.md"
    full_path = run / "panel-full.md"
    _prepare_run_descendant(
        run, panel_path, "standalone panel digest", create_parents=True,
    )
    _prepare_run_descendant(
        run, full_path, "standalone full panel", create_parents=True,
    )
    _write_run_text(
        run,
        panel_path,
        canonical.grouped.decode("utf-8"),
        "standalone panel digest",
    )
    _write_run_text(
        run,
        full_path,
        canonical.full.decode("utf-8"),
        "standalone full panel",
    )


def _reconcile_external_results_locked(wf: dict, run: Path) -> None:
    ref = parse_review_ref(wf["ref"])
    for action in _current_actions(wf, ActionKind.REVIEWER):
        if action.get("driver") != ActionDriver.EXTERNAL or action.get("status") != "claimed":
            continue
        path = Path(action["result_path"])
        if not path.is_file():
            continue
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise WorkflowError("corrupt_result", f"cannot read external reviewer result: {exc}") from exc
        candidate = {
            **action,
            "accepted_path": str(path),
            "accepted_sha256": hashlib.sha256(body).hexdigest(),
        }
        result = _accepted_external_result(wf, candidate, run)
        action.update(
            status="settled",
            ok=bool(result.ok),
            diagnostic=result.error if result.error else (None if result.ok else "reviewer failed"),
            accepted_path=str(path),
            accepted_sha256=candidate["accepted_sha256"],
            reported_model=result.reported_model,
            model_attribution=result.model_attribution,
        )


def _ensure_followups_locked(wf: dict, run: Path) -> None:
    if not _reviewer_barrier(wf):
        return
    if (
        not _current_actions(wf, ActionKind.REVIEWER)
        and _current_actions(wf, ActionKind.SYNTHESIS)
    ):
        _render_effective_panel_locked(wf, run)
        return
    by_source = {action.get("source_action_id"): action for action in _current_actions(wf, ActionKind.FORMATTER)}
    for source, result in _effective_results(wf, run):
        if result.ok and not findings.parse_seat(result).findings_parsed and source["action_id"] not in by_source:
            formatter = _formatter_action(wf, run, source, result)
            wf["actions"].append(formatter)
            by_source[source["action_id"]] = formatter
    formatters = _current_actions(wf, ActionKind.FORMATTER)
    if any(action.get("status") != "settled" for action in formatters):
        return
    _render_effective_panel_locked(wf, run)
    if _panel(wf, run)["usable"] and not _current_actions(wf, ActionKind.SYNTHESIS):
        wf["actions"].append(_synthesis_action(wf, run))


def _host_result_template(ref: ReviewRef, action: dict) -> dict | None:
    if not action.get("submission_path"):
        return None
    artifact_path = action.get("ingress_path")
    if action.get("kind") == ActionKind.REVIEWER and action.get("driver") == ActionDriver.NATIVE:
        artifact_path = action["return_transport"]["primary"]["ingress_path"]
    return {
        "schema": SCHEMA, "ref": review_ref_to_dict(ref), "action_id": action["action_id"],
        "status": "ok", "artifact": {"path": artifact_path, "sha256": "<sha256-of-artifact>"},
        "judgment": None,
        "diagnostic": None,
    }


def _work_item(action: dict, ref: ReviewRef) -> WorkItem:
    return WorkItem(action["action_id"], str(action["kind"]), str(action["driver"]),
                    action.get("seat"), action.get("role"), action.get("model"),
                    action.get("channel"), action["access"], action["prompt_path"],
                    action.get("ingress_path"), action.get("result_path"),
                    action.get("submission_path"), action.get("timeout_seconds"),
                    action.get("return_transport"), _host_result_template(ref, action))


def _derive_step(wf: dict, run: Path) -> ReviewStep:
    ref, target = parse_review_ref(wf["ref"]), wf.get("target")
    current = _current_actions(wf)
    ready = [action for action in current if action.get("status") == "ready"]
    claimed = [action["action_id"] for action in current if action.get("status") == "claimed"]
    panel = _panel(wf, run)
    common = {"ref": ref, "resolved_target": target, "display": (target or {}).get("display"), "panel": panel}
    if ready:
        return ReviewStep(
            StepType.WORK_BATCH,
            work_items=tuple(_work_item(action, ref) for action in ready),
            in_flight=tuple(claimed),
            **common,
        )
    if claimed:
        return ReviewStep(StepType.WAITING, in_flight=tuple(claimed), **common)
    if not _reviewer_barrier(wf):
        return ReviewStep(StepType.WAITING, **common)
    if panel["usable"] == 0:
        return ReviewStep(StepType.TERMINAL, outcome={"status": "all_failed", "judgment": None,
            "minor_only": False, **panel, "diagnostic": "all reviewer actions failed",
            "panel_path": str(run / "panel.md"), "full_path": str(run / "panel-full.md"),
            "synthesis_path": None}, **common)
    synthesis = next(iter(_current_actions(wf, ActionKind.SYNTHESIS)), None)
    if synthesis is None or synthesis.get("status") != "settled":
        return ReviewStep(StepType.WAITING, **common)
    failed, judgment = not synthesis.get("ok"), synthesis.get("judgment") or {}
    synthesis_path = None
    if not failed:
        synthesis_path, _body = _read_accepted_bytes(synthesis, run)
    synthesis_path_value = str(synthesis_path) if synthesis_path is not None else None
    status = (
        "synthesis_failed"
        if failed
        else "complete"
        if panel["quorum_met"]
        else "quorum_not_met"
    )
    diagnostic = synthesis.get("diagnostic") if failed else None
    if status == "quorum_not_met":
        diagnostic = "strict-majority quorum not met; outcome is non-certifying"
    return ReviewStep(StepType.TERMINAL, outcome={
        "status": status,
        "judgment": None if failed else judgment.get("verdict"),
        "minor_only": False if failed else bool(judgment.get("minor_only", False)),
        **panel, "diagnostic": diagnostic,
        "panel_path": str(run / "panel.md"), "full_path": str(run / "panel-full.md"),
        "synthesis_path": synthesis_path_value}, **common)


def _standalone_run_terminal_for_prune(
    run: Path,
    expected_session_segment: str,
) -> bool:
    """Classify a guarded standalone run through the canonical workflow engine.

    This is an internal, read-only pruning seam rather than an additional public
    workflow operation. Any validation error is deliberately left for the
    pruning caller to interpret conservatively as "protect this run".
    """
    guarded = _guard_review_path(
        session_segment=expected_session_segment,
        run_id=run.name,
        create=False,
    )
    lexical_run = Path(os.path.abspath(run))
    if guarded != lexical_run or run != lexical_run:
        raise WorkflowError(
            "unsafe_review_path",
            "prune classifier run is not the exact guarded standalone path",
        )
    wf = _workflow(run)
    ref = parse_review_ref(wf["ref"])
    if (
        ref.session_segment != expected_session_segment
        or ref.run_id != run.name
    ):
        raise WorkflowError(
            "corrupt_workflow",
            "standalone workflow reference does not match its inspected location",
        )
    return _derive_step(wf, run).type == StepType.TERMINAL


def _corrupt_workflow(message: str) -> None:
    raise WorkflowError("corrupt_workflow", message)


def _validate_issued_path(
    run: Path,
    value: object,
    expected: Path,
    label: str,
    *,
    required_file: bool = False,
) -> Path:
    """Validate an engine-derived path through the shared descendant walker."""
    if not isinstance(value, str) or value != str(expected) or not expected.is_absolute():
        _corrupt_workflow(f"{label} is not its exact issued absolute path")
    return _prepare_run_descendant(
        run,
        expected,
        label,
        create_parents=False,
        required_file=required_file,
        corrupt=True,
    )


def _read_authoritative_prompt(path: Path, expected: str, label: str) -> None:
    try:
        actual = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise WorkflowError(
            "corrupt_workflow",
            f"cannot read {label}: {exc}",
        ) from exc
    if actual != expected:
        _corrupt_workflow(f"{label} bytes do not match the Python-owned prompt")


def _validate_action_lifecycle(action: dict, run: Path) -> None:
    status = action["status"]
    ok = action["ok"]
    diagnostic = action["diagnostic"]
    claim_id = action["claim_id"]
    accepted_path = action["accepted_path"]
    accepted_sha = action["accepted_sha256"]
    submission_sha = action["submission_sha256"]
    if ok is not None and not isinstance(ok, bool):
        _corrupt_workflow(f"action {action['action_id']!r} has invalid ok state")
    if accepted_path is not None and not isinstance(accepted_path, str):
        _corrupt_workflow(f"action {action['action_id']!r} has an invalid accepted path")
    if diagnostic is not None and not isinstance(diagnostic, str):
        _corrupt_workflow(f"action {action['action_id']!r} has invalid diagnostic")
    if (accepted_path is None) != (accepted_sha is None):
        _corrupt_workflow(f"action {action['action_id']!r} has partial accepted evidence")
    if accepted_sha is not None and (
        not isinstance(accepted_sha, str) or SHA_RE.fullmatch(accepted_sha) is None
    ):
        _corrupt_workflow(f"action {action['action_id']!r} has invalid accepted hash")
    if submission_sha is not None and (
        not isinstance(submission_sha, str) or SHA_RE.fullmatch(submission_sha) is None
    ):
        _corrupt_workflow(f"action {action['action_id']!r} has invalid submission hash")
    if status == "ready":
        if any(value is not None for value in (
            ok, diagnostic, claim_id, accepted_path, accepted_sha, submission_sha,
        )):
            _corrupt_workflow(f"ready action {action['action_id']!r} carries lifecycle residue")
    elif status == "claimed":
        if not isinstance(claim_id, str) or _CLAIM_RE.fullmatch(claim_id) is None:
            _corrupt_workflow(f"claimed action {action['action_id']!r} lacks a valid claim")
        if any(value is not None for value in (
            ok, diagnostic, accepted_path, accepted_sha, submission_sha,
        )):
            _corrupt_workflow(f"claimed action {action['action_id']!r} carries settlement residue")
    else:
        if not isinstance(claim_id, str) or _CLAIM_RE.fullmatch(claim_id) is None:
            _corrupt_workflow(f"settled action {action['action_id']!r} lacks a valid claim")
        if not isinstance(ok, bool):
            _corrupt_workflow(f"settled action {action['action_id']!r} lacks a boolean result")
        if ok and diagnostic is not None:
            _corrupt_workflow(f"successful action {action['action_id']!r} carries a diagnostic")
        if not ok and (not isinstance(diagnostic, str) or not diagnostic.strip()):
            _corrupt_workflow(f"failed action {action['action_id']!r} lacks a diagnostic")
        if ok and accepted_path is None:
            _corrupt_workflow(f"successful action {action['action_id']!r} lacks accepted evidence")
    if accepted_path is not None:
        if accepted_path not in _issued_artifact_paths(action):
            _corrupt_workflow(f"action {action['action_id']!r} accepted an unissued path")
        _validate_issued_path(
            run,
            accepted_path,
            Path(accepted_path),
            f"action {action['action_id']} accepted path",
            required_file=True,
        )


def _validate_retry_receipts(wf: dict, current_attempt: int) -> None:
    receipts = wf["retry_receipts"]
    if not isinstance(receipts, dict):
        _corrupt_workflow("retry_receipts is not an object")
    seen_sources: set[str] = set()
    receipts_by_created: dict[str, dict] = {}
    actions = {action["action_id"]: action for action in wf["actions"]}
    for key, receipt in receipts.items():
        if not isinstance(key, str) or not isinstance(receipt, dict):
            _corrupt_workflow("retry receipt key/value is invalid")
        kind = receipt.get("kind")
        if kind == "seat_retry":
            required = {
                "kind", "source_attempt_id", "normalized_seats",
                "request_sha256", "created_attempt_id", "created_action_ids",
            }
        elif kind == "synthesis_restart":
            required = {
                "kind", "source_attempt_id", "created_attempt_id",
                "created_action_id",
            }
        else:
            _corrupt_workflow("retry receipt kind is invalid")
        if set(receipt) != required:
            _corrupt_workflow("retry receipt has missing or unknown fields")
        source = receipt["source_attempt_id"]
        created = receipt["created_attempt_id"]
        if (
            not isinstance(source, str)
            or ATTEMPT_RE.fullmatch(source) is None
            or not isinstance(created, str)
            or ATTEMPT_RE.fullmatch(created) is None
            or _attempt_number(created) != _attempt_number(source) + 1
            or source in seen_sources
            or created in receipts_by_created
        ):
            _corrupt_workflow("retry receipt attempt linkage is invalid")
        seen_sources.add(source)
        receipts_by_created[created] = receipt
        if kind == "seat_retry":
            names = receipt["normalized_seats"]
            created_ids = receipt["created_action_ids"]
            if (
                not isinstance(names, list)
                or not names
                or any(not isinstance(name, str) for name in names)
                or len(set(names)) != len(names)
                or names != [seat for seat in wf["roster"] if seat in names]
                or not isinstance(created_ids, list)
                or receipt["request_sha256"] != _retry_sha(names)
                or key != f"seat_retry:{source}:{receipt['request_sha256']}"
            ):
                _corrupt_workflow("seat retry receipt is invalid")
            expected_actions = sorted(
                (
                    action
                    for action in wf["actions"]
                    if action["attempt_id"] == created
                    and action["kind"] == ActionKind.REVIEWER
                ),
                key=lambda action: (action["ordinal"], action["action_id"]),
            )
            expected_ids = [action["action_id"] for action in expected_actions]
            expected_names = [action["seat"] for action in expected_actions]
            if (
                created_ids != expected_ids
                or names != expected_names
                or any(action_id not in actions for action_id in created_ids)
            ):
                _corrupt_workflow("seat retry receipt action linkage is invalid")
        else:
            action_id = receipt["created_action_id"]
            if (
                key != f"synthesis_restart:{source}"
                or not isinstance(action_id, str)
                or action_id not in actions
                or actions[action_id]["attempt_id"] != created
                or actions[action_id]["kind"] != ActionKind.SYNTHESIS
            ):
                _corrupt_workflow("synthesis restart receipt action linkage is invalid")

    expected_created = {
        f"attempt-{attempt_number:04d}"
        for attempt_number in range(2, current_attempt + 1)
    }
    if set(receipts_by_created) != expected_created:
        _corrupt_workflow("retry receipt chain does not cover every post-initial attempt")
    for attempt_number in range(2, current_attempt + 1):
        created = f"attempt-{attempt_number:04d}"
        expected_source = f"attempt-{attempt_number - 1:04d}"
        if receipts_by_created[created]["source_attempt_id"] != expected_source:
            _corrupt_workflow("retry receipt chain is not contiguous")


def _validate_workflow(wf: dict, run: Path, record: dict, snapshot: Path) -> None:
    """Fail closed on every mutable standalone action and workflow field."""
    if set(wf) != {
        "schema", "workflow_identity", "ref", "target", "roster", "actions",
        "retry_receipts",
    }:
        _corrupt_workflow("workflow has missing or unknown fields")
    identity = wf["workflow_identity"]
    if (
        not isinstance(identity, dict)
        or set(identity) != {
            "kind", "host", "force_external_channels", "prompt_mode",
            "timeout_seconds", "provider_timeouts", "prompt_metadata_sha256",
        }
        or identity.get("kind") != "standalone_review"
        or not isinstance(identity.get("host"), str)
        or identity.get("host") not in HOSTS
        or not _valid_force_external_channels(identity.get("force_external_channels"))
        or not isinstance(identity.get("prompt_mode"), str)
        or identity.get("prompt_mode") not in {"standard", "inline_diff"}
        or not isinstance(identity.get("timeout_seconds"), int)
        or isinstance(identity.get("timeout_seconds"), bool)
        or not 0 < identity["timeout_seconds"] <= 540
        or not isinstance(identity.get("prompt_metadata_sha256"), str)
        or SHA_RE.fullmatch(identity["prompt_metadata_sha256"]) is None
        or not isinstance(record.get("snapshot"), str)
        or record.get("snapshot") not in {"target.md", "target.diff"}
        or not isinstance(record.get("target_descriptor"), str)
        or identity["prompt_metadata_sha256"]
        != _target_prompt_metadata_sha256(
            record.get("target_notes"),
            record.get("target_diff_cmd"),
            record.get("snapshot"),
            record.get("target_descriptor"),
        )
    ):
        _corrupt_workflow("standalone workflow identity is invalid")
    ref = parse_review_ref(wf["ref"])
    target = wf["target"]
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "scope", "base", "state", "descriptor", "sha256", "display"}
        or not isinstance(target.get("kind"), str)
        or target.get("kind") not in {"plan", "code"}
        or record.get("snapshot") != review_runs.snapshot_name(target.get("kind"))
        or target.get("kind") != ("plan" if snapshot.name == "target.md" else "code")
        or not all(isinstance(target.get(field), str) for field in (
            "scope", "base", "state", "descriptor", "sha256", "display",
        ))
        or not isinstance(target.get("state"), str)
        or target.get("state") not in {"clean", "dirty"}
        or target.get("sha256") != ref.target_sha256
        or target.get("base") != record.get("target_base")
        or target.get("descriptor") != record.get("target_descriptor")
        or target.get("display") != (
            f"RESOLVED TARGET: kind={target.get('kind')} scope={target.get('scope')} "
            f"base={target.get('base')} state={target.get('state')}"
        )
    ):
        _corrupt_workflow("workflow target metadata is invalid")
    roster = wf["roster"]
    host_roles = native_roles(RoutePolicy.from_identity(identity))
    signatures = record.get("seat_signatures")
    seat_channels = record.get("seat_channels")
    provider_timeouts = identity.get("provider_timeouts")
    if (
        not isinstance(roster, list)
        or not roster
        or any(not isinstance(seat, str) for seat in roster)
        or len(set(roster)) != len(roster)
        or not isinstance(signatures, dict)
        or roster != list(signatures)
        or not isinstance(seat_channels, dict)
        or set(seat_channels) != set(roster)
        or not isinstance(provider_timeouts, dict)
        or set(provider_timeouts) != set(roster)
    ):
        _corrupt_workflow("workflow roster authority is invalid")
    expected_task: list[str] = []
    expected_external: list[str] = []
    for seat in roster:
        signature = signatures[seat]
        channel = seat_channels[seat]
        if (
            not isinstance(signature, dict)
            or not isinstance(signature.get("kind"), str)
            or signature.get("kind") not in {"task", "subprocess"}
        ):
            _corrupt_workflow(f"seat signature for {seat!r} is invalid")
        expected_signature_keys = {"kind", "model"} | (
            {"provider"} if signature["kind"] == "subprocess" else set()
        )
        if (
            set(signature) != expected_signature_keys
            or not isinstance(signature.get("model"), str)
            or not signature["model"]
            or not isinstance(channel, str)
            or seats.CHANNEL_TO_LEGACY_KIND.get(channel) is None
            or (
                signature["kind"] == "subprocess"
                and seats.CHANNEL_TO_LEGACY_KIND[channel] != signature["provider"]
            )
        ):
            _corrupt_workflow(f"seat signature/channel for {seat!r} is invalid")
        timeout_value = provider_timeouts[seat]
        if signature["kind"] == "task":
            expected_task.append(seat)
            if (
                host_roles is None
                or channel != host_roles.channel
                or timeout_value is not None
            ):
                _corrupt_workflow(f"native seat authority for {seat!r} is invalid")
        else:
            expected_external.append(seat)
            if (
                not isinstance(timeout_value, int)
                or isinstance(timeout_value, bool)
                or not 0 < timeout_value <= 540
            ):
                _corrupt_workflow(f"external timeout authority for {seat!r} is invalid")
    if (
        record.get("task_seats") != expected_task
        or record.get("subprocess_seats") != expected_external
        or record.get("task_seat_models")
        != {seat: signatures[seat]["model"] for seat in expected_task}
    ):
        _corrupt_workflow("run roster projections are invalid")
    actions = wf["actions"]
    if not isinstance(actions, list) or not actions or any(not isinstance(action, dict) for action in actions):
        _corrupt_workflow("workflow actions are invalid")
    current_attempt = _attempt_number(ref.attempt_id)
    by_id: dict[str, dict] = {}
    reviewers_by_attempt: dict[str, set[str]] = {}
    synthesis_by_attempt: dict[str, int] = {}
    expected_prompt_paths: dict[str, Path] = {}
    expected_transport_prompts: dict[str, Path] = {}
    for action in actions:
        kind = action.get("kind")
        if not isinstance(kind, str) or kind not in _ACTION_KEYS or set(action) != _ACTION_KEYS[kind]:
            _corrupt_workflow("action has missing, unknown, or kind-incompatible fields")
        action_id = action["action_id"]
        match = ACTION_RE.fullmatch(action_id) if isinstance(action_id, str) else None
        attempt_id = action["attempt_id"]
        ordinal = action["ordinal"]
        if (
            match is None
            or action_id in by_id
            or not isinstance(attempt_id, str)
            or ATTEMPT_RE.fullmatch(attempt_id) is None
            or action_id.split(":", 1)[0] != attempt_id
            or match.group(1) != kind
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or action_id != _action_id(_attempt_number(attempt_id), kind, ordinal)
            or not 1 <= _attempt_number(attempt_id) <= current_attempt
            or not isinstance(action.get("driver"), str)
            or action.get("driver") not in {member.value for member in ActionDriver}
            or not isinstance(action.get("status"), str)
            or action.get("status") not in _ACTION_STATUSES
        ):
            _corrupt_workflow(f"action identity is invalid: {action_id!r}")
        by_id[action_id] = action
        root = run / "attempts" / attempt_id
        digest = _hash_action(action_id)
        if kind == ActionKind.REVIEWER:
            seat = action["seat"]
            if seat not in roster or ordinal != roster.index(seat) + 1:
                _corrupt_workflow(f"reviewer action {action_id!r} has invalid seat/ordinal")
            if seat in reviewers_by_attempt.setdefault(attempt_id, set()):
                _corrupt_workflow(f"attempt {attempt_id!r} repeats reviewer seat {seat!r}")
            reviewers_by_attempt[attempt_id].add(seat)
            signature = signatures[seat]
            native = signature["kind"] == "task"
            reported_model = action.get("reported_model")
            model_attribution = action.get("model_attribution")
            if native:
                if (
                    reported_model is not None
                    or model_attribution != ATTRIBUTION_REQUESTED_ONLY
                ):
                    _corrupt_workflow(
                        f"native reviewer action {action_id!r} has invalid attribution"
                    )
            else:
                if (
                    reported_model is not None
                    and not isinstance(reported_model, str)
                ):
                    _corrupt_workflow(
                        f"external reviewer action {action_id!r} has invalid reported model"
                    )
                if (
                    model_attribution is not None
                    and not isinstance(model_attribution, str)
                ):
                    _corrupt_workflow(
                        f"external reviewer action {action_id!r} has invalid attribution"
                    )
                if action["status"] != "settled" and (
                    reported_model is not None or model_attribution is not None
                ):
                    _corrupt_workflow(
                        f"external reviewer action {action_id!r} has premature attribution"
                    )
                if action["status"] == "settled" and model_attribution != attribution_for(
                    reported_model
                ):
                    _corrupt_workflow(
                        f"external reviewer action {action_id!r} has mismatched attribution"
                    )
            channel = seat_channels[seat]
            expected_role = (
                host_roles.reviewer_role_name
                if native and host_roles is not None
                else None
            )
            if (
                action["driver"] != (ActionDriver.NATIVE if native else ActionDriver.EXTERNAL)
                or action["model"] != signature["model"]
                or action["channel"] != channel
                or action["provider"] != seats.CHANNEL_TO_LEGACY_KIND[channel]
                or action["role"] != expected_role
                or action["access"] != _reviewer_access(
                    host_roles, native=native, channel=channel,
                )
                or action["timeout_seconds"] != provider_timeouts[seat]
                or action["ingress_path"] is not None
            ):
                _corrupt_workflow(f"reviewer action {action_id!r} violates its frozen route")
            prompt_path = _validate_issued_path(
                run, action["prompt_path"], root / "prompts" / f"reviewer-{ordinal:04d}.txt",
                f"reviewer action {action_id} prompt", required_file=True,
            )
            expected_prompt_paths[action_id] = prompt_path
            if native:
                expected_submission = root / "submissions" / f"{digest}.json"
                _validate_issued_path(run, action["submission_path"], expected_submission,
                                      f"reviewer action {action_id} submission")
                if action["result_path"] is not None:
                    _corrupt_workflow(f"native reviewer action {action_id!r} has a result path")
                transport = action["return_transport"]
                primary_ingress = root / "ingress" / "scribe" / f"{digest}.md"
                fallback_ingress = root / "ingress" / "host-write" / f"{digest}.md"
                transport_prompt = root / "transport" / f"scribe-{ordinal:04d}.txt"
                if (
                    not isinstance(transport, dict)
                    or set(transport) != {"primary", "fallback"}
                    or not isinstance(transport.get("primary"), dict)
                    or set(transport["primary"]) != {
                        "kind", "role", "model", "prompt_template_path",
                        "data_marker", "ingress_path",
                    }
                    or transport["primary"].get("kind") != "scribe"
                    or host_roles is None
                    or transport["primary"].get("role") != host_roles.scribe_role
                    or transport["primary"].get("model") != host_roles.scribe_model
                    or transport["primary"].get("data_marker") != "{{REVIEWER_RETURN_DATA}}"
                    or not isinstance(transport.get("fallback"), dict)
                    or set(transport["fallback"]) != {"kind", "ingress_path"}
                    or transport["fallback"].get("kind") != "host_write"
                ):
                    _corrupt_workflow(f"native reviewer action {action_id!r} has invalid transport")
                _validate_issued_path(run, transport["primary"]["prompt_template_path"], transport_prompt,
                                      f"reviewer action {action_id} scribe prompt", required_file=True)
                _validate_issued_path(run, transport["primary"]["ingress_path"], primary_ingress,
                                      f"reviewer action {action_id} scribe ingress")
                _validate_issued_path(run, transport["fallback"]["ingress_path"], fallback_ingress,
                                      f"reviewer action {action_id} fallback ingress")
                expected_transport_prompts[action_id] = transport_prompt
            else:
                expected_result = root / "results" / f"{ordinal:04d}.json"
                _validate_issued_path(run, action["result_path"], expected_result,
                                      f"reviewer action {action_id} result")
                if action["submission_path"] is not None or action["return_transport"] is not None:
                    _corrupt_workflow(f"external reviewer action {action_id!r} has native transport")
        elif kind == ActionKind.FORMATTER:
            prompt_path = _validate_issued_path(
                run, action["prompt_path"], root / "prompts" / f"formatter-{ordinal:04d}.txt",
                f"formatter action {action_id} prompt", required_file=True,
            )
            expected_prompt_paths[action_id] = prompt_path
            _validate_issued_path(run, action["ingress_path"],
                                  root / "ingress" / "formatter" / f"{digest}.md",
                                  f"formatter action {action_id} ingress")
            _validate_issued_path(run, action["submission_path"],
                                  root / "submissions" / f"{digest}.json",
                                  f"formatter action {action_id} submission")
            rerouted = action["rerouted_from"]
            # Only a host that could mint a native formatter can have lost one.
            if rerouted not in (None, "native") or (rerouted is not None and host_roles is None):
                _corrupt_workflow(f"formatter action {action_id!r} has an invalid reroute mark")
            expected_route = _formatter_route(None if rerouted else host_roles)
            if (
                any(action[field] != value for field, value in expected_route.items())
                or action["result_path"] is not None
                or action["timeout_seconds"] is not None
                or action["return_transport"] is not None
            ):
                _corrupt_workflow(f"formatter action {action_id!r} violates its frozen route")
        else:
            synthesis_by_attempt[attempt_id] = synthesis_by_attempt.get(attempt_id, 0) + 1
            if synthesis_by_attempt[attempt_id] != 1 or ordinal != 0:
                _corrupt_workflow(f"attempt {attempt_id!r} has invalid synthesis cardinality")
            prompt_path = _validate_issued_path(
                run, action["prompt_path"], root / "prompts" / "synthesis-0000.txt",
                f"synthesis action {action_id} prompt", required_file=True,
            )
            expected_prompt_paths[action_id] = prompt_path
            _validate_issued_path(run, action["ingress_path"],
                                  root / "ingress" / "synthesis" / f"{digest}.md",
                                  f"synthesis action {action_id} ingress")
            _validate_issued_path(run, action["submission_path"],
                                  root / "submissions" / f"{digest}.json",
                                  f"synthesis action {action_id} submission")
            if (
                action["driver"] != ActionDriver.PARENT
                or any(action[field] is not None for field in (
                    "seat", "role", "model", "channel", "result_path",
                    "timeout_seconds", "return_transport",
                ))
                or action["access"] != ACCESS_PARENT
            ):
                _corrupt_workflow(f"synthesis action {action_id!r} violates its frozen route")
        _validate_action_lifecycle(action, run)
        if action["driver"] == ActionDriver.EXTERNAL and action["submission_sha256"] is not None:
            _corrupt_workflow(f"external action {action_id!r} carries a submission hash")
    if reviewers_by_attempt.get("attempt-0001") != set(roster):
        _corrupt_workflow("initial attempt does not contain the complete frozen roster")
    if max(_attempt_number(action["attempt_id"]) for action in actions) != current_attempt:
        _corrupt_workflow("active ref does not name the newest action attempt")
    for action in actions:
        action_id = action["action_id"]
        if action["kind"] == ActionKind.FORMATTER:
            source_id = action["source_action_id"]
            if not isinstance(source_id, str):
                _corrupt_workflow(f"formatter action {action_id!r} has an invalid source reference")
            source = by_id.get(source_id)
            if (
                source is None
                or source["kind"] != ActionKind.REVIEWER
                or source["status"] != "settled"
                or source["ok"] is not True
                or action["seat"] != source["seat"]
                or action["ordinal"] != source["ordinal"]
                or _attempt_number(action["attempt_id"]) < _attempt_number(source["attempt_id"])
            ):
                _corrupt_workflow(f"formatter action {action_id!r} has an invalid source")
            source_result = _materialized_reviewer_result(wf, source, run)
            _read_authoritative_prompt(
                expected_prompt_paths[action_id],
                prompts.standalone_formatter(source_result.output),
                f"formatter action {action_id} prompt",
            )
        elif action["kind"] == ActionKind.SYNTHESIS:
            attempt_number = _attempt_number(action["attempt_id"])
            expected = prompts.standalone_synthesis(
                str(run / "panel.md"),
                str(run / "panel-full.md"),
                _effective_artifact_manifest(
                    wf,
                    run,
                    max_attempt_number=attempt_number,
                ),
            )
            _read_authoritative_prompt(
                expected_prompt_paths[action_id],
                expected,
                f"synthesis action {action_id} prompt",
            )
            judgment = action["judgment"]
            if action["status"] == "settled" and action["ok"] is True:
                if (
                    not isinstance(judgment, dict)
                    or set(judgment) != {"verdict", "minor_only"}
                    or judgment.get("verdict") not in {"APPROVED", "REVISE"}
                    or not isinstance(judgment.get("minor_only"), bool)
                    or (judgment["verdict"] == "APPROVED" and judgment["minor_only"])
                ):
                    _corrupt_workflow(f"synthesis action {action_id!r} has invalid judgment")
            elif judgment is not None:
                _corrupt_workflow(f"non-successful synthesis action {action_id!r} carries judgment")
        else:
            expected = _authoritative_reviewer_prompt(
                run,
                record,
                snapshot,
                action["seat"],
            )
            _read_authoritative_prompt(
                expected_prompt_paths[action_id],
                expected,
                f"reviewer action {action_id} prompt",
            )
            if action["driver"] == ActionDriver.NATIVE:
                ingress = action["return_transport"]["primary"]["ingress_path"]
                _read_authoritative_prompt(
                    expected_transport_prompts[action_id],
                    prompts.standalone_scribe_transport(ingress),
                    f"reviewer action {action_id} scribe prompt",
                )
        if action["accepted_path"] is not None:
            _read_accepted_bytes(action, run)
    _validate_retry_receipts(wf, current_attempt)


def _advance_locked(wf: dict, run: Path, *, adopt_pointer: bool = False) -> ReviewStep:
    """The sole render/follow-up/persist transition, called with the lock held."""
    ref = parse_review_ref(wf["ref"])
    expected = _guard_review_path(
        session_segment=ref.session_segment,
        run_id=ref.run_id,
        create=False,
    )
    if Path(os.path.abspath(run)) != expected:
        raise WorkflowError(
            "unsafe_review_path",
            f"workflow advance path is not the issued standalone run path: {run}",
        )
    record, snapshot = _verified_foundation(wf, run)
    _validate_workflow(wf, run, record, snapshot)
    _reconcile_external_results_locked(wf, run)
    _ensure_followups_locked(wf, run)
    _validate_workflow(wf, run, record, snapshot)
    _atomic(run / "workflow.json", wf)
    # Ordinary transitions use an issued reference and must never repoint the
    # harness session. Only an explicit start/resume/adoption may move the
    # session pointer, and it does so after the durable workflow write above.
    if adopt_pointer:
        _write_pointer_locked(wf)
    return _derive_step(wf, run)


def _matching_pointer_identity(
    session: str,
    *,
    target_sha: str,
    target_spec: str,
    target_base: str,
    signatures: dict,
    host: str,
    force_external_channels: list[str],
    prompt_mode: str,
    prompt_metadata_sha256: str,
) -> dict | None:
    """The pointed-at identity when it describes THIS start, else None.

    The route policy is part of the match key, not merely part of the identity
    it returns. Two starts on the same target with the same seats can differ
    only in which channels they forced external, and the returned identity is
    what the new start adopts its timeout envelope from; matching without the
    policy would carry one route's envelope into the other's run.
    """
    pointer = read_standalone_pointer(session)
    if pointer is None:
        return None
    _data, wf = pointer
    ref = parse_review_ref(wf["ref"])
    run_dir = _guard_review_path(
        session_segment=ref.session_segment,
        run_id=ref.run_id,
        create=False,
    )
    run = review_runs.read_run_json(run_dir)
    review_runs.verify_run_record(
        run,
        expected_run_id=run_dir.name,
        source=run_dir / review_runs.RUN_JSON_NAME,
    )
    identity = wf.get("workflow_identity") or {}
    if (
        run.get("target_sha256") == target_sha
        and run.get("target_spec") == target_spec
        and run.get("target_base") == target_base
        and run.get("seat_signatures") == signatures
        and identity.get("host") == host
        and identity.get("force_external_channels") == force_external_channels
        and identity.get("prompt_mode") == prompt_mode
        and identity.get("prompt_metadata_sha256") == prompt_metadata_sha256
    ):
        return identity
    return None


@_public_workflow_boundary
def start_review(request: ReviewRequest) -> ReviewStep:
    if request.timeout_seconds is not None and request.timeout_seconds <= 0:
        raise WorkflowError("invalid_timeout", "timeout must be a positive integer")
    # Both refusals above and here are pure argument checks, so they run before
    # the target work a mistyped channel name would otherwise pay for. Neither
    # writes anything, so the zero-write behavior noted below is unchanged.
    host = _host()
    policy = _resolve_route_policy(request, host)
    roles = native_roles(policy)
    base = request.base or "main"
    resolved_intent = _resolve_target_intent(request.target_input, base)
    if resolved_intent is None:
        return ReviewStep(StepType.NEEDS_INPUT, question=NEEDS_INPUT_QUESTION)
    target, base = resolved_intent
    if target.kind not in {"plan", "code"} or not isinstance(target.descriptor, str):
        raise WorkflowError(
            "target_error",
            "resolved target carries invalid canonical prompt metadata",
        )
    snapshot_name = review_runs.snapshot_name(target.kind)
    session = _session(request)
    # Validate the existing prefix before any seat/provider work, but retain
    # the longstanding zero-write behavior for requests rejected later.
    _guard_review_path(
        session_segment=session,
        create=False,
        allow_missing=True,
    )
    target_state = "dirty" if targets.is_dirty() else "clean"
    resolved = _resolve_seats(request, policy)
    signatures: dict[str, dict] = {}
    for name, spec, execution in resolved:
        signatures[name] = {
            "kind": "task" if execution.native else "subprocess",
            "model": _spent_model(name, spec, execution, roles),
        }
        if not execution.native:
            signatures[name]["provider"] = seats.CHANNEL_TO_LEGACY_KIND[execution.channel]
    target_sha, prompt_mode = review_runs.sha256_text(target.content), "inline_diff" if request.inline else "standard"
    if (
        not isinstance(target.notes, list)
        or any(not isinstance(note, str) for note in target.notes)
        or (target.diff_cmd is not None and not isinstance(target.diff_cmd, str))
    ):
        raise WorkflowError(
            "target_error",
            "resolved target carries invalid reviewer prompt metadata",
        )
    target_notes = list(target.notes)
    target_diff_cmd = target.diff_cmd
    prompt_metadata_sha = _target_prompt_metadata_sha256(
        target_notes,
        target_diff_cmd,
        snapshot_name,
        target.descriptor,
    )
    target_base = base if target.replay_spec == "branch" else ""
    frozen = (
        _matching_pointer_identity(
            session,
            target_sha=target_sha,
            target_spec=target.replay_spec,
            target_base=target_base,
            signatures=signatures,
            host=host,
            force_external_channels=policy.identity_value(),
            prompt_mode=prompt_mode,
            prompt_metadata_sha256=prompt_metadata_sha,
        )
        if request.timeout_seconds is None
        else None
    )
    timeout, raw_warning = _timeout(request.timeout_seconds, frozen)
    if raw_warning is not None:
        print(
            f"warning: standalone provider timeout {raw_warning}s exceeds the "
            "540s provider budget; using 540s with 60s settlement grace",
            file=sys.stderr,
        )
    if frozen is None and host == "cursor" and any(
        execution.native for _name, _spec, execution in resolved
    ):
        # Named where the run is minted rather than left to the docs: a bare
        # `crew review` typed into Cursor's integrated terminal mints in-session
        # seats only a Cursor agent session can spawn, and by the time nothing
        # answers the operator has already spent the panel. Gating on a seat
        # actually resolving native covers the suppression too: with the channel
        # forced external (or an all-external roster) none does, so there is no
        # failure here to point at. A start that adopts a frozen identity stays
        # silent; a start naming its own `--timeout` never consults the pointer,
        # so it re-emits, which is the same shape the warning above has.
        print(
            "note: this panel issues in-session cursor seats, which nothing "
            "spawns outside a Cursor agent session; set [review]."
            'force_external_channels = ["cursor"] (or pass --force-external '
            "cursor) to run them through the cursor CLI instead",
            file=sys.stderr,
        )
    current_provider_timeouts = {
        name: (
            None
            if execution.native
            else _provider_timeout(
                name,
                seats.CHANNEL_TO_LEGACY_KIND[execution.channel],
                execution.channel,
                timeout,
            )
        )
        for name, spec, execution in resolved
    }
    frozen_provider_timeouts = (
        frozen.get("provider_timeouts")
        if request.timeout_seconds is None and frozen is not None
        else None
    )
    provider_timeouts = {
        name: (
            None
            if current_provider_timeouts[name] is None
            else max(
                current_provider_timeouts[name],
                int((frozen_provider_timeouts or {}).get(name, 0)),
            )
        )
        for name, _spec, _execution in resolved
    }
    identity = {
        "kind": "standalone_review",
        "host": host,
        # Provenance: which channels this run declined to drive in-session. Every
        # later step rebuilds its route policy from here, so the record is what
        # the run is judged by, not merely a note about it.
        "force_external_channels": policy.identity_value(),
        "prompt_mode": prompt_mode,
        "timeout_seconds": timeout,
        "provider_timeouts": provider_timeouts,
        "prompt_metadata_sha256": prompt_metadata_sha,
    }
    run_id, digest = review_runs.mint_identity(target_sha256=target_sha, target_spec=target.replay_spec,
        target_base=target_base, seat_signatures=signatures,
        workflow_identity=identity)
    ref = ReviewRef(session, run_id, "attempt-0001", target_sha)
    run = _guard_review_path(
        session_segment=ref.session_segment,
        run_id=ref.run_id,
        create=True,
    )
    with _workflow_lock(run):
        if (run / "workflow.json").is_file():
            wf = _workflow(run)
            _verify_host(wf)
            if wf.get("workflow_identity") != identity:
                raise WorkflowError("conflict", "standalone run identity differs; start a new review", "conflict")
            step = _advance_locked(wf, run, adopt_pointer=True)
            if step.outcome and step.outcome.get("status") == "synthesis_failed":
                step = _create_synthesis_restart_locked(
                    wf,
                    run,
                    parse_review_ref(wf["ref"]),
                    wf.setdefault("retry_receipts", {}),
                )
                _write_pointer_locked(wf)
                return step
            return step
        snapshot_path = run / snapshot_name
        record = {"run_id": run_id, "identity_digest": digest, "target_sha256": target_sha,
            "target_spec": target.replay_spec, "target_base": base if target.replay_spec == "branch" else "",
            "target_descriptor": target.descriptor, "snapshot": snapshot_name,
            "target_notes": target_notes, "target_diff_cmd": target_diff_cmd,
            "subprocess_seats": [name for name, _spec, execution in resolved if not execution.native],
            "task_seats": [name for name, _spec, execution in resolved if execution.native],
            "task_seat_models": {
                name: _spent_model(name, spec, execution, roles)
                for name, spec, execution in resolved
                if execution.native
            },
            "seat_signatures": signatures, "host": host,
            "seat_channels": {name: execution.channel for name, _spec, execution in resolved},
            "workflow_identity": identity, "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        review_runs.write_run_json_once(run, record)
        _write_run_text(
            run,
            snapshot_path,
            target.content,
            "standalone target snapshot",
        )
        actions = [
            _reviewer_action(
                run,
                ref,
                ordinal=ordinal,
                seat=name,
                model=_spent_model(name, spec, execution, roles),
                channel=execution.channel,
                driver=(
                    ActionDriver.NATIVE
                    if execution.native
                    else ActionDriver.EXTERNAL
                ),
                provider=seats.CHANNEL_TO_LEGACY_KIND[execution.channel],
                policy=policy,
                timeout_seconds=provider_timeouts[name],
                prompt=_authoritative_reviewer_prompt(
                    run,
                    record,
                    snapshot_path,
                    name,
                ),
            )
            for ordinal, (name, spec, execution) in enumerate(resolved, 1)
        ]
        display_base = base if target.replay_spec == "branch" else ""
        display_state = target_state
        wf = {"schema": SCHEMA, "workflow_identity": identity, "ref": review_ref_to_dict(ref),
            "target": {"kind": target.kind, "scope": target.scope,
                "base": base if target.replay_spec == "branch" else "",
                "state": target_state, "descriptor": target.descriptor,
                "sha256": target_sha,
                "display": (
                    f"RESOLVED TARGET: kind={target.kind} scope={target.scope} "
                    f"base={display_base} state={display_state}"
                )},
            "roster": [name for name, _spec, _execution in resolved], "actions": actions,
            "retry_receipts": {}}
        return _advance_locked(wf, run, adopt_pointer=True)


@_public_workflow_boundary
def next_review(ref: ReviewRef) -> ReviewStep:
    run = _guard_review_path(
        session_segment=ref.session_segment,
        run_id=ref.run_id,
        create=False,
    )
    with _workflow_lock(run):
        wf, run = _load_current_locked(ref, run)
        return _advance_locked(wf, run)


@_public_workflow_boundary
def claim_review_action(request: ClaimRequest) -> ClaimResponse:
    run = _guard_review_path(
        session_segment=request.ref.session_segment,
        run_id=request.ref.run_id,
        create=False,
    )
    with _workflow_lock(run):
        wf, run = _load_current_locked(request.ref, run)
        action = _load_action(wf, request.action_id)
        if _action_attempt(action) != request.ref.attempt_id:
            raise WorkflowError("stale_ref", "action does not belong to the active attempt", "stale_ref")
        if action.get("driver") == ActionDriver.EXTERNAL:
            raise WorkflowError("invalid_action", "external reviewer actions are executed by review-execute")
        claimed_now = action.get("status") == "ready"
        if claimed_now:
            action["status"] = "claimed"
            action["claim_id"] = hashlib.sha256(f"{request.action_id}:{time.time_ns()}".encode()).hexdigest()[:16]
        elif action.get("status") not in {"claimed", "settled"}:
            raise WorkflowError("invalid_action", "action is not claimable")
        _advance_locked(wf, run)
        native = action.get("driver") == ActionDriver.NATIVE
        if claimed_now:
            authorization = "spawn" if native else "perform"
            action_status = "claimed"
        else:
            authorization = "do_not_spawn" if native else "do_not_perform"
            action_status = (
                "already_claimed"
                if action.get("status") == "claimed"
                else "settled"
            )
        return ClaimResponse(request.ref, authorization, action_status,
                             _work_item(action, request.ref) if claimed_now else None)


def _frozen_external_provider(action: dict, policy: RoutePolicy):
    """Reconstruct one action's live route and require its frozen identity.

    ``policy`` comes from the run's FROZEN identity, not from live config: the
    host is separately proven to still match, but a config file edited between
    the mint and this call would otherwise silently change how the frozen route
    is judged.
    """
    seat_name = action.get("seat")
    spec = seats.seat_spec(seat_name)
    try:
        execution = (
            channels.resolve_seat(
                spec,
                capabilities=channels.active_capabilities(),
                # Drift is judged against the same view that froze the run.
                declared_native=native_channel_for(spec, policy),
            )
            if spec is not None
            else None
        )
        selected_provider = (
            seats.CHANNEL_TO_LEGACY_KIND.get(execution.channel)
            if execution is not None
            else None
        )
        live_model = (
            (execution.model or seat_name)
            if execution is not None
            else None
        )
    except Exception as exc:
        raise WorkflowError(
            "provider_config_drift",
            f"cannot reconstruct frozen provider route for seat {seat_name!r}: {exc}",
            "conflict",
        ) from exc
    matches = (
        action.get("driver") == ActionDriver.EXTERNAL
        and execution is not None
        and not execution.native
        # A missing native pin reads as "no native route for this seat", which is
        # the same shape as "this host has no native channel". Asking the shared
        # question keeps a stale external action on the host's own channel from
        # running just because its pin is absent.
        and not has_no_route_here(execution, policy)
        and execution.engine_runnable
        and execution.channel == action.get("channel")
        and selected_provider == action.get("provider")
        and live_model == action.get("model")
    )
    if not matches:
        raise WorkflowError(
            "provider_config_drift",
            f"live provider route for seat {seat_name!r} no longer matches its frozen "
            "provider/channel/model/driver; start a new standalone review",
            "conflict",
        )
    try:
        return get_provider_for_channel(seat_name, execution.channel)
    except Exception as exc:
        raise WorkflowError(
            "provider_error",
            f"cannot construct provider for seat {seat_name!r}: {exc}",
        ) from exc


def _normalize_provider_return(
    result: object,
    *,
    seat_name: str,
    model: str,
    elapsed: float,
) -> ProviderResult:
    valid_core = (
        isinstance(result, ProviderResult)
        and isinstance(result.name, str)
        and isinstance(result.model, str)
        and isinstance(result.ok, bool)
        and isinstance(result.output, str)
        and (result.error is None or isinstance(result.error, str))
        and isinstance(result.elapsed, (int, float))
        and not isinstance(result.elapsed, bool)
        and math.isfinite(float(result.elapsed))
        and float(result.elapsed) >= 0
        and (
            (result.ok and result.error is None)
            or (
                not result.ok
                and isinstance(result.error, str)
                and bool(result.error.strip())
            )
        )
    )
    if not valid_core:
        return ProviderResult(
            seat_name, model, False, "",
            "provider returned an invalid result object", elapsed,
        )
    if result.name != seat_name or result.model != model:
        return ProviderResult(
            seat_name,
            model,
            False,
            "",
            "provider identity mismatch: returned "
            f"name={result.name!r}, model={result.model!r}; expected "
            f"name={seat_name!r}, model={model!r}",
            float(result.elapsed),
        )
    sanitized = ProviderResult(
        name=result.name,
        model=result.model,
        ok=result.ok,
        output=result.output,
        error=result.error,
        elapsed=float(result.elapsed),
        reported_model=(
            None if result.reported_model is None else str(result.reported_model)
        ),
    )
    return _normalize_reviewer_success(sanitized)


@_public_workflow_boundary
def execute_external_review(ref: ReviewRef, action_id: str) -> ReviewStep:
    run = _guard_review_path(
        session_segment=ref.session_segment,
        run_id=ref.run_id,
        create=False,
    )
    with _workflow_lock(run):
        wf, run = _load_current_locked(ref, run)
        action = _load_action(wf, action_id)
        if _action_attempt(action) != ref.attempt_id or action.get("driver") != ActionDriver.EXTERNAL:
            raise WorkflowError("invalid_action", "review-execute accepts active external reviewer actions only")
        if action.get("status") in {"settled", "claimed"}:
            return _advance_locked(wf, run)
        if action.get("status") != "ready":
            raise WorkflowError("invalid_action", "external reviewer action is not executable")
        seat_name, model = action["seat"], action["model"]
        timeout, result_path = int(action["timeout_seconds"]), Path(action["result_path"])
        provider = _frozen_external_provider(
            action, RoutePolicy.from_identity(wf["workflow_identity"])
        )
        try:
            available, availability_diagnostic = provider.is_available()
            if not isinstance(available, bool) or not isinstance(availability_diagnostic, str):
                raise ValueError("provider availability response is malformed")
        except Exception as exc:
            raise WorkflowError(
                "provider_error",
                f"cannot resolve provider for seat {seat_name!r}: {exc}",
            ) from exc
        if action.get("provider") == "agy":
            try:
                current_timeout = math.ceil(provider.effective_timeout(timeout))
            except Exception as exc:
                raise WorkflowError(
                    "provider_error",
                    f"cannot resolve provider timeout for seat {seat_name!r}: {exc}",
                ) from exc
            if current_timeout != timeout:
                if current_timeout <= 540:
                    guidance = (
                        "start a new standalone review with explicit "
                        f"--timeout {current_timeout}"
                    )
                else:
                    guidance = (
                        "lower the Agy timeout configuration to at most 540s, "
                        "then start a new standalone review with explicit "
                        "--timeout <current-effective-seconds>"
                    )
                raise WorkflowError(
                    "provider_timeout_config_drift",
                    f"seat {seat_name!r} now requires {current_timeout}s but action "
                    f"froze {timeout}s; {guidance}",
                    "conflict",
                )
        prompt = Path(action["prompt_path"]).read_text(encoding="utf-8")
        action["status"] = "claimed"
        claim_id = hashlib.sha256(f"{action_id}:{time.time_ns()}".encode()).hexdigest()[:16]
        action["claim_id"] = claim_id
        claimed_step = _advance_locked(wf, run)
        action = _load_action(wf, action_id)
        if (
            action.get("status") != "claimed"
            or action.get("claim_id") != claim_id
        ):
            return claimed_step
        if not available:
            result = ProviderResult(
                name=seat_name,
                model=model,
                ok=False,
                output="",
                error=f"skipped: {availability_diagnostic or 'provider unavailable'}",
                elapsed=0.0,
                run_id=ref.run_id,
                target_sha256=ref.target_sha256,
                action_id=action_id,
                attempt_id=ref.attempt_id,
                channel=action.get("channel"),
            )
            guarded_run = _guard_review_path(
                session_segment=ref.session_segment,
                run_id=ref.run_id,
                create=False,
            )
            if guarded_run != Path(os.path.abspath(run)):
                raise WorkflowError(
                    "unsafe_review_path",
                    "external result sink no longer belongs to the issued run",
                )
            _validate_issued_path(
                run,
                str(result_path),
                result_path,
                f"external reviewer action {action_id} result sink",
            )
            _prepare_run_descendant(
                run,
                result_path,
                f"external reviewer action {action_id} result sink",
                create_parents=False,
            )
            stamp_attribution(result)
            _atomic(result_path, result.to_dict())
            return _advance_locked(wf, run)
    started = time.monotonic()
    try:
        raw_result = provider.run(prompt, model=model, timeout=timeout)
        result = _normalize_provider_return(
            raw_result,
            seat_name=seat_name,
            model=model,
            elapsed=time.monotonic() - started,
        )
    except Exception as exc:
        provider_diagnostic = str(exc).strip() or (
            f"provider raised {type(exc).__name__} without a diagnostic"
        )
        result = ProviderResult(
            name=seat_name,
            model=model,
            ok=False,
            output="",
            error=provider_diagnostic,
            elapsed=time.monotonic() - started,
        )
    result.run_id = ref.run_id
    result.target_sha256, result.action_id, result.attempt_id = ref.target_sha256, action_id, ref.attempt_id
    result.channel = action.get("channel")
    guarded_run = _guard_review_path(
        session_segment=ref.session_segment,
        run_id=ref.run_id,
        create=False,
    )
    if guarded_run != Path(os.path.abspath(run)):
        raise WorkflowError(
            "unsafe_review_path",
            "external result sink no longer belongs to the issued run",
        )
    _validate_issued_path(
        run,
        str(result_path),
        result_path,
        f"external reviewer action {action_id} result sink",
    )
    _prepare_run_descendant(
        run,
        result_path,
        f"external reviewer action {action_id} result sink",
        create_parents=False,
    )
    stamp_attribution(result)
    _atomic(result_path, result.to_dict())
    with _workflow_lock(run):
        wf, run = _load_current_locked(ref, run)
        action = _load_action(wf, action_id)
        if action.get("status") == "claimed" and action.get("claim_id") != claim_id:
            raise WorkflowError("conflict", "external action claim changed during execution", "conflict")
        return _advance_locked(wf, run)


def _require_canonical_synthesis_panels(wf: dict, run: Path) -> None:
    expected = _canonical_panel_bytes(wf, run)
    for name, body, label in (
        ("panel.md", expected.grouped, "standalone panel digest"),
        ("panel-full.md", expected.full, "standalone full panel"),
    ):
        path = run / name
        try:
            safe = _prepare_run_descendant(
                run,
                path,
                label,
                create_parents=False,
                required_file=True,
            )
            observed = safe.read_bytes()
        except (OSError, WorkflowError) as exc:
            raise WorkflowError(
                "invalid_submission",
                f"synthesis panel is not authoritative: {name}: {exc}",
            ) from exc
        if observed != body:
            raise WorkflowError(
                "invalid_submission",
                f"synthesis panel is not authoritative: {name} does not match canonical bytes",
            )


def _validate_submission_locked(
    wf: dict,
    action: dict,
    result: HostResult,
    run: Path,
) -> _SubmissionAdmission:
    if result.status != HostStatus.OK:
        valid_failure = (
            result.artifact is None
            and result.judgment is None
            and isinstance(result.diagnostic, str)
            and bool(result.diagnostic.strip())
        )
        if not valid_failure:
            raise WorkflowError(
                "invalid_submission",
                "failed HostResult requires artifact=null, judgment=null, "
                "and a diagnostic",
            )
        return _SubmissionAdmission(None, False, result.diagnostic)
    if result.artifact is None:
        raise WorkflowError("invalid_submission", "successful submission requires an artifact")
    if result.diagnostic is not None:
        raise WorkflowError(
            "invalid_submission",
            "successful HostResult requires diagnostic=null",
        )
    artifact_path = Path(result.artifact["path"])
    if action.get("kind") == ActionKind.REVIEWER and action.get("driver") == ActionDriver.NATIVE:
        transports = action.get("return_transport") or {}
        allowed = {value.get("ingress_path") for value in transports.values() if isinstance(value, dict)}
    else:
        allowed = {action.get("ingress_path")}
    if str(artifact_path) not in allowed:
        raise WorkflowError("invalid_submission", "artifact path was not issued for this action")
    try:
        artifact_path = _prepare_run_descendant(
            run,
            artifact_path,
            f"action {action['action_id']} submitted artifact",
            create_parents=False,
            required_file=True,
        )
        artifact_bytes = artifact_path.read_bytes()
        digest = hashlib.sha256(artifact_bytes).hexdigest()
    except (OSError, WorkflowError) as exc:
        raise WorkflowError(
            "invalid_submission",
            f"artifact path or sha256 is not authoritative: {exc}",
        ) from exc
    if digest != result.artifact["sha256"]:
        raise WorkflowError("invalid_submission", "artifact path or sha256 is not authoritative")

    kind = action.get("kind")
    if kind != ActionKind.SYNTHESIS and result.judgment is not None:
        raise WorkflowError("invalid_submission", "reviewer and formatter results cannot carry judgment")
    if kind == ActionKind.SYNTHESIS:
        judgment = result.judgment
        if (not isinstance(judgment, dict) or judgment.get("verdict") not in {"APPROVED", "REVISE"}
                or not isinstance(judgment.get("minor_only"), bool)
                or (judgment["verdict"] == "APPROVED" and judgment["minor_only"])):
            raise WorkflowError(
                "invalid_submission",
                "synthesis judgment must be APPROVED or REVISE with valid minor_only",
            )
        _require_canonical_synthesis_panels(wf, run)

    if kind == ActionKind.REVIEWER:
        try:
            reviewer_output = artifact_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return _SubmissionAdmission(
                artifact_path,
                False,
                "successful reviewer artifact must be valid UTF-8",
            )
        output_problem = _reviewer_output_error(reviewer_output)
        if output_problem is not None:
            return _SubmissionAdmission(artifact_path, False, output_problem)
    if kind == ActionKind.FORMATTER:
        try:
            artifact_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return _SubmissionAdmission(
                artifact_path,
                False,
                "successful formatter artifact must be valid UTF-8",
            )
    if kind == ActionKind.SYNTHESIS:
        try:
            synthesis_text = artifact_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return _SubmissionAdmission(
                artifact_path,
                False,
                "successful synthesis artifact must be valid UTF-8",
            )
        if not synthesis_text.strip():
            return _SubmissionAdmission(
                artifact_path,
                False,
                "successful synthesis artifact must be non-empty",
            )
    return _SubmissionAdmission(artifact_path, True, None)


@_public_workflow_boundary
def submit_review(request: SubmissionRequest) -> ReviewStep:
    path = Path(request.submission_path)
    if not request.consume:
        raise WorkflowError("invalid_request", "--consume is required")
    result = request.result
    run = _guard_review_path(
        session_segment=result.ref.session_segment,
        run_id=result.ref.run_id,
        create=False,
    )
    with _workflow_lock(run):
        wf, run = _load_current_locked(result.ref, run)
        action = _load_action(wf, result.action_id)
        if _action_attempt(action) != result.ref.attempt_id or action.get("submission_path") != str(path):
            raise WorkflowError("invalid_submission", "submission path or action was not issued for this attempt")
        try:
            path = _prepare_run_descendant(
                run,
                path,
                f"action {action['action_id']} submission",
                create_parents=False,
                required_file=True,
            )
            submission_bytes = path.read_bytes()
            file_result = parse_host_result(json.loads(submission_bytes))
        except (OSError, ValueError, TypeError, WorkflowError) as exc:
            raise WorkflowError(
                "invalid_submission",
                f"cannot read submission file: {exc}",
            ) from exc
        if file_result != result:
            raise WorkflowError(
                "invalid_submission",
                "typed HostResult does not match the issued submission file",
            )
        submission_sha = hashlib.sha256(submission_bytes).hexdigest()
        if action.get("status") == "settled":
            if action.get("submission_sha256") != submission_sha:
                raise WorkflowError("conflict", "action already has a different accepted submission", "conflict")
            step = _advance_locked(wf, run)
        else:
            if action.get("status") != "claimed":
                raise WorkflowError("invalid_submission", "action has not been claimed")
            admission = _validate_submission_locked(wf, action, result, run)
            artifact_path = admission.artifact_path
            accepted_ok, diagnostic = admission.accepted_ok, admission.diagnostic
            if accepted_ok and action.get("kind") == ActionKind.REVIEWER:
                action["accepted_path"] = str(artifact_path)
                action["accepted_sha256"] = result.artifact["sha256"]
            if accepted_ok and action.get("kind") == ActionKind.FORMATTER:
                source = _load_action(wf, action["source_action_id"])
                candidate = _materialized_reviewer_result(wf, source, run)
                candidate.repaired_output = artifact_path.read_text(encoding="utf-8")
                if findings.parse_seat(candidate).findings_parsed:
                    action["accepted_path"] = str(artifact_path)
                    action["accepted_sha256"] = result.artifact["sha256"]
                else:
                    accepted_ok, diagnostic = False, "formatter output did not satisfy the findings parser"
            if accepted_ok and action.get("kind") == ActionKind.SYNTHESIS:
                action["accepted_path"] = str(artifact_path)
                action["accepted_sha256"] = result.artifact["sha256"]
            action.update(status="settled", ok=accepted_ok, diagnostic=diagnostic,
                          submission_sha256=submission_sha)
            if action.get("kind") == ActionKind.SYNTHESIS:
                action["judgment"] = result.judgment if accepted_ok else None
            step = _advance_locked(wf, run)
    with contextlib.suppress(OSError):
        path.unlink()
    return step


@_public_workflow_boundary
def recover_review_action(request: RecoveryRequest) -> ReviewStep:
    if request.confirmation != "not_running":
        raise WorkflowError("not_confirmed", "recovery requires not_running confirmation")
    run = _guard_review_path(
        session_segment=request.ref.session_segment,
        run_id=request.ref.run_id,
        create=False,
    )
    with _workflow_lock(run):
        wf, run = _load_current_locked(request.ref, run)
        action = _load_action(wf, request.action_id)
        if _action_attempt(action) != request.ref.attempt_id:
            raise WorkflowError("stale_ref", "action does not belong to the active attempt", "stale_ref")
        expected_diagnostic = RECOVERY_DIAGNOSTICS.get(
            (action.get("kind"), action.get("driver")),
        )
        if (
            action.get("status") != "claimed"
            or request.diagnostic_code != expected_diagnostic
        ):
            raise WorkflowError("invalid_recovery", "diagnostic code does not match the claimed action")
        _reconcile_external_results_locked(wf, run)
        if action.get("status") == "claimed":
            if _reroute_lost_formatter(action):
                return _advance_locked(wf, run)
            extra = {}
            if action.get("kind") == ActionKind.REVIEWER:
                extra = {
                    "reported_model": None,
                    "model_attribution": ATTRIBUTION_REQUESTED_ONLY,
                }
            action.update(
                status="settled",
                ok=False,
                diagnostic=request.diagnostic_code,
                **extra,
            )
        return _advance_locked(wf, run)


def _normalized_retry_names(wf: dict, request: RetryRequest, retryable: list[str]) -> list[str]:
    roster = list(wf["roster"])
    if request.seats is None:
        return [seat for seat in roster if seat in retryable]
    if len(set(request.seats)) != len(request.seats):
        raise WorkflowError("duplicate_seat", "retry seats must be unique")
    unknown = [seat for seat in request.seats if seat not in roster]
    if unknown:
        raise WorkflowError("unknown_seat", f"unknown retry seat {unknown[0]!r}")
    invalid = [seat for seat in request.seats if seat not in retryable]
    if invalid:
        raise WorkflowError("no_pending_seats", f"seat {invalid[0]!r} is not retryable", "not_retryable")
    return [seat for seat in roster if seat in request.seats]


def _retry_sha(names: list[str]) -> str:
    body = json.dumps({"seats": names}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _source_receipt(receipts: dict, source_attempt: str) -> dict | None:
    matches = [
        receipt
        for receipt in receipts.values()
        if receipt.get("source_attempt_id") == source_attempt
    ]
    if len(matches) > 1:
        raise WorkflowError("corrupt_workflow", "source attempt has multiple retry receipts")
    return matches[0] if matches else None


def _pending_for_attempt(wf: dict, run: Path, attempt_id: str) -> list[str]:
    """Reconstruct the source attempt's all-pending normalized roster."""
    attempt_number = _attempt_number(attempt_id)
    effective = {
        action["seat"]: result
        for action, result in _effective_results(
            wf,
            run,
            max_attempt_number=attempt_number,
        )
    }
    successes = {seat for seat, result in effective.items() if result.ok}
    return [seat for seat in wf["roster"] if seat not in successes]


def _create_synthesis_restart_locked(
    wf: dict,
    run: Path,
    source_ref: ReviewRef,
    receipts: dict,
) -> ReviewStep:
    source_attempt = source_ref.attempt_id
    if _source_receipt(receipts, source_attempt) is not None:
        raise WorkflowError("conflict", "source attempt already has a retry receipt", "conflict")
    next_ref = dataclasses.replace(
        source_ref,
        attempt_id=f"attempt-{_attempt_number(source_attempt) + 1:04d}",
    )
    wf["ref"] = review_ref_to_dict(next_ref)
    synthesis = _synthesis_action(wf, run)
    wf["actions"].append(synthesis)
    receipts[f"synthesis_restart:{source_attempt}"] = {
        "kind": "synthesis_restart",
        "source_attempt_id": source_attempt,
        "created_attempt_id": next_ref.attempt_id,
        "created_action_id": synthesis["action_id"],
    }
    return _advance_locked(wf, run)


@_public_workflow_boundary
def retry_review(request: RetryRequest) -> ReviewStep:
    run = _guard_review_path(
        session_segment=request.ref.session_segment,
        run_id=request.ref.run_id,
        create=False,
    )
    with _workflow_lock(run):
        wf = _workflow(run)
        _verify_host(wf)
        stored, receipts = parse_review_ref(wf["ref"]), wf.setdefault("retry_receipts", {})
        if stored != request.ref:
            receipt = _source_receipt(receipts, request.ref.attempt_id)
            if receipt is None:
                raise WorkflowError("stale_ref", "review reference does not match current workflow", "stale_ref")
            if receipt.get("created_attempt_id") != stored.attempt_id:
                raise WorkflowError("stale_ref", "retry receipt no longer names the active attempt", "stale_ref")
            if receipt.get("kind") == "seat_retry":
                if request.seats is None:
                    names = _pending_for_attempt(wf, run, request.ref.attempt_id)
                    if (
                        receipt.get("normalized_seats") != names
                        or receipt.get("request_sha256") != _retry_sha(names)
                    ):
                        raise WorkflowError(
                            "conflict",
                            "omitted retry request conflicts with its receipt",
                            "conflict",
                        )
                else:
                    duplicate = len(set(request.seats)) != len(request.seats)
                    unknown = any(seat not in wf["roster"] for seat in request.seats)
                    if duplicate or unknown:
                        raise WorkflowError("conflict", "retry request conflicts with its receipt", "conflict")
                    names = [seat for seat in wf["roster"] if seat in request.seats]
                if receipt.get("request_sha256") != _retry_sha(names):
                    raise WorkflowError("conflict", "retry request conflicts with its receipt", "conflict")
            else:
                raise WorkflowError("conflict", "retry request conflicts with its receipt", "conflict")
            return _advance_locked(wf, run)
        # Refusing a retry must not reconcile, render, or persist anything.
        source_step = _derive_step(wf, run)
        if source_step.type != StepType.TERMINAL:
            raise WorkflowError("active_attempt", "the source attempt is still active", "not_retryable")
        source_attempt = stored.attempt_id
        if _source_receipt(receipts, source_attempt) is not None:
            raise WorkflowError("conflict", "source attempt already has a retry receipt", "conflict")
        next_ref = dataclasses.replace(
            stored,
            attempt_id=f"attempt-{_attempt_number(source_attempt) + 1:04d}",
        )
        effective = {
            action["seat"]: (action, result)
            for action, result in _effective_results(wf, run)
        }
        retryable = list((source_step.panel or {}).get("pending") or [])
        names = _normalized_retry_names(wf, request, retryable)
        if not names:
            raise WorkflowError("no_pending_seats", "the source attempt has no pending seats to retry", "not_retryable")
        request_sha = _retry_sha(names)
        new_actions = []
        for seat in names:
            source, _result = effective[seat]
            new_actions.append(
                _reviewer_action(
                    run,
                    next_ref,
                    ordinal=int(source["ordinal"]),
                    seat=seat,
                    model=source["model"],
                    channel=source["channel"],
                    driver=source["driver"],
                    provider=source["provider"],
                    # The FROZEN route, never live detection or live config: a
                    # retry reissues the same route the run was minted with.
                    policy=RoutePolicy.from_identity(wf["workflow_identity"]),
                    timeout_seconds=(
                        source.get("timeout_seconds")
                        if source["driver"] == ActionDriver.EXTERNAL
                        else None
                    ),
                    prompt=Path(source["prompt_path"]).read_text(encoding="utf-8"),
                )
            )
        wf["ref"], wf["actions"] = review_ref_to_dict(next_ref), [*wf["actions"], *new_actions]
        receipts[f"seat_retry:{source_attempt}:{request_sha}"] = {
            "kind": "seat_retry",
            "source_attempt_id": source_attempt,
            "normalized_seats": names,
            "request_sha256": request_sha,
            "created_attempt_id": next_ref.attempt_id,
            "created_action_ids": [action["action_id"] for action in new_actions],
        }
        return _advance_locked(wf, run)
