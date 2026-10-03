"""Engine-owned implementation rounds composed with loop-owned code reviews."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar

import loop_state
from build_state import (
    ACTION_RE,
    BuildAction,
    BuildJournal,
    report_status,
    validate_writer,
    writer_fence,
)
from build_state import recovery_argv as writer_recovery_argv
from models import (
    LOAD_MISSING,
    LOAD_OK,
    SCHEMA_VERSION,
    LoopState,
    atomic_write_json,
    read_state_json,
    utc_now_iso,
)
from state_discovery import crew_base, is_active_value

from multiagent import (
    channels,
    config,
    continuations,
    execution,
    review_runs,
    seats,
    targets,
)
from multiagent import (
    review_workflow as review,
)
from multiagent.providers import ProviderResult, get_provider, transport_failure

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BuildRequest:
    raw_arguments: str
    session_id: str


@dataclass(frozen=True, slots=True)
class BuildRef:
    session_segment: str
    loop_instance_id: str


@dataclass(frozen=True, slots=True)
class BuildSelection:
    task: str
    panel: str | None = None
    seats: str | None = None
    executor: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenExecutor:
    executor: str
    host: str
    channel: str
    provider: str | None
    model: str | None
    reasoning: str | None
    role: str | None
    resume_executor: bool


@dataclass(frozen=True, slots=True)
class RoundPolicy:
    retries: int
    timeout_seconds: int
    base_timeout_seconds: int
    dispatch_options: dict[str, object]


@dataclass(frozen=True, slots=True)
class BuildResult:
    ref: BuildRef
    action_id: str
    status: str
    returned_sha256: str
    execution_receipt_sha256: str
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class BuildExecutionReceipt:
    ref: BuildRef
    action_id: str
    executor: FrozenExecutor
    before: execution.WorkspaceFacts
    after: execution.WorkspaceFacts
    transport_status: str
    returned_sha256: str
    diagnostic: str | None
    report_authoritative: bool


@dataclass(frozen=True, slots=True)
class BuildDecision:
    ref: BuildRef
    question_id: str
    kind: str
    confirmation: str | None = None
    workspace_sha256: str | None = None
    completed_action: str | None = None
    answer: str | None = None


@dataclass(frozen=True, slots=True)
class BuildQuestion:
    question_id: str
    kind: str
    text: str
    workspace_sha256: str
    action_id: str | None
    review_ref: review.ReviewRef | None
    outcome_sha256: str | None = None
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BuildRetryAuthorization:
    decision: BuildDecision
    source_ref: review.ReviewRef


@dataclass(frozen=True, slots=True)
class BuildWorkItem:
    owner: BuildRef
    action_id: str
    kind: str
    driver: str
    role: str | None
    model: str | None
    channel: str | None
    access: str
    prompt_path: str
    returned_path: str | None
    timeout_seconds: int | None
    host_allowance_seconds: int | None
    commands: dict[str, tuple[str, ...]]
    review_ref: review.ReviewRef | None = None
    review_item: review.WorkItem | None = None
    native_transport: dict | None = None


@dataclass(frozen=True, slots=True)
class BuildStep:
    type: str
    ref: BuildRef | None
    display: str
    question: BuildQuestion | None = None
    work_items: tuple[BuildWorkItem, ...] = ()
    in_flight: tuple[str, ...] = ()
    outcome: dict[str, object] | None = None


def step_to_dict(step: BuildStep) -> dict[str, object]:
    data = {
        "schema": 1,
        **dataclasses.asdict(step),
        "ref": ref_to_dict(step.ref) if step.ref else None,
    }
    for raw, item in zip(data["work_items"], step.work_items):
        raw["owner"] = ref_to_dict(item.owner)
        if item.review_ref:
            raw["review_ref"] = review.review_ref_to_dict(item.review_ref)
            raw["review_item"] = review.work_item_to_dict(item.review_item)
    if step.question and step.question.review_ref:
        data["question"]["review_ref"] = review.review_ref_to_dict(
            step.question.review_ref
        )
    return data


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def ref_to_dict(ref: BuildRef) -> dict[str, object]:
    return {"schema": 1, **dataclasses.asdict(ref)}


def _fields(value: object, cls: type, *, optional: bool = False) -> dict:
    required = {f.name for f in dataclasses.fields(cls)}
    if not isinstance(value, dict) or (
        set(value) - required if optional else set(value) != required
    ):
        raise review.WorkflowError(
            "invalid_build_record", f"{cls.__name__} has missing or unknown fields"
        )
    return dict(value)


def parse_build_ref(value: object) -> BuildRef:
    if (
        not isinstance(value, dict)
        or type(value.get("schema")) is not int
        or value["schema"] != 1
    ):
        raise review.WorkflowError("invalid_ref", "build reference requires schema 1")
    fields = _fields({k: v for k, v in value.items() if k != "schema"}, BuildRef)
    if any(
        not isinstance(v, str) or re.fullmatch(r"[A-Za-z0-9_-]+", v) is None
        for v in fields.values()
    ):
        raise review.WorkflowError("invalid_ref", "build owner has invalid identity")
    return BuildRef(**fields)


def result_to_dict(result: BuildResult) -> dict[str, object]:
    return {**dataclasses.asdict(result), "ref": ref_to_dict(result.ref)}


def parse_build_result(value: object) -> BuildResult:
    data = _fields(value, BuildResult)
    data["ref"] = parse_build_ref(data["ref"])
    if (
        not isinstance(data["action_id"], str)
        or not ACTION_RE.fullmatch(data["action_id"])
        or not isinstance(data["status"], str)
        or data["status"]
        not in {
            "completed",
            "blocked",
            "invalid_report",
            "failed",
            "timeout",
            "cancelled",
            "unavailable",
        }
        or any(
            not isinstance(data[k], str) or not review.SHA_RE.fullmatch(data[k])
            for k in ("returned_sha256", "execution_receipt_sha256")
        )
        or (data["diagnostic"] is not None and not isinstance(data["diagnostic"], str))
    ):
        raise review.WorkflowError(
            "invalid_result", "build result has invalid identity, status or digests"
        )
    return BuildResult(**data)


def decision_to_dict(decision: BuildDecision) -> dict[str, object]:
    return {**dataclasses.asdict(decision), "ref": ref_to_dict(decision.ref)}


def parse_build_decision(value: object) -> BuildDecision:
    data = _fields(value, BuildDecision, optional=True)
    if not {"ref", "question_id", "kind"} <= set(data):
        raise review.WorkflowError(
            "invalid_decision", "decision requires owner, question and kind"
        )
    data["ref"] = parse_build_ref(data["ref"])
    if (
        not isinstance(data["question_id"], str)
        or not review.SHA_RE.fullmatch(data["question_id"])
        or not isinstance(data["kind"], str)
        or any(
            v is not None and not isinstance(v, str)
            for k, v in data.items()
            if k != "ref"
        )
    ):
        raise review.WorkflowError(
            "invalid_decision", "decision fields must be text or null"
        )
    return BuildDecision(**data)


def journal_to_dict(journal: BuildJournal) -> dict[str, object]:
    data = dataclasses.asdict(journal)
    data["accepted_actions"] = [result_to_dict(r) for r in journal.accepted_actions]
    data["decisions"] = [decision_to_dict(d) for d in journal.decisions]
    if journal.action and journal.action.result:
        data["action"]["result"] = result_to_dict(journal.action.result)
    if journal.review_ref:
        data["review_ref"] = review.review_ref_to_dict(journal.review_ref)
    if journal.pending_review_inputs:
        data["pending_review_inputs"] = journal.pending_review_inputs.to_dict()
    if journal.question and journal.question.review_ref:
        data["question"]["review_ref"] = review.review_ref_to_dict(
            journal.question.review_ref
        )
    if journal.retry_authorization:
        data["retry_authorization"] = {
            "decision": decision_to_dict(journal.retry_authorization.decision),
            "source_ref": review.review_ref_to_dict(
                journal.retry_authorization.source_ref
            ),
        }
    return data


def _facts(value: object) -> execution.WorkspaceFacts:
    data = _fields(value, execution.WorkspaceFacts)
    if any(v is not None and (not isinstance(v, str) or not v) for v in data.values()):
        raise review.WorkflowError(
            "invalid_journal", "workspace facts must be nonblank text or null"
        )
    return execution.WorkspaceFacts(**data)


def journal_from_dict(value: object) -> BuildJournal:
    data = _fields(value, BuildJournal)
    try:
        validate_writer(data)
    except ValueError as exc:
        raise review.WorkflowError("invalid_journal", str(exc)) from exc
    data["selection"] = BuildSelection(**_fields(data["selection"], BuildSelection))
    data["executor"] = FrozenExecutor(**_fields(data["executor"], FrozenExecutor))
    data["policy"] = RoundPolicy(**_fields(data["policy"], RoundPolicy))
    data["baseline"] = _facts(data["baseline"])
    if data["action"] is not None:
        action = _fields(data["action"], BuildAction)
        action["before"] = (
            _facts(action["before"]) if action["before"] is not None else None
        )
        action["result"] = (
            parse_build_result(action["result"])
            if action["result"] is not None
            else None
        )
        data["action"] = BuildAction(**action)
    for key in ("accepted_actions", "decisions", "applied_outcomes", "feedback_paths"):
        if not isinstance(data[key], list):
            raise review.WorkflowError("invalid_journal", f"{key} must be a list")
    data["accepted_actions"] = [parse_build_result(r) for r in data["accepted_actions"]]
    data["decisions"] = [parse_build_decision(d) for d in data["decisions"]]
    data["review_ref"] = (
        review.parse_review_ref(data["review_ref"]) if data["review_ref"] else None
    )
    data["pending_review_inputs"] = (
        review.PreparedLoopReview.from_dict(data["pending_review_inputs"])
        if data["pending_review_inputs"] is not None
        else None
    )
    if data["question"] is not None:
        question = _fields(data["question"], BuildQuestion)
        question["review_ref"] = (
            review.parse_review_ref(question["review_ref"])
            if question["review_ref"] is not None
            else None
        )
        if not isinstance(question["advisories"], list):
            raise review.WorkflowError(
                "invalid_journal", "question advisories must be a list"
            )
        question["advisories"] = tuple(question["advisories"])
        data["question"] = BuildQuestion(**question)
    if data["retry_authorization"] is not None:
        auth = _fields(data["retry_authorization"], BuildRetryAuthorization)
        data["retry_authorization"] = BuildRetryAuthorization(
            parse_build_decision(auth["decision"]),
            review.parse_review_ref(auth["source_ref"]),
        )
    journal = BuildJournal(**data)
    if (
        not isinstance(journal.request_id, str)
        or not review.SHA_RE.fullmatch(journal.request_id)
        or not isinstance(journal.raw_arguments, str)
        or not isinstance(journal.selection.task, str)
        or not journal.selection.task.strip()
        or any(
            v is not None and not isinstance(v, str)
            for v in (
                journal.selection.panel,
                journal.selection.seats,
                journal.selection.executor,
            )
        )
        or not isinstance(journal.stage, str)
        or journal.stage
        not in {"implementation", "revision", "prepare_review", "review", "done"}
        or any(
            type(v) is not int or v < 0
            for v in (
                journal.action_ordinal,
                journal.round_attempt,
                journal.review_generation,
            )
        )
        or type(journal.policy.retries) is not int
        or not 0 <= journal.policy.retries <= 2
        or type(journal.policy.timeout_seconds) is not int
        or journal.policy.timeout_seconds <= 0
        or type(journal.policy.base_timeout_seconds) is not int
        or journal.policy.base_timeout_seconds <= 0
        or not isinstance(journal.policy.dispatch_options, dict)
        or type(journal.round_policy_ready) is not bool
        or type(journal.format_correction) is not bool
        or (journal.round_prompt_path is None) != (journal.round_prompt_sha256 is None)
        or journal.action is not None
        and journal.round_prompt_path is None
        or journal.round_prompt_sha256 is not None
        and (
            not isinstance(journal.round_prompt_sha256, str)
            or not review.SHA_RE.fullmatch(journal.round_prompt_sha256)
        )
        or any(
            v is not None and not isinstance(v, str)
            for v in (
                journal.round_prompt_path,
                journal.supplementary_answer,
                journal.executor.provider,
                journal.executor.model,
                journal.executor.reasoning,
                journal.executor.role,
                journal.cancellation_diagnostic,
            )
        )
        or any(not isinstance(p, str) or not p for p in journal.feedback_paths)
        or type(journal.executor.resume_executor) is not bool
        or journal.executor.host not in {"claude", "codex", "cursor", "unknown"}
        or not isinstance(journal.executor.executor, str)
        or not isinstance(journal.executor.channel, str)
        or any(
            not isinstance(d, str) or not review.SHA_RE.fullmatch(d)
            for d in journal.applied_outcomes
        )
    ):
        raise review.WorkflowError(
            "invalid_journal", "build journal progress or policy is invalid"
        )
    if journal.question and (
        not isinstance(journal.question.question_id, str)
        or not review.SHA_RE.fullmatch(journal.question.question_id)
        or not isinstance(journal.question.workspace_sha256, str)
        or not review.SHA_RE.fullmatch(journal.question.workspace_sha256)
        or not isinstance(journal.question.kind, str)
        or journal.question.kind
        not in {
            "executor_blocked",
            "executor_report_recovery",
            "execution_recovery",
            "completion_advisory",
            "synthesis_retry",
            "review_timeout_changed",
            "route_unavailable",
            "workspace_guard",
            "feedback_recovery",
        }
        or journal.question.outcome_sha256 is not None
        and (
            not isinstance(journal.question.outcome_sha256, str)
            or not review.SHA_RE.fullmatch(journal.question.outcome_sha256)
        )
        or not isinstance(journal.question.text, str)
        or any(not isinstance(v, str) for v in journal.question.advisories)
    ):
        raise review.WorkflowError("invalid_journal", "build question is invalid")
    if (
        journal.action
        and journal.action.status == "settled"
        and journal.action.result not in journal.accepted_actions
    ):
        raise review.WorkflowError(
            "invalid_journal", "settled action requires its accepted receipt"
        )
    if (
        parse_arguments(journal.raw_arguments) != journal.selection
        or sha256(
            canonical(
                {
                    "task": journal.selection.task,
                    "panel": journal.selection.panel,
                    "seats": journal.selection.seats,
                }
            )
        )
        != journal.request_id
    ):
        raise review.WorkflowError(
            "invalid_journal", "immutable request bytes or selection identity changed"
        )
    frozen = journal.executor
    if (
        frozen.role == "crew:executor"
        and (
            frozen.executor != "crew:executor"
            or frozen.host not in {"claude", "codex"}
            or frozen.channel != frozen.host
            or (frozen.host == "codex" and frozen.resume_executor)
            or any(
                v is not None for v in (frozen.provider, frozen.model, frozen.reasoning)
            )
        )
        or frozen.role is not None
        and frozen.role != "crew:executor"
        or frozen.role is None
        and (
            not frozen.provider
            or not frozen.model
            or frozen.executor == "crew:executor"
        )
    ):
        raise review.WorkflowError(
            "invalid_journal", "frozen executor identity is inconsistent"
        )
    return journal


def parse_arguments(raw: str) -> BuildSelection:
    if not isinstance(raw, str) or "\x00" in raw:
        raise review.WorkflowError(
            "invalid_request", "raw arguments must be text without NUL"
        )
    rest, values = raw, {}
    while True:
        match = re.match(r"\s*(--panel|--seats|--executor)(?=\s|$)", rest)
        if not match:
            break
        name, tail = match[1][2:], rest[match.end() :]
        token = re.match(r"""\s+("[^"]*"|'[^']*'|[^\s]+)""", tail)
        if token is None:
            raise review.WorkflowError("invalid_options", f"--{name} requires a value")
        try:
            words = shlex.split(token[1])
        except ValueError as exc:
            raise review.WorkflowError("invalid_options", str(exc)) from exc
        if (
            len(words) != 1
            or not words[0]
            or words[0].startswith("--")
            or name in values
        ):
            raise review.WorkflowError(
                "invalid_options", f"duplicate or missing --{name}"
            )
        values[name], rest = words[0], tail[token.end() :]
    task = rest.lstrip() if values else raw
    if re.match(r"\s*--", task) or not task.strip():
        raise review.WorkflowError(
            "invalid_options",
            "build requires task text after supported leading options",
        )
    return BuildSelection(
        task, values.get("panel"), values.get("seats"), values.get("executor")
    )


