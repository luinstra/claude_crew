"""Passive build records and writer protection, without workflow engine imports."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING

from multiagent.review_runs import session_segment

if TYPE_CHECKING:
    from multiagent import execution
    from multiagent import review_workflow as review
    from multiagent.build_workflow import (
        BuildDecision,
        BuildQuestion,
        BuildResult,
        BuildRetryAuthorization,
        BuildSelection,
        FrozenExecutor,
        RoundPolicy,
    )

ACTION_RE = re.compile(r"action-[0-9]{4,}")


def report_status(content: bytes) -> str:
    """Validate the exact report marker without normalizing authoritative bytes."""
    try:
        lines = content.decode("utf-8").split("\n")
    except UnicodeError:
        return "invalid_report"
    lines = [line.removesuffix("\r") for line in lines]
    final = next((line for line in reversed(lines) if line.strip()), "")
    return {
        "CREW_BUILD_STATUS: COMPLETED": "completed",
        "CREW_BUILD_STATUS: BLOCKED": "blocked",
    }.get(final, "invalid_report")


@dataclass(frozen=True, slots=True)
class BuildAction:
    action_id: str
    kind: str
    prompt_path: str
    prompt_sha256: str
    status: str = "ready"
    before: execution.WorkspaceFacts | None = None
    handle: str | None = None
    result: BuildResult | None = None


@dataclass(slots=True)
class BuildJournal:
    version: int
    request_id: str
    raw_arguments: str
    selection: BuildSelection
    executor: FrozenExecutor
    policy: RoundPolicy
    baseline: execution.WorkspaceFacts
    stage: str = "implementation"
    action_ordinal: int = 0
    round_attempt: int = 0
    round_prompt_path: str | None = None
    round_prompt_sha256: str | None = None
    action: BuildAction | None = None
    accepted_actions: list[BuildResult] = field(default_factory=list)
    applied_outcomes: list[str] = field(default_factory=list)
    decisions: list[BuildDecision] = field(default_factory=list)
    outstanding_writer: str | None = None
    question: BuildQuestion | None = None
    review_generation: int = 0
    review_ref: review.ReviewRef | None = None
    pending_review_inputs: review.PreparedLoopReview | None = None
    retry_authorization: BuildRetryAuthorization | None = None
    feedback_paths: list[str] = field(default_factory=list)
    supplementary_answer: str | None = None
    format_correction: bool = False
    round_policy_ready: bool = True
    cancellation_diagnostic: str | None = None


def validate_writer(value: object) -> str | None:
    """Validate the record shape and writer claim only; policy stays in the engine."""
    if (
        not isinstance(value, dict)
        or set(value) != {f.name for f in fields(BuildJournal)}
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise ValueError("uncertain build journal shape or version")
    action = value["action"]
    writer = value["outstanding_writer"]
    if writer is not None and (
        not isinstance(writer, str) or ACTION_RE.fullmatch(writer) is None
    ):
        raise ValueError("invalid writer action identity")
    if action is not None:
        if (
            not isinstance(action, dict)
            or set(action) != {f.name for f in fields(BuildAction)}
            or not isinstance(action["action_id"], str)
            or ACTION_RE.fullmatch(action["action_id"]) is None
            or not isinstance(action["status"], str)
            or action["status"] not in {"ready", "claimed", "settled", "recovered"}
            or not isinstance(action["kind"], str)
            or action["kind"] not in {"implementation", "revision"}
            or not isinstance(action["prompt_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", action["prompt_sha256"]) is None
            or not isinstance(action["prompt_path"], str)
            or (action["handle"] is not None and not isinstance(action["handle"], str))
            or action["status"] in {"claimed", "settled", "recovered"}
            and not isinstance(action["before"], dict)
        ):
            raise ValueError("invalid build action")
        if (action["status"] == "claimed") != (writer == action["action_id"]):
            raise ValueError("claimed writing work requires its matching fence")
    if writer is not None and (action is None or action["action_id"] != writer):
        raise ValueError("writer fence differs from outstanding claim")
    return writer


def recovery_argv(session: str, lifetime: str, action_id: str) -> tuple[str, ...]:
    return (
        str(Path(__file__).resolve().parent.parent / "crew"),
        "build-recover",
        "--session-segment",
        session_segment(session),
        "--loop-instance-id",
        lifetime,
        "--action-id",
        action_id,
        "--confirmation",
        "not_running",
    )


def writer_fence(data: Mapping[str, object]) -> str | None:
    value = data.get("bl_workflow")
    if value is None:
        return None
    uncertain = (
        "Uncertain build writer journal; retain state and evidence for inspection."
    )
    try:
        writer = validate_writer(value)
    except ValueError:
        return uncertain
    if writer is None:
        return None
    session = data.get("session_id")
    lifetime = data.get("loop_instance_id")
    if (
        not isinstance(session, str)
        or not isinstance(lifetime, str)
        or re.fullmatch(r"[A-Za-z0-9_-]+", lifetime) is None
    ):
        return uncertain
    return (
        "Outstanding writer; explicit operator confirmation of actual quiescence required: "
        + shlex.join(recovery_argv(session, lifetime, writer))
    )
