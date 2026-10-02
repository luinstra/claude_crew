"""Engine-owned planning loop over the shared loop and review transactions."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

import loop_state
from models import SCHEMA_VERSION, LOAD_OK, LOAD_MISSING, read_state_json, utc_now_iso
from state_discovery import anchor_path, is_active_value
from multiagent import channels, review_runs, review_workflow as review

logger = logging.getLogger(__name__)
T = TypeVar("T")
QUESTIONS = (
    ("scope", "What should the plan accomplish, and what is outside its scope?"),
    ("constraints", "Which constraints, existing behavior, and design decisions must it preserve?"),
    ("acceptance", "What observable checks will establish that the work is complete?"),
)
ACTION_RE = re.compile(r"^action-[0-9]{4,}$")
LIFETIME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class MeasureRef:
    session_segment: str
    loop_instance_id: str


@dataclass(frozen=True, slots=True)
class CapturedRequirements:
    question_id: str
    answers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MeasureRequest:
    raw_arguments: str
    session_id: str
    requirements: CapturedRequirements | None = None


@dataclass(frozen=True, slots=True)
class Selection:
    task: str
    panel: str | None = None
    seats: str | None = None


@dataclass(frozen=True, slots=True)
class Requirements:
    mode: str
    source: str
    content: str
    sha256: str


@dataclass(frozen=True, slots=True)
class MeasureQuestion:
    question_id: str
    kind: str
    text: str
    questions: tuple[tuple[str, str], ...] = ()
    proposed_verdict: str | None = None
    minor_only: bool = False
    advisories: tuple[str, ...] = ()
    retained_phase: str | None = None
    retained_run_id: str | None = None
    outcome_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class MeasureWorkItem:
    owner: MeasureRef
    action_id: str
    kind: str
    driver: str
    role: str | None
    model: str | None
    channel: str | None
    access: str
    prompt_path: str
    staging_path: str | None = None
    submission_path: str | None = None
    return_transport: dict[str, object] | None = None
    review_ref: review.ReviewRef | None = None
    review_item: review.WorkItem | None = None
    returned_path: str | None = None


@dataclass(frozen=True, slots=True)
class MeasureStep:
    type: str
    ref: MeasureRef | None = None
    display: str | None = None
    question: MeasureQuestion | None = None
    work_items: tuple[MeasureWorkItem, ...] = ()
    in_flight: tuple[str, ...] = ()
    outcome: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ReviewPreparation:
    request: review.ReviewRequest
    binding: review.LoopReviewBinding


@dataclass(frozen=True, slots=True)
class MeasureResult:
    ref: MeasureRef
    action_id: str
    status: str
    plan_sha256: str | None
    returned_sha256: str
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class MeasureSubmission:
    result: MeasureResult


@dataclass(frozen=True, slots=True)
class MeasureRecovery:
    ref: MeasureRef
    action_id: str
    confirmation: str


@dataclass(frozen=True, slots=True)
class MeasureDecision:
    ref: MeasureRef
    question_id: str
    kind: str
    confirmation: str | None = None
    retained_phase: str | None = None
    retained_run_id: str | None = None
    completed_action: str | None = None
    plan_sha256: str | None = None
    requirements: CapturedRequirements | None = None


@dataclass(frozen=True, slots=True)
class MeasureRetryAuthorization:
    decision: MeasureDecision
    source_ref: review.ReviewRef


@dataclass(slots=True)
class MeasureAction:
    ordinal: int
    item: MeasureWorkItem
    status: str = "ready"
    result: MeasureResult | None = None
    sealed_path: str | None = None
    canonical_path: str | None = None


@dataclass(slots=True)
class MeasureJournal:
    version: int
    request_id: str
    raw_arguments: str
    selection: Selection
    requirements: Requirements | None
    advisor_role: str
    advisor_model: str
    advisor_channel: str
    advisor_access: str
    stage: str = "planning"
    action_ordinal: int = 0
    review_generation: int = 0
    review_ref: review.ReviewRef | None = None
    pending_review_inputs: review.PreparedLoopReview | None = None
    action: MeasureAction | None = None
    accepted_actions: list[MeasureResult] = field(default_factory=list)
    applied_outcomes: list[str] = field(default_factory=list)
    question: MeasureQuestion | None = None
    prior_blocking_count: int = 0
    feedback_path: str | None = None
    legacy_confirmed: bool = False
    full_review_path: str | None = None
    retry_authorization: MeasureRetryAuthorization | None = None


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def ref_to_dict(ref: MeasureRef) -> dict[str, object]:
    return {"schema": 1, **dataclasses.asdict(ref)}


def parse_measure_ref(value: object) -> MeasureRef:
    if (not isinstance(value, dict) or set(value) != {"schema", "session_segment", "loop_instance_id"}
            or type(value.get("schema")) is not int or value["schema"] != 1
            or not isinstance(value.get("session_segment"), str)
            or not review.SESSION_RE.fullmatch(value["session_segment"])
            or not isinstance(value.get("loop_instance_id"), str)
            or not LIFETIME_RE.fullmatch(value["loop_instance_id"])):
        raise review.WorkflowError("invalid_ref", "MeasureRef must contain the exact schema-1 identity")
    return MeasureRef(value["session_segment"], value["loop_instance_id"])


def result_to_dict(result: MeasureResult) -> dict[str, object]:
    return {**dataclasses.asdict(result), "ref": ref_to_dict(result.ref)}


def parse_measure_result(value: object) -> MeasureResult:
    fields = {f.name for f in dataclasses.fields(MeasureResult)}
    if not isinstance(value, dict) or set(value) != fields:
        raise review.WorkflowError("invalid_submission", "measure result has missing or unknown fields")
    result = MeasureResult(**{**value, "ref": parse_measure_ref(value["ref"])})
    if (not isinstance(result.action_id, str) or not ACTION_RE.fullmatch(result.action_id)
            or not isinstance(result.status, str) or result.status not in {status.value for status in review.HostStatus}
            or not isinstance(result.returned_sha256, str) or not review.SHA_RE.fullmatch(result.returned_sha256)
            or (result.plan_sha256 is not None and (not isinstance(result.plan_sha256, str)
                or not review.SHA_RE.fullmatch(result.plan_sha256)))
            or (result.status == "ok" and result.plan_sha256 is None)
            or (result.diagnostic is not None and not isinstance(result.diagnostic, str))):
        raise review.WorkflowError("invalid_submission", "measure result fields are invalid")
    return result


def work_to_dict(item: MeasureWorkItem) -> dict[str, object]:
    value = dataclasses.asdict(item)
    value["owner"] = ref_to_dict(item.owner)
    value["review_ref"] = review.review_ref_to_dict(item.review_ref) if item.review_ref else None
    value["review_item"] = review.work_item_to_dict(item.review_item) if item.review_item else None
    return value


def issued_commands(item: MeasureWorkItem) -> dict[str, str]:
    from multiagent.workflow_transport import render_argv
    executable = str(Path(__file__).resolve().parents[2] / "crew")
    if item.review_ref:
        ref = item.review_ref
        flags = ("--session-segment", ref.session_segment, "--run-id", ref.run_id,
                 "--attempt-id", ref.attempt_id, "--target-sha256", ref.target_sha256,
                 "--action-id", item.action_id)
        if item.driver == "external":
            return {"execute": render_argv((executable, "review-execute", *flags)),
                    "recover": render_argv((executable, "review-recover", *flags,
                                            "--diagnostic-code", "external_process_lost"))}
        recovery_code = ("parent_synthesis_lost" if item.kind == "synthesis" else
                         "parent_formatter_lost" if item.kind == "formatter" and item.driver == "parent" else
                         "formatter_task_lost" if item.kind == "formatter" else "native_task_lost")
        commands = {"claim": render_argv((executable, "review-claim", *flags)),
                "capture": render_argv((executable, "review-capture", *flags)),
                "recover": render_argv((executable, "review-recover", *flags, "--diagnostic-code", recovery_code))}
        if item.driver == "native" and item.role == "crew:reviewer":
            commands.update(native_bind=render_argv((executable, "review-native-bind", *flags)),
                            native_capture=render_argv((executable, "review-native-capture", *flags)))
        return commands
    flags = ("--session-segment", item.owner.session_segment, "--loop-instance-id", item.owner.loop_instance_id,
             "--action-id", item.action_id)
    return {"claim": render_argv((executable, "measure-twice-claim", *flags)),
            "capture": render_argv((executable, "measure-twice-capture", *flags)),
            "native_bind": render_argv((executable, "measure-twice-native-bind", *flags)),
            "native_capture": render_argv((executable, "measure-twice-native-capture", *flags)),
            "recover": render_argv((executable, "measure-twice-recover", *flags))}


def step_to_dict(step: MeasureStep) -> dict[str, object]:
    value: dict[str, object] = {"schema": 1, "type": str(step.type)}
    if step.ref:
        value["ref"] = ref_to_dict(step.ref)
    if step.display:
        value["display"] = step.display
    if step.question:
        value["question"] = dataclasses.asdict(step.question)
    if step.work_items:
        value["work_items"] = [{**work_to_dict(item), "commands": issued_commands(item)} for item in step.work_items]
    if step.in_flight:
        value["in_flight"] = list(step.in_flight)
    if step.outcome:
        value["outcome"] = step.outcome
    return value


def journal_to_dict(journal: MeasureJournal) -> dict[str, object]:
    value = dataclasses.asdict(journal)
    if journal.review_ref:
        value["review_ref"] = review.review_ref_to_dict(journal.review_ref)
    if journal.pending_review_inputs:
        value["pending_review_inputs"] = journal.pending_review_inputs.to_dict()
    if journal.retry_authorization:
        authorization = journal.retry_authorization
        value["retry_authorization"] = {
            "decision": {**dataclasses.asdict(authorization.decision), "ref": ref_to_dict(authorization.decision.ref)},
            "source_ref": review.review_ref_to_dict(authorization.source_ref)}
    value["accepted_actions"] = [result_to_dict(result) for result in journal.accepted_actions]
    if journal.action:
        value["action"] = {**dataclasses.asdict(journal.action), "item": work_to_dict(journal.action.item),
            "result": result_to_dict(journal.action.result) if journal.action.result else None}
    return value


def _exact(value: object, cls: type[T]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {f.name for f in dataclasses.fields(cls)}:
        raise review.WorkflowError("invalid_journal", f"{cls.__name__} has missing or unknown fields")
    return value


def _question(value: object) -> MeasureQuestion | None:
    if value is None:
        return None
    fields = _exact(value, MeasureQuestion)
    if (not isinstance(fields["question_id"], str) or not review.SHA_RE.fullmatch(fields["question_id"])
            or not isinstance(fields["kind"], str) or not isinstance(fields["text"], str)
            or not isinstance(fields["questions"], (list, tuple))
            or any(not isinstance(q, (list, tuple)) or len(q) != 2
                   or any(not isinstance(part, str) for part in q) for q in fields["questions"])
            or not isinstance(fields["advisories"], (list, tuple))
            or any(not isinstance(part, str) for part in fields["advisories"])
            or type(fields["minor_only"]) is not bool
            or any(fields[key] is not None and not isinstance(fields[key], str)
                   for key in ("proposed_verdict", "retained_phase", "retained_run_id", "outcome_sha256"))
            or (fields["outcome_sha256"] is not None and not review.SHA_RE.fullmatch(fields["outcome_sha256"]))):
        raise review.WorkflowError("invalid_journal", "measure question fields are invalid")
    return MeasureQuestion(**{**fields, "questions": tuple(tuple(q) for q in fields["questions"]),
                              "advisories": tuple(fields["advisories"])})


def parse_measure_work(value: object) -> MeasureWorkItem:
    fields = _exact(value, MeasureWorkItem)
    if (any(not isinstance(fields[key], str) for key in ("action_id", "kind", "driver", "access", "prompt_path"))
            or any(fields[key] is not None and not isinstance(fields[key], str)
                   for key in ("role", "model", "channel", "staging_path", "submission_path", "returned_path"))
            or (fields["return_transport"] is not None and not isinstance(fields["return_transport"], dict))):
        raise review.WorkflowError("invalid_journal", "measure work item fields are invalid")
    return MeasureWorkItem(**{**fields, "owner": parse_measure_ref(fields["owner"]),
        "review_ref": review.parse_review_ref(fields["review_ref"]) if fields["review_ref"] is not None else None,
        "review_item": review.parse_work_item(fields["review_item"]) if fields["review_item"] is not None else None})


def journal_from_dict(value: object) -> MeasureJournal:
    # Existing schema-4 journals predate the optional raw-panel feedback path.
    if isinstance(value, dict) and "full_review_path" not in value:
        value = {**value, "full_review_path": None}
    if isinstance(value, dict) and "retry_authorization" not in value:
        value = {**value, "retry_authorization": None}
    fields = _exact(value, MeasureJournal)
    if fields["version"] != 1 or type(fields["version"]) is not int:
        raise review.WorkflowError("invalid_journal", "unsupported measure journal version")
    if (any(not isinstance(fields[key], str) for key in
            ("request_id", "raw_arguments", "advisor_role", "advisor_model", "advisor_channel", "advisor_access", "stage"))
            or any(not isinstance(fields[key], list) for key in ("accepted_actions", "applied_outcomes"))
            or any(not isinstance(digest, str) for digest in fields["applied_outcomes"])
            or type(fields["prior_blocking_count"]) is not int or fields["prior_blocking_count"] < 0
            or type(fields["legacy_confirmed"]) is not bool
            or any(fields[key] is not None and not isinstance(fields[key], str)
                   for key in ("feedback_path", "full_review_path"))):
        raise review.WorkflowError("invalid_journal", "measure journal field types are invalid")
    action = None
    if fields["action"] is not None:
        a = _exact(fields["action"], MeasureAction)
        item = parse_measure_work(a["item"])
        if (not isinstance(a["status"], str) or any(a[key] is not None and not isinstance(a[key], str)
                for key in ("sealed_path", "canonical_path"))):
            raise review.WorkflowError("invalid_journal", "measure action fields are invalid")
        action = MeasureAction(**{**a, "item": item, "result": parse_measure_result(a["result"]) if a["result"] else None})
    selected = _exact(fields["selection"], Selection)
    if (not isinstance(selected["task"], str) or any(selected[key] is not None
            and not isinstance(selected[key], str) for key in ("panel", "seats"))):
        raise review.WorkflowError("invalid_journal", "measure selection fields are invalid")
    requirements = None
    if fields["requirements"] is not None:
        saved = _exact(fields["requirements"], Requirements)
        if (any(not isinstance(part, str) for part in saved.values())
                or saved["mode"] not in {"document", "answers", "legacy_task"}
                or not review.SHA_RE.fullmatch(saved["sha256"])):
            raise review.WorkflowError("invalid_journal", "requirements snapshot fields are invalid")
        requirements = Requirements(**saved)
    authorization = None
    if fields["retry_authorization"] is not None:
        saved = _exact(fields["retry_authorization"], MeasureRetryAuthorization)
        decision = _exact(saved["decision"], MeasureDecision)
        if (not isinstance(decision["kind"], str) or decision["kind"] not in {"retry_synthesis", "retry_review"}
                or not isinstance(decision["question_id"], str) or not review.SHA_RE.fullmatch(decision["question_id"])
                or any(decision[key] is not None for key in
                       ("confirmation", "retained_phase", "retained_run_id", "completed_action", "plan_sha256", "requirements"))):
            raise review.WorkflowError("invalid_journal", "retry authorization must contain an exact retry decision")
        authorization = MeasureRetryAuthorization(
            MeasureDecision(**{**decision, "ref": parse_measure_ref(decision["ref"])}),
            review.parse_review_ref(saved["source_ref"]))
    journal = MeasureJournal(**{**fields,
        "selection": Selection(**selected),
        "requirements": requirements,
        "review_ref": review.parse_review_ref(fields["review_ref"]) if fields["review_ref"] else None,
        "pending_review_inputs": review.PreparedLoopReview.from_dict(fields["pending_review_inputs"]) if fields["pending_review_inputs"] else None,
        "action": action, "accepted_actions": [parse_measure_result(r) for r in fields["accepted_actions"]],
        "question": _question(fields["question"]), "retry_authorization": authorization})
    if (journal.stage not in {"planning", "revision", "replanning", "prepare_review", "reviewing", "done", "legacy"}
            or type(journal.action_ordinal) is not int or journal.action_ordinal < 0
            or type(journal.review_generation) is not int or journal.review_generation < 0
            or not isinstance(journal.raw_arguments, str)
            or not review.SHA_RE.fullmatch(journal.request_id)
            or any(not review.SHA_RE.fullmatch(d) for d in journal.applied_outcomes)):
        raise review.WorkflowError("invalid_journal", "measure journal progress is invalid")
    if journal.requirements and sha256(journal.requirements.content.encode("utf-8")) != journal.requirements.sha256:
        raise review.WorkflowError("invalid_journal", "requirements snapshot digest differs")
    return journal


def parse_arguments(raw: str) -> Selection:
    if not isinstance(raw, str) or "\x00" in raw:
        raise review.WorkflowError("invalid_request", "raw arguments must be text without NUL")
    rest = raw
    values: dict[str, str] = {}
    while True:
        match = re.match(r"\s*(--panel|--seats)(?=\s|$)", rest)
        if not match:
            break
        name = match[1][2:]
        tail = rest[match.end():]
        value_match = re.match(r'''\s+("[^"]*"|'[^']*'|[^\s]+)''', tail)
        if value_match is None:
            raise review.WorkflowError("invalid_options", f"--{name} requires a value")
        try:
            tokens = shlex.split(value_match[1])
        except ValueError as exc:
            raise review.WorkflowError("invalid_options", str(exc)) from exc
        if len(tokens) != 1 or not tokens[0] or tokens[0].startswith("--") or name in values:
            raise review.WorkflowError("invalid_options", f"duplicate or missing --{name}")
        values[name] = tokens[0]
        rest = tail[value_match.end():]
    # Only the separator after leading flags is removed; task bytes are preserved.
    task = rest.lstrip() if values else raw
    if re.match(r"\s*--", task):
        raise review.WorkflowError("invalid_options", "only leading --panel and --seats are supported")
    return Selection(task, values.get("panel"), values.get("seats"))


def _document(selection: Selection) -> Path | None:
    try:
        tokens = shlex.split(selection.task)
    except ValueError:
        return None
    if len(tokens) > 1 and all(token.endswith(".md") for token in tokens):
        raise review.WorkflowError("conflicting_requirements_sources", "multiple explicit documents")
    return Path(anchor_path(tokens[0])) if len(tokens) == 1 and tokens[0].endswith(".md") else None


def interview_question(raw: str) -> MeasureQuestion:
    identity = sha256(_canonical({"raw_sha256": sha256(raw.encode("utf-8")), "mode": "answers", "questions": QUESTIONS}))
    return MeasureQuestion(identity, "requirements", "Answer each ordered question before planning.", QUESTIONS)


def _requirements(request: MeasureRequest, selection: Selection, *, legacy: bool = False) -> Requirements | None:
    document = _document(selection)
    if document is not None:
        if request.requirements is not None:
            raise review.WorkflowError("conflicting_requirements_sources", "document and captured answers compete")
        try:
            content = document.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise review.WorkflowError("requirements_source_unreadable", f"cannot read requirements source {document}: {exc}") from exc
        if not content.strip():
            raise review.WorkflowError("requirements_source_unreadable", f"requirements source is empty: {document}")
        return Requirements("document", str(document), content, sha256(content.encode("utf-8")))
    captured = request.requirements
    if captured is None:
        if legacy and selection.task.strip():
            return Requirements("legacy_task", "preserved task", selection.task, sha256(selection.task.encode("utf-8")))
        return None
    question = interview_question(request.raw_arguments)
    if (not isinstance(captured.answers, tuple) or captured.question_id != question.question_id or len(captured.answers) != len(QUESTIONS)
            or any(not isinstance(answer, str) or not answer.strip() for answer in captured.answers)):
        raise review.WorkflowError("conflicting_requirements_sources", "answers do not match the ordered request-bound interview")
    content = json.dumps({"task": selection.task, "answers": dict(zip((q[0] for q in QUESTIONS), captured.answers))}, ensure_ascii=False)
    return Requirements("answers", "captured interview", content, sha256(content.encode("utf-8")))


def request_from_dict(value: object, session_id: str) -> MeasureRequest:
    if (not isinstance(value, dict) or set(value) != {"schema", "raw_arguments", "requirements"}
            or type(value.get("schema")) is not int or value["schema"] != 1
            or not isinstance(value["raw_arguments"], str)):
        raise review.WorkflowError("invalid_request", "request file must be an exact schema-1 JSON envelope")
    captured = None
    if value["requirements"] is not None:
        fields = value["requirements"]
        if (not isinstance(fields, dict) or set(fields) != {"question_id", "answers"}
                or not isinstance(fields["question_id"], str) or not review.SHA_RE.fullmatch(fields["question_id"])
                or not isinstance(fields["answers"], list) or len(fields["answers"]) != len(QUESTIONS)
                or any(not isinstance(answer, str) or not answer.strip() for answer in fields["answers"])):
            raise review.WorkflowError("invalid_request", "requirements must contain a question digest and three ordered nonblank string answers")
        captured = CapturedRequirements(fields["question_id"], tuple(fields["answers"]))
    return MeasureRequest(value["raw_arguments"], session_id, captured)


def _request_identity(raw: str, selection: Selection, requirements: Requirements | None) -> str:
    return sha256(_canonical({"raw_sha256": sha256(raw.encode("utf-8")),
        "panel": selection.panel, "seats": selection.seats,
        "source_mode": requirements.mode if requirements else None,
        "source_sha256": requirements.sha256 if requirements else None}))


def _advisor() -> review.HostRoles:
    roles = review.native_roles(review.RoutePolicy.resolve(channels.current_host(), ()))
    if roles is None or roles.advisor_role is None or roles.advisor_model is None:
        raise review.WorkflowError("unsupported_planning_host", "this host has no admitted production advisor route")
    role = Path(__file__).resolve().parents[2] / "agents" / (roles.advisor_role.split(":")[-1] + ".md")
    if not role.is_file():
        raise review.WorkflowError("unsupported_planning_host", "the issued advisor role is unavailable")
    return roles


def _verify_advisor_route(journal: MeasureJournal) -> None:
    roles = _advisor()
    if (roles.channel, roles.advisor_role, roles.advisor_model, roles.advisor_access) != (
            journal.advisor_channel, journal.advisor_role, journal.advisor_model, journal.advisor_access):
        raise review.WorkflowError("unsupported_planning_host", "this host cannot perform the frozen advisor route")


def _ref(data: dict[str, object]) -> MeasureRef:
    if not data:
        raise review.WorkflowError("missing_state", "the measure-twice state is missing or was swept")
    return parse_measure_ref({"schema": 1, "session_segment": review_runs.session_segment(data["session_id"]),
                              "loop_instance_id": data["loop_instance_id"]})


def _check_owner(data: dict[str, object], ref: MeasureRef, *, active: bool = True) -> None:
    if _ref(data) != ref or (active and not is_active_value(data.get("active"))):
        raise review.WorkflowError("stale_owner", "measure lifetime is inactive or replaced", "conflict")


def _transaction(ref: MeasureRef, operation: Callable[[dict[str, object], MeasureJournal], T], *, active: bool = True) -> T:
    ref = parse_measure_ref(ref_to_dict(ref))
    _guard_storage(ref.session_segment)
    answer: list[T] = []
    def mutate(data: dict[str, object]) -> dict[str, object]:
        _check_owner(data, ref, active=active)
        journal = journal_from_dict(data.get("mt_workflow"))
        accepted_ids = [result.action_id for result in journal.accepted_actions]
        if (any(result.ref != ref for result in journal.accepted_actions)
                or len(accepted_ids) != len(set(accepted_ids))
                or len(journal.applied_outcomes) != len(set(journal.applied_outcomes))):
            raise review.WorkflowError("invalid_journal", "measure receipts have invalid owners or duplicates")
        if journal.retry_authorization and (journal.retry_authorization.decision.ref != ref
                or journal.retry_authorization.source_ref.session_segment != ref.session_segment):
            raise review.WorkflowError("invalid_journal", "retry authorization belongs to another owner")
        if journal.action:
            action = journal.action
            root = _namespace(ref, journal)
            if (action.item.owner != ref or action.item.kind != "advisor"
                    or action.status not in {"ready", "claimed", "promotion_pending", "settled"}
                    or type(action.ordinal) is not int or action.ordinal < 1
                    or action.item.action_id != f"action-{action.ordinal:04d}"
                    or action.ordinal != journal.action_ordinal
                    or action.item.staging_path != str(root / "staging" / action.item.action_id / "plan.md")
                    or action.canonical_path != str(root / f"plan-{action.ordinal}.md")
                    or action.item.submission_path != str(root / "submissions" / f"{action.item.action_id}.json")
                    or action.item.returned_path != str(root / "ingress" / f"{action.item.action_id}.txt")):
                raise review.WorkflowError("invalid_journal", "advisor action identity or owned paths differ")
        answer.append(operation(data, journal))
        data["mt_workflow"] = journal_to_dict(journal)
        data["schema"] = SCHEMA_VERSION
        return data
    loop_state.mutate(loop_state.resolve("mt", ref.session_segment), mutate)
    return answer[0]


def _new_journal(request: MeasureRequest, selection: Selection, requirements: Requirements | None) -> MeasureJournal:
    roles = _advisor()
    return MeasureJournal(1, _request_identity(request.raw_arguments, selection, requirements),
        request.raw_arguments, selection, requirements, roles.advisor_role, roles.advisor_model,
        roles.channel, roles.advisor_access)


def _terminal(data: dict[str, object], ref: MeasureRef | None) -> MeasureStep:
    overrides = data.get("last_verdict_overrides") or []
    summary = (f"Completion recorded over advisories: {', '.join(overrides)}" if overrides and data.get("exit_kind") == "approved" else
               "Plan approved" if data.get("exit_kind") == "approved" else str(data.get("reason") or data.get("exit_kind") or "inactive"))
    artifacts = None
    if ref and data.get("mt_workflow") is not None:
        artifacts = str(_namespace(ref, journal_from_dict(data["mt_workflow"])))
    return MeasureStep(review.StepType.TERMINAL, ref, summary,
        outcome={"status": data.get("exit_kind") or "inactive", "plan_file": data.get("plan_file") or None,
                 "verdict": data.get("last_verdict") or None, "overrides": overrides, "lifetime_plan_artifacts": artifacts})


def start_measure_twice(request: MeasureRequest) -> MeasureStep:
    session = review._session(review.ReviewRequest("", session_id=request.session_id))
    selection = parse_arguments(request.raw_arguments)
    _guard_storage(session)
    path = loop_state.resolve("mt", session)
    existing, status = read_state_json(path)
    if status not in (LOAD_OK, LOAD_MISSING):
        raise loop_state.LoopStateError(status, f"refusing to touch {path}: {status}")
    previous = None
    if existing is not None and not is_active_value(existing.get("active")):
        sticky = existing.get("force_exit") or existing.get("exit_kind") == "force_exit"
        journal = journal_from_dict(existing["mt_workflow"]) if existing.get("mt_workflow") is not None else None
        identical = request.raw_arguments == (journal.raw_arguments if journal else str(existing.get("task") or existing.get("task_description") or ""))
        if identical and journal and not sticky:
            requirements = (_requirements(request, selection) if request.requirements is not None or _document(selection)
                            else journal.requirements)
            identical = journal.request_id == _request_identity(request.raw_arguments, selection, requirements)
        if sticky or identical:
            result: list[MeasureStep] = []
            def replay(data: dict[str, object]) -> dict[str, object]:
                if data != existing:
                    raise review.WorkflowError("active_request_conflict", "terminal lifetime changed during replay", "conflict")
                result.append(_terminal(data, _ref(data) if data.get("loop_instance_id") else None))
                return data
            loop_state.mutate(path, replay)
            return result[0]
        previous, existing = existing, None
    if existing is not None and existing.get("mt_workflow") is None:
        def migrate(data: dict[str, object]) -> dict[str, object]:
            if (data.get("loop_instance_id"), data.get("session_id"), data.get("active")) != (
                    existing.get("loop_instance_id"), existing.get("session_id"), existing.get("active")):
                raise review.WorkflowError("active_request_conflict", "legacy lifetime changed during migration", "conflict")
            if data.get("mt_workflow") is not None:
                return data
            if data.get("loop", "") not in ("mt", ""):
                raise review.WorkflowError("invalid_state", "cannot migrate another loop")
            if not data.get("loop_instance_id"):
                data["loop_instance_id"] = str(uuid.uuid4())
            data["session_id"] = session
            raw = str(data.get("task") or data.get("task_description") or "")
            selected = parse_arguments(raw)
            if data.get("phase") == "done":
                journal = MeasureJournal(1, _request_identity(raw, selected, None), raw, selected, None, "", "", "", "")
            else:
                journal = _new_journal(MeasureRequest(raw, session), selected, None)
            journal.stage = "legacy"
            if data.get("phase") == "done":
                journal.stage = "done"
            ref = _ref(data)
            journal.question = None if journal.stage == "done" else _bound_question(ref, "legacy_work_not_running",
                "Confirm old advisor/reviewer work has finished or been stopped; file existence is insufficient.",
                retained_phase=str(data.get("phase") or "drafting"), retained_run_id=str(data.get("run_id") or ""))
            data.update(mt_workflow=journal_to_dict(journal), schema=SCHEMA_VERSION, awaiting_input=True)
            return data
        data = loop_state.mutate(path, migrate)
        return next_measure_twice(_ref(data))
    if existing is not None:
        journal = journal_from_dict(existing["mt_workflow"])
        if request.raw_arguments != journal.raw_arguments or selection != journal.selection:
            raise review.WorkflowError("active_request_conflict", "different immutable request cannot replace an active loop", "conflict")
        if journal.stage == "legacy" or journal.legacy_confirmed:
            if request.requirements is not None:
                raise review.WorkflowError("active_request_conflict", "legacy requirements are answered through the bound decision", "conflict")
            return next_measure_twice(_ref(existing))
        requirements = _requirements(request, selection)
        if journal.request_id != _request_identity(request.raw_arguments, selection, requirements):
            raise review.WorkflowError("active_request_conflict", "different immutable request cannot replace an active loop", "conflict")
        return next_measure_twice(_ref(existing))
    requirements = _requirements(request, selection)
    _advisor()
    if requirements is None:
        return MeasureStep(review.StepType.NEEDS_INPUT, question=interview_question(request.raw_arguments))
    journal = _new_journal(request, selection, requirements)
    data = loop_state.initialize(request.raw_arguments, session, journal=journal_to_dict(journal), previous=previous)
    return next_measure_twice(_ref(data))


def _bound_question(ref: MeasureRef, kind: str, text: str, **kwargs: object) -> MeasureQuestion:
    identity = sha256(_canonical({"owner": ref_to_dict(ref), "kind": kind, "text": text, **kwargs}))
    return MeasureQuestion(identity, kind, text, **kwargs)


def _park(data: dict[str, object], journal: MeasureJournal, question: MeasureQuestion, *,
          replaces_question_id: str | None = None) -> None:
    identity = {
        "question_id": question.question_id, "action_ordinal": journal.action_ordinal,
        "review_generation": journal.review_generation,
        "review_ref": review.review_ref_to_dict(journal.review_ref) if journal.review_ref else None}
    if replaces_question_id is not None:
        identity["replaces_question_id"] = replaces_question_id
    journal.question = dataclasses.replace(question, question_id=sha256(_canonical(identity)))
    data["awaiting_input"] = True


def _namespace(ref: MeasureRef, journal: MeasureJournal) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", journal.selection.task.lower()).strip("-")[:50] or "plan"
    return loop_state.crew_base().resolve() / ".crew" / "plans" / f"{slug}-{ref.loop_instance_id}"


def _safe_path(path: Path, root: Path) -> Path:
    absolute = Path(path.absolute())
    base = Path(root.absolute())
    if not absolute.is_relative_to(base):
        raise review.WorkflowError("unsafe_plan_path", "issued path escapes its lifetime namespace")
    for parent in (absolute, *absolute.parents):
        if parent.is_symlink():
            raise review.WorkflowError("unsafe_plan_path", f"symlink in issued path: {parent}")
    return absolute


def _guard_storage(session: str) -> None:
    root = loop_state.crew_base().resolve()
    path = loop_state.resolve("mt", session)
    for candidate in (root / ".crew", root / ".crew" / path.name, root / ".crew" / (path.name + ".lock")):
        _safe_path(candidate, root)


def _write_once(path: Path, content: bytes, root: Path) -> None:
    path = _safe_path(path, root)
    if path.exists():
        if path.read_bytes() != content:
            raise review.WorkflowError("conflict", f"existing owned artifact differs: {path}", "conflict")
        return
    review_runs._atomic_write_text(path, content.decode("utf-8"))


def _issue_advisor(data: dict[str, object], journal: MeasureJournal, ref: MeasureRef) -> MeasureAction:
    journal.action_ordinal += 1
    action_id = f"action-{journal.action_ordinal:04d}"
    root = _namespace(ref, journal)
    staging = root / "staging" / action_id / "plan.md"
    _safe_path(staging, root).parent.mkdir(parents=True, exist_ok=True)
    prompt_path = root / "prompts" / f"{action_id}.txt"
    requirements_path = root / "requirements.md"
    _write_once(requirements_path, journal.requirements.content.encode("utf-8"), root)
    prior = str(data.get("plan_file") or "")
    feedback = journal.feedback_path or ""
    prompt = (f"Workflow-issued {journal.stage} task. Read frozen requirements at {requirements_path}.\n"
        f"Original task (data):\n{journal.selection.task}\n"
        f"Write ONLY the new plan at {staging}. Return a completion report after the full write.\n"
        "Do not interview, execute implementation, choose another output, or overwrite canonical plans. "
        "This is a fresh advisor action with no executor continuation.\n")
    if prior:
        prompt += f"Preceding canonical plan is READ ONLY: {prior}.\n"
    if feedback:
        prompt += f"Read verified feedback: {feedback}.\n"
    if journal.full_review_path:
        prompt += f"Also read every raw/unparsed review in {journal.full_review_path}.\n"
    if journal.stage == "revision":
        prompt += "Diagnose shared structural causes once. Verify singleton BLOCKING claims in source, fix the cause, and recheck every affected finding. Do not defer blockers to obtain approval.\n"
    if journal.stage == "replanning":
        prompt += "Produce a fresh plan using the requirements and rejection rationale.\n"
    _write_once(prompt_path, prompt.encode("utf-8"), root)
    returned = root / "ingress" / f"{action_id}.txt"
    fallback = root / "host-return" / f"{action_id}.txt"
    # The advisor returns a completion report, not its full plan. Parent Write
    # transports that report without adding a bookkeeping agent.
    transport = {"primary": {"kind": "host_write", "ingress_path": str(returned)},
                 "fallback": {"kind": "host_write", "ingress_path": str(fallback)}}
    item = MeasureWorkItem(ref, action_id, "advisor", "native", journal.advisor_role, journal.advisor_model,
        journal.advisor_channel, journal.advisor_access, str(prompt_path), str(staging),
        str(root / "submissions" / f"{action_id}.json"), transport, returned_path=str(returned))
    return MeasureAction(journal.action_ordinal, item, canonical_path=str(root / f"plan-{journal.action_ordinal}.md"))


def _promote(data: dict[str, object], journal: MeasureJournal, ref: MeasureRef) -> None:
    action = journal.action
    if action is None or action.status != "promotion_pending" or action.result is None:
        raise review.WorkflowError("invalid_action", "no sealed candidate to promote")
    root = _namespace(ref, journal)
    sealed = _safe_path(Path(action.sealed_path), root)
    content = sealed.read_bytes()
    if sha256(content) != action.result.plan_sha256:
        raise review.WorkflowError("conflict", "sealed plan digest differs", "conflict")
    _write_once(Path(action.canonical_path), content, root)
    action.status = "settled"
    journal.accepted_actions.append(action.result)
    data["plan_file"] = action.canonical_path
    data["awaiting_input"] = False
    journal.stage = "prepare_review"
    logger.debug("Promoted advisor %s for lifetime %s", action.item.action_id, ref.loop_instance_id)


def _observe_blocking_count(data: dict[str, object], journal: MeasureJournal, ref: MeasureRef) -> None:
    try:
        synthesis = Path(journal.feedback_path).read_text(encoding="utf-8") if journal.feedback_path else ""
    except (OSError, UnicodeDecodeError) as exc:
        _park(data, journal, _bound_question(ref, "feedback_recovery",
            f"Retained review feedback is unreadable: {exc}. Restore access to its original bytes and resume, or cancel."))
        return
    count_match = re.search(r"^BLOCKING_CAUSES:\s*(\d+)\s*$", synthesis, re.MULTILINE)
    if count_match is None:
        return  # Unknown does not erase the last observed count.
    blocking = int(count_match[1])
    if blocking > journal.prior_blocking_count and journal.prior_blocking_count > 0:
        _park(data, journal, _bound_question(ref, "rising_findings",
            f"Distinct blocking causes increased from {journal.prior_blocking_count} to {blocking}. Continue structural revision or cancel?"))
    journal.prior_blocking_count = blocking


def _apply_outcome(data: dict[str, object], journal: MeasureJournal, ref: MeasureRef,
                   step: review.ReviewStep, *, force: bool = False) -> None:
    evidence = review._read_loop_review_evidence_under_owner_lock(step.ref)
    digest = evidence.source.accepted_outcome_sha256
    if digest in journal.applied_outcomes:
        return
    verdict = "FAILED" if not evidence.usable_seats else step.outcome["judgment"]
    question = journal.question
    if force and question is None:
        raise review.WorkflowError("stale_question", "force requires an outstanding completion question")
    authorization = loop_state.CompletionAuthorization(question.text, question.advisories) if force else None
    try:
        loop_state.apply_verdict(data, evidence, verdict, minor_only=bool(step.outcome["minor_only"]),
                                 force=force, force_authorization=authorization)
    except loop_state._Advisory as exc:
        _park(data, journal, _bound_question(ref, "completion_advisory", exc.diagnostic,
            proposed_verdict=verdict, minor_only=bool(step.outcome["minor_only"]),
            advisories=exc.conditions, outcome_sha256=digest),
            replaces_question_id=question.question_id if force else None)
        return
    journal.question = None
    journal.applied_outcomes.append(digest)
    journal.feedback_path = step.outcome.get("synthesis_path") or step.outcome.get("full_path")
    journal.full_review_path = step.outcome.get("full_path")
    journal.review_ref = None
    journal.pending_review_inputs = None
    journal.retry_authorization = None
    if data.get("phase") == "done":
        journal.stage = "done"
    elif verdict == "FAILED":
        journal.stage = "prepare_review"
    else:
        journal.stage = "replanning" if verdict == "REJECT" else "revision"
        journal.action = None
        _observe_blocking_count(data, journal, ref)


def _next_locked(data: dict[str, object], journal: MeasureJournal, ref: MeasureRef) -> MeasureStep | ReviewPreparation | None:
    if not is_active_value(data.get("active")):
        return _terminal(data, ref)
    if data.get("phase") == "done":
        loop_state.deactivate(data)
        journal.stage = "done"
        return _terminal(data, ref)
    bound = loop_state.bound_reason(data)
    if bound:
        data.update(active=False, exit_kind="force_exit", force_exit=True, reason=bound, completed_at=utc_now_iso())
        return _terminal(data, ref)
    if journal.question:
        if journal.question.kind == "feedback_recovery":
            journal.question = None
            data["awaiting_input"] = False
            _observe_blocking_count(data, journal, ref)
            if journal.question is None:
                return None
        authorization = journal.retry_authorization
        if authorization and authorization.decision.question_id == journal.question.question_id:
            request = review.RetryRequest(authorization.source_ref,
                () if authorization.decision.kind == "retry_synthesis" else None)
            step = review._retry_loop_review_under_owner_lock(request)
            journal.review_ref = step.ref
            journal.question = None
            data["awaiting_input"] = False
            return None
        data["awaiting_input"] = True
        return MeasureStep(review.StepType.NEEDS_INPUT, ref, question=journal.question)
    if journal.action and journal.action.status == "promotion_pending":
        _promote(data, journal, ref)
        return None
    if journal.stage in {"planning", "revision", "replanning"}:
        if journal.action is None:
            _verify_advisor_route(journal)
            journal.action = _issue_advisor(data, journal, ref)
        action = journal.action
        if action.status == "ready":
            _verify_advisor_route(journal)
            return MeasureStep(review.StepType.WORK_BATCH, ref, work_items=(action.item,))
        if action.status == "claimed":
            return MeasureStep(review.StepType.WAITING, ref, in_flight=(action.item.action_id,))
        raise review.WorkflowError("invalid_journal", "advisor stage has no outstanding action")
    if journal.stage == "prepare_review":
        if journal.pending_review_inputs is None:
            binding = review.LoopReviewBinding(ref.session_segment, "mt", ref.loop_instance_id, journal.review_generation + 1)
            return ReviewPreparation(review.ReviewRequest(str(data["plan_file"]),
                panel=journal.selection.panel, seats=journal.selection.seats, session_id=ref.session_segment), binding)
        prepared = journal.pending_review_inputs
        step = review._start_loop_review_under_owner_lock(prepared)
        journal.review_ref = step.ref
        journal.retry_authorization = None
        record = prepared.record
        evidence = loop_state.ReviewEvidence(step.ref.run_id, step.ref.target_sha256, record["target_spec"], "",
            tuple(record["seat_signatures"]), (), loop_state.BindingStatus.MATCH,
            loop_state.LoopReviewSource(review.parse_loop_binding(record["workflow_identity"]["loop_binding"]),
                step.ref, None))
        loop_state.bind_review(data, evidence)
        journal.stage = "reviewing"
        return None
    if journal.stage == "reviewing":
        # The outer transaction already owns the loop lock; do not reacquire it.
        run = review._guard_review_path(session_segment=ref.session_segment, run_id=journal.review_ref.run_id, create=False)
        with review._workflow_lock(run):
            wf = review._workflow(run)
            binding = review.parse_loop_binding(wf["workflow_identity"].get("loop_binding"))
            review._owner_state(binding, journal.review_ref, wf)
            review._verify_host(wf)
            step = review._advance_locked(wf, run)
        journal.review_ref = step.ref
        if step.type == review.StepType.TERMINAL:
            if step.outcome["status"] == "synthesis_failed":
                _park(data, journal, _bound_question(ref, "synthesis_retry", "Synthesis failed; retry synthesis only or cancel?"))
            else:
                _apply_outcome(data, journal, ref, step)
            return None
        items = tuple(MeasureWorkItem(ref, item.action_id, item.kind, item.driver, item.role, item.model,
            item.channel, item.access, item.prompt_path, submission_path=item.submission_path,
            return_transport=item.return_transport, review_ref=step.ref, review_item=item,
            returned_path=item.ingress_path) for item in step.work_items)
        return MeasureStep(step.type, ref, step.display, work_items=items, in_flight=step.in_flight)
    raise review.WorkflowError("invalid_journal", f"unsupported stage: {journal.stage}")


def next_measure_twice(ref: MeasureRef) -> MeasureStep:
    # A bounded reconciliation chain, not a wait or file-size polling loop.
    for _transition in range(8):
        step = _transaction(ref, lambda data, journal: _next_locked(data, journal, ref), active=False)
        if isinstance(step, ReviewPreparation):
            # Provider timeout resolution can invoke a CLI. Neither owner nor
            # review lock is held while the immutable preparation is computed.
            prepared = review.prepare_loop_review(step.request, step.binding)
            def checkpoint(data: dict[str, object], journal: MeasureJournal) -> None:
                if not is_active_value(data.get("active")) or loop_state.bound_reason(data):
                    return
                if (journal.stage != "prepare_review" or journal.pending_review_inputs is not None
                        or journal.question is not None):
                    return
                if (journal.review_generation + 1 != step.binding.review_generation
                        or str(data.get("plan_file")) != step.request.target_input):
                    raise review.WorkflowError("conflict", "review preparation no longer matches the owned plan", "conflict")
                journal.review_generation = step.binding.review_generation
                journal.pending_review_inputs = prepared
            _transaction(ref, checkpoint)
            continue
        if step is not None:
            return step
    raise review.WorkflowError("invalid_journal", "measure reconciliation exceeded its bounded transition chain")


def claim_measure_action(ref: MeasureRef, action_id: str) -> dict[str, object]:
    def claim(data: dict[str, object], journal: MeasureJournal) -> dict[str, object]:
        if loop_state.bound_reason(data) or journal.question:
            raise review.WorkflowError("work_not_admitted", "bound or human wait prevents new advisor work")
        action = journal.action
        if action is None or action.item.action_id != action_id:
            raise review.WorkflowError("invalid_action", "advisor action was not issued")
        if action.status != "ready":
            return {"authorization": "do_not_spawn", "action_status": action.status}
        _verify_advisor_route(journal)
        action.status = "claimed"
        data["awaiting_input"] = False
        return {"authorization": "spawn", "action_status": "claimed", "work_item": work_to_dict(action.item)}
    return _transaction(ref, claim)


def _sealed_plan_path(result: MeasureResult, journal: MeasureJournal) -> Path:
    root = _namespace(result.ref, journal)
    return _safe_path(root / "sealed" / result.action_id / f"{result.plan_sha256}.md", root)


def _seal_advisor_plan(result: MeasureResult, journal: MeasureJournal, content: bytes) -> Path:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise review.WorkflowError("plan_source_unreadable", f"advisor plan is not UTF-8: {exc}") from exc
    if not text.strip() or sha256(content) != result.plan_sha256:
        raise review.WorkflowError("invalid_submission", "advisor plan is empty or its digest differs")
    sealed = _sealed_plan_path(result, journal)
    _write_once(sealed, content, _namespace(result.ref, journal))
    return sealed


def submit_measure_action(request: MeasureSubmission) -> MeasureStep:
    result = parse_measure_result(result_to_dict(request.result))
    def seal(data: dict[str, object], journal: MeasureJournal) -> None:
        receipt = next((r for r in journal.accepted_actions if r.action_id == result.action_id), None)
        if receipt is not None:
            if receipt != result:
                raise review.WorkflowError("conflict", "different result for accepted advisor action", "conflict")
            return
        action = journal.action
        if action is None or action.item.action_id != result.action_id or action.item.owner != result.ref:
            raise review.WorkflowError("invalid_submission", "advisor result does not belong to the issued action")
        if action.status == "promotion_pending":
            if action.result != result:
                raise review.WorkflowError("conflict", "sealed advisor result differs", "conflict")
            return
        if action.status != "claimed" or loop_state.bound_reason(data):
            raise review.WorkflowError("invalid_submission", "advisor action is not actively claimed")
        root = _namespace(result.ref, journal)
        transport_receipt = _safe_path(root / "transport-receipts" / f"{result.action_id}.json", root)
        if transport_receipt.exists():
            try:
                record = json.loads(transport_receipt.read_bytes())
            except (OSError, ValueError) as exc:
                raise review.WorkflowError("invalid_submission", f"advisor capture receipt is unreadable: {exc}") from exc
            if not isinstance(record, dict) or set(record) != {"capture", "result"}:
                raise review.WorkflowError("invalid_submission", "advisor capture receipt has invalid fields")
            if parse_measure_result(record["result"]) != result:
                raise review.WorkflowError("conflict", "submission differs from captured advisor result", "conflict")
        action.result = result
        if result.status != "ok":
            action.status = "settled"
            journal.accepted_actions.append(result)
            _park(data, journal, _bound_question(result.ref, "advisor_retry", result.diagnostic or "Advisor failed; retry or cancel?"))
            return
        sealed = _sealed_plan_path(result, journal)
        source = sealed if sealed.exists() else _safe_path(Path(action.item.staging_path), root)
        if transport_receipt.exists() and source != sealed:
            raise review.WorkflowError("plan_source_unreadable", "captured advisor plan has no retained sealed bytes")
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise review.WorkflowError("plan_source_unreadable", f"cannot read retained or issued advisor plan: {exc}") from exc
        sealed = _seal_advisor_plan(result, journal, content)
        action.sealed_path = str(sealed)
        action.status = "promotion_pending"
        data["awaiting_input"] = False
    _transaction(result.ref, seal)
    return next_measure_twice(result.ref)


def recover_measure_action(request: MeasureRecovery) -> MeasureStep:
    if request.confirmation != "not_running":
        raise review.WorkflowError("not_confirmed", "lost advisor recovery requires not_running")
    def recover(data: dict[str, object], journal: MeasureJournal) -> None:
        action = journal.action
        if action is None or action.item.action_id != request.action_id or action.status != "claimed":
            raise review.WorkflowError("invalid_recovery", "advisor claim is not outstanding")
        action.status = "settled"
        _park(data, journal, _bound_question(request.ref, "advisor_retry", "Confirmed lost advisor; retry or cancel?"))
    _transaction(request.ref, recover)
    return next_measure_twice(request.ref)


def cancel_measure_twice(ref: MeasureRef, reason: str) -> MeasureStep:
    def cancel(data: dict[str, object], journal: MeasureJournal) -> None:
        loop_state.deactivate(data, cancel=True, reason=reason)
        journal.question = None
        journal.review_ref = None
        journal.pending_review_inputs = None
        journal.retry_authorization = None
    _transaction(ref, cancel, active=False)
    return next_measure_twice(ref)


def _adopt_plan(data: dict[str, object], journal: MeasureJournal, ref: MeasureRef, content: bytes) -> None:
    action = _issue_advisor(data, journal, ref)
    journal.action = action
    result = MeasureResult(ref, action.item.action_id, "ok", sha256(content), sha256(b"legacy-adoption"))
    root = _namespace(ref, journal)
    sealed = root / "sealed" / action.item.action_id / f"{result.plan_sha256}.md"
    _write_once(sealed, content, root)
    action.status, action.result, action.sealed_path = "promotion_pending", result, str(sealed)
    _promote(data, journal, ref)


def _legacy_confirm(data: dict[str, object], journal: MeasureJournal, decision: MeasureDecision) -> None:
    question = journal.question
    if (decision.confirmation != "not_running" or decision.retained_phase != question.retained_phase
            or decision.retained_run_id != question.retained_run_id
            or decision.retained_phase != str(data.get("phase") or "drafting")
            or decision.retained_run_id != str(data.get("run_id") or "")):
        raise review.WorkflowError("not_confirmed", "legacy quiescence confirmation is stale or incomplete")
    journal.legacy_confirmed = True
    if data.get("phase") == "done":
        journal.stage = "done"
        journal.question = None
        return
    try:
        requirements = _requirements(MeasureRequest(journal.raw_arguments, decision.ref.session_segment), journal.selection, legacy=True)
    except review.WorkflowError as exc:
        _park(data, journal, _bound_question(decision.ref, "legacy_requirements", str(exc)))
        return
    if requirements is None:
        _park(data, journal, _bound_question(decision.ref, "legacy_requirements", "Preserved task is empty; provide bound requirements answers.",
            questions=QUESTIONS))
        return
    journal.requirements = requirements
    _legacy_continue(data, journal, decision.ref, completed_action=decision.completed_action,
                     plan_sha256=decision.plan_sha256)


def _legacy_continue(data: dict[str, object], journal: MeasureJournal, ref: MeasureRef, *,
                     completed_action: str | None = None, plan_sha256: str | None = None) -> None:
    """Resume retained planning semantics after quiescence AND requirements readiness."""
    if not journal.legacy_confirmed or journal.requirements is None:
        raise review.WorkflowError("not_confirmed", "legacy continuation requires quiescence and requirements")
    requirements = journal.requirements
    journal.request_id = _request_identity(journal.raw_arguments, journal.selection, requirements)
    journal.question = None
    data["awaiting_input"] = False
    plan = Path(anchor_path(str(data.get("plan_file") or "")))
    try:
        content = plan.read_bytes()
        if not content.decode("utf-8").strip():
            content = b""
    except (OSError, UnicodeDecodeError):
        content = b""
    verdict = str(data.get("last_verdict") or "")
    expected_action = {"REVISE": "revision", "REJECT": "replan", "": "initial_plan"}.get(verdict)
    if completed_action is not None:
        if completed_action != expected_action or not content or plan_sha256 != sha256(content):
            raise review.WorkflowError("invalid_completion", "legacy completion attestation does not match the retained action and readable plan")
        _adopt_plan(data, journal, ref, content)
        data["phase"] = "drafting"
        return
    if data.get("phase") == "reviewing":
        if not content:
            _park(data, journal, _bound_question(ref, "legacy_plan_recovery", "Legacy review plan is unreadable; explicitly retry planning or cancel."))
            return
        _adopt_plan(data, journal, ref, content)
        data["phase"] = "drafting"
        return
    if verdict in {"REVISE", "REJECT"}:
        feedback = review_runs.reviews_dir(ref.session_segment) / str(data.get("run_id") or "") / "panel-full.md"
        if not content or not feedback.is_file():
            _park(data, journal, _bound_question(ref, "legacy_plan_recovery", "Prior verdict plan or panel is missing; explicitly retry planning or cancel."))
            return
        try:
            if not feedback.read_text(encoding="utf-8").strip():
                raise ValueError("prior panel is empty")
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            _park(data, journal, _bound_question(ref, "legacy_plan_recovery", f"Prior panel is unreadable: {exc}"))
            return
        # Verify old identity before retaining its feedback as provenance.
        try:
            evidence = loop_state.legacy_evidence(data, ref.session_segment)
        except (loop_state.LoopStateError, review_runs.ReviewRunError) as exc:
            _park(data, journal, _bound_question(ref, "legacy_plan_recovery", f"Prior panel identity is unusable: {exc}"))
            return
        if evidence.source.manifest_identity_digest is None:
            _park(data, journal, _bound_question(ref, "legacy_plan_recovery", "Prior panel manifest is missing; explicitly retry planning or cancel."))
            return
        record = review_runs.read_run_json(feedback.parent)
        if (record["target_sha256"] != evidence.target_sha256
                or record["target_spec"] != evidence.target_spec or record["target_base"] != evidence.target_base
                or set(record["seat_signatures"]) != set(evidence.expected_seats)
                or (data.get("manifest_identity_digest") is not None
                    and data["manifest_identity_digest"] != evidence.source.manifest_identity_digest)):
            _park(data, journal, _bound_question(ref, "legacy_plan_recovery", "Prior panel identity differs from the retained review; explicitly retry planning or cancel."))
            return
        _adopt_plan(data, journal, ref, content)
        journal.feedback_path = str(feedback)
        journal.full_review_path = str(feedback)
        journal.stage = "revision" if verdict == "REVISE" else "replanning"
        journal.action = None
    elif content:
        _adopt_plan(data, journal, ref, content)
    else:
        journal.stage = "planning"


def decide_measure_twice(ref: MeasureRef, decision: MeasureDecision) -> MeasureStep:
    if decision.ref != ref:
        raise review.WorkflowError("invalid_decision", "decision owner differs")
    def decide(data: dict[str, object], journal: MeasureJournal) -> None:
        question = journal.question
        if question is None or question.question_id != decision.question_id:
            raise review.WorkflowError("stale_question", "decision does not match the outstanding question")
        if decision.kind == "cancel":
            loop_state.deactivate(data, cancel=True, reason="human decision")
            journal.question = None
            journal.review_ref = None
            journal.pending_review_inputs = None
            journal.retry_authorization = None
            return
        if decision.kind == "legacy_work_not_running" and question.kind == decision.kind:
            _legacy_confirm(data, journal, decision)
            return
        if decision.kind == "retry_review" and question.kind == "completion_advisory" and any(
                advisory in question.advisories for advisory in ("target-drift", "target-no-longer-resolves")):
            try:
                content = Path(str(data["plan_file"])).read_bytes().decode("utf-8")
                if not content.strip():
                    raise ValueError("current plan is empty")
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                _park(data, journal, _bound_question(ref, "plan_recovery", f"Current plan is unreadable: {exc}; retry planning or cancel?"))
                return
            journal.question = None
            journal.review_ref = None
            journal.pending_review_inputs = None
            journal.stage = "prepare_review"
            data.update(phase="drafting", awaiting_input=False)
            return
        if (decision.kind, question.kind) in {("retry_synthesis", "synthesis_retry"), ("retry_review", "completion_advisory")}:
            if any(getattr(decision, key) is not None for key in
                   ("confirmation", "retained_phase", "retained_run_id", "completed_action", "plan_sha256", "requirements")):
                raise review.WorkflowError("invalid_decision", "retry authorization requires only its owner, question and retry kind")
            authorization = MeasureRetryAuthorization(decision, journal.review_ref)
            # Persist operator intent before the separate review receipt commit.
            if journal.retry_authorization and journal.retry_authorization.decision.question_id == question.question_id:
                if journal.retry_authorization != authorization:
                    raise review.WorkflowError("stale_question", "retry decision conflicts with its authorization")
            journal.retry_authorization = authorization
            return
        if decision.kind == "force" and question.kind == "completion_advisory" and decision.confirmation == "force":
            if loop_state.bound_reason(data):
                _next_locked(data, journal, ref)
                return
            run = review._guard_review_path(session_segment=ref.session_segment, run_id=journal.review_ref.run_id, create=False)
            with review._workflow_lock(run):
                wf = review._workflow(run)
                step = review._derive_step(wf, run)
            evidence = review._read_loop_review_evidence_under_owner_lock(step.ref)
            if question.outcome_sha256 != evidence.source.accepted_outcome_sha256:
                raise review.WorkflowError("stale_question", "accepted outcome changed after the advisory")
            _apply_outcome(data, journal, ref, step, force=True)
            return
        if decision.kind == "continue" and question.kind == "rising_findings":
            journal.question = None
        elif decision.kind == "retry_advisor" and question.kind in {"advisor_retry", "legacy_plan_recovery", "plan_recovery"}:
            journal.question = None
            journal.action = None
            if question.kind in {"legacy_plan_recovery", "plan_recovery"}:
                journal.stage = "planning"
                journal.review_ref = None
                journal.pending_review_inputs = None
                data["phase"] = "drafting"
        elif decision.kind == "retry_requirements" and question.kind == "legacy_requirements":
            journal.requirements = _requirements(MeasureRequest(journal.raw_arguments, ref.session_segment), journal.selection, legacy=True)
            if journal.requirements is None:
                raise review.WorkflowError("requirements_source_unreadable", "legacy requirements are still unavailable")
            _legacy_continue(data, journal, ref, completed_action=decision.completed_action, plan_sha256=decision.plan_sha256)
            return
        elif decision.kind == "requirements" and question.kind == "legacy_requirements":
            captured = decision.requirements
            if captured is None or captured.question_id != question.question_id:
                raise review.WorkflowError("stale_question", "legacy requirements must bind the human question")
            canonical = CapturedRequirements(interview_question(journal.raw_arguments).question_id, captured.answers)
            journal.requirements = _requirements(MeasureRequest(journal.raw_arguments, ref.session_segment, canonical), journal.selection)
            _legacy_continue(data, journal, ref, completed_action=decision.completed_action, plan_sha256=decision.plan_sha256)
            return
        else:
            raise review.WorkflowError("invalid_decision", "decision does not answer the issued question")
        if journal.requirements is not None:
            journal.request_id = _request_identity(journal.raw_arguments, journal.selection, journal.requirements)
        data["awaiting_input"] = False
    _transaction(ref, decide)
    return next_measure_twice(ref)