def _root(ref: BuildRef) -> Path:
    path = (
        crew_base().resolve()
        / ".crew"
        / "reviews"
        / ref.session_segment
        / f"build-{ref.loop_instance_id}"
    )
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise review.WorkflowError(
                "unsafe_path", f"build namespace traverses a symlink: {ancestor}"
            )
    return path


def _safe(path: Path, ref: BuildRef) -> Path:
    from multiagent.measure_twice import _safe_path

    path = _safe_path(path, _root(ref))
    if path.exists() and not path.is_file():
        raise review.WorkflowError(
            "unsafe_path", "build artifact must be a regular file"
        )
    return path


def _write_once(path: Path, content: bytes, ref: BuildRef) -> None:
    from multiagent.workflow_transport import write_bytes

    path = _safe(path, ref)
    if path.exists():
        if path.read_bytes() != content:
            raise review.WorkflowError(
                "conflict", "immutable build artifact differs", "conflict"
            )
        return
    write_bytes(path, content)


def _transaction(ref: BuildRef, operation: Callable[[dict, BuildJournal], T]) -> T:
    ref = parse_build_ref(ref_to_dict(ref))
    result: list[T] = []

    def mutate(data: dict) -> dict:
        if (
            data.get("loop_instance_id") != ref.loop_instance_id
            or review_runs.session_segment(data.get("session_id") or "")
            != ref.session_segment
        ):
            raise review.WorkflowError(
                "stale_owner", "build owner is missing or replaced", "conflict"
            )
        journal = journal_from_dict(data.get("bl_workflow"))
        if (
            len({r.action_id for r in journal.accepted_actions})
            != len(journal.accepted_actions)
            or len(set(journal.applied_outcomes)) != len(journal.applied_outcomes)
            or any(r.ref != ref for r in journal.accepted_actions)
            or any(d.ref != ref for d in journal.decisions)
        ):
            raise review.WorkflowError(
                "invalid_journal", "build receipts have duplicate or foreign owners"
            )
        if journal.action:
            expected_prompt = str(
                _root(ref) / "prompts" / f"{journal.action.action_id}.txt"
            )
            if (
                journal.action.prompt_path != expected_prompt
                or journal.action.action_id != f"action-{journal.action_ordinal:04d}"
            ):
                raise review.WorkflowError(
                    "invalid_journal",
                    "issued action ordinal or prompt path differs from its owner",
                )
        result.append(operation(data, journal))
        data["bl_workflow"] = journal_to_dict(journal)
        return data

    loop_state.mutate(loop_state.resolve("bl", ref.session_segment), mutate)
    return result[0]


def _admit(data: dict, journal: BuildJournal) -> None:
    if not is_active_value(data.get("active")) or loop_state.bound_reason(data):
        raise review.WorkflowError(
            "work_not_admitted", "build is inactive or its work bound elapsed"
        )
    if data.get("awaiting_input") or journal.question:
        raise review.WorkflowError(
            "work_not_admitted", "an unanswered build question prevents new work"
        )


def _workspace_digest() -> str:
    workspace = str(crew_base().resolve())
    facts = execution.observe_workspace(workspace)
    try:
        target = targets.resolve("working-tree", cwd=workspace)
        observation = {"status": "resolved", "content": target.content}
    except targets.TargetError as exc:
        observation = {"status": "unavailable", "error": str(exc)}
    return sha256(
        canonical(
            {
                "workspace": workspace,
                "facts": dataclasses.asdict(facts),
                "target": observation,
            }
        )
    )


def _park(
    data: dict,
    journal: BuildJournal,
    ref: BuildRef,
    kind: str,
    text: str,
    *,
    outcome_sha256: str | None = None,
    advisories: tuple[str, ...] = (),
) -> None:
    digest = _workspace_digest()
    action_id = journal.action.action_id if journal.action else None
    previous = journal.question.question_id if journal.question else None
    identity = {
        "ref": ref_to_dict(ref),
        "kind": kind,
        "workspace": digest,
        "action": action_id,
        "review": review.review_ref_to_dict(journal.review_ref)
        if journal.review_ref
        else None,
        "outcome": outcome_sha256,
        "advisories": advisories,
        "previous": previous,
        # Accepted decisions distinguish a recurrence after its prior question clears.
        "decision_sequence": len(journal.decisions),
    }
    journal.question = BuildQuestion(
        sha256(canonical(identity)),
        kind,
        text,
        digest,
        action_id,
        journal.review_ref,
        outcome_sha256,
        advisories,
    )
    data["awaiting_input"] = True


def _freeze_executor(selection: execution.ExecutorSelection) -> FrozenExecutor:
    host = channels.current_host()
    if selection.executor == "crew:executor":
        if host not in {"claude", "codex"} or selection.channel != host:
            raise review.WorkflowError(
                "route_unavailable",
                "crew:executor requires an admitted Claude or Codex native executor route",
            )
        return FrozenExecutor(
            selection.executor,
            host,
            host,
            None,
            None,
            None,
            "crew:executor",
            selection.resume_executor if host == "claude" else False,
        )
    spec = seats.seat_spec(selection.executor)
    return FrozenExecutor(
        selection.executor,
        host,
        selection.channel,
        spec.provider,
        spec.model,
        getattr(spec, "reasoning_effort", None),
        None,
        selection.resume_executor,
    )


def _route_matches(frozen: FrozenExecutor) -> bool:
    try:
        selected = execution.ExecutorSelection(
            frozen.executor, 0, frozen.resume_executor, "state", frozen.channel
        )
        if _freeze_executor(selected) != frozen:
            return False
        if frozen.role:
            return True
        spec = seats.seat_spec(frozen.executor)
        route = channels.resolve_seat(
            spec, declared_native=channels.task_native_channel(frozen.host)
        )
        return bool(
            route
            and route.engine_runnable
            and route.supports_workspace_write
            and not route.native
            and route.channel == frozen.channel
        )
    except (execution.ExecutionError, review.WorkflowError, AttributeError):
        return False


def _round_policy(frozen: FrozenExecutor) -> RoundPolicy:
    timeout = base_timeout = config.dispatch_timeout() or 1800
    if frozen.provider is not None:
        provider = get_provider(frozen.executor)
        effective = getattr(provider, "effective_timeout", None)
        if callable(effective):
            timeout = math.ceil(effective(timeout))
    return RoundPolicy(
        0 if frozen.role else config.build_executor_retries() or 0,
        timeout,
        base_timeout,
        {}
        if frozen.provider is None
        else config.dispatch_provider_options(frozen.provider),
    )


def park_review_timeout(ref: BuildRef, diagnostic: str) -> None:
    _transaction(
        ref,
        lambda data, journal: _park(
            data, journal, ref, "review_timeout_changed", diagnostic
        ),
    )


def _owner_argv(ref: BuildRef, verb: str) -> tuple[str, ...]:
    return (
        str(Path(__file__).resolve().parents[2] / "crew"),
        verb,
        "--session-segment",
        ref.session_segment,
        "--loop-instance-id",
        ref.loop_instance_id,
    )


def recovery_argv(ref: BuildRef, action_id: str) -> tuple[str, ...]:
    return writer_recovery_argv(ref.session_segment, ref.loop_instance_id, action_id)


def _round_prompt_content(journal: BuildJournal, ref: BuildRef) -> bytes:
    content = _safe(Path(journal.round_prompt_path), ref).read_bytes()
    if sha256(content) != journal.round_prompt_sha256:
        raise review.WorkflowError("conflict", "immutable round prompt changed")
    return content


def _action_prompt_content(journal: BuildJournal, ref: BuildRef) -> bytes:
    action = journal.action
    content = _safe(Path(action.prompt_path), ref).read_bytes()
    if (
        sha256(content) != action.prompt_sha256
        or action.prompt_sha256 != journal.round_prompt_sha256
    ):
        raise review.WorkflowError("conflict", "issued implementation prompt changed")
    return content


def _issue(data: dict, journal: BuildJournal, ref: BuildRef) -> None:
    _admit(data, journal)
    if journal.action is not None:
        return
    ordinal = journal.action_ordinal + 1
    action_id = f"action-{ordinal:04d}"
    prompt = _root(ref) / "prompts" / f"{action_id}.txt"
    feedback = _feedback_references(journal)
    if journal.round_prompt_path:
        content = _round_prompt_content(journal, ref)
    else:
        text = (
            "Implement the immutable task in the canonical workspace "
            + str(crew_base().resolve())
            + ".\n"
            "Leave every change UNSTAGED and UNCOMMITTED; stay on the same branch. Do not commit, push, stage, switch branches, reset or clean.\n"
            "Diagnose structural causes of blocking findings and fix them; never silently defer blockers to finish.\n"
            f"Original task (preserve its scope):\n{journal.selection.task}\n"
            + "\n".join(feedback)
        )
        if journal.executor.role:
            text += "\nUse TodoWrite to track implementation and verification.\n"
        if journal.supplementary_answer:
            text += (
                f"\nSupplementary operator answer:\n{journal.supplementary_answer}\n"
            )
        if journal.format_correction:
            text += "\nThe previous return had an invalid terminal marker. Correct its report format.\n"
        text += (
            "\nThe final nonblank LF-delimited report line MUST be exactly CREW_BUILD_STATUS: COMPLETED or CREW_BUILD_STATUS: BLOCKED. "
            "COMPLETED asserts no unresolved task blocker. Do not quote, indent, fence or suffix that line. Nothing follows it.\n"
        )
        content = text.encode("utf-8")
    _write_once(prompt, content, ref)
    if journal.round_prompt_path is None:
        journal.round_prompt_path = str(prompt)
        journal.round_prompt_sha256 = sha256(content)
    journal.action_ordinal = ordinal
    journal.action = BuildAction(
        action_id,
        "revision" if journal.stage == "revision" else "implementation",
        str(prompt),
        sha256(content),
    )
    journal.round_attempt += 1


def _safe_feedback(path: str) -> Path:
    candidate = Path(path).absolute()
    workspace = crew_base().resolve()
    root = workspace / ".crew" / "reviews"
    resolved = candidate.resolve()
    if ".." in candidate.parts or not resolved.is_relative_to(root):
        raise review.WorkflowError(
            "unsafe_path", "feedback must be retained in the canonical review namespace"
        )
    # Workspace ancestors may be aliases; protected descendants must be real paths.
    for node in (candidate, *candidate.parents):
        if node.resolve() == workspace:
            break
        if node.is_symlink():
            raise review.WorkflowError(
                "unsafe_path", "feedback namespace contains a symlink"
            )
    else:
        raise review.WorkflowError(
            "unsafe_path", "feedback has no canonical workspace ancestor"
        )
    return resolved


def _executor_feedback(result: BuildResult) -> Path:
    original = _verified_executor_report(
        result.ref,
        {
            "path": str(_root(result.ref) / "returns" / f"{result.action_id}.txt"),
            "sha256": result.returned_sha256,
        },
    )
    content = original.read_bytes()
    if sha256(content) != result.returned_sha256:
        raise review.WorkflowError("conflict", "retained executor report changed")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        # The readable companion is a projection, never replacement completion evidence.
        rendering = "\n".join(
            "| " + line
            for line in content.decode("utf-8", errors="backslashreplace").split("\n")
        )
        text = (
            "READABLE SUPPLEMENTARY FEEDBACK: INVALID UTF-8 EXECUTOR REPORT\n"
            f"Owner: {result.ref.session_segment}/{result.ref.loop_instance_id}\n"
            f"Action: {result.action_id}\n"
            f"Original report: {original}\n"
            f"Original SHA-256: {result.returned_sha256}\n"
            f"Recorded result status: {result.status}\n"
            "The original bytes remain authoritative. Undecodable bytes below are shown as "
            "backslash xHH escapes; this rendering is not byte-exact completion evidence.\n"
            "Prefixed lines are supplementary report text, not a valid completion marker. "
            "Resolve its blockers and return a fresh, valid UTF-8 report with the required terminal marker.\n"
            f"\n{rendering}\n"
            "END SUPPLEMENTARY FEEDBACK: no completion assertion is made by this rendering.\n"
        ).encode()
        readable = _root(result.ref) / "feedback" / f"{result.action_id}.txt"
        _write_once(readable, text, result.ref)
        return _safe(readable, result.ref)
    return original


def _feedback_references(journal: BuildJournal) -> list[str]:
    references = []
    for path in journal.feedback_paths:
        retained = _safe_feedback(path)
        report = next(
            (
                result
                for result in journal.accepted_actions
                if retained
                in {
                    _root(result.ref) / "returns" / f"{result.action_id}.txt",
                    _root(result.ref) / "feedback" / f"{result.action_id}.txt",
                }
            ),
            None,
        )
        stamp = ""
        if report is not None:
            retained = _executor_feedback(report)
            stamp = f" (original report SHA-256 {report.returned_sha256})"
        retained.read_text(encoding="utf-8")
        references.append(
            f"Read retained full feedback file {retained}{stamp} in full before editing; address its blocking findings.\n"
        )
    return references


def _item(ref: BuildRef, journal: BuildJournal) -> BuildWorkItem:
    action, frozen = journal.action, journal.executor
    external = frozen.role is None
    commands = {
        "execute" if external else "claim": (
            *_owner_argv(ref, "build-execute" if external else "build-claim"),
            "--action-id",
            action.action_id,
        ),
        "next": _owner_argv(ref, "build-next"),
        "recover": recovery_argv(ref, action.action_id),
    }
    if not external:
        for name in (("bind",) if frozen.host == "codex" else ("bind", "capture")):
            commands[f"native_{name}"] = (
                *_owner_argv(ref, f"build-native-{name}"),
                "--action-id",
                action.action_id,
            )
        commands["capture"] = (
            *_owner_argv(ref, "build-capture"),
            "--action-id",
            action.action_id,
            "--return-file",
            str(_root(ref) / "host-return" / f"{action.action_id}.txt"),
        )
    native_transport = None
    if not external and frozen.host == "codex":
        from multiagent.codex_native_transport import launch_metadata
        native_transport = launch_metadata(ref, action.action_id, frozen.role,
            frozen.model, frozen.reasoning, action.prompt_path,
            str(_root(ref) / "host-return" / f"{action.action_id}.txt"))
    return BuildWorkItem(
        ref,
        action.action_id,
        action.kind,
        "external" if external else "native",
        frozen.role,
        frozen.model,
        frozen.channel,
        "workspace-write",
        action.prompt_path,
        str(_root(ref) / "host-return" / f"{action.action_id}.txt"),
        journal.policy.timeout_seconds if external else None,
        journal.policy.timeout_seconds + 60 if external else None,
        commands,
        native_transport=native_transport,
    )


def _projection(data: dict, journal: BuildJournal, ref: BuildRef) -> BuildStep:
    if not is_active_value(data.get("active")):
        return BuildStep(
            review.StepType.TERMINAL,
            ref,
            "Build ended"
            + (
                " over advisories: " + ", ".join(data["last_verdict_overrides"])
                if data.get("last_verdict_overrides")
                else ""
            ),
            outcome={
                "status": data.get("exit_kind"),
                "last_verdict_overrides": data.get("last_verdict_overrides") or [],
                "reason": data.get("reason"),
                "writer_fence": writer_fence(data),
                "cancellation_diagnostic": journal.cancellation_diagnostic,
                "owned_handle": journal.action.handle
                if journal.outstanding_writer
                else None,
            },
        )
    if journal.outstanding_writer:
        return BuildStep(
            review.StepType.WAITING,
            ref,
            "Claimed writer requires actual completion or confirmed quiescence: "
            + shlex.join(recovery_argv(ref, journal.outstanding_writer)),
            in_flight=(journal.outstanding_writer,),
        )
    if journal.question:
        return BuildStep(
            review.StepType.NEEDS_INPUT,
            ref,
            journal.question.text,
            question=journal.question,
        )
    if journal.action and journal.action.status == "ready":
        return BuildStep(
            review.StepType.WORK_BATCH,
            ref,
            "Issued implementation work",
            work_items=(_item(ref, journal),),
        )
    return BuildStep(review.StepType.WAITING, ref, f"Build stage: {journal.stage}")


def start_build(request: BuildRequest) -> BuildStep:
    session = review._session(
        review.ReviewRequest("working-tree", session_id=request.session_id)
    )
    selection = parse_arguments(request.raw_arguments)
    override = os.environ.get("CLAUDE_WORKING_DIRECTORY")
    if override and Path(override).resolve() != crew_base().resolve():
        raise review.WorkflowError(
            "workspace_conflict",
            "build execution is pinned to the canonical project workspace",
        )
    request_id = sha256(
        canonical(
            {"task": selection.task, "panel": selection.panel, "seats": selection.seats}
        )
    )
    path = loop_state.resolve("bl", session)
    ref = None
    stamp_note = None
    observed, _ = read_state_json(path)
    prepared_route = None
    if not observed or not is_active_value(observed.get("active")):
        chosen = execution.resolve_executor(session, selection.executor)
        frozen = _freeze_executor(chosen)
        if not _route_matches(frozen):
            raise review.WorkflowError(
                "route_unavailable", "executor route lacks its frozen write capability"
            )
        prepared_route = (frozen, _round_policy(frozen))
    with loop_state.admission_lock(), loop_state.admission_states(session) as states:
        data, _status = states.get(path, (None, LOAD_MISSING))
        if data and is_active_value(data.get("active")):
            journal = journal_from_dict(data.get("bl_workflow"))
            if journal.request_id != request_id:
                raise review.WorkflowError(
                    "active_request_conflict",
                    "task/panel/seats differ from the active build",
                )
            ref = BuildRef(session, data["loop_instance_id"])
            if (
                selection.executor is not None
                and selection.executor != journal.executor.executor
            ):
                stamp_note = f"Active executor stamp {journal.executor.executor!r} retains authority over the later flag {selection.executor!r}."
        elif data and (data.get("force_exit") or data.get("exit_kind") == "force_exit"):
            raise review.WorkflowError(
                "active_request_conflict",
                "a safety-exited loop cannot be replaced by build start",
            )
        loop_state.check_admission(
            states, replacing=path, continuing=path if ref else None
        )
    if ref is None:
        if prepared_route is None:
            raise review.WorkflowError(
                "active_request_conflict", "admission changed; retry the request"
            )
        frozen, policy = prepared_route
        try:
            with continuations.continuation_lock(session, "build-executor"):
                baseline = execution.observe_workspace(str(crew_base().resolve()))
                if not baseline.complete:
                    raise review.WorkflowError(
                        "workspace_guard",
                        "cannot establish canonical HEAD/index/branch baseline",
                    )
                with (
                    loop_state.admission_lock(),
                    loop_state.admission_states(session) as states,
                ):
                    loop_state.check_admission(states, replacing=path)
                    current, _ = states.get(path, (None, LOAD_MISSING))
                    if current != data:
                        raise review.WorkflowError(
                            "active_request_conflict",
                            "lifetime changed during admission",
                        )
                    journal = BuildJournal(
                        1,
                        request_id,
                        request.raw_arguments,
                        selection,
                        frozen,
                        policy,
                        baseline,
                    )
                    import uuid

                    deadline = config.deadline_minutes()
                    deadline = 240 if deadline is None else deadline
                    fresh = dataclasses.asdict(
                        LoopState(
                            active=True,
                            loop="bl",
                            task=selection.task,
                            session_id=session,
                            loop_instance_id=str(uuid.uuid4()),
                            started_at=utc_now_iso(),
                            executor=frozen.executor,
                            resume_executor=frozen.resume_executor,
                            deadline_minutes=deadline,
                            no_deadline=deadline == 0,
                        )
                    )
                    fresh.update(
                        schema=SCHEMA_VERSION, bl_workflow=journal_to_dict(journal)
                    )
                    continuations.invalidate(session, "build-executor")
                    atomic_write_json(path, fresh)
                    ref = BuildRef(session, fresh["loop_instance_id"])
        except continuations.ContinuationLockError:
            return BuildStep(
                review.StepType.WAITING,
                None,
                "Start was not admitted: continuation chain is busy. Retain and retry the same build request after the owned writer finishes; no lifetime was issued",
            )
    step = next_build(ref)
    return (
        replace(step, display=step.display + " " + stamp_note) if stamp_note else step
    )


def resume_build(session_id: str) -> BuildStep:
    session = review._session(
        review.ReviewRequest("working-tree", session_id=session_id)
    )
    with loop_state.admission_lock(), loop_state.admission_states(session):
        path = loop_state.resolve("bl", session)
        data, status = read_state_json(path)
        if status != LOAD_OK:
            raise review.WorkflowError(
                status, "no readable build lifetime for this session"
            )
        journal_from_dict(data.get("bl_workflow"))
        ref = BuildRef(session, data["loop_instance_id"])
    return next_build(ref)


@dataclass(frozen=True, slots=True)
class BuildRoundPreparation:
    executor: FrozenExecutor


@dataclass(frozen=True, slots=True)
class BuildPreparation:
    request: review.ReviewRequest
    binding: review.LoopReviewBinding
    summary: str
    report: dict[str, str] | None


def _guard_clean(journal: BuildJournal) -> bool:
    return (
        journal.baseline.complete
        and execution.observe_workspace(str(crew_base().resolve())) == journal.baseline
    )


def _apply_outcome(
    data: dict,
    journal: BuildJournal,
    ref: BuildRef,
    step: review.ReviewStep,
    *,
    force: bool = False,
) -> None:
    if not _guard_clean(journal):
        _park(
            data,
            journal,
            ref,
            "workspace_guard",
            "HEAD/index/branch changed or cannot be observed; restore the issued baseline before review or completion",
        )
        return
    evidence = review._read_loop_review_evidence_under_owner_lock(step.ref)
    digest = evidence.source.accepted_outcome_sha256
    if digest in journal.applied_outcomes:
        return
    verdict = "FAILED" if not evidence.usable_seats else step.outcome["judgment"]
    question = journal.question
    authorization = (
        loop_state.CompletionAuthorization(question.text, question.advisories)
        if force and question
        else None
    )
    try:
        loop_state.apply_verdict(
            data,
            evidence,
            verdict,
            minor_only=bool(step.outcome["minor_only"]),
            force=force,
            force_authorization=authorization,
            loop="bl",
            session_id=ref.session_segment,
        )
    except loop_state._Advisory as exc:
        _park(
            data,
            journal,
            ref,
            "completion_advisory",
            exc.diagnostic,
            outcome_sha256=digest,
            advisories=exc.conditions,
        )
        return
    journal.applied_outcomes.append(digest)
    journal.feedback_paths = list(
        dict.fromkeys(
            p
            for p in (step.outcome.get("synthesis_path"), step.outcome.get("full_path"))
            if p
        )
    )
    journal.question = None
    journal.review_ref = None
    journal.pending_review_inputs = None
    journal.retry_authorization = None
    if data.get("phase") == "done":
        journal.stage = "done"
    elif verdict == "FAILED":
        journal.stage = "prepare_review"
    else:
        journal.stage = "revision"
        journal.action = None
        journal.round_attempt = 0
        journal.round_prompt_path = None
        journal.round_prompt_sha256 = None
        journal.round_policy_ready = False


def _review_item(
    ref: BuildRef, review_ref: review.ReviewRef, item: review.WorkItem
) -> BuildWorkItem:
    # One projection for issuance and native launch; Review owns the full claim.
    commands = review.issued_review_commands(review_ref, item)
    commands["next"] = _owner_argv(ref, "build-next")
    return BuildWorkItem(
        ref,
        item.action_id,
        item.kind,
        item.driver,
        item.role,
        item.model,
        item.channel,
        item.access,
        item.prompt_path,
        item.ingress_path,
        item.timeout_seconds,
        item.timeout_seconds + 60 if item.timeout_seconds is not None else None,
        commands,
        review_ref,
        item,
        item.native_transport,
    )


def _review_items(ref: BuildRef, step: review.ReviewStep) -> BuildStep:
    return BuildStep(
        step.type,
        ref,
        step.display or "Loop-owned code review",
        work_items=tuple(_review_item(ref, step.ref, item) for item in step.work_items),
        in_flight=step.in_flight,
        outcome=step.outcome,
    )


def _verified_executor_report(ref: BuildRef, artifact: dict[str, str]) -> Path:
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"path", "sha256"}
        or not isinstance(artifact["path"], str)
        or not isinstance(artifact["sha256"], str)
        or not review.SHA_RE.fullmatch(artifact["sha256"])
    ):
        raise review.WorkflowError(
            "invalid_checkpoint", "executor report reference is malformed"
        )
    path = _safe(Path(artifact["path"]), ref)
    if path.parent != _root(ref) / "returns" or not re.fullmatch(
        r"action-[0-9]{4,}\.txt", path.name
    ):
        raise review.WorkflowError(
            "invalid_checkpoint", "executor report is outside its owner's returns"
        )
    content = path.read_bytes()
    if sha256(content) != artifact["sha256"]:
        raise review.WorkflowError("conflict", "retained executor report changed")
    return path


def _next_locked(
    data: dict, journal: BuildJournal, ref: BuildRef
) -> BuildStep | BuildPreparation | BuildRoundPreparation | None:
    # Terminal authority wins before replay, so accepting a late completion can
    # only finalize its fence; it cannot map a result to retries or review work.
    if is_active_value(data.get("active")):
        if data.get("phase") == "done":
            loop_state.deactivate(data)
        else:
            bound = loop_state.bound_reason(data)
            if bound:
                data.update(
                    active=False,
                    force_exit=True,
                    exit_kind="force_exit",
                    reason=bound,
                    completed_at=utc_now_iso(),
                )
    if journal.outstanding_writer:
        try:
            _reconcile_completion(data, journal, ref)
        except review.WorkflowError as exc:
            if is_active_value(data.get("active")):
                raise
            terminal = _projection(data, journal, ref)
            return replace(
                terminal,
                display=f"{terminal.display}; retained completion rejected ({exc.code}): {exc}",
            )
    if not is_active_value(data.get("active")) or journal.outstanding_writer:
        return _projection(data, journal, ref)
    if journal.question:
        auth = journal.retry_authorization
        if auth and auth.decision.question_id == journal.question.question_id:
            step = review._retry_loop_review_under_owner_lock(
                review.RetryRequest(
                    auth.source_ref,
                    () if auth.decision.kind == "retry_synthesis" else None,
                )
            )
            journal.review_ref = step.ref
            journal.question = None
            data["awaiting_input"] = False
            return None
        data["awaiting_input"] = True
        return _projection(data, journal, ref)
    if not _route_matches(journal.executor):
        _park(
            data,
            journal,
            ref,
            "route_unavailable",
            "The frozen executor route/model/capability changed; restore it and retry_route",
        )
        return _projection(data, journal, ref)
    if journal.stage in {"implementation", "revision"}:
        if not journal.round_policy_ready:
            return BuildRoundPreparation(journal.executor)
        try:
            _issue(data, journal, ref)
        except (OSError, UnicodeError) as exc:
            _park(
                data,
                journal,
                ref,
                "feedback_recovery",
                f"Retained feedback cannot be read: {exc}",
            )
        return _projection(data, journal, ref)
    if journal.stage == "prepare_review":
        if not _guard_clean(journal):
            _park(
                data,
                journal,
                ref,
                "workspace_guard",
                "HEAD/index/branch changed; restore the issued baseline before review",
            )
            return _projection(data, journal, ref)
        if journal.pending_review_inputs is None:
            summary = f"Immutable task and acceptance:\n{journal.selection.task}"
            report = None
            if journal.action and journal.action.result:
                report = {
                    "path": str(
                        _root(ref) / "returns" / f"{journal.action.action_id}.txt"
                    ),
                    "sha256": journal.action.result.returned_sha256,
                }
                try:
                    _verified_executor_report(ref, report)
                except (OSError, UnicodeError, review.WorkflowError) as exc:
                    _park(
                        data,
                        journal,
                        ref,
                        "feedback_recovery",
                        f"Retained executor context cannot be read or verified: {exc}",
                    )
                    return _projection(data, journal, ref)
            else:
                summary += "\nOperator-adopted edits after confirmed quiescence"
            return BuildPreparation(
                review.ReviewRequest(
                    "working-tree",
                    panel=journal.selection.panel,
                    seats=journal.selection.seats,
                    session_id=ref.session_segment,
                ),
                review.LoopReviewBinding(
                    ref.session_segment,
                    "bl",
                    ref.loop_instance_id,
                    journal.review_generation + 1,
                ),
                summary,
                report,
            )
        prepared = journal.pending_review_inputs
        step = review._start_loop_review_under_owner_lock(prepared)
        journal.review_ref = step.ref
        evidence = loop_state.ReviewEvidence(
            step.ref.run_id,
            step.ref.target_sha256,
            "working-tree",
            "",
            tuple(prepared.record["seat_signatures"]),
            (),
            loop_state.BindingStatus.MATCH,
            loop_state.LoopReviewSource(
                review.parse_loop_binding(
                    prepared.record["workflow_identity"]["loop_binding"]
                ),
                step.ref,
                None,
            ),
        )
        loop_state.bind_review(data, evidence)
        journal.stage = "review"
        return None
    if journal.stage == "review":
        run = review._guard_review_path(
            session_segment=ref.session_segment,
            run_id=journal.review_ref.run_id,
            create=False,
        )
        with review._workflow_lock(run):
            wf = review._workflow(run)
            binding = review.parse_loop_binding(wf["workflow_identity"]["loop_binding"])
            review._owner_state(binding, journal.review_ref, wf)
            step = review._advance_locked(wf, run)
        journal.review_ref = step.ref
        if step.type == review.StepType.TERMINAL:
            if step.outcome["status"] == "synthesis_failed":
                _park(
                    data,
                    journal,
                    ref,
                    "synthesis_retry",
                    "Retry synthesis with the accepted panel, or cancel",
                )
                return _projection(data, journal, ref)
            _apply_outcome(data, journal, ref, step)
            return None
        return _review_items(ref, step)
    raise review.WorkflowError("invalid_journal", "build stage cannot advance")


def next_build(ref: BuildRef) -> BuildStep:
    for _ in range(16):
        step = _transaction(ref, lambda data, journal: _next_locked(data, journal, ref))
        if isinstance(step, BuildRoundPreparation):
            policy = _round_policy(step.executor)

            def checkpoint_policy(
                data: dict,
                journal: BuildJournal,
                step: BuildRoundPreparation = step,
                policy: RoundPolicy = policy,
            ) -> None:
                _admit(data, journal)
                if journal.executor != step.executor or journal.action is not None:
                    raise review.WorkflowError(
                        "stale_owner", "round changed during preparation"
                    )
                if not journal.round_policy_ready:
                    journal.policy = policy
                    journal.round_policy_ready = True

            _transaction(ref, checkpoint_policy)
        elif isinstance(step, BuildPreparation):
            prepared = review.prepare_loop_review(
                step.request,
                step.binding,
                executor_summary=step.summary,
                executor_report=step.report,
            )

            def checkpoint(
                data: dict,
                journal: BuildJournal,
                prepared: review.PreparedLoopReview = prepared,
                generation: int = step.binding.review_generation,
            ) -> None:
                _admit(data, journal)
                if journal.stage != "prepare_review":
                    raise review.WorkflowError(
                        "stale_owner",
                        "review preparation no longer belongs to this build",
                    )
                if journal.pending_review_inputs is None:
                    journal.pending_review_inputs = prepared
                    journal.review_generation = generation

            _transaction(ref, checkpoint)
        elif step is not None:
            return step
    raise review.WorkflowError("invalid_journal", "build advancement did not settle")


class _AlreadyClaimed(Exception):
    pass


def _same_execution_probe(current: BuildJournal, observed: BuildJournal) -> bool:
    return (
        current.action == observed.action
        and current.round_prompt_path == observed.round_prompt_path
        and current.round_prompt_sha256 == observed.round_prompt_sha256
        and current.policy == observed.policy
        and current.executor == observed.executor
        and current.stage == observed.stage
        and current.review_ref == observed.review_ref
        and current.review_generation == observed.review_generation
        and current.round_policy_ready == observed.round_policy_ready
        and current.baseline == observed.baseline
        and current.question is None
        and current.outstanding_writer is None
        and current.action is not None
        and current.action.status == "ready"
        and _route_matches(current.executor)
    )


def _claim(
    ref: BuildRef,
    action_id: str,
    before: execution.WorkspaceFacts,
    *,
    external: bool,
    observed: BuildJournal | None = None,
) -> BuildWorkItem:
    def claim(data: dict, journal: BuildJournal) -> BuildWorkItem:
        action = journal.action
        if observed is not None and not _same_execution_probe(journal, observed):
            raise _AlreadyClaimed()
        _admit(data, journal)
        if (
            action is None
            or action.action_id != action_id
            or (journal.executor.role is None) != external
        ):
            raise review.WorkflowError(
                "invalid_action",
                "action or execution route differs from the issued work",
            )
        if action.status != "ready":
            raise _AlreadyClaimed()
        if not _route_matches(journal.executor):
            raise review.WorkflowError(
                "route_unavailable", "frozen executor route changed before claim"
            )
        if not before.complete or before != journal.baseline:
            _park(
                data,
                journal,
                ref,
                "workspace_guard",
                "Cannot claim work over changed or unknown HEAD/index/branch",
            )
            return _item(ref, journal)
        try:
            _feedback_references(journal)
            _round_prompt_content(journal, ref)
        except (OSError, UnicodeError) as exc:
            _park(
                data,
                journal,
                ref,
                "feedback_recovery",
                f"Retained feedback cannot be read: {exc}",
            )
            return _item(ref, journal)
        _action_prompt_content(journal, ref)
        journal.action = replace(action, status="claimed", before=before)
        journal.outstanding_writer = action_id
        return _item(ref, journal)

    return _transaction(ref, claim)


def claim_build_action(ref: BuildRef, action_id: str) -> dict[str, object]:
    try:
        item = _claim(
            ref,
            action_id,
            execution.observe_workspace(str(crew_base().resolve())),
            external=False,
        )
    except _AlreadyClaimed:
        return {"authorization": "wait", "action_id": action_id}
    claimed = _transaction(
        ref, lambda _data, journal: journal.outstanding_writer == action_id
    )
    if not claimed:
        step = next_build(ref)
        return {"authorization": step.type, "step": step_to_dict(step)}
    return {"authorization": "spawn", "work_item": dataclasses.asdict(item)}


def _receipt(
    ref: BuildRef,
    action: BuildAction,
    frozen: FrozenExecutor,
    content: bytes,
    status: str,
    after: execution.WorkspaceFacts,
    diagnostic: str | None,
    report_authoritative: bool,
) -> bytes:
    return canonical(
        {
            "ref": ref_to_dict(ref),
            "action_id": action.action_id,
            "executor": dataclasses.asdict(frozen),
            "before": dataclasses.asdict(action.before),
            "after": dataclasses.asdict(after),
            "transport_status": status,
            "returned_sha256": sha256(content),
            "diagnostic": diagnostic,
            "report_authoritative": report_authoritative,
        }
    )


def _read_artifact(path: Path, ref: BuildRef) -> bytes:
    try:
        return _safe(path, ref).read_bytes()
    except OSError as exc:
        raise review.WorkflowError(
            "invalid_result", f"Retained completion cannot be read: {exc}"
        ) from exc


def _read_json(path: Path, ref: BuildRef) -> tuple[object, bytes]:
    body = _read_artifact(path, ref)
    try:
        return json.loads(body), body
    except (ValueError, UnicodeError) as exc:
        raise review.WorkflowError(
            "invalid_result", "Retained completion is not valid JSON"
        ) from exc


def _read_receipt(ref: BuildRef, action_id: str) -> tuple[BuildExecutionReceipt, bytes]:
    value, body = _read_json(
        _root(ref) / "execution-receipts" / f"{action_id}.json", ref
    )
    fields = _fields(value, BuildExecutionReceipt)
    fields["ref"] = parse_build_ref(fields["ref"])
    fields["executor"] = FrozenExecutor(**_fields(fields["executor"], FrozenExecutor))
    fields["before"] = _facts(fields["before"])
    fields["after"] = _facts(fields["after"])
    receipt = BuildExecutionReceipt(**fields)
    if (
        receipt.ref != ref
        or receipt.action_id != action_id
        or type(receipt.executor.resume_executor) is not bool
        or type(receipt.report_authoritative) is not bool
        or not isinstance(receipt.transport_status, str)
        or receipt.transport_status
        not in {"ok", "failed", "timeout", "cancelled", "unavailable"}
        or not isinstance(receipt.returned_sha256, str)
        or not review.SHA_RE.fullmatch(receipt.returned_sha256)
        or receipt.diagnostic is not None
        and not isinstance(receipt.diagnostic, str)
    ):
        raise review.WorkflowError(
            "invalid_result", "Retained receipt has invalid owner or completion facts"
        )
    return receipt, body


def _reconcile_completion(data: dict, journal: BuildJournal, ref: BuildRef) -> None:
    action_id = journal.outstanding_writer
    path = _safe(_root(ref) / "submissions" / f"{action_id}.json", ref)
    if not path.exists():
        return
    value, _body = _read_json(path, ref)
    result = parse_build_result(value)
    if result.ref != ref or result.action_id != action_id:
        raise review.WorkflowError(
            "invalid_result",
            "Retained submission differs from the outstanding owner/action",
        )
    _accept_result(data, journal, result)


def _retain_completion(
    ref: BuildRef,
    action_id: str,
    content: bytes,
    *,
    status: str,
    after: execution.WorkspaceFacts,
    diagnostic: str | None,
    report_authoritative: bool = True,
) -> BuildResult:
    return _transaction(
        ref,
        lambda data, journal: _retain_completion_locked(
            data,
            journal,
            ref,
            action_id,
            content,
            status=status,
            after=after,
            diagnostic=diagnostic,
            report_authoritative=report_authoritative,
        ),
    )


def _retain_completion_locked(
    data: dict,
    journal: BuildJournal,
    ref: BuildRef,
    action_id: str,
    content: bytes,
    *,
    status: str,
    after: execution.WorkspaceFacts,
    diagnostic: str | None,
    report_authoritative: bool = True,
) -> BuildResult:
    accepted = next(
        (r for r in journal.accepted_actions if r.action_id == action_id), None
    )
    if accepted:
        receipt, _body = _read_receipt(ref, action_id)
        if (
            accepted.returned_sha256 != sha256(content)
            or receipt.transport_status != status
            or accepted.diagnostic != diagnostic
            or receipt.report_authoritative != report_authoritative
        ):
            raise review.WorkflowError(
                "conflict", "capture differs from the immutable accepted return"
            )
        return accepted
    action = journal.action
    if (
        action is None
        or action.action_id != action_id
        or not isinstance(action.status, str)
        or action.status not in {"claimed", "settled"}
    ):
        raise review.WorkflowError(
            "invalid_action", "completion requires its durable claimed action"
        )
    if (
        status not in {"ok", "failed", "timeout", "cancelled", "unavailable"}
        or diagnostic is not None
        and not isinstance(diagnostic, str)
        or type(report_authoritative) is not bool
    ):
        raise review.WorkflowError("invalid_result", "invalid transport completion")
    observed_after = after
    receipt_path = _root(ref) / "execution-receipts" / f"{action_id}.json"
    if receipt_path.exists():
        saved, _body = _read_receipt(ref, action_id)
        if (
            saved.returned_sha256 != sha256(content)
            or saved.transport_status != status
            or saved.diagnostic != diagnostic
            or saved.executor != journal.executor
            or saved.before != action.before
            or saved.report_authoritative != report_authoritative
        ):
            raise review.WorkflowError(
                "conflict", "capture differs from the retained completion"
            )
        observed_after = saved.after
    body = _receipt(
        ref,
        action,
        journal.executor,
        content,
        status,
        observed_after,
        diagnostic,
        report_authoritative,
    )
    result = BuildResult(
        ref,
        action_id,
        (report_status(content) if report_authoritative else "invalid_report")
        if status == "ok"
        else status,
        sha256(content),
        sha256(body),
        diagnostic,
    )
    if action.result is not None and action.result != result:
        raise review.WorkflowError(
            "conflict", "completion differs from the accepted action"
        )
    _write_once(_root(ref) / "returns" / f"{action_id}.txt", content, ref)
    _write_once(_root(ref) / "execution-receipts" / f"{action_id}.json", body, ref)
    _write_once(
        _root(ref) / "submissions" / f"{action_id}.json",
        canonical(result_to_dict(result)),
        ref,
    )
    _accept_result(data, journal, result)
    return result


def _map_result(data: dict, journal: BuildJournal, ref: BuildRef) -> None:
    result = journal.action.result
    if result.status == "completed":
        journal.stage = "prepare_review"
        data["awaiting_input"] = False
    elif result.status in {"blocked", "invalid_report"}:
        report = _verified_executor_report(
            ref,
            {
                "path": str(_root(ref) / "returns" / f"{result.action_id}.txt"),
                "sha256": result.returned_sha256,
            },
        )
        _park(
            data,
            journal,
            ref,
            "executor_blocked"
            if result.status == "blocked"
            else "executor_report_recovery",
            f"Executor returned {result.status}; read the full retained report at "
            f"{report} "
            f"(SHA-256 {result.returned_sha256}) to resolve its blocker or report format. "
            "Use this bound question with build-decide: answer_executor with your answer, "
            "or retry_executor to request a corrected execution/report; both require stack_edits confirmation. "
            "Use cancel to end the build. Review remains unadmitted.",
        )
    elif (
        journal.executor.role is None
        and result.status in {"failed", "timeout"}
        and journal.round_attempt <= journal.policy.retries
    ):
        journal.action = None
        data["awaiting_input"] = False
    else:
        _park(
            data,
            journal,
            ref,
            "execution_recovery",
            result.diagnostic
            or "Terminated executor needs an explicit operator decision; partial edits remain",
        )


def _accept_result(data: dict, journal: BuildJournal, result: BuildResult) -> None:
    ref = result.ref
    accepted = next(
        (r for r in journal.accepted_actions if r.action_id == result.action_id), None
    )
    if accepted:
        if accepted != result:
            raise review.WorkflowError(
                "conflict", "submission differs from accepted result"
            )
        return
    action = journal.action
    if (
        action is None
        or action.action_id != result.action_id
        or action.status != "claimed"
        or journal.outstanding_writer != result.action_id
    ):
        raise review.WorkflowError(
            "invalid_action", "submission has no outstanding issued claim"
        )
    receipt, body = _read_receipt(ref, result.action_id)
    content = _read_artifact(_root(ref) / "returns" / f"{result.action_id}.txt", ref)
    expected = (
        (report_status(content) if receipt.report_authoritative else "invalid_report")
        if receipt.transport_status == "ok"
        else receipt.transport_status
    )
    if (
        sha256(body) != result.execution_receipt_sha256
        or sha256(content) != result.returned_sha256
        or receipt.returned_sha256 != sha256(content)
        or receipt.executor != journal.executor
        or expected != result.status
        or receipt.before != action.before
        or receipt.diagnostic != result.diagnostic
    ):
        raise review.WorkflowError(
            "invalid_result", "submission differs from engine-observed receipt"
        )
    journal.accepted_actions.append(result)
    journal.action = replace(action, status="settled", result=result)
    journal.outstanding_writer = None
    if not is_active_value(data.get("active")):
        return
    if (
        not action.before.complete
        or receipt.after != action.before
        or not _guard_clean(journal)
    ):
        _park(
            data,
            journal,
            ref,
            "workspace_guard",
            "Executor changed HEAD/index/branch or observation is incomplete; restore baseline",
        )
        return
    _map_result(data, journal, ref)


def submit_build_action(result: BuildResult) -> BuildStep:
    result = parse_build_result(result_to_dict(result))
    ref = result.ref
    _transaction(ref, lambda data, journal: _accept_result(data, journal, result))
    return next_build(ref)


def execute_build_action(ref: BuildRef, action_id: str) -> BuildStep:
    data = loop_state.read(loop_state.resolve("bl", ref.session_segment))
    if data.get("loop_instance_id") != ref.loop_instance_id:
        raise review.WorkflowError("stale_owner", "build executor owner was replaced")
    journal = journal_from_dict(data["bl_workflow"])
    action = journal.action
    if (
        action is None
        or action.action_id != action_id
        or journal.executor.role is not None
    ):
        raise review.WorkflowError(
            "invalid_action", "build-execute requires its issued external action"
        )
    if action.status != "ready":
        return next_build(ref)
    if not _route_matches(journal.executor):
        return next_build(ref)
    override = os.environ.get("CLAUDE_WORKING_DIRECTORY")
    if override and Path(override).resolve() != crew_base().resolve():
        raise review.WorkflowError(
            "workspace_conflict",
            "working-directory override conflicts with the canonical build workspace",
        )
    provider = get_provider(journal.executor.executor)
    effective = getattr(provider, "effective_timeout", None)
    current_timeout = (
        math.ceil(effective(journal.policy.base_timeout_seconds))
        if callable(effective)
        else journal.policy.base_timeout_seconds
    )
    if current_timeout != journal.policy.timeout_seconds:

        def park_drift(data: dict, current: BuildJournal) -> None:
            if not _same_execution_probe(current, journal):
                return
            _admit(data, current)
            _park(
                data,
                current,
                ref,
                "route_unavailable",
                "Provider timeout floor changed during a frozen round; restore it before retry_route",
            )

        _transaction(ref, park_drift)
        return next_build(ref)
    holder: list[BuildResult] = []

    def claim(before: execution.WorkspaceFacts) -> None:
        _claim(ref, action_id, before, external=True, observed=journal)
        if _transaction(ref, lambda _data, j: j.outstanding_writer != action_id):
            raise _AlreadyClaimed()

    def settle(finish: Callable[[], dict], returned: ProviderResult) -> dict:
        def owned(data: dict, current: BuildJournal) -> dict:
            if (
                current.executor != journal.executor
                or current.outstanding_writer != action_id
            ):
                raise review.WorkflowError(
                    "invalid_action",
                    "settlement differs from the outstanding owner/route",
                )
            if (
                returned.name != current.executor.executor
                or returned.ok
                and returned.model != current.executor.model
            ):
                raise review.WorkflowError(
                    "provider_identity_mismatch",
                    "executor return differs from its frozen seat/model; writing work requires recovery",
                )
            envelope = finish()
            transport_status = returned.transport_status or (
                "ok"
                if returned.ok
                else "timeout"
                if returned.continuation and returned.continuation.failure == "timeout"
                else transport_failure(0, returned.error)
            )
            if transport_status in {"timeout_unconfirmed", "termination_unconfirmed"}:
                return envelope
            holder.append(
                _retain_completion_locked(
                    data,
                    current,
                    ref,
                    action_id,
                    returned.exact_output
                    if returned.exact_output is not None
                    else returned.output.encode("utf-8"),
                    status=transport_status,
                    after=execution.WorkspaceFacts(
                        envelope["head_after"],
                        envelope["staged_after"],
                        envelope["branch_after"],
                    ),
                    diagnostic=returned.error,
                    report_authoritative=returned.report_authoritative,
                )
            )
            return envelope

        return _transaction(ref, owned)

    try:
        execution.execute_write(
            execution.WriteRequest(
                journal.executor.executor,
                _action_prompt_content(journal, ref).decode("utf-8"),
                journal.executor.model,
                journal.policy.timeout_seconds,
                journal.policy.dispatch_options,
                str(crew_base().resolve()),
                ref.session_segment,
                "build-executor" if journal.executor.resume_executor else None,
                ref.loop_instance_id,
                build_report=True,
            ),
            provider=provider,
            before_run=claim,
            settlement=settle,
        )
    except _AlreadyClaimed:
        return next_build(ref)
    except execution.ExecutionBusy:
        return BuildStep(
            review.StepType.WAITING,
            ref,
            "Action or continuation chain is already owned; no provider launch",
            in_flight=(action_id,),
        )
    # Review reconciliation happens after execution has released continuation.
    return submit_build_action(holder[0]) if holder else next_build(ref)


def capture_build_return(
    ref: BuildRef,
    action_id: str,
    content: bytes,
    *,
    status: str = "ok",
    diagnostic: str | None = None,
    handle: str | None = None,
    completion_observed: bool = False,
    launch_refused: bool = False,
) -> BuildStep:
    after = execution.observe_workspace(str(crew_base().resolve()))

    def retain_native(data: dict, journal: BuildJournal) -> BuildResult:
        from multiagent.workflow_transport import require_capture_flags
        require_capture_flags(journal.executor.host, handle, completion_observed, launch_refused)
        if journal.executor.role != "crew:executor":
            raise review.WorkflowError(
                "invalid_action",
                "host capture cannot settle an external provider action",
            )
        if journal.executor.host == "codex":
            from multiagent.codex_native_transport import require_capture

            if launch_refused and journal.action and journal.action.handle is not None:
                raise review.WorkflowError(
                    "invalid_native_binding", "bound writer cannot be reported as unlaunched"
                )
            require_capture(
                _safe(_root(ref) / "native-transport" / "anchor", ref).parent,
                action_id, handle, completion_observed, status=status,
                diagnostic=diagnostic, launch_refused=launch_refused, ref=ref,
            )
        return _retain_completion_locked(
            data, journal, ref, action_id, content,
            status=status, after=after, diagnostic=diagnostic,
        )

    result = _transaction(ref, retain_native)
    return submit_build_action(result)


def capture_build_file(
    ref: BuildRef,
    action_id: str,
    returned_file: Path,
    *,
    status: str = "ok",
    diagnostic: str | None = None,
    handle: str | None = None,
    completion_observed: bool = False,
    launch_refused: bool = False,
) -> BuildStep:
    """Read only the current native action's issued host-return artifact."""

    def read(_data: dict, journal: BuildJournal) -> bytes:
        action = journal.action
        if (
            journal.executor.role != "crew:executor"
            or action is None
            or action.action_id != action_id
            or action.status not in {"claimed", "settled"}
        ):
            raise review.WorkflowError(
                "invalid_action",
                "file capture requires the current claimed native action",
            )
        expected = Path(_item(ref, journal).returned_path)
        supplied = _safe(returned_file, ref)
        if supplied != expected or not supplied.is_file():
            raise review.WorkflowError(
                "invalid_result",
                "return file must be the issued regular host-return artifact",
            )
        return supplied.read_bytes()

    content = _transaction(ref, read)
    return capture_build_return(
        ref, action_id, content, status=status, diagnostic=diagnostic,
        handle=handle, completion_observed=completion_observed, launch_refused=launch_refused,
    )


def bind_build_native(
    ref: BuildRef, action_id: str, handle: str, output_file: str | None = None
) -> dict[str, object]:
    from multiagent.workflow_transport import native_action_channel
    if native_action_channel(ref, action_id) == "codex":
        from multiagent.codex_native_transport import NativeLaunch, bind_native_launch
        if output_file is not None:
            raise review.WorkflowError("unsupported_native_capture", "Codex exposes no output file")
        return bind_native_launch(NativeLaunch(ref, action_id, handle))
    from multiagent.claude_native_transport import NativeLaunch, bind_native_launch

    if output_file is None:
        raise review.WorkflowError("invalid_native_binding", "Claude binding requires its observed output file")
    return bind_native_launch(NativeLaunch(ref, action_id, handle, Path(output_file)))


def capture_build_native(
    ref: BuildRef,
    action_id: str,
    handle: str,
    output_file: str,
    *,
    completion_observed: bool,
) -> BuildStep:
    from multiagent.claude_native_transport import NativeLaunch, capture_native_return
    from multiagent.workflow_transport import native_action_channel
    if native_action_channel(ref, action_id) == "codex":
        raise review.WorkflowError("unsupported_native_capture", "Codex uses build-capture with its observed final reply")

    return capture_native_return(
        NativeLaunch(ref, action_id, handle, Path(output_file)),
        completion_observed=completion_observed,
    )


def recover_build_action(ref: BuildRef, action_id: str, confirmation: str) -> BuildStep:
    if confirmation != "not_running":
        raise review.WorkflowError(
            "not_confirmed",
            "writer recovery requires explicit confirmation of actual quiescence: not_running",
        )

    def recover(data: dict, journal: BuildJournal) -> None:
        if journal.outstanding_writer != action_id or journal.action is None:
            raise review.WorkflowError(
                "invalid_recovery", "no matching outstanding writer"
            )
        journal.outstanding_writer = None
        journal.action = replace(journal.action, status="recovered")
        if is_active_value(data.get("active")):
            _park(
                data,
                journal,
                ref,
                "execution_recovery",
                "Writer is confirmed quiescent; inspect tracked/untracked edits before adoption or acknowledged stacking",
            )

    _transaction(ref, recover)
    return next_build(ref)


def cancel_build(
    ref: BuildRef,
    reason: str = "operator cancelled",
    *,
    cancel_handle: Callable[[str], bool] | None = None,
) -> BuildStep:
    handles: list[str] = []

    def cancel(data: dict, journal: BuildJournal) -> None:
        loop_state.deactivate(data, cancel=True, reason=reason)
        journal.question = None
        journal.retry_authorization = None
        if journal.outstanding_writer:
            if journal.action.handle:
                handles.append(journal.action.handle)
            journal.cancellation_diagnostic = "Host cancellation API unavailable; actual completion or explicit quiescence confirmation is required"

    _transaction(ref, cancel)
    for handle in handles:
        if cancel_handle is not None:
            try:
                stopped = cancel_handle(handle)
                diagnostic = (
                    "Cancellation requested for owned handle; await actual completion or confirm quiescence"
                    if stopped
                    else "Host could not cancel the owned handle; writer fence retained"
                )
            except Exception as exc:  # noqa: BLE001 - host cancellation failures retain the writer fence
                diagnostic = (
                    f"Owned handle cancellation failed: {exc}; writer fence retained"
                )
            _transaction(
                ref,
                lambda _data, journal, diagnostic=diagnostic: setattr(
                    journal, "cancellation_diagnostic", diagnostic
                ),
            )
    return next_build(ref)


def validate_review_retry(data: dict, wf: dict, source_ref: review.ReviewRef) -> None:
    journal = journal_from_dict(data["bl_workflow"])
    authorization = journal.retry_authorization
    binding = review.parse_loop_binding(wf["workflow_identity"]["loop_binding"])
    owner = BuildRef(binding.session_segment, binding.loop_instance_id)
    if (
        authorization is None
        or authorization.source_ref != source_ref
        or authorization.decision.ref != owner
    ):
        raise review.WorkflowError(
            "retry_not_authorized",
            "build review retry requires its bound durable decision",
        )
    decision = authorization.decision
    receipt = review._source_receipt(
        wf.get("retry_receipts", {}), source_ref.attempt_id
    )
    stored = review.parse_review_ref(wf["ref"])
    question_kind = (
        "synthesis_retry"
        if decision.kind == "retry_synthesis"
        else "completion_advisory"
    )
    receipt_kind = (
        "synthesis_restart" if decision.kind == "retry_synthesis" else "seat_retry"
    )
    if journal.question:
        if (
            journal.question.question_id != decision.question_id
            or journal.question.kind != question_kind
            or journal.review_ref != source_ref
        ):
            raise review.WorkflowError(
                "retry_not_authorized", "retry question or source attempt changed"
            )
    elif receipt is None or journal.review_ref != stored:
        raise review.WorkflowError(
            "retry_not_authorized", "retry is neither pending nor reconciled"
        )
    if (
        source_ref.run_id != stored.run_id
        or source_ref.target_sha256 != stored.target_sha256
        or (
            receipt is not None
            and (
                receipt["kind"] != receipt_kind
                or receipt["created_attempt_id"] != stored.attempt_id
            )
        )
        or (receipt is None and stored != source_ref)
    ):
        raise review.WorkflowError(
            "retry_not_authorized", "retry receipt differs from bound authorization"
        )


def _replace_review_generation(data: dict, journal: BuildJournal) -> None:
    journal.stage = "prepare_review"
    journal.review_ref = None
    journal.pending_review_inputs = None
    journal.retry_authorization = None
    journal.question = None
    data.update(phase="drafting", awaiting_input=False)


def decide_build(decision: BuildDecision) -> BuildStep:
    decision = parse_build_decision(decision_to_dict(decision))
    ref = decision.ref

    def decide(data: dict, journal: BuildJournal) -> None:
        previous = next(
            (d for d in journal.decisions if d.question_id == decision.question_id),
            None,
        )
        if previous:
            if previous != decision:
                raise review.WorkflowError(
                    "conflict", "answer differs from the accepted decision"
                )
            return
        question = journal.question
        if question is None or question.question_id != decision.question_id:
            raise review.WorkflowError(
                "stale_question", "decision must answer the current issued question"
            )
        if decision.kind == "cancel":
            if any(
                getattr(decision, k) is not None
                for k in (
                    "confirmation",
                    "workspace_sha256",
                    "completed_action",
                    "answer",
                )
            ):
                raise review.WorkflowError(
                    "invalid_decision", "cancel takes no extra decision fields"
                )
            loop_state.deactivate(data, cancel=True, reason="operator cancelled")
            journal.question = None
            journal.decisions.append(decision)
            return
        if (
            not is_active_value(data.get("active"))
            or loop_state.bound_reason(data)
            or journal.outstanding_writer
        ):
            raise review.WorkflowError(
                "work_not_admitted",
                "decision cannot authorize inactive, expired or uncertain writing work",
            )
        allowed = {
            "executor_blocked": {"answer_executor", "retry_executor", "adopt_edits"},
            "executor_report_recovery": {
                "answer_executor",
                "retry_executor",
                "adopt_edits",
            },
            "execution_recovery": {"answer_executor", "retry_executor", "adopt_edits"},
            "review_timeout_changed": {"fresh_review"},
            "completion_advisory": {"force", "retry_review"},
            "synthesis_retry": {"retry_synthesis"},
            "route_unavailable": {"retry_route"},
            "workspace_guard": {"recheck_workspace"},
            "feedback_recovery": {"retry_feedback"},
        }
        if decision.kind not in allowed.get(question.kind, set()):
            raise review.WorkflowError(
                "invalid_decision", "decision kind does not answer the issued question"
            )
        confirmations = {
            "answer_executor": "stack_edits",
            "retry_executor": "stack_edits",
            "adopt_edits": "completed",
            "fresh_review": "fresh_review",
            "force": "force",
        }
        if decision.confirmation != confirmations.get(decision.kind):
            raise review.WorkflowError(
                "not_confirmed", "decision lacks its required exact confirmation"
            )
        needs_digest = decision.kind in {
            "answer_executor",
            "retry_executor",
            "adopt_edits",
            "fresh_review",
        }
        if decision.workspace_sha256 != (
            question.workspace_sha256 if needs_digest else None
        ):
            raise review.WorkflowError(
                "invalid_decision", "workspace digest differs or is inapplicable"
            )
        if decision.completed_action != (
            question.action_id if decision.kind == "adopt_edits" else None
        ):
            raise review.WorkflowError(
                "invalid_decision", "completed action differs or is inapplicable"
            )
        if (
            decision.kind == "answer_executor"
            and (not decision.answer or not decision.answer.strip())
        ) or (decision.kind != "answer_executor" and decision.answer is not None):
            raise review.WorkflowError(
                "invalid_decision",
                "answer must be nonblank and applicable only to answer_executor",
            )
        if _workspace_digest() != question.workspace_sha256 and not (
            decision.kind == "recheck_workspace" and _guard_clean(journal)
        ):
            if question.kind == "completion_advisory" and decision.kind == "force":
                run = review._guard_review_path(
                    session_segment=ref.session_segment,
                    run_id=journal.review_ref.run_id,
                    create=False,
                )
                with review._workflow_lock(run):
                    step = review._derive_step(review._workflow(run), run)
                _apply_outcome(data, journal, ref, step, force=True)
                if journal.question is None:
                    journal.decisions.append(decision)
            else:
                _park(
                    data,
                    journal,
                    ref,
                    question.kind,
                    question.text,
                    outcome_sha256=question.outcome_sha256,
                    advisories=question.advisories,
                )
            return
        if decision.kind in {"retry_synthesis", "retry_review"}:
            if (
                decision.kind == "retry_review"
                and review_runs.sha256_text(targets.resolve("working-tree").content)
                != journal.review_ref.target_sha256
            ):
                _replace_review_generation(data, journal)
            else:
                journal.retry_authorization = BuildRetryAuthorization(
                    decision, journal.review_ref
                )
        elif decision.kind == "force":
            run = review._guard_review_path(
                session_segment=ref.session_segment,
                run_id=journal.review_ref.run_id,
                create=False,
            )
            with review._workflow_lock(run):
                step = review._derive_step(review._workflow(run), run)
            _apply_outcome(data, journal, ref, step, force=True)
            if journal.question:
                return
        else:
            if decision.kind == "retry_route" and not _route_matches(journal.executor):
                raise review.WorkflowError(
                    "route_unavailable", "frozen route is still unavailable"
                )
            if decision.kind in {
                "adopt_edits",
                "recheck_workspace",
            } and not _guard_clean(journal):
                raise review.WorkflowError(
                    "workspace_guard",
                    "cannot waive unknown or changed HEAD/index/branch baseline",
                )
            if (
                decision.kind == "recheck_workspace"
                and journal.action
                and journal.action.result
            ):
                receipt = json.loads(
                    _safe(
                        _root(ref)
                        / "execution-receipts"
                        / f"{journal.action.action_id}.json",
                        ref,
                    ).read_bytes()
                )
                if (
                    not journal.action.before.complete
                    or not _facts(receipt["after"]).complete
                ):
                    raise review.WorkflowError(
                        "workspace_guard",
                        "recheck cannot waive an unknown original workspace observation",
                    )
            if decision.kind == "adopt_edits" and (
                journal.action is None
                or journal.action.before is None
                or not journal.action.before.complete
            ):
                raise review.WorkflowError(
                    "workspace_guard", "adoption cannot repair missing guard history"
                )
            journal.question = None
            if decision.kind in {"retry_executor", "answer_executor"}:
                result = next(
                    (
                        r
                        for r in journal.accepted_actions
                        if r.action_id == question.action_id
                    ),
                    None,
                )
                if result is not None:
                    journal.feedback_paths.append(str(_executor_feedback(result)))
                journal.action = None
                journal.round_prompt_path = None
                journal.round_prompt_sha256 = None
                journal.round_attempt = 0
                journal.round_policy_ready = False
                journal.supplementary_answer = decision.answer
                journal.format_correction = question.kind == "executor_report_recovery"
            elif decision.kind in {"adopt_edits", "fresh_review"}:
                _replace_review_generation(data, journal)
            elif (
                decision.kind == "recheck_workspace"
                and journal.action
                and journal.action.result
            ):
                _map_result(data, journal, ref)
        if journal.retry_authorization is None:
            data["awaiting_input"] = journal.question is not None
        journal.decisions.append(decision)

    _transaction(ref, decide)
    return next_build(ref)
